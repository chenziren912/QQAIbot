"""Regression coverage for durable group/reconnect behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.service import AgentService, normalise_onebot_event
from app.onebot import OneBotActionTimeoutError


class FakeSecretStore:
    def get_llm_api_key(self) -> str:
        return "test-key"


class HistoryAdapter:
    def __init__(self, histories: dict[str, list[dict[str, Any]]]) -> None:
        self.connected = True
        self.connection_id = 1
        self.histories = histories
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        if action == "get_group_msg_history":
            return {"data": {"messages": self.histories.get(str(params["group_id"]), [])}}
        if action == "get_group_list":
            return {"data": []}
        raise AssertionError("unexpected OneBot action: " + action)

    async def disconnect(self, **_: Any) -> None:
        self.connected = False


def _message(group_id: str, message_id: str, text: str, timestamp: int) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "message_id": message_id,
        "time": timestamp,
        "user_id": "42",
        "raw_message": text,
    }


@pytest.mark.asyncio
async def test_reconnect_backfill_deduplicates_and_skips_disabled_groups(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=FakeSecretStore())
    service.db.upsert_group("100", "enabled")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    service.db.upsert_group("200", "disabled")
    service.db.set_group_config("200", False)

    old = _message("100", "old", "already seen", 1)
    service.db.insert_event(normalise_onebot_event(old))
    own = _message("100", "own", "the app sent this", 3)
    service.db.add_sent_message("own", "100", 1, "the app sent this")
    adapter = HistoryAdapter(
        {
            "100": [old, _message("100", "new", "missed during reconnect", 2), own],
            "200": [_message("200", "ignored", "must not be collected", 2)],
        }
    )
    service.adapter = adapter  # type: ignore[assignment]
    scheduled: list[str] = []

    async def record_schedule(group_id: str) -> None:
        scheduled.append(group_id)

    service._schedule_worker = record_schedule  # type: ignore[method-assign]
    await service._recover_enabled_groups_after_reconnect(adapter)  # type: ignore[arg-type]

    pending = service.db.pending_events("100")
    # Reconnect history is restored into the raw context, but never treated
    # as a new live agent trigger (an old @ must not produce a late reply).
    assert [event["message_id"] for event in pending] == ["old"]
    assert [event["message_id"] for event in service.db.unarchived_events("100")] == ["old", "new", "own"]
    assert scheduled == ["100"]
    assert [params["group_id"] for action, params in adapter.calls if action == "get_group_msg_history"] == [100]
    assert service.db.pending_events("200") == []

    await service.stop()


@pytest.mark.asyncio
async def test_disable_waits_for_existing_worker_cancellation(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=FakeSecretStore())
    service.db.upsert_group("100", "enabled")
    service.db.set_group_config("100", True)
    started = asyncio.Event()

    async def blocked_worker() -> None:
        started.set()
        await asyncio.Event().wait()

    worker = asyncio.create_task(blocked_worker())
    service._workers["100"] = worker
    await started.wait()

    await service.disable_group("100", "", "inherit")

    assert worker.cancelled()
    assert not service.db.get_group("100")["enabled"]
    await service.stop()


@pytest.mark.asyncio
async def test_retry_does_not_reset_processed_events(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=FakeSecretStore())
    service.db.upsert_group("100", "enabled")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    event_id = service.db.insert_event(normalise_onebot_event(_message("100", "done", "already summarized", 1)))
    assert event_id is not None
    service.db.mark_events_processed([event_id])
    scheduled: list[str] = []

    async def record_schedule(group_id: str) -> None:
        scheduled.append(group_id)

    service._schedule_worker = record_schedule  # type: ignore[method-assign]
    await service.retry_group("100")

    assert service.db.pending_events("100") == []
    assert scheduled == ["100"]
    await service.stop()


@pytest.mark.asyncio
async def test_live_events_wait_for_initial_history_backfill(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=FakeSecretStore())
    service.db.upsert_group("100", "enabled")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", False)
    service._history_backfill_groups.add("100")
    scheduled: list[str] = []

    async def record_schedule(group_id: str) -> None:
        scheduled.append(group_id)

    service._schedule_worker = record_schedule  # type: ignore[method-assign]
    await service.handle_onebot_event(_message("100", "live", "arrived during initialization", 2))

    assert [event["message_id"] for event in service.db.pending_events("100")] == ["live"]
    assert scheduled == []
    service._history_backfill_groups.discard("100")
    await service.stop()


@pytest.mark.asyncio
async def test_reconnect_history_timeout_keeps_live_processing_path_available(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=FakeSecretStore())
    service.db.upsert_group("100", "enabled")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)

    class TimeoutAdapter(HistoryAdapter):
        async def call(self, action: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
            self.calls.append((action, params))
            if action == "get_group_msg_history":
                raise OneBotActionTimeoutError("timed out")
            return {"data": []}

    adapter = TimeoutAdapter({})
    service.adapter = adapter  # type: ignore[assignment]
    scheduled: list[str] = []

    async def record_schedule(group_id: str) -> None:
        scheduled.append(group_id)

    service._schedule_worker = record_schedule  # type: ignore[method-assign]
    await service._recover_enabled_groups_after_reconnect(adapter)  # type: ignore[arg-type]

    assert "实时消息仍可继续" in str(service.db.get_group("100")["last_error"])
    assert scheduled == ["100"]
    await service.stop()


@pytest.mark.asyncio
async def test_reset_group_memory_clears_group_rules_and_schedules_safe_recompute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path, secret_store=FakeSecretStore())
    try:
        service.db.upsert_group("100", "重算群")
        service.db.set_group_config("100", True, "只对旧群生效", "inherit")
        service.db.set_group_initialized("100", True)
        event_id = service.db.insert_event(
            {
                "dedupe_key": "reset:100:1",
                "group_id": "100",
                "event_type": "message.group",
                "message_id": "reset-message",
                "occurred_at": 1,
                "sender_id": "member",
                "sender_name": "成员",
                "normalized_text": "长期偏好是蓝色",
                "content": {"text": "长期偏好是蓝色"},
                "raw": {},
                "pending": False,
                "archived": True,
                "memory_processed": True,
            }
        )
        assert event_id
        service.db.save_summary("100", "旧摘要", event_id, turn_id=1)
        service.db.upsert_group_memory(
            "100",
            "preference:member:color",
            "preference",
            "成员偏好蓝色。",
            {"event_id": event_id, "quote": "长期偏好是蓝色"},
        )
        scheduled: list[str] = []

        async def schedule(group_id: str) -> None:
            scheduled.append(group_id)

        monkeypatch.setattr(service, "_schedule_worker", schedule)
        result = await service.reset_group_memory("100")

        assert result["scheduled"] is True
        assert result["global_rules_preserved"] is True
        assert scheduled == ["100"]
        assert service.db.get_group("100")["prompt_override"] == ""
        assert service.db.get_summary("100") == ""
        assert service.db.list_group_memories("100", active_only=False) == []
        assert service.db.memory_pending_events("100")
        assert service.db.unarchived_events("100")
    finally:
        await service.stop()
