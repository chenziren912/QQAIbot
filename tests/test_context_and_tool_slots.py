"""Service-level context, trusted reply ID, and multi-slot regression tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.llm import LLMResult
from app.service import AgentService, DIRECT_MENTION_CONTEXT_MARKER, normalise_onebot_event


class MemorySecrets:
    def get_llm_api_key(self) -> str:
        return "test-key"


class SendingAdapter:
    connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        if action == "send_group_msg":
            return {"data": {"message_id": "sent-%s" % len(self.calls)}}
        if action == "delete_msg":
            return {"data": {}}
        raise AssertionError("unexpected OneBot action " + action)

    async def disconnect(self, **_: Any) -> None:
        self.connected = False


def _message(message_id: str, text: str, time: int, *, mention: bool = False) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "post_type": "message",
        "message_type": "group",
        "group_id": "100",
        "message_id": message_id,
        "time": time,
        "self_id": "bot-id",
        "user_id": "member-id",
        "sender": {"nickname": "成员"},
        "raw_message": text,
    }
    if mention:
        raw["message"] = [
            {"type": "at", "data": {"qq": "bot-id"}},
            {"type": "text", "data": {"text": text}},
        ]
    return raw


@pytest.mark.asyncio
async def test_worker_passes_latest_raw_window_verbatim_and_keeps_new_event_out_of_archive_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []

    class CapturingClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_turn(self, **kwargs: Any) -> LLMResult:
            captured.append(kwargs)
            return LLMResult("更新后的摘要", [])

    monkeypatch.setattr("app.service.ChatCompletionsClient", CapturingClient)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)

    processed_ids: list[int] = []
    for index in range(1, 11):
        event_id = service.db.insert_event(normalise_onebot_event(_message(str(index), "原文-%s" % index, index)))
        assert event_id is not None
        processed_ids.append(event_id)
    service.db.mark_events_processed(processed_ids)
    # NapCat is commonly configured not to report self messages.  A locally
    # audited bot reply must nevertheless count as one of the latest ten raw
    # messages passed to the next model request.
    service.db.add_sent_message("bot-10", "100", 1, "机器人原文-10")
    current_id = service.db.insert_event(normalise_onebot_event(_message("11", "本轮原文-11", 11)))
    assert current_id is not None

    await service._run_group_worker("100")

    assert len(captured) == 1
    recent = captured[0]["recent_context_text"]
    current = captured[0]["event_text"]
    # All newest raw messages remain verbatim (including the locally audited
    # bot reply and the current live event); no current-window message is
    # immediately handed to the archival summary section.
    for index in range(1, 11):
        assert "原文-%s" % index in recent
    assert "机器人: 机器人原文-10" in recent
    assert "本轮原文-11" in recent
    assert "没有新的旧消息需要写入滚动摘要" in current
    assert '"message_id":"3"' in recent
    assert '"message_id":"bot-10"' in recent
    assert '"message_id":"11"' in recent
    await service.stop()


def test_recent_context_never_replays_an_old_live_direct_mention_marker(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    old = normalise_onebot_event(_message("old-at", "旧的 @ 内容", 1, mention=True))
    current = normalise_onebot_event(_message("current", "现在的新消息", 2))
    old_id = service.db.insert_event(old)
    current_id = service.db.insert_event(current)
    assert old_id and current_id
    service.db.mark_events_processed([old_id])

    old_event = service.db.recent_group_message_events("100", 10)[0]
    assert old_event["content"]["live_direct_mention"] is True
    raw_recent = service._format_events([old_event], include_direct_mention_marker=False)
    current_batch = service._format_events(service.db.pending_events("100"))
    assert DIRECT_MENTION_CONTEXT_MARKER not in raw_recent
    assert "旧的 @ 内容" in raw_recent
    assert DIRECT_MENTION_CONTEXT_MARKER not in current_batch
    service.db.close()


@pytest.mark.asyncio
async def test_trusted_message_metadata_allows_only_exposed_reply_ids_and_keeps_newlines(
    tmp_path: Path,
) -> None:
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "测试群")
    adapter = SendingAdapter()
    service.adapter = adapter  # type: ignore[assignment]

    trusted_event_id = service.db.insert_event(
        normalise_onebot_event(_message("trusted-id", "@机器人 请回复", 1, mention=True))
    )
    hidden_event_id = service.db.insert_event(normalise_onebot_event(_message("hidden-id", "旧消息", 2)))
    assert trusted_event_id and hidden_event_id
    pending = service.db.pending_events("100")
    formatted = service._format_events([event for event in pending if event["message_id"] == "trusted-id"])
    assert DIRECT_MENTION_CONTEXT_MARKER in formatted
    assert "服务生成的可信消息元数据" in formatted
    assert '"message_id":"trusted-id"' in formatted

    turn = service.db.create_turn("100", [trusted_event_id])
    valid = await service._execute_tool(
        turn,
        "100",
        "send_group_message",
        {"text": "第一行\n第二行", "reply_to_message_id": "trusted-id"},
        "trusted-call",
        trusted_reply_message_ids=["trusted-id"],
    )
    assert valid["ok"]
    assert adapter.calls[-1][1]["message"] == [
        {"type": "reply", "data": {"id": "trusted-id"}},
        {"type": "text", "data": {"text": "第一行\n第二行"}},
    ]

    # The fabricated ID really belongs to this group, but it was not exposed
    # by service-generated metadata in this turn.  It is safely downgraded to
    # a plain message rather than becoming a model-controlled reply target.
    hidden_turn = service.db.create_turn("100", [hidden_event_id])
    fabricated = await service._execute_tool(
        hidden_turn,
        "100",
        "send_group_message",
        {"text": "普通发送", "reply_to_message_id": "hidden-id"},
        "fabricated-call",
        trusted_reply_message_ids=["trusted-id"],
    )
    assert fabricated["ok"]
    assert fabricated["ignored_reply_to_message_id"] == "hidden-id"
    assert "可信当前群消息元数据" in fabricated["warning"]
    assert adapter.calls[-1][1]["message"] == [{"type": "text", "data": {"text": "普通发送"}}]
    await service.stop()


@pytest.mark.asyncio
async def test_same_event_batch_has_independent_durable_tool_slots(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "测试群")
    event_id = service.db.insert_event(normalise_onebot_event(_message("event", "需要两条消息", 1)))
    assert event_id is not None
    adapter = SendingAdapter()
    service.adapter = adapter  # type: ignore[assignment]

    first_turn = service.db.create_turn("100", [event_id])
    first_slot = await service._execute_tool(
        first_turn, "100", "send_group_message", {"text": "第一条"}, "slot-0", operation_slot=0
    )
    second_slot = await service._execute_tool(
        first_turn, "100", "send_group_message", {"text": "第二条"}, "slot-1", operation_slot=1
    )
    assert first_slot["ok"] and second_slot["ok"]
    assert len(adapter.calls) == 2

    retry_turn = service.db.create_turn("100", [event_id])
    retry_slot_0 = await service._execute_tool(
        retry_turn, "100", "send_group_message", {"text": "不应重发 0"}, "retry-0", operation_slot=0
    )
    retry_slot_1 = await service._execute_tool(
        retry_turn, "100", "send_group_message", {"text": "不应重发 1"}, "retry-1", operation_slot=1
    )
    assert retry_slot_0 == first_slot
    assert retry_slot_1 == second_slot
    assert len(adapter.calls) == 2
    await service.stop()
