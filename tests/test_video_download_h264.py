from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from app.db import Database
from app.service import AgentService, _probe_h264_formats


def _probe(*, h264: bool, url: str = "https://www.youtube.com/watch?v=test") -> Dict[str, Any]:
    formats = []
    if h264:
        formats.append(
            {
                "format_id": "18",
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
                "height": 360,
                "width": 640,
                "ext": "mp4",
                "tbr": 500,
            }
        )
    return {
        "ok": True,
        "metadata": {
            "h264_available": h264,
            "h264_formats": formats,
            "resolved_url": url,
            "title": "测试视频",
            "max_h264_height": 360 if h264 else 0,
            "estimated_transcode": {
                "height_bucket": 360,
                "seconds_min": 1.8,
                "seconds_max": 2.1,
                "includes_upload": False,
            },
        },
    }


class DownloadProcess:
    returncode = 0

    def __init__(self, path: Path) -> None:
        self.path = path

    async def communicate(self) -> tuple[bytes, bytes]:
        return (str(self.path).encode("utf-8"), b"")


class TranscodeProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


def _media_metadata(video_codec: str = "h264", audio_codec: str = "aac") -> Dict[str, Any]:
    return {
        "_probe_ok": True,
        "streams": [
            {
                "codec_type": "video",
                "codec_name": video_codec,
                "codec_tag_string": "avc1" if video_codec == "h264" else "",
                "profile": "Main",
                "level": 31,
                "width": 640,
                "height": 360,
                "pix_fmt": "yuv420p",
            },
            {"codec_type": "audio", "codec_name": audio_codec, "channels": 2},
        ],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
    }


def _faststart_mp4_bytes() -> bytes:
    # Minimal top-level atom sequence sufficient for the bounded fast-start
    # parser; ffprobe is mocked in these branch tests.
    return (
        (16).to_bytes(4, "big")
        + b"ftyp"
        + b"12345678"
        + (8).to_bytes(4, "big")
        + b"moov"
        + (8).to_bytes(4, "big")
        + b"mdat"
    )


async def valid_media_probe(path: Path) -> Dict[str, Any]:
    return {
        "_probe_ok": True,
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "codec_tag_string": "avc1", "profile": "Main", "level": 31, "width": 640, "height": 360, "pix_fmt": "yuv420p"},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2},
        ],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
    }


@pytest.mark.asyncio
async def test_non_h264_creates_confirmation_without_starting_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data")
    calls = 0

    async def fake_probe(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _probe(h264=False)

    async def should_not_download(*args: Any, **kwargs: Any) -> DownloadProcess:
        nonlocal calls
        calls += 1
        raise AssertionError("download must wait for explicit approval")

    monkeypatch.setattr(service, "_yt_dlp_probe", fake_probe)
    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", should_not_download)
    try:
        result = await service._youtube_download(
            "private:1", {"url": "https://www.youtube.com/watch?v=test"}
        )
        pending = service.db.get_active_video_download_confirmation("private:1")
    finally:
        await service.stop()

    assert result["ok"] is False
    assert result["awaiting_confirmation"] is True
    assert result["confirmation_id"]
    assert result["required_tool"] == "send_group_message"
    assert result["qq_side_effect"] is False
    assert "同意" in result["user_visible_text"]
    assert calls == 0
    assert pending and pending["confirmation_id"] == result["confirmation_id"]


@pytest.mark.asyncio
async def test_confirmation_approval_downloads_once_and_records_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "Workspace"
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(workspace_root))
    service = AgentService(tmp_path / "data")
    output = service.conversation_workspace("private:1") / "approved.mp4"
    download_calls = 0
    commands: list[list[str]] = []
    probe_results = iter([_probe(h264=False), _probe(h264=True)])

    async def fake_probe(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return next(probe_results)

    async def fake_download(*command: Any, **kwargs: Any) -> DownloadProcess:
        nonlocal download_calls
        if "--print" in command:
            download_calls += 1
            commands.append([str(item) for item in command])
        output.write_bytes(b"mp4")
        return DownloadProcess(output)

    monkeypatch.setattr(service, "_yt_dlp_probe", fake_probe)
    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_download)
    monkeypatch.setattr(service, "_probe_media_file", valid_media_probe)
    try:
        initial = await service._youtube_download(
            "private:1",
            {"url": "https://www.youtube.com/watch?v=test", "filename": "approved.mp4"},
        )
        approved = await service._confirm_video_download(
            "private:1",
            {"confirmation_id": initial["confirmation_id"], "decision": "approve"},
            actor_id="42",
        )
        stored = service.db.get_video_download_confirmation("private:1", initial["confirmation_id"])
    finally:
        await service.stop()

    assert initial["awaiting_confirmation"] is True
    assert approved["ok"] is True
    assert approved["confirmed_non_h264"] is True
    assert approved["path"] == "approved.mp4"
    assert download_calls == 1
    assert stored and stored["status"] == "completed" and stored["actor_id"] == "42"
    selector = commands[0][commands[0].index("-f") + 1]
    assert "avc1" in selector


@pytest.mark.asyncio
async def test_h264_source_uses_avc_selector_and_audio_only_skips_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data")
    output = service.conversation_workspace("private:1") / "video.mp4"
    commands: list[list[str]] = []

    async def h264_probe(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _probe(h264=True)

    async def fake_download(*command: Any, **kwargs: Any) -> DownloadProcess:
        commands.append([str(item) for item in command])
        output.write_bytes(b"mp4")
        return DownloadProcess(output)

    monkeypatch.setattr(service, "_yt_dlp_probe", h264_probe)
    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_download)
    monkeypatch.setattr(service, "_probe_media_file", valid_media_probe)
    try:
        result = await service._youtube_download(
            "private:1", {"url": "https://www.youtube.com/watch?v=test", "filename": "video.mp4"}
        )
        audio_result = await service._youtube_download(
            "private:1",
            {
                "url": "https://www.youtube.com/watch?v=test",
                "filename": "audio.mp3",
                "audio_only": True,
            },
        )
    finally:
        await service.stop()

    assert result["ok"] is True
    selector = commands[0][commands[0].index("-f") + 1]
    assert "avc1" in selector and "h264" in selector
    assert audio_result["ok"] is True


@pytest.mark.asyncio
async def test_download_rejects_and_removes_video_when_ffprobe_cannot_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data")
    output = service.conversation_workspace("private:1") / "unverified.mp4"

    async def h264_probe(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _probe(h264=True)

    async def fake_download(*command: Any, **kwargs: Any) -> DownloadProcess:
        output.write_bytes(b"not-a-real-video")
        return DownloadProcess(output)

    async def failed_media_probe(path: Path) -> Dict[str, Any]:
        return {"_probe_ok": False, "_probe_error": "ffprobe JSON 无效"}

    monkeypatch.setattr(service, "_yt_dlp_probe", h264_probe)
    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_download)
    monkeypatch.setattr(service, "_probe_media_file", failed_media_probe)
    try:
        result = await service._youtube_download(
            "private:1", {"url": "https://www.youtube.com/watch?v=test", "filename": "unverified.mp4"}
        )
    finally:
        await service.stop()

    assert result["ok"] is False
    assert "ffprobe" in result["error"]
    assert not output.exists()


@pytest.mark.asyncio
async def test_unclear_waits_and_decline_never_starts_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data")
    downloads = 0

    async def no_h264_probe(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _probe(h264=False)

    async def should_not_download(*args: Any, **kwargs: Any) -> DownloadProcess:
        nonlocal downloads
        downloads += 1
        raise AssertionError("declined confirmation must not download")

    monkeypatch.setattr(service, "_yt_dlp_probe", no_h264_probe)
    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", should_not_download)
    try:
        initial = await service._youtube_download(
            "private:1", {"url": "https://www.youtube.com/watch?v=test"}
        )
        unclear = await service._confirm_video_download(
            "private:1", {"confirmation_id": initial["confirmation_id"], "decision": "unclear"}
        )
        declined = await service._confirm_video_download(
            "private:1", {"confirmation_id": initial["confirmation_id"], "decision": "decline"}
        )
    finally:
        await service.stop()

    assert unclear["ok"] is True and unclear["awaiting_confirmation"] is True
    assert declined["ok"] is True and declined["download_started"] is False
    assert downloads == 0


@pytest.mark.asyncio
async def test_transcode_fast_path_audio_remux_and_full_encode_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path / "data")
    source = tmp_path / "source.mp4"
    source.write_bytes(_faststart_mp4_bytes())
    calls: list[list[str]] = []
    metadata = _media_metadata()

    async def fake_probe(path: Path) -> Dict[str, Any]:
        return metadata

    async def fake_ffmpeg(*command: Any, **kwargs: Any) -> TranscodeProcess:
        calls.append([str(item) for item in command])
        Path(str(command[-1])).write_bytes(_faststart_mp4_bytes())
        return TranscodeProcess()

    monkeypatch.setattr("app.service._find_local_executable", lambda name: "ffmpeg.exe")
    monkeypatch.setattr(service, "_probe_media_file", fake_probe)
    monkeypatch.setattr("app.service.asyncio.create_subprocess_exec", fake_ffmpeg)
    try:
        # Already fast-start H.264/AAC: no ffmpeg process is needed.
        fast_path = await service._transcode_video_for_qq(source)
        assert fast_path == source
        assert calls == []

        # H.264 video with non-AAC audio: copy the video, transcode audio.
        async def opus_probe(path: Path) -> Dict[str, Any]:
            return _media_metadata(audio_codec="opus")

        monkeypatch.setattr(service, "_probe_media_file", opus_probe)
        remuxed = await service._transcode_video_for_qq(source)
        assert remuxed.is_file()
        assert "-c:v" in calls[-1] and calls[-1][calls[-1].index("-c:v") + 1] == "copy"
        assert "libx264" not in calls[-1]

        # Non-H.264 video: full H.264/AAC encode.
        async def vp9_probe(path: Path) -> Dict[str, Any]:
            return _media_metadata(video_codec="vp9")

        monkeypatch.setattr(service, "_probe_media_file", vp9_probe)
        encoded = await service._transcode_video_for_qq(source)
        assert encoded.is_file()
        assert "libx264" in calls[-1]
    finally:
        await service.stop()


def test_recover_interrupted_video_download_confirmations(tmp_path: Path) -> None:
    db = Database(tmp_path / "agent.sqlite3")
    try:
        first = db.create_video_download_confirmation(
            "first", "private:1", "youtube", {}, "first.mp4", {}, "", "2999-01-01T00:00:00Z"
        )
        db.resolve_video_download_confirmation("private:1", first["confirmation_id"], "approve")
        db.claim_video_download_confirmation("private:1", first["confirmation_id"])
        second = db.create_video_download_confirmation(
            "second", "private:1", "youtube", {}, "second.mp4", {}, "", "2999-01-01T00:00:00Z"
        )
        db.resolve_video_download_confirmation("private:1", second["confirmation_id"], "approve")
        assert db.recover_video_download_confirmations() == 2
        assert db.get_video_download_confirmation("private:1", "first")["status"] == "failed"
        assert db.get_video_download_confirmation("private:1", "second")["status"] == "failed"

        expired = db.create_video_download_confirmation(
            "expired", "private:1", "youtube", {}, "expired.mp4", {}, "", "2000-01-01T00:00:00+00:00"
        )
        db.resolve_video_download_confirmation("private:1", expired["confirmation_id"], "approve")
        assert db.get_video_download_confirmation("private:1", "expired")["status"] == "expired"
        approved_expired = db.create_video_download_confirmation(
            "approved-expired", "private:1", "youtube", {}, "approved.mp4", {}, "", "2999-01-01T00:00:00+00:00"
        )
        db.resolve_video_download_confirmation("private:1", approved_expired["confirmation_id"], "approve")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE video_download_confirmations SET expires_at = ? WHERE confirmation_id = ?",
                ("2000-01-01T00:00:00+00:00", approved_expired["confirmation_id"]),
            )
        assert db.claim_video_download_confirmation(
            "private:1", approved_expired["confirmation_id"]
        ) is None
        assert db.get_video_download_confirmation("private:1", "approved-expired")["status"] == "expired"
    finally:
        db.close()


def test_h264_probe_only_reports_formats_that_fit_the_720p_selector() -> None:
    summary = _probe_h264_formats(
        {
            "formats": [
                {"format_id": "137", "vcodec": "avc1.640028", "height": 1080},
                {"format_id": "136", "vcodec": "avc1.4d401f", "height": 720},
            ]
        }
    )
    assert summary["h264_available"] is True
    assert [item["format_id"] for item in summary["h264_formats"]] == ["136"]
    only_1080 = _probe_h264_formats(
        {"formats": [{"format_id": "137", "vcodec": "avc1.640028", "height": 1080}]}
    )
    assert only_1080["h264_available"] is False
