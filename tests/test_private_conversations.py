"""Private OneBot sessions share the agent pipeline without becoming groups."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.service import AgentService, normalise_onebot_event


class Secrets:
    def get_llm_api_key(self) -> str:
        return "test-key"


class Adapter:
    connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        if action in {"send_private_msg", "send_group_msg"}:
            return {"data": {"message_id": "private-reply"}}
        raise AssertionError(action)


def _private_message(message_id: str = "p1") -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": "42",
        "message_id": message_id,
        "time": 1,
        "self_id": "99",
        "sender": {"nickname": "小明"},
        "raw_message": "你好",
    }


def test_private_event_uses_collision_free_session_id() -> None:
    event = normalise_onebot_event(_private_message())
    assert event["group_id"] == "private:42"
    assert event["content"]["conversation_type"] == "private"
    assert event["pending"] is True


@pytest.mark.asyncio
async def test_private_session_can_be_enabled_and_send_current_session_message(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=Secrets())
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    service.db.upsert_group("private:42", "私聊 · 小明", "private")
    service.db.set_group_config("private:42", True)
    service.db.set_group_initialized("private:42", True)

    scheduled: list[str] = []

    async def record_schedule(conversation_id: str) -> None:
        scheduled.append(conversation_id)

    service._schedule_worker = record_schedule  # type: ignore[method-assign]
    await service.handle_onebot_event(_private_message())
    pending = service.db.pending_events("private:42")
    assert [item["message_id"] for item in pending] == ["p1"]
    assert scheduled == ["private:42"]

    turn_id = service.db.create_turn("private:42", [pending[0]["id"]])
    result = await service._execute_tool(
        turn_id, "private:42", "send_group_message", {"text": "收到"}, "send-private"
    )
    assert result["ok"] is True
    assert adapter.calls[0][0] == "send_private_msg"
    assert adapter.calls[0][1]["user_id"] == 42

    await service.stop()

