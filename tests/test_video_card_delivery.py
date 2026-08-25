"""Downloaded Bilibili videos must be QQ video cards, not generic files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.onebot import OneBotActionTimeoutError
from app.service import AgentService


class Adapter:
    connected = True

    def __init__(self, *, reject_video: bool = False, timeout_video: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reject_video = reject_video
        self.timeout_video = timeout_video

    async def call(self, action: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        self.calls.append((action, dict(params)))
        message = params.get("message")
        is_video = isinstance(message, list) and bool(message) and message[0].get("type") == "video"
        if is_video and self.timeout_video:
            raise OneBotActionTimeoutError("NapCat did not confirm the video message")
        if is_video and self.reject_video:
            raise RuntimeError("NapCat rejected video segment")
        return {"status": "ok", "data": {"message_id": str(len(self.calls))}}


@pytest.mark.asyncio
async def test_video_card_uses_video_segment_for_group_and_private(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data")
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"mp4")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    try:
        group = await service._send_video_to_conversation("123", video, "group-demo.mp4")
        private = await service._send_video_to_conversation("private:456", video, "private-demo.mp4")
    finally:
        await service.stop()

    assert group["ok"] is True
    assert group["delivery"] == "video_card"
    assert private["ok"] is True
    assert private["delivery"] == "video_card"
    assert [call[0] for call in adapter.calls] == ["send_group_msg", "send_private_msg"]
    assert adapter.calls[0][1]["group_id"] == 123
    assert adapter.calls[1][1]["user_id"] == 456
    for _, params in adapter.calls:
        assert params["message"] == [
            {
                "type": "video",
                "data": {"file": str(video), "name": params["message"][0]["data"]["name"]},
            }
        ]


@pytest.mark.asyncio
async def test_video_card_falls_back_to_normal_file_only_after_explicit_video_failure(
    tmp_path: Path,
) -> None:
    service = AgentService(tmp_path / "data")
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"mp4")
    adapter = Adapter(reject_video=True)
    service.adapter = adapter  # type: ignore[assignment]
    try:
        result = await service._send_video_to_conversation("123", video, video.name)
    finally:
        await service.stop()

    assert result["ok"] is True
    assert result["delivery"] == "file_fallback"
    assert "NapCat rejected video segment" in result["video_error"]
    assert [call[0] for call in adapter.calls] == ["send_group_msg", "upload_group_file"]
    assert adapter.calls[0][1]["message"][0]["type"] == "video"
    assert adapter.calls[1][1]["file"] == str(video)


@pytest.mark.asyncio
async def test_video_card_timeout_never_auto_uploads_a_duplicate_file(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data")
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"mp4")
    adapter = Adapter(timeout_video=True)
    service.adapter = adapter  # type: ignore[assignment]
    try:
        result = await service._send_video_to_conversation("123", video, video.name)
    finally:
        await service.stop()

    assert result["ok"] is False
    assert result["delivery"] == "video_card_uncertain"
    assert "未自动回退" in result["error"]
    assert [call[0] for call in adapter.calls] == ["send_group_msg"]


class DownloadedVideoProcess:
    returncode = 0

    def __init__(self, path: Path) -> None:
        self.path = path

    async def communicate(self) -> tuple[bytes, bytes]:
        return (str(self.path).encode("utf-8"), b"")


@pytest.mark.asyncio
async def test_bilibili_download_delivers_mp4_as_video_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_root = tmp_path / "Workspace"
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(workspace_root))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "视频群")
    video = service.conversation_workspace("123") / "downloaded.mp4"
    video.write_bytes(b"mp4")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]

    async def fake_create(*_: object, **__: object) -> DownloadedVideoProcess:
        return DownloadedVideoProcess(video)

    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_create)
    monkeypatch.setattr("app.service._windows_configured_https_proxy", lambda: "")
    try:
        result = await service._bilibili_download(
            "123", {"url": "https://www.bilibili.com/video/BV1xx411c7mD"}
        )
    finally:
        await service.stop()

    assert result["ok"] is True
    assert result["delivery"] == "video_card"
    video_calls = [
        params
        for action, params in adapter.calls
        if action == "send_group_msg"
        and isinstance(params.get("message"), list)
        and params["message"]
        and params["message"][0].get("type") == "video"
    ]
    assert len(video_calls) == 1
    assert video_calls[0]["message"][0]["data"]["file"] == str(video)
    assert not [call for call in adapter.calls if call[0] == "upload_group_file"]


@pytest.mark.asyncio
async def test_bilibili_download_transcodes_non_qq_video_before_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The downloaded container/codec is normalized before QQ delivery."""

    workspace_root = tmp_path / "Workspace"
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(workspace_root))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "视频群")
    source = service.conversation_workspace("123") / "downloaded.webm"
    source.write_bytes(b"webm")
    compatible = service.conversation_workspace("123") / "downloaded.qq.mp4"
    compatible.write_bytes(b"h264-aac-mp4")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]

    async def fake_create(*_: object, **__: object) -> DownloadedVideoProcess:
        return DownloadedVideoProcess(source)

    async def fake_transcode(path: Path) -> Path:
        assert path == source
        return compatible

    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_create)
    monkeypatch.setattr(service, "_transcode_video_for_qq", fake_transcode)
    monkeypatch.setattr("app.service._windows_configured_https_proxy", lambda: "")
    try:
        result = await service._bilibili_download(
            "123", {"url": "https://www.bilibili.com/video/BV1xx411c7mD"}
        )
    finally:
        await service.stop()

    assert result["ok"] is True
    assert result["qq_compatible"] is True
    video_calls = [
        params for action, params in adapter.calls
        if action == "send_group_msg" and params.get("message", [{}])[0].get("type") == "video"
    ]
    assert video_calls[0]["message"][0]["data"]["file"] == str(compatible)


@pytest.mark.asyncio
async def test_bilibili_audio_only_keeps_normal_file_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "Workspace"
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(workspace_root))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "视频群")
    audio = service.conversation_workspace("123") / "downloaded.m4a"
    audio.write_bytes(b"audio")
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]

    async def fake_create(*_: object, **__: object) -> DownloadedVideoProcess:
        return DownloadedVideoProcess(audio)

    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_create)
    monkeypatch.setattr("app.service._windows_configured_https_proxy", lambda: "")
    try:
        result = await service._bilibili_download(
            "123",
            {
                "url": "https://www.bilibili.com/video/BV1xx411c7mD",
                "audio_only": True,
            },
        )
    finally:
        await service.stop()

    assert result["ok"] is True
    assert result["delivery"] == "file"
    assert not [
        call
        for call in adapter.calls
        if call[0] == "send_group_msg"
        and isinstance(call[1].get("message"), list)
        and call[1]["message"]
        and call[1]["message"][0].get("type") == "video"
    ]
    assert [call for call in adapter.calls if call[0] == "upload_group_file"]
