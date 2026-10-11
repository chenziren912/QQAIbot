"""Google Flow asynchronous video tool and conversation isolation coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import app.service as service_module
from app.onebot import OneBotActionError
from app.flow_video import FlowVideoClient, FlowVideoError
from app.llm import TOOLS
from app.service import AgentService


class FakeFlowClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.polls = 0

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "runtime": "qqbot-ai-browser", "submission_confirmation": "flow_page_dom", "ai_browser_control": True}

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {
            "id": "video_flow_1",
            "status": "queued",
            "submission_confirmed": True,
            "progress": 0,
            "model": kwargs["model"],
            "seconds": str(kwargs["seconds"]),
            "size": kwargs["size"],
            "created_at": "2026-08-26T00:00:00Z",
        }

    async def get(self, job_id: str) -> dict[str, Any]:
        self.polls += 1
        return {
            "id": job_id,
            "status": "completed",
            "progress": 100,
            "model": "gemini-omni-flash",
            "seconds": "8",
            "size": "1280x720",
            "completed_at": "2026-08-26T00:01:00Z",
        }

    async def download_content(self, job_id: str, destination: Path, *, index: int = 1) -> int:
        destination.write_bytes(b"fake mp4")
        return destination.stat().st_size


class FakeAdapter:
    connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, action: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append((action, params))
        return {"status": "ok", "data": {"message_id": str(len(self.calls))}}


class FlakyNoticeAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def call(self, action: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append((action, params))
        if self.failures:
            self.failures -= 1
            raise OneBotActionError(
                action,
                {"status": "failed", "retcode": 100, "message": "网络连接异常"},
            )
        return {"status": "ok", "data": {"message_id": str(len(self.calls))}}


@pytest.mark.asyncio
async def test_flow_video_create_list_and_result_are_async_and_scoped(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data")
    client = FakeFlowClient()
    service._flow_video_client = lambda: client  # type: ignore[method-assign]
    try:
        create_turn = service.db.create_turn("123", [])
        created = await service._execute_tool(
            create_turn,
            "123",
            "Builtin_video_generation_create",
            {"prompt": "一只纸飞机飞过城市", "seconds": 8},
            "create-1",
        )
        assert created["ok"] is True
        assert created["async"] is True
        assert created["job_id"] == "video_flow_1"
        assert created["status"] == "queued"
        assert client.created[0]["model"] == "gemini-omni-flash"
        # ``n`` is the public tool field; the client adapter must receive its
        # Python keyword as ``outputs`` so the request reaches the Flow API.
        assert client.created[0]["outputs"] == 1
        assert client.created[0]["workspace_dir"] == str(service.conversation_workspace("123"))

        list_turn = service.db.create_turn("123", [])
        listed = await service._execute_tool(
            list_turn,
            "123",
            "Builtin_video_generation_list",
            {},
            "list-1",
        )
        assert listed["ok"] is True
        assert listed["current_conversation_only"] is True
        assert listed["tasks"][0]["status"] == "completed"

        other_turn = service.db.create_turn("456", [])
        hidden = await service._execute_tool(
            other_turn,
            "456",
            "Builtin_video_generation_result",
            {"job_id": "video_flow_1"},
            "result-other",
        )
        assert hidden["ok"] is False
        assert hidden["current_conversation_only"] is True
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_flow_video_rejects_veo_and_only_exposes_omni_flash(tmp_path: Path) -> None:
    tool = next(item for item in TOOLS if item["function"]["name"] == "Builtin_video_generation_create")
    assert tool["function"]["parameters"]["properties"]["model"]["enum"] == ["gemini-omni-flash"]

    service = AgentService(tmp_path / "data")
    try:
        turn_id = service.db.create_turn("123", [])
        result = await service._execute_tool(
            turn_id,
            "123",
            "Builtin_video_generation_create",
            {"prompt": "测试", "model": "veo-3.1-fast"},
            "veo-rejected",
        )
        assert result["ok"] is False
        assert "固定使用 Gemini Omni Flash 1.1" in result["error"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_flow_video_client_rejects_non_omni_model() -> None:
    client = FlowVideoClient()
    with pytest.raises(FlowVideoError, match="固定使用 Gemini Omni Flash 1.1"):
        await client.create(prompt="测试", model="veo-3.1-fast")


def test_flow_timeout_detection_tracks_configured_wait_ceiling() -> None:
    assert AgentService._is_retryable_flow_timeout(
        "Timed out after 900 seconds waiting for Google Flow."
    ) is True
    assert AgentService._is_retryable_flow_timeout(
        "Timed out after 1200 seconds waiting for Google Flow."
    ) is True
    assert AgentService._is_retryable_flow_timeout(
        "Timed out after 1800 seconds waiting for Flow."
    ) is True
    assert AgentService._is_retryable_flow_timeout("HTTP request timed out") is False


@pytest.mark.asyncio
async def test_completed_flow_video_is_downloaded_transcoded_and_sent_once(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data")
    client = FakeFlowClient()
    adapter = FakeAdapter()
    service._flow_video_client = lambda: client  # type: ignore[method-assign]
    service.adapter = adapter  # type: ignore[assignment]

    async def passthrough(path: Path) -> Path:
        return path

    service._transcode_video_for_qq = passthrough  # type: ignore[method-assign]
    service.db.upsert_group("123", "video")
    job = service.db.create_flow_video_job("123", "video_flow_2", status="completed")
    try:
        first = await service._deliver_flow_video_job(job)
        second = await service._deliver_flow_video_job(
            service.db.get_flow_video_job("123", "video_flow_2") or job
        )
    finally:
        await service.stop()

    assert first["ok"] is True
    assert second["already_sent"] is True
    assert [action for action, _ in adapter.calls] == ["send_group_msg"]


@pytest.mark.asyncio
async def test_flow_timeout_auto_retries_once_and_links_jobs(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data")
    client = FakeFlowClient()
    service._flow_video_client = lambda: client  # type: ignore[method-assign]
    service.db.upsert_group("123", "video")
    original = service.db.create_flow_video_job(
        "123",
        "video_timeout_1",
        prompt="一只小猫在窗边打哈欠",
        model="gemini-omni-flash",
        seconds=8,
        size="1280x720",
        outputs=1,
    )
    try:
        updated = await service._flow_video_mark_terminal_failure(
            original,
            "Timed out after 1200 seconds waiting for Google Flow.",
            notify=False,
        )
        replacement = service.db.get_flow_video_job("123", "video_flow_1")
    finally:
        await service.stop()

    assert replacement is not None
    assert replacement["retry_count"] == 1
    assert replacement["retry_of_job_id"] == "video_timeout_1"
    assert updated["status"] == "failed"
    assert "video_flow_1" in updated["error"]
    assert len(client.created) == 1


@pytest.mark.asyncio
async def test_second_flow_timeout_is_sent_back_as_private_agent_event(tmp_path: Path) -> None:
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "video")
    service.db.set_group_config("123", enabled=False)
    original = service.db.create_flow_video_job(
        "123",
        "video_timeout_2",
        prompt="一只小猫在窗边打哈欠",
        retry_count=1,
        retry_of_job_id="video_timeout_1",
    )
    try:
        await service._flow_video_mark_terminal_failure(
            original,
            "Timed out after 1200 seconds waiting for Google Flow.",
            notify=False,
        )
        events = service.db.pending_events("123")
    finally:
        await service.stop()

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "service.flow_video_failure"
    assert "Timed out after 1200 seconds" in event["normalized_text"]
    assert "没有队列长度或拥堵指标" in event["normalized_text"]
    assert event["content"]["flow_video_failure"] is True
    assert event["content"]["retry_recommended"] is False


@pytest.mark.asyncio
async def test_flow_failure_notice_retries_transient_onebot_network_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AgentService(tmp_path / "data")
    adapter = FlakyNoticeAdapter()
    service.adapter = adapter  # type: ignore[assignment]
    monkeypatch.setattr(service_module, "FLOW_VIDEO_FAILURE_NOTICE_RETRY_DELAYS", (0.0, 0.0))
    try:
        result = await service._send_flow_video_failure_notice(
            "123",
            "video_notice_1",
            "Flow 任务失败",
        )
    finally:
        await service.stop()

    assert result["ok"] is True
    assert len(adapter.calls) == 2
