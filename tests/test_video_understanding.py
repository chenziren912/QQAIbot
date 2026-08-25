from __future__ import annotations

from pathlib import Path

import pytest

import app.service as service_module
from app.llm import ChatCompletionsClient
from app.service import AgentService, WorkspaceError, normalise_onebot_event


class FakeAdapter:
    connected = True

    def __init__(self) -> None:
        self.calls = []

    async def call(self, action, params=None, timeout=None):
        self.calls.append((action, dict(params or {})))
        return {"status": "ok", "data": {"message_id": str(len(self.calls))}}


class FakeProcess:
    returncode = 0

    def __init__(self, args) -> None:
        self.args = list(args)

    async def communicate(self):
        pattern = Path(self.args[-1])
        pattern.parent.mkdir(parents=True, exist_ok=True)
        for index in range(1, 4):
            (pattern.parent / ("frame-%08d.jpg" % index)).write_bytes(b"not-real-video-frame")
        return b"", b""


def test_video_event_exposes_video_metadata() -> None:
    event = normalise_onebot_event(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 123,
            "user_id": 9,
            "message_id": 44,
            "message": [{"type": "video", "data": {"file": "video-1", "name": "demo.mp4"}}],
        }
    )
    assert event["content"]["video"]["id"] == "video-1"
    assert event["content"]["video"]["name"] == "demo.mp4"
    assert "视频元数据" in AgentService._format_events([event])


@pytest.mark.asyncio
async def test_video_frames_are_split_at_300k_and_merged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(tmp_path / "Workspace"))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "视频群")
    service.adapter = FakeAdapter()
    monkeypatch.setattr(service.secret_store, "get_llm_api_key", lambda: "test-key")
    video = service.conversation_workspace("123") / "demo.mp4"
    video.write_bytes(b"video")
    turn_id = service.db.create_turn("123", [])

    monkeypatch.setattr(service_module.shutil, "which", lambda name: "ffmpeg.exe" if name == "ffmpeg" else None)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess(args)

    monkeypatch.setattr(service_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        service_module,
        "image_file_to_data_uri",
        lambda path, mime=None: "data:image/jpeg;base64," + ("A" * 200_000),
    )
    frame_calls = []

    async def fake_analyze(self, frame_parts, **kwargs):
        frame_calls.append((len(frame_parts), kwargs["frame_start"], kwargs["frame_end"]))
        return "分段 %s-%s：包含绝大多数画面内容" % (kwargs["frame_start"], kwargs["frame_end"])

    async def fake_merge(self, summaries, **kwargs):
        return "最终合并：" + " | ".join(summaries)

    monkeypatch.setattr(ChatCompletionsClient, "analyze_video_frames", fake_analyze)
    monkeypatch.setattr(ChatCompletionsClient, "summarize_video_summaries", fake_merge)

    result = await service._execute_tool(
        turn_id,
        "123",
        "Builtin_video_understanding",
        {"path": "demo.mp4"},
        "video-call",
    )
    assert result["ok"] is True, result
    assert result["frame_interval"] == 10
    assert result["frames"] == 3
    assert result["chunks"] == 3
    assert len(frame_calls) == 3
    assert "最终合并" in result["summary"]
    assert service.adapter.calls[0][0] == "send_group_msg"


@pytest.mark.asyncio
async def test_video_download_refreshes_stale_event_url_before_giving_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QQ event URLs are temporary; a fresh OneBot URL is a safe fallback."""

    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(tmp_path / "Workspace"))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "视频群")

    class RefreshAdapter(FakeAdapter):
        async def call(self, action, params=None, timeout=None):
            self.calls.append((action, dict(params or {})))
            assert action == "get_group_file_url"
            return {"status": "ok", "data": {"url": "https://fresh.qq.example/video.mp4"}}

    service.adapter = RefreshAdapter()
    downloads: list[str] = []

    async def fake_download(source: str, target: Path) -> None:
        downloads.append(source)
        if source == "https://stale.qq.example/video.mp4":
            raise WorkspaceError("QQ 临时视频地址返回 HTTP 403")
        target.write_bytes(b"fresh-video")

    monkeypatch.setattr(service, "_download_remote_video_to_path", fake_download)
    path = await service._download_video_to_workspace(
        "123",
        "video-file-id",
        "demo.mp4",
        source_url="https://stale.qq.example/video.mp4",
    )

    assert path.name == "demo.mp4"
    assert path.read_bytes() == b"fresh-video"
    assert downloads == ["https://stale.qq.example/video.mp4", "https://fresh.qq.example/video.mp4"]
    assert [action for action, _params in service.adapter.calls] == ["get_group_file_url"]


@pytest.mark.asyncio
async def test_video_download_falls_back_from_broken_group_url_to_generic_cached_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad fresh CDN URL must not hide NapCat's local get_file cache."""

    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(tmp_path / "Workspace"))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "视频群")
    cached = tmp_path / "napcat-cache.mp4"
    cached.write_bytes(b"cached-video")

    class CandidateAdapter(FakeAdapter):
        async def call(self, action, params=None, timeout=None):
            self.calls.append((action, dict(params or {})))
            if action == "get_group_file_url":
                return {"status": "ok", "data": {"url": "https://fresh.qq.example/video.mp4"}}
            if action == "get_file":
                return {"status": "ok", "data": {"path": str(cached)}}
            raise AssertionError(action)

    service.adapter = CandidateAdapter()
    downloads: list[str] = []

    async def fake_download(source: str, target: Path) -> None:
        downloads.append(source)
        raise WorkspaceError("读取 QQ 临时视频地址失败：TLS certificate verify failed")

    monkeypatch.setattr(service, "_download_remote_video_to_path", fake_download)
    path = await service._download_video_to_workspace("123", "video-file-id", "demo.mp4")

    assert path.name == "demo.mp4"
    assert path.read_bytes() == b"cached-video"
    assert downloads == ["https://fresh.qq.example/video.mp4"]
    assert [action for action, _params in service.adapter.calls] == ["get_group_file_url", "get_file"]


@pytest.mark.asyncio
async def test_video_source_failure_requests_one_visible_explanation_in_next_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed pre-analysis download is repairable without replaying it."""

    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(tmp_path / "Workspace"))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "视频群")
    adapter = FakeAdapter()
    service.adapter = adapter
    event_id = service.db.insert_event(
        normalise_onebot_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "user_id": 9,
                "message_id": "video-request",
                "message": "请分析这个视频",
            }
        )
    )
    assert event_id is not None
    turn_id = service.db.create_turn("123", [event_id])

    async def unavailable(*_args, **_kwargs):
        raise WorkspaceError("读取 QQ 临时视频地址失败：<urlopen error [Errno 2] No such file or directory>")

    monkeypatch.setattr(service, "_download_video_to_workspace", unavailable)
    failed = await service._execute_tool(
        turn_id,
        "123",
        "Builtin_video_understanding",
        {"file_id": "video-file-id", "url": "https://temporary.qq.example/video"},
        "video-call",
        operation_slot=0,
    )
    assert failed["ok"] is False
    assert failed["retry_safe"] is True
    assert failed["required_tool"] == "send_group_message"
    assert failed["repair_uses_next_slot"] is True
    assert "重新发送原视频" in failed["user_visible_text"]
    assert not adapter.calls

    # The source-analysis operation is already durably recorded at slot 0.
    # A concise failure notice must use slot 1, not be deduplicated into that
    # failed operation, and it must not trigger another video download.
    sent = await service._execute_tool(
        turn_id,
        "123",
        "send_group_message",
        {"text": failed["user_visible_text"]},
        "video-failure-notice",
        operation_slot=1,
    )
    assert sent["ok"] is True
    assert [action for action, _params in adapter.calls] == ["send_group_msg"]
