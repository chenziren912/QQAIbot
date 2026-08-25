from __future__ import annotations

from pathlib import Path

import pytest

from app.service import AgentService, WorkspaceError, _is_bilibili_url, _is_youtube_url


class Secrets:
    def get_llm_api_key(self) -> str:
        return "test-key"


class FailedProcess:
    returncode = 1

    async def communicate(self):
        return (
            b"ERROR: HTTP Error 403: Forbidden\n",
            b"",
        )


@pytest.mark.asyncio
async def test_bilibili_download_preserves_provider_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data", secret_store=Secrets())
    captured = {}

    async def fake_create(*command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        return FailedProcess()

    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_create)
    monkeypatch.setenv("FFMPEG_PATH", str(tmp_path / "missing-ffmpeg.exe"))
    try:
        result = await service._bilibili_download(
            "private:1",
            {
                "url": "https://www.bilibili.com/video/BV1xx411c7mD",
            },
        )
    finally:
        await service.stop()

    assert result["ok"] is False
    assert "Bilibili 返回 HTTP 403" in result["error"]
    assert "HTTP Error 403" in result["yt_dlp_output"]
    assert "--cookies-from-browser" not in captured["command"]


@pytest.mark.asyncio
async def test_bilibili_download_uses_dedicated_runtime_cookie_file_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data", secret_store=Secrets())
    (tmp_path / "data" / "bilibili-cookies.txt").write_text(
        "# Netscape HTTP Cookie File\n", encoding="utf-8"
    )
    youtube_cookie = tmp_path / "youtube-cookies.txt"
    youtube_cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    captured = {}

    async def fake_create(*command, **kwargs):
        captured["command"] = list(command)
        return FailedProcess()

    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_create)
    monkeypatch.setenv("YTDLP_COOKIE_FILE", str(youtube_cookie))
    monkeypatch.setenv("YTDLP_COOKIES_FROM_BROWSER", "chrome")
    monkeypatch.setattr(
        "app.service._windows_configured_https_proxy", lambda: "http://127.0.0.1:10808"
    )
    try:
        result = await service._bilibili_download(
            "private:1", {"url": "https://www.bilibili.com/video/BV1xx411c7mD"}
        )
    finally:
        await service.stop()

    assert result["ok"] is False
    assert result["cookies_from_file"] is True
    assert "--cookies" in captured["command"]
    assert str(tmp_path / "data" / "bilibili-cookies.txt") in captured["command"]
    assert str(youtube_cookie) not in captured["command"]
    assert "--cookies-from-browser" not in captured["command"]
    assert "--proxy" in captured["command"]
    assert "http://127.0.0.1:10808" in captured["command"]
    assert result["proxy_from_windows"] is True


@pytest.mark.asyncio
async def test_bilibili_download_rejects_non_bilibili_url_before_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data", secret_store=Secrets())

    async def fail_create(*_: object, **__: object) -> FailedProcess:
        raise AssertionError("yt-dlp must not be started for a non-Bilibili URL")

    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fail_create)
    try:
        with pytest.raises(WorkspaceError, match="仅支持 Bilibili"):
            await service._bilibili_download("private:1", {"url": "https://www.youtube.com/watch?v=test"})
    finally:
        await service.stop()


def test_bilibili_url_validation_accepts_official_and_short_urls_only() -> None:
    assert _is_bilibili_url("https://www.bilibili.com/video/BV1xx411c7mD")
    assert _is_bilibili_url("https://b23.tv/abc123")
    assert _is_bilibili_url("https://bili2233.cn/abc123")
    assert not _is_bilibili_url("https://www.bilibili.com.example.invalid/video/BV1xx411c7mD")
    assert not _is_bilibili_url("https://www.youtube.com/watch?v=test")
    assert not _is_bilibili_url("file:///D:/video.mp4")


@pytest.mark.asyncio
async def test_youtube_download_uses_dedicated_cookie_file_and_preserves_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data", secret_store=Secrets())
    youtube_cookie = tmp_path / "data" / "youtube-cookies.txt"
    youtube_cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    captured = {}

    async def fake_create(*command, **kwargs):
        captured["command"] = list(command)
        return FailedProcess()

    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_create)
    monkeypatch.setattr(
        "app.service._windows_configured_https_proxy", lambda: "http://127.0.0.1:10808"
    )
    try:
        result = await service._youtube_download(
            "private:1", {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}
        )
    finally:
        await service.stop()

    assert result["ok"] is False
    assert "HTTP 403" in result["error"]
    assert str(youtube_cookie) in captured["command"]
    assert "--cookies" in captured["command"]
    assert "--proxy" in captured["command"]


@pytest.mark.asyncio
async def test_youtube_download_query_uses_ytsearch_without_websearch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data", secret_store=Secrets())
    captured = {}

    async def fake_create(*command, **kwargs):
        captured["command"] = list(command)
        return FailedProcess()

    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_create)
    monkeypatch.setattr(
        "app.service._find_local_executable",
        lambda name: r"C:\Program Files\nodejs\node.exe" if name == "node" else "",
    )
    try:
        result = await service._youtube_download(
            "private:1", {"query": "Sharks Official MV 144p"}
        )
    finally:
        await service.stop()

    assert result["ok"] is False
    assert "ytsearch1:Sharks Official MV 144p" in captured["command"]
    assert captured["command"][captured["command"].index("--js-runtimes") + 1] == (
        r"node:C:\Program Files\nodejs\node.exe"
    )


def test_youtube_url_validation_accepts_official_hosts_only() -> None:
    assert _is_youtube_url("https://www.youtube.com/watch?v=jNQXAC9IVRw")
    assert _is_youtube_url("https://youtu.be/jNQXAC9IVRw")
    assert _is_youtube_url("https://music.youtube.com/watch?v=jNQXAC9IVRw")
    assert _is_youtube_url("https://www.youtube-nocookie.com/embed/jNQXAC9IVRw")
    assert not _is_youtube_url("https://www.youtube.com.example.invalid/watch?v=test")
    assert not _is_youtube_url("file:///D:/video.mp4")
