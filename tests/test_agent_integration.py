"""High-value integration tests for queue limits, tool boundaries, and retry safety."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.llm import LLMResult
from app.service import AgentService, MAX_EVENT_TEXT_CHARS, MAX_HISTORY_EVENTS, normalise_onebot_event


class MemorySecrets:
    def __init__(self) -> None:
        self.api_key = "test-key"
        self.onebot_token = ""

    def get_llm_api_key(self) -> str:
        return self.api_key

    def set_llm_api_key(self, value: str) -> None:
        self.api_key = value

    def get_onebot_token(self) -> str:
        return self.onebot_token

    def set_onebot_token(self, value: str) -> None:
        self.onebot_token = value


class FakeAdapter:
    def __init__(self, history: List[Dict[str, Any]] | None = None) -> None:
        self.connected = True
        self.connection_id = 1
        self.history = history or []
        self.calls: List[tuple[str, Dict[str, Any]]] = []

    async def call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((action, params))
        if action == "get_group_msg_history":
            return {"data": {"messages": self.history}}
        if action == "send_group_msg":
            return {"data": {"message_id": "agent-message"}}
        if action == "delete_msg":
            return {"data": {}}
        if action == "get_group_list":
            return {"data": []}
        raise AssertionError("unexpected OneBot action " + action)

    async def disconnect(self, **_: Any) -> None:
        self.connected = False


def message(group_id: str, message_id: str, text: str, time: int = 1, **extra: Any) -> Dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "message_id": message_id,
        "time": time,
        "user_id": "42",
        "raw_message": text,
        **extra,
    }


def test_history_limit_and_live_deduplication(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    history = [message("100", str(index), "x", index) for index in range(201)]
    assert len(service._limit_history(history, "100")) == MAX_HISTORY_EVENTS

    # The newest chronological message wins when text capacity is the smaller
    # cap.  The initial event may itself be longer than the cap, but no second
    # event is allowed to push the batch across it.
    long_history = [message("100", "old", "a" * 30_000, 1), message("100", "new", "b" * 30_000, 2)]
    selected = service._limit_history(long_history, "100")
    assert [item["message_id"] for item in selected] == ["new"]
    assert len(normalise_onebot_event(selected[0], history=True)["normalized_text"]) <= MAX_EVENT_TEXT_CHARS

    historical = normalise_onebot_event(message("100", "same", "hello"), history=True)
    live = normalise_onebot_event(message("100", "same", "hello", sub_type="normal"))
    assert service.db.insert_event(historical) is not None
    assert service.db.insert_event(live) is None
    service.db.close()


@pytest.mark.asyncio
async def test_enabled_group_initializes_and_disabled_group_is_not_collected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class SummaryClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_turn(self, **_: Any) -> LLMResult:
            return LLMResult("初始摘要", [])

    monkeypatch.setattr("app.service.ChatCompletionsClient", SummaryClient)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "测试群")
    service.adapter = FakeAdapter([message("100", "1", "历史消息")])  # type: ignore[assignment]

    await service.enable_group("100", "", "inherit")
    # Bootstrap history now stays verbatim in the newest 50K context window;
    # it is intentionally not immediately compressed into a summary.
    await asyncio.sleep(0.02)
    assert service.db.get_summary("100") == ""
    assert service.db.pending_events("100") == []
    assert service.db.get_group("100")["initialized"]

    service.db.upsert_group("200", "关闭群")
    await service.handle_onebot_event(message("200", "disabled", "不应保存"))
    assert service.db.pending_events("200") == []
    await service.stop()


@pytest.mark.asyncio
async def test_completed_turn_keeps_llm_warning_for_group_and_turn_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warning = "模型调用失败\n阶段：工具执行后的最终摘要\nHTTP 503 Service Unavailable"
    calls: list[dict[str, Any]] = []

    class WarningClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_turn(self, **kwargs: Any) -> LLMResult:
            calls.append(kwargs)
            return LLMResult("安全回退摘要", [], warning=warning)

    monkeypatch.setattr("app.service.ChatCompletionsClient", WarningClient)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    event_id = service.db.insert_event(normalise_onebot_event(message("100", "warning-event", "一条消息")))
    assert event_id is not None

    await service._run_group_worker("100")

    group = service.db.get_group("100")
    turn = service.db.list_recent_turns(1)[0]
    assert group and group["last_error"] == warning
    assert turn["status"] == "completed"
    assert turn["summary_text"] == "安全回退摘要"
    assert turn["error"] == warning
    assert calls[0].get("autonomous_reply_required", False) is False
    await service.stop()


@pytest.mark.asyncio
async def test_tools_cannot_cross_group_and_batch_retry_never_sends_twice(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    adapter = FakeAdapter()
    service.adapter = adapter  # type: ignore[assignment]
    service.db.upsert_group("100", "当前群")
    service.db.upsert_group("200", "其他群")
    current_event = service.db.insert_event(normalise_onebot_event(message("100", "in-current", "hello")))
    blocked_event = service.db.insert_event(normalise_onebot_event(message("100", "blocked", "hello", 2)))
    other_event = service.db.insert_event(normalise_onebot_event(message("200", "in-other", "hello")))
    assert current_event and blocked_event and other_event

    blocked_turn = service.db.create_turn("100", [blocked_event])
    blocked = await service._execute_tool(
        blocked_turn,
        "100",
        "send_group_message",
        {"text": "不能跨群引用", "reply_to_message_id": "in-other"},
        "blocked-call",
    )
    assert not blocked["ok"]
    assert "跨群引用" in blocked["error"]
    assert not [call for call in adapter.calls if call[0] == "send_group_msg"]

    # A retry gets a new model call id (and can even propose different text),
    # but uses the same durable event batch.  It must reuse the old result.
    first_turn = service.db.create_turn("100", [current_event])
    first = await service._execute_tool(
        first_turn, "100", "send_group_message", {"text": "发送一次"}, "first-call"
    )
    retry_turn = service.db.create_turn("100", [current_event])
    retry = await service._execute_tool(
        retry_turn, "100", "send_group_message", {"text": "不要重复发送"}, "retry-call"
    )
    sent = [call for call in adapter.calls if call[0] == "send_group_msg"]
    assert first == {"ok": True, "message_id": "agent-message"}
    assert retry == first
    assert len(sent) == 1

    # A stale/hallucinated reply ID has no locally-known group owner.  Reply
    # is optional, so it must be omitted while the requested text still goes
    # out.  The warning is persisted in the audit/result and the durable
    # operation reservation still prevents a retry from sending twice.
    fallback_event = service.db.insert_event(normalise_onebot_event(message("100", "fallback", "hello", 4)))
    assert fallback_event
    fallback_turn = service.db.create_turn("100", [fallback_event])
    fallback = await service._execute_tool(
        fallback_turn,
        "100",
        "send_group_message",
        {"text": "不引用也要发送", "reply_to_message_id": "hallucinated-id"},
        "fallback-call",
    )
    assert fallback["ok"]
    assert fallback["ignored_reply_to_message_id"] == "hallucinated-id"
    assert "已忽略该可选引用" in fallback["warning"]
    fallback_send = adapter.calls[-1]
    assert fallback_send[0] == "send_group_msg"
    assert fallback_send[1]["message"] == [{"type": "text", "data": {"text": "不引用也要发送"}}]
    audit = service.db.get_tool_audit(fallback_turn, "fallback-call")
    assert audit is not None
    assert json.loads(audit["result_json"])["warning"] == fallback["warning"]

    fallback_retry_turn = service.db.create_turn("100", [fallback_event])
    fallback_retry = await service._execute_tool(
        fallback_retry_turn,
        "100",
        "send_group_message",
        {"text": "重试时也不应再次发送", "reply_to_message_id": "hallucinated-id"},
        "fallback-retry-call",
    )
    assert fallback_retry == fallback
    assert len([call for call in adapter.calls if call[0] == "send_group_msg"]) == 2

    # A tool echo without `self_id` is still recognized from the durable
    # message-id record, so it cannot start an AI feedback loop.
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    scheduled: list[str] = []

    async def record_schedule(group_id: str) -> None:
        scheduled.append(group_id)

    service._schedule_worker = record_schedule  # type: ignore[method-assign]
    await service.handle_onebot_event(message("100", "agent-message", "机器人回声", 3))
    echo = next(event for event in service.db.list_recent_events() if event["message_id"] == "agent-message")
    assert echo["is_self"] and not echo["pending"]
    assert scheduled == []

    unknown_turn = service.db.create_turn("100", [other_event])
    recalled = await service._execute_tool(
        unknown_turn, "100", "recall_own_message", {"message_id": "not-ours"}, "recall-call"
    )
    assert not recalled["ok"]
    assert not [call for call in adapter.calls if call[0] == "delete_msg"]
    await service.stop()


@pytest.mark.asyncio
async def test_only_pre_action_validation_failures_are_marked_retry_safe(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    adapter = FakeAdapter()
    service.adapter = adapter  # type: ignore[assignment]
    service.db.upsert_group("100", "测试群")
    event_id = service.db.insert_event(normalise_onebot_event(message("100", "event", "hello")))
    assert event_id is not None
    turn_id = service.db.create_turn("100", [event_id])

    # This fails before reservation and before calling NapCat, so an LLM is
    # allowed one corrected tool proposal in the same turn.
    invalid = await service._execute_tool(
        turn_id, "100", "send_group_message", {"text": ""}, "invalid-call"
    )
    assert invalid["ok"] is False
    assert invalid["retry_safe"] is True
    assert "未向 QQ 发起操作" in invalid["retry_safe_reason"]
    assert not adapter.calls
    audit = service.db.get_tool_audit(turn_id, "invalid-call")
    assert audit is not None
    assert json.loads(audit["result_json"])["retry_safe"] is True

    class AmbiguousAdapter(FakeAdapter):
        async def call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
            self.calls.append((action, params))
            raise RuntimeError("network timeout after request was handed to adapter")

    ambiguous = AmbiguousAdapter()
    service.adapter = ambiguous  # type: ignore[assignment]
    second_event = service.db.insert_event(normalise_onebot_event(message("100", "event-2", "hello", 2)))
    assert second_event is not None
    second_turn = service.db.create_turn("100", [second_event])
    unsafe = await service._execute_tool(
        second_turn, "100", "send_group_message", {"text": "可能已发送"}, "ambiguous-call"
    )
    assert unsafe["ok"] is False
    assert "retry_safe" not in unsafe
    assert [call[0] for call in ambiguous.calls] == ["send_group_msg"]

    # A durable operation replay is also intentionally not retry-safe: the
    # original action may have run before an earlier process exited.
    retry_turn = service.db.create_turn("100", [second_event])
    deduplicated = await service._execute_tool(
        retry_turn, "100", "send_group_message", {"text": "不应重发"}, "dedup-call"
    )
    assert deduplicated["ok"] is False
    assert "retry_safe" not in deduplicated
    assert len(ambiguous.calls) == 1
    await service.stop()


@pytest.mark.asyncio
async def test_images_are_retained_even_when_vision_upload_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.settings.llm.vision_enabled = False
    service.db.upsert_group("100", "图片群")
    raw = message(
        "100",
        "image-event",
        "",
        message=[{"type": "image", "data": {"url": "https://cdn.example/image.png"}}],
    )
    event_id = service.db.insert_event(normalise_onebot_event(raw))
    assert event_id
    event = service.db.pending_events("100")[0]
    png = b"\x89PNG\r\n\x1a\nimage-payload"

    async def fake_download(url: str, **_: Any) -> Any:
        return service.media.store_bytes(png, source_url=url)

    monkeypatch.setattr(service.media, "download_image", fake_download)
    assert await service._prepare_image_parts([event]) == []
    stored_event = service.db.pending_events("100")[0]
    stored_path = Path(stored_event["content"]["images"][0]["stored_path"])
    assert stored_path.exists()
    await service.stop()
