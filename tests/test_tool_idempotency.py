"""Regression test for durable QQ state-changing tool reservations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.service import AgentService, normalise_onebot_event


class FakeSecretStore:
    def get_llm_api_key(self) -> str:
        return "test-key"


class SendingAdapter:
    connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        assert action == "send_group_msg"
        return {"status": "ok", "retcode": 0, "data": {"message_id": "9001"}}

    async def disconnect(self, **_: Any) -> None:
        self.connected = False


@pytest.mark.asyncio
async def test_same_event_batch_cannot_send_twice_across_retried_turns(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=FakeSecretStore())
    service.db.upsert_group("100", "group")
    event_id = service.db.insert_event(
        normalise_onebot_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "100",
                "message_id": "event-1",
                "time": 1,
                "user_id": "42",
                "raw_message": "please answer",
            }
        )
    )
    assert event_id is not None
    adapter = SendingAdapter()
    service.adapter = adapter  # type: ignore[assignment]

    first_turn = service.db.create_turn("100", [event_id])
    first = await service._execute_tool(
        first_turn,
        "100",
        "send_group_message",
        {"text": "一次回复"},
        "first-call",
    )
    # Retrying the same unprocessed event batch produces a new database turn
    # and generally a new model tool-call id; it must still not transmit again.
    retry_turn = service.db.create_turn("100", [event_id])
    retry = await service._execute_tool(
        retry_turn,
        "100",
        "send_group_message",
        {"text": "模型重试时的不同文本"},
        "new-call-id",
    )

    assert first == {"ok": True, "message_id": "9001"}
    assert retry == first
    assert len(adapter.calls) == 1
    await service.stop()
