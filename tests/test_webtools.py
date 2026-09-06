from __future__ import annotations

import json
from typing import Any

import pytest

import app.webtools as webtools


def _parser(*links: tuple[str, str]) -> webtools._ExtractedHTML:
    parser = webtools._ExtractedHTML()
    parser.links = list(links)
    return parser


def test_google_search_cookie_loader_accepts_only_google_netscape_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie_file = tmp_path / "google-search-cookies.txt"
    cookie_file.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".google.com\tTRUE\t/\tTRUE\t0\tSID\tsearch-session",
                "other.example\tTRUE\t/\tFALSE\t0\tignored\tsecret",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOOGLE_SEARCH_COOKIES_FILE", str(cookie_file))
    monkeypatch.delenv("GOOGLE_COOKIES_FILE", raising=False)
    monkeypatch.delenv("QQ_AI_DATA_DIR", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    jar, diagnostic = webtools._read_google_search_cookies()

    assert jar is not None
    assert diagnostic == {"configured": True, "loaded": 1, "skipped": 1, "error": ""}
    loaded = list(jar.jar)
    assert len(loaded) == 1
    assert loaded[0].name == "SID"
    assert loaded[0].domain == ".google.com"
    assert loaded[0].value == "search-session"


@pytest.mark.asyncio
async def test_google_search_sends_cookie_only_to_google_and_reports_counts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie_file = tmp_path / "google-cookies.txt"
    cookie_file.write_text(
        ".google.com\tTRUE\t/\tTRUE\t0\tSID\tsearch-session\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GOOGLE_SEARCH_COOKIES_FILE", str(cookie_file))
    monkeypatch.delenv("GOOGLE_COOKIES_FILE", raising=False)
    monkeypatch.delenv("QQ_AI_DATA_DIR", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    calls: list[tuple[str, Any]] = []

    async def fake_download(url: str, **kwargs: Any):
        calls.append((url, kwargs.get("cookies")))
        if "google.com/search" in url:
            raise webtools.WebToolError(
                "Google challenge",
                code="provider_unavailable",
                provider="Google",
                url=url,
            )
        return "", "text/html", url, _parser(("https://example.com/article", "文章"))

    monkeypatch.setattr(webtools, "_download_text", fake_download)
    result = await webtools.google_search("带 Cookie 的搜索", max_results=2)

    assert result["ok"] is True
    assert result["provider"] == "Bing"
    assert result["google_cookie_diagnostics"]["loaded"] == 1
    assert calls[0][1] is not None
    assert len(list(calls[0][1].jar)) == 1
    assert calls[1][1] is None
    assert "search-session" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_search_falls_back_from_google_challenge_to_bing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_download(url: str, **_: Any):
        calls.append(url)
        if "google.com/search" in url:
            return "SG_REL unusual traffic", "text/html", url, _parser(
                ("https://www.google.com/preferences", "设置")
            )
        if "bing.com/search" in url:
            return "", "text/html", url, _parser(("https://example.com/article", "官方文章"))
        raise AssertionError(url)

    monkeypatch.setattr(webtools, "_download_text", fake_download)
    result = await webtools.google_search("最新版本", max_results=3)

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["provider"] == "Bing"
    assert result["results"] == [
        {
            "provider": "Bing",
            "source_id": result["results"][0]["source_id"],
            "title": "官方文章",
            "url": "https://example.com/article",
            "summary": "",
            "snippet": "",
        }
    ]
    assert [item["provider"] for item in result["attempts"]] == ["Google", "Bing"]
    assert result["attempts"][0]["ok"] is False
    assert result["attempts"][0]["error"]["code"] == "provider_unavailable"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_search_all_provider_failures_are_not_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_download(url: str, **_: Any):
        raise webtools.WebToolError(
            "HTTP 403",
            code="forbidden",
            provider="search",
            url=url,
            status_code=403,
        )

    monkeypatch.setattr(webtools, "_download_text", fake_download)
    result = await webtools.google_search("无法核验")

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["results"] == []
    assert result["retry_safe"] is True
    assert result["error"]["code"] == "all_search_providers_failed"
    assert len(result["attempts"]) == 3
    assert all(item["ok"] is False for item in result["attempts"])
    assert all(item["error"]["status_code"] == 403 for item in result["attempts"])


@pytest.mark.asyncio
async def test_fetch_link_returns_structured_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_download(url: str, **_: Any):
        raise webtools.WebToolError("网页拒绝访问", code="forbidden", url=url, status_code=403)

    monkeypatch.setattr(webtools, "_download_text", fake_download)
    result = await webtools.fetch_link("https://example.com/private")

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["retry_safe"] is True
    assert result["error"]["code"] == "forbidden"
    assert result["error"]["status_code"] == 403
    assert result["attempts"][0]["provider"] == "fetch"


@pytest.mark.asyncio
async def test_search_timeout_keeps_a_distinct_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_download(url: str, **_: Any):
        raise webtools.WebToolError("请求超时", code="timeout", url=url)

    monkeypatch.setattr(webtools, "_download_text", fake_download)
    result = await webtools.google_search("需要超时诊断")

    assert result["ok"] is False
    assert result["verified"] is False
    assert [item["error"]["code"] for item in result["attempts"]] == ["timeout"] * 3
