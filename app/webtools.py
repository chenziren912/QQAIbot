"""Bounded, SSRF-conscious read-only web tools for group agent turns.

The functions here intentionally return ordinary JSON-friendly dictionaries.
Their output is still untrusted web data when passed back to a model; this
module only narrows the network/file surface and bounds response sizes.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import socket
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx


MAX_QUERY_CHARS = 400
MAX_FETCH_URL_CHARS = 2_048
MAX_RESULTS = 8
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EXTRACTED_TEXT_CHARS = 16_000
MAX_REDIRECTS = 5
USER_AGENT = "QQGroupAIAgent/1.0 (local read-only web tool)"
GOOGLE_SEARCH_COOKIE_FILENAMES = ("google-search-cookies.txt", "google-cookies.txt")
GOOGLE_SEARCH_COOKIE_ENV_NAMES = ("GOOGLE_SEARCH_COOKIES_FILE", "GOOGLE_COOKIES_FILE")


class WebToolError(ValueError):
    """A safe, displayable failure from a network lookup tool."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "web_tool_error",
        provider: str = "",
        url: str = "",
        status_code: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "web_tool_error")
        self.provider = str(provider or "")
        self.url = str(url or "")
        self.status_code = int(status_code or 0)

    def diagnostic(self) -> Dict[str, Any]:
        """Return structured diagnostics without cookies or query credentials."""

        result: Dict[str, Any] = {
            "code": self.code,
            "message": str(self),
        }
        if self.provider:
            result["provider"] = self.provider
        if self.url:
            result["url"] = _public_url_label(self.url)
        if self.status_code:
            result["status_code"] = self.status_code
        return result


def _public_url_label(value: str) -> str:
    """Display an origin/path without leaking a query or fragment."""

    try:
        parsed = urlparse(value)
    except ValueError:
        return "[无效 URL]"
    if not parsed.scheme or not parsed.netloc:
        return "[无效 URL]"
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _normalise_url(value: Any) -> str:
    if not isinstance(value, str):
        raise WebToolError("URL 必须是字符串。")
    url = value.strip()
    if not url:
        raise WebToolError("URL 不能为空。")
    if len(url) > MAX_FETCH_URL_CHARS:
        raise WebToolError("URL 过长，最多允许 %s 个字符。" % MAX_FETCH_URL_CHARS)
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise WebToolError("URL 格式无效。") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise WebToolError("仅允许公网 http 或 https URL。")
    if parsed.username or parsed.password:
        raise WebToolError("URL 不允许携带用户名或密码。")
    return url


def _is_public_ip(value: str) -> bool:
    try:
        # is_global excludes loopback, private, link-local, multicast,
        # reserved, documentation and carrier-grade NAT ranges.
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _google_search_cookie_candidates() -> List[Path]:
    """Return local, site-specific Google search cookie locations.

    Cookie files are deliberately resolved at request time.  The launcher can
    set ``QQ_AI_DATA_DIR`` before importing this module, while an operator can
    also rotate the file without restarting the Python service.  We never use
    browser profiles or a cookie file belonging to another site here.
    """

    candidates: List[Path] = []
    for env_name in GOOGLE_SEARCH_COOKIE_ENV_NAMES:
        value = os.environ.get(env_name, "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    data_dir = os.environ.get("QQ_AI_DATA_DIR", "").strip()
    if data_dir:
        root = Path(data_dir).expanduser()
        candidates.extend(root / filename for filename in GOOGLE_SEARCH_COOKIE_FILENAMES)
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        root = Path(local_appdata).expanduser() / "QQAIGroupAgent" / "data"
        candidates.extend(root / filename for filename in GOOGLE_SEARCH_COOKIE_FILENAMES)

    unique: List[Path] = []
    seen = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _is_google_cookie_domain(value: str) -> bool:
    domain = str(value or "").strip().lstrip(".").rstrip(".").casefold()
    return domain == "google.com" or domain.endswith(".google.com")


def _read_google_search_cookies() -> Tuple[Optional[httpx.Cookies], Dict[str, Any]]:
    """Load a Netscape cookie export without exposing cookie values.

    The returned diagnostic intentionally contains only counts and a generic
    error.  Cookie values, paths and authorization material never enter the
    search result or application logs.
    """

    cookie_path = next((candidate for candidate in _google_search_cookie_candidates() if candidate.is_file()), None)
    if cookie_path is None:
        return None, {"configured": False, "loaded": 0, "skipped": 0, "error": ""}

    try:
        text = cookie_path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        return httpx.Cookies(), {
            "configured": True,
            "loaded": 0,
            "skipped": 0,
            "error": "Google 搜索 Cookie 文件不是 UTF-8 文本",
        }
    except OSError:
        return httpx.Cookies(), {
            "configured": True,
            "loaded": 0,
            "skipped": 0,
            "error": "无法读取 Google 搜索 Cookie 文件",
        }

    jar = httpx.Cookies()
    loaded = 0
    skipped = 0
    now = int(time.time())
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.lstrip().startswith("#"):
            continue
        fields = line.split("\t", 6)
        if len(fields) != 7:
            skipped += 1
            continue
        domain, include_subdomains, cookie_path, _secure, expires, name, value = fields
        if not name or not _is_google_cookie_domain(domain):
            skipped += 1
            continue
        try:
            expiry = int(expires or "0")
        except ValueError:
            skipped += 1
            continue
        if expiry > 0 and expiry <= now:
            skipped += 1
            continue
        normalized_domain = domain.strip().casefold()
        if include_subdomains.strip().upper() == "TRUE" and not normalized_domain.startswith("."):
            normalized_domain = "." + normalized_domain
        normalized_path = cookie_path.strip() or "/"
        if not normalized_path.startswith("/"):
            normalized_path = "/"
        try:
            jar.set(name.strip(), value, domain=normalized_domain, path=normalized_path)
        except (TypeError, ValueError):
            skipped += 1
            continue
        loaded += 1
    return jar, {"configured": True, "loaded": loaded, "skipped": skipped, "error": ""}


async def _ensure_public_destination(url: str) -> None:
    """Reject local/private DNS targets before every redirect hop."""

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").strip().rstrip(".").lower()
    if not hostname:
        raise WebToolError("URL 缺少主机名。")
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise WebToolError("不允许访问本机或局域网主机。")
    if _is_public_ip(hostname):
        return
    try:
        # A literal non-public address reaches this branch too, but DNS lookup
        # would fail; make the reason explicit instead.
        if ipaddress.ip_address(hostname):  # type: ignore[arg-type]
            raise WebToolError("不允许访问非公网 IP。")
    except ValueError:
        pass
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise WebToolError("URL 端口无效。") from exc
    try:
        rows = await asyncio.get_running_loop().run_in_executor(
            None, lambda: socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        )
    except OSError as exc:
        raise WebToolError("无法解析公网域名 %s：%s" % (hostname, str(exc)[:300])) from exc
    addresses = {row[4][0] for row in rows if row and row[4]}
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise WebToolError("URL 解析到了非公网地址，已拒绝访问。")


class _ExtractedHTML(HTMLParser):
    """Small dependency-free visible-text/link extractor."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "canvas", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._current_href = ""
        self._current_anchor_text: List[str] = []
        self.text_parts: List[str] = []
        self.links: List[Tuple[str, str]] = []
        self.title = ""
        self._in_title = False
        self._title_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        lowered = tag.lower()
        if lowered in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if lowered == "title":
            self._in_title = True
        if lowered == "a":
            values = dict(attrs)
            self._current_href = str(values.get("href") or "")
            self._current_anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if lowered == "title":
            self._in_title = False
            self.title = " ".join(self._title_parts).strip()[:500]
        if lowered == "a" and self._current_href:
            text = " ".join(self._current_anchor_text).strip()
            if text:
                self.links.append((self._current_href, text[:500]))
            self._current_href = ""
            self._current_anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        self.text_parts.append(text)
        if self._in_title:
            self._title_parts.append(text)
        if self._current_href:
            self._current_anchor_text.append(text)


async def _download_text(
    url: str,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    cookies: Optional[httpx.Cookies] = None,
) -> Tuple[str, str, str, _ExtractedHTML]:
    """Fetch a validated public resource and return text/content-type/final URL/parser."""

    current = _normalise_url(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html, text/plain;q=0.9, */*;q=0.1"}
    timeout = httpx.Timeout(20.0, connect=10.0)
    # Remote lookups intentionally keep the user's system proxy settings: it
    # is often how Google is reachable in a local Windows deployment.
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        headers=headers,
        trust_env=True,
        cookies=cookies,
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            await _ensure_public_destination(current)
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise WebToolError(
                                "网页重定向未提供目标地址。",
                                code="redirect_missing_location",
                                url=current,
                            )
                        current = _normalise_url(urljoin(current, location))
                        continue
                    if response.status_code >= 400:
                        raise WebToolError(
                            "抓取 %s 失败：HTTP %s %s。"
                            % (_public_url_label(current), response.status_code, response.reason_phrase),
                            code=(
                                "forbidden"
                                if response.status_code in {401, 403}
                                else "http_error"
                            ),
                            url=current,
                            status_code=response.status_code,
                        )
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    received = bytearray()
                    async for part in response.aiter_bytes():
                        received.extend(part)
                        if len(received) > max_bytes:
                            raise WebToolError(
                                "网页响应超过 %s MiB 上限。" % max(1, max_bytes // (1024 * 1024)),
                                code="response_too_large",
                                url=current,
                            )
            except httpx.TimeoutException as exc:
                raise WebToolError(
                    "抓取 %s 时超时：%s" % (_public_url_label(current), str(exc)[:500]),
                    code="timeout",
                    url=current,
                ) from exc
            except httpx.HTTPError as exc:
                raise WebToolError(
                    "抓取 %s 时网络错误：%s" % (_public_url_label(current), str(exc)[:500]),
                    code="network_error",
                    url=current,
                ) from exc

            raw = bytes(received)
            encoding = response.encoding or "utf-8"
            text = raw.decode(encoding, errors="replace")
            parser = _ExtractedHTML()
            if "html" in content_type or not content_type:
                parser.feed(text)
                parser.close()
                visible_text = "\n".join(parser.text_parts)
            elif content_type.startswith("text/") or content_type in {"application/json", "application/xml", "text/xml"}:
                visible_text = text
            else:
                raise WebToolError(
                    "该链接返回 %s，当前只支持 HTML、纯文本、JSON 或 XML。" % (content_type or "未知类型"),
                    code="unsupported_content_type",
                    url=current,
                )
            return visible_text[:MAX_EXTRACTED_TEXT_CHARS], content_type or "text/html", current, parser
    raise WebToolError(
        "网页重定向次数超过上限。",
        code="redirect_limit",
        url=current,
    )


SEARCH_PROVIDER_ORDER = ("Google", "Bing", "DuckDuckGo")
SEARCH_PROVIDER_HOSTS = {
    "Google": ("google.com", "googleusercontent.com"),
    "Bing": ("bing.com",),
    "DuckDuckGo": ("duckduckgo.com",),
}


def _search_url(provider: str, query: str, count: int) -> str:
    if provider == "Google":
        return "https://www.google.com/search?" + urlencode(
            {"q": query, "num": count, "hl": "zh-CN"}
        )
    if provider == "Bing":
        return "https://www.bing.com/search?" + urlencode(
            {"q": query, "count": count, "setlang": "zh-Hans"}
        )
    return "https://html.duckduckgo.com/html/?" + urlencode({"q": query, "kl": "cn-zh"})


def _is_search_provider_host(hostname: str, provider: str) -> bool:
    host = hostname.lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in SEARCH_PROVIDER_HOSTS[provider])


def _search_result_url(raw_href: str, provider: str) -> str:
    """Unwrap common provider redirect URLs and reject internal links."""

    base = {
        "Google": "https://www.google.com",
        "Bing": "https://www.bing.com",
        "DuckDuckGo": "https://html.duckduckgo.com",
    }[provider]
    href = urljoin(base, str(raw_href or "").strip())
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    if provider == "Google" and parsed.path == "/url":
        href = (query.get("q") or query.get("url") or [""])[0]
    elif provider == "DuckDuckGo" and parsed.path.rstrip("/") in {"/l", "/redirect"}:
        href = (query.get("uddg") or query.get("u") or [""])[0]
    elif provider == "Bing" and parsed.path.startswith("/ck/"):
        # Bing's tracking URL often contains a base64-ish ``u`` parameter;
        # if it cannot be decoded, leaving it out is safer than presenting a
        # Bing tracking page as the source.
        href = (query.get("url") or query.get("u") or [""])[0]
    try:
        parsed = urlparse(href)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if _is_search_provider_host(parsed.hostname, provider):
        return ""
    return href


def _search_results_from_parser(parser: _ExtractedHTML, provider: str, count: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    seen = set()
    for raw_href, title in parser.links:
        href = _search_result_url(raw_href, provider)
        if not href:
            continue
        label = _public_url_label(href)
        if label in seen:
            continue
        seen.add(label)
        clean_title = " ".join(str(title or "").split())[:500] or label
        source_id = "%s-%s" % (
            provider.casefold().replace("duckduckgo", "ddg").replace(" ", "-"),
            hashlib.sha256(href.encode("utf-8", errors="replace")).hexdigest()[:12],
        )
        # The dependency-free HTML parser intentionally does not guess a
        # snippet from arbitrary neighbouring page text.  Keep an explicit
        # empty summary rather than presenting navigation or ad copy as fact.
        results.append(
            {
                "provider": provider,
                "source_id": source_id,
                "title": clean_title,
                "url": href,
                "summary": "",
                "snippet": "",
            }
        )
        if len(results) >= count:
            break
    return results


def _search_provider_unavailable(provider: str, body: str, final_url: str) -> Optional[WebToolError]:
    lowered = (body or "").casefold()
    if provider == "Google" and (
        "sg_rel" in lowered
        or "unusual traffic" in lowered
        or "our systems have detected unusual traffic" in lowered
        or "sorry/index" in final_url.casefold()
    ):
        return WebToolError(
            "Google 返回了 SG_REL/异常流量页面，未取得可核验搜索结果。",
            code="provider_unavailable",
            provider=provider,
            url=final_url,
        )
    if any(marker in lowered for marker in ("not available in your country", "服务不可用", "暂时无法提供")):
        return WebToolError(
            "%s 返回不可用页面，未取得可核验搜索结果。" % provider,
            code="provider_unavailable",
            provider=provider,
            url=final_url,
        )
    return None


def _search_attempt(
    provider: str,
    search_url: str,
    error: Optional[BaseException] = None,
    *,
    cookie_diagnostic: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if isinstance(error, WebToolError):
        diagnostic = error.diagnostic()
    elif error is not None:
        diagnostic = {
            "code": "provider_error",
            "message": str(error)[:600] or "搜索提供商失败",
            "provider": provider,
            "url": _public_url_label(search_url),
        }
    else:
        diagnostic = {"code": "ok", "message": "取得可核验结果", "provider": provider}
    result = {
        "provider": provider,
        "url": _public_url_label(search_url),
        "ok": error is None,
        "error": None if error is None else diagnostic,
    }
    if cookie_diagnostic is not None:
        # Counts/status are useful when diagnosing a Google challenge, but the
        # cookie path and values must never leave the local process.
        result["cookie_diagnostics"] = {
            "configured": bool(cookie_diagnostic.get("configured")),
            "loaded": max(0, int(cookie_diagnostic.get("loaded") or 0)),
            "skipped": max(0, int(cookie_diagnostic.get("skipped") or 0)),
            "error": str(cookie_diagnostic.get("error") or "")[:200],
        }
    return result


async def google_search(query: Any, *, max_results: Any = 5) -> Dict[str, Any]:
    """Search Google, then Bing and DuckDuckGo when a provider is unusable.

    The historical function name is kept for API compatibility.  A successful
    response always contains provider/source IDs and real source URLs; an
    empty or challenge page is a failed attempt and never a fake success.
    """

    if not isinstance(query, str):
        raise WebToolError("搜索词必须是字符串。", code="invalid_query")
    normalized = " ".join(query.split())
    if not normalized:
        raise WebToolError("搜索词不能为空。", code="invalid_query")
    if len(normalized) > MAX_QUERY_CHARS:
        raise WebToolError(
            "搜索词过长，最多允许 %s 个字符。" % MAX_QUERY_CHARS,
            code="invalid_query",
        )
    try:
        count = max(1, min(int(max_results), MAX_RESULTS))
    except (TypeError, ValueError) as exc:
        raise WebToolError("max_results 必须是整数。", code="invalid_query") from exc

    attempts: List[Dict[str, Any]] = []
    google_cookies, google_cookie_diagnostic = _read_google_search_cookies()
    for provider in SEARCH_PROVIDER_ORDER:
        search_url = _search_url(provider, normalized, count)
        cookie_diagnostic = google_cookie_diagnostic if provider == "Google" else None
        try:
            download_kwargs = {"cookies": google_cookies} if provider == "Google" and google_cookies is not None else {}
            text, _content_type, final_url, parser = await _download_text(search_url, **download_kwargs)
            unavailable = _search_provider_unavailable(provider, text, final_url)
            if unavailable:
                raise unavailable
            results = _search_results_from_parser(parser, provider, count)
            if not results:
                raise WebToolError(
                    "%s 返回空结果，未取得可核验来源。" % provider,
                    code="empty_results",
                    provider=provider,
                    url=final_url,
                )
        except Exception as exc:
            attempts.append(
                _search_attempt(
                    provider,
                    search_url,
                    exc,
                    cookie_diagnostic=cookie_diagnostic,
                )
            )
            continue
        attempts.append(_search_attempt(provider, search_url, cookie_diagnostic=cookie_diagnostic))
        return {
            "ok": True,
            "query": normalized,
            "results": results,
            "source": provider,
            "provider": provider,
            "verified": True,
            "attempts": attempts,
            "google_cookie_diagnostics": google_cookie_diagnostic,
        }

    return {
        "ok": False,
        "query": normalized,
        "results": [],
        "source": None,
        "provider": None,
        "verified": False,
        "attempts": attempts,
        "google_cookie_diagnostics": google_cookie_diagnostic,
        "retry_safe": True,
        "qq_side_effect": False,
        "error": {
            "code": "all_search_providers_failed",
            "message": "Google、Bing、DuckDuckGo 均未返回可核验搜索结果；暂时无法核实事实。",
            "attempts": attempts,
        },
    }


async def fetch_link(url: Any, *, max_chars: Any = 12_000) -> Dict[str, Any]:
    """Fetch a public link and extract a bounded readable text representation."""

    requested_url = str(url or "").strip()
    try:
        requested_chars = max(500, min(int(max_chars), MAX_EXTRACTED_TEXT_CHARS))
    except (TypeError, ValueError) as exc:
        raise WebToolError("max_chars 必须是整数。", code="invalid_parameter") from exc
    try:
        normalized_url = _normalise_url(url)
        text, content_type, final_url, parser = await _download_text(normalized_url)
    except WebToolError as exc:
        return {
            "ok": False,
            "url": requested_url[:MAX_FETCH_URL_CHARS],
            "content_type": "",
            "title": "",
            "text": "",
            "truncated": False,
            "verified": False,
            "retry_safe": True,
            "qq_side_effect": False,
            "error": exc.diagnostic(),
            "attempts": [
                {
                    "provider": "fetch",
                    "url": _public_url_label(requested_url),
                    "ok": False,
                    "error": exc.diagnostic(),
                }
            ],
        }
    return {
        "ok": True,
        "url": final_url,
        "content_type": content_type,
        "title": parser.title,
        "text": text[:requested_chars],
        "truncated": len(text) > requested_chars,
        "verified": True,
        "provider": "fetch",
        "source_id": "fetch-" + hashlib.sha256(final_url.encode("utf-8", errors="replace")).hexdigest()[:12],
        "attempts": [
            {
                "provider": "fetch",
                "url": _public_url_label(final_url),
                "ok": True,
                "error": None,
            }
        ],
    }
