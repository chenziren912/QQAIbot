"""Bounded, SSRF-conscious read-only web tools for group agent turns.

The functions here intentionally return ordinary JSON-friendly dictionaries.
Their output is still untrusted web data when passed back to a model; this
module only narrows the network/file surface and bounds response sizes.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
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


class WebToolError(ValueError):
    """A safe, displayable failure from a network lookup tool."""


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


async def _download_text(url: str, *, max_bytes: int = MAX_RESPONSE_BYTES) -> Tuple[str, str, str, _ExtractedHTML]:
    """Fetch a validated public resource and return text/content-type/final URL/parser."""

    current = _normalise_url(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html, text/plain;q=0.9, */*;q=0.1"}
    timeout = httpx.Timeout(20.0, connect=10.0)
    # Remote lookups intentionally keep the user's system proxy settings: it
    # is often how Google is reachable in a local Windows deployment.
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, headers=headers, trust_env=True) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            await _ensure_public_destination(current)
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise WebToolError("网页重定向未提供目标地址。")
                        current = _normalise_url(urljoin(current, location))
                        continue
                    if response.status_code >= 400:
                        raise WebToolError(
                            "抓取 %s 失败：HTTP %s %s。"
                            % (_public_url_label(current), response.status_code, response.reason_phrase)
                        )
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    received = bytearray()
                    async for part in response.aiter_bytes():
                        received.extend(part)
                        if len(received) > max_bytes:
                            raise WebToolError("网页响应超过 %s MiB 上限。" % max(1, max_bytes // (1024 * 1024)))
            except httpx.HTTPError as exc:
                raise WebToolError("抓取 %s 时网络错误：%s" % (_public_url_label(current), str(exc)[:500])) from exc

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
                raise WebToolError("该链接返回 %s，当前只支持 HTML、纯文本、JSON 或 XML。" % (content_type or "未知类型"))
            return visible_text[:MAX_EXTRACTED_TEXT_CHARS], content_type or "text/html", current, parser
    raise WebToolError("网页重定向次数超过上限。")


async def google_search(query: Any, *, max_results: Any = 5) -> Dict[str, Any]:
    """Search Google and return a bounded list of ordinary public result links."""

    if not isinstance(query, str):
        raise WebToolError("搜索词必须是字符串。")
    normalized = " ".join(query.split())
    if not normalized:
        raise WebToolError("搜索词不能为空。")
    if len(normalized) > MAX_QUERY_CHARS:
        raise WebToolError("搜索词过长，最多允许 %s 个字符。" % MAX_QUERY_CHARS)
    try:
        count = max(1, min(int(max_results), MAX_RESULTS))
    except (TypeError, ValueError) as exc:
        raise WebToolError("max_results 必须是整数。") from exc
    search_url = "https://www.google.com/search?" + urlencode({"q": normalized, "num": count, "hl": "zh-CN"})
    _text, _content_type, _final_url, parser = await _download_text(search_url)
    results: List[Dict[str, str]] = []
    seen = set()
    for raw_href, title in parser.links:
        href = urljoin("https://www.google.com", raw_href)
        parsed = urlparse(href)
        if parsed.path == "/url":
            href = (parse_qs(parsed.query).get("q") or [""])[0]
            parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        host = parsed.hostname.lower()
        if host == "google.com" or host.endswith(".google.com"):
            continue
        label = _public_url_label(href)
        if label in seen:
            continue
        seen.add(label)
        results.append({"title": title[:500], "url": href})
        if len(results) >= count:
            break
    return {"ok": True, "query": normalized, "results": results, "source": "Google"}


async def fetch_link(url: Any, *, max_chars: Any = 12_000) -> Dict[str, Any]:
    """Fetch a public link and extract a bounded readable text representation."""

    try:
        requested_chars = max(500, min(int(max_chars), MAX_EXTRACTED_TEXT_CHARS))
    except (TypeError, ValueError) as exc:
        raise WebToolError("max_chars 必须是整数。") from exc
    text, content_type, final_url, parser = await _download_text(_normalise_url(url))
    return {
        "ok": True,
        "url": final_url,
        "content_type": content_type,
        "title": parser.title,
        "text": text[:requested_chars],
        "truncated": len(text) > requested_chars,
    }

