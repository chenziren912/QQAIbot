from __future__ import annotations

from typing import Any, Dict, List

import pytest

import app.config as config
from app.llm import LLMResult
from app.service import AgentService


class MemorySecrets:
    def get_llm_api_key(self) -> str:
        return "test-key"

    def get_onebot_token(self) -> None:
        return None


def add_event(
    service: AgentService,
    *,
    group_id: str = "100",
    message_id: str = "m1",
    text: str = "我以后写新项目优先用 Rust",
    pending: bool = False,
    memory_processed: bool = False,
) -> Dict[str, Any]:
    event_id = service.db.insert_event(
        {
            "dedupe_key": f"{group_id}:message:{message_id}",
            "group_id": group_id,
            "event_type": "message.group",
            "sub_type": "normal",
            "message_id": message_id,
            "occurred_at": 100,
            "sender_id": "42",
            "sender_name": "小陈",
            "self_id": "999",
            "normalized_text": text,
            "content": {},
            "raw": {},
            "is_self": False,
            "pending": pending,
            "archived": False,
            "memory_processed": memory_processed,
        }
    )
    assert event_id is not None
    return service.db.memory_pending_events(group_id, limit=100)[-1]


def preference_proposal(event_id: int, quote: str, value: str = "新项目优先用 Rust") -> Dict[str, Any]:
    return {
        "proposal_id": "p1",
        "operation": "remember",
        "memory_type": "preference",
        "subject_id": "42",
        "subject_name": "小陈",
        "predicate": "编程语言偏好",
        "value": value,
        "target_memory_id": "",
        "temporal_status": "ongoing",
        "source_event_ids": [str(event_id)],
        "evidence": [{"event_id": str(event_id), "quote": quote}],
        "confidence": 0.99,
        "verification_reason": "原文是说话者自己的明确偏好。",
    }


@pytest.mark.asyncio
async def test_memory_batch_is_evidence_backed_searchable_and_group_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "来源群")
    service.db.upsert_group("200", "其他群")
    event = add_event(service)

    class Extractor:
        async def extract_memory_proposals(self, **kwargs: Any) -> List[Dict[str, Any]]:
            assert kwargs["reasoning_effort"] == "high"
            assert kwargs["event_records"][0]["sender_id"] == "42"
            return [preference_proposal(event["id"], "优先用 Rust")]

    assert await service._process_group_memory_batch(Extractor(), "100", [event], "high") == 1
    assert service.db.memory_pending_events("100") == []
    memories = service.list_group_memories("100", include_inactive=False)
    assert len(memories) == 1
    assert memories[0]["confidence_status"] == "confirmed"
    assert memories[0]["evidence"][0]["evidence_text"] == "优先用 Rust"
    assert "优先用 Rust" in service._build_group_memory_context("100", [event])
    assert service.list_group_memories("200", include_inactive=False) == []

    turn_id = service.db.create_turn("100", [event["id"]])
    result = await service._execute_tool(
        turn_id,
        "100",
        "Builtin_querymemory",
        {"query": "Rust", "limit": 10},
        "memory-search",
    )
    assert result["ok"] is True
    assert result["current_group_only"] is True
    assert result["memories"][0]["statement"].endswith("新项目优先用 Rust")

    # Chinese words need not be adjacent in the canonical structured
    # statement; deterministic n-grams still retrieve the right memory.
    chinese_result = await service._execute_tool(
        turn_id,
        "100",
        "Builtin_querymemory",
        {"query": "小陈偏好", "limit": 10},
        "memory-search-cjk",
    )
    assert chinese_result["ok"] is True
    assert chinese_result["memories"][0]["memory_id"] == memories[0]["id"]


@pytest.mark.asyncio
async def test_invented_memory_quote_does_not_advance_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "证据群")
    event = add_event(service)

    class BadExtractor:
        async def extract_memory_proposals(self, **_: Any) -> List[Dict[str, Any]]:
            return [preference_proposal(event["id"], "我从没说过的 Python 偏好")]

    with pytest.raises(ValueError, match="逐字片段"):
        await service._process_group_memory_batch(BadExtractor(), "100", [event], "off")
    assert [row["id"] for row in service.db.memory_pending_events("100")] == [event["id"]]
    assert service.db.list_group_memories("100") == []


@pytest.mark.asyncio
async def test_out_of_batch_evidence_does_not_advance_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "批次证据群")
    current = add_event(service, message_id="current", text="本轮只谈今天天气")
    unrelated = add_event(service, message_id="old", text="旧消息说优先用 Rust")

    class OutOfBatchExtractor:
        async def extract_memory_proposals(self, **_: Any) -> List[Dict[str, Any]]:
            return [preference_proposal(unrelated["id"], "优先用 Rust")]

    with pytest.raises(ValueError, match="批次之外"):
        await service._process_group_memory_batch(
            OutOfBatchExtractor(), "100", [current], "off"
        )
    assert {row["id"] for row in service.db.memory_pending_events("100")} == {
        current["id"],
        unrelated["id"],
    }
    assert service.db.list_group_memories("100") == []


@pytest.mark.asyncio
async def test_missing_memory_protocol_never_silently_advances_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "兼容性群")
    event = add_event(service)

    class ClientWithoutMemoryProtocol:
        pass

    assert await service._process_group_memory_batch(
        ClientWithoutMemoryProtocol(), "100", [event], "off"
    ) == -1
    assert [row["id"] for row in service.db.memory_pending_events("100")] == [event["id"]]


@pytest.mark.asyncio
async def test_memory_failure_does_not_replay_completed_group_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    calls = {"turn": 0, "memory": 0}

    class FailingMemoryClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def run_turn(self, **_: Any) -> LLMResult:
            calls["turn"] += 1
            return LLMResult(summary="内部摘要", tool_results=[])

        async def extract_memory_proposals(self, **_: Any) -> List[Dict[str, Any]]:
            calls["memory"] += 1
            raise RuntimeError("memory provider unavailable")

    monkeypatch.setattr("app.service.ChatCompletionsClient", FailingMemoryClient)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.settings.llm.model = "test-model"
    service.db.upsert_group("100", "重试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    event = add_event(service, pending=True)

    await service._run_group_worker("100")
    assert calls == {"turn": 1, "memory": 1}
    assert service.db.pending_events("100") == []
    assert [row["id"] for row in service.db.memory_pending_events("100")] == [event["id"]]
    assert "不会重放" in service.db.get_group("100")["last_error"]

    class RecoveredMemoryClient(FailingMemoryClient):
        async def run_turn(self, **_: Any) -> LLMResult:
            raise AssertionError("memory-only retry must not rerun the group agent turn")

        async def extract_memory_proposals(self, **_: Any) -> List[Dict[str, Any]]:
            calls["memory"] += 1
            return []

    monkeypatch.setattr("app.service.ChatCompletionsClient", RecoveredMemoryClient)
    await service._run_group_worker("100")
    assert calls == {"turn": 1, "memory": 2}
    assert service.db.memory_pending_events("100") == []


@pytest.mark.asyncio
async def test_human_memory_correction_keeps_old_revision_and_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "人工核验群")
    event = add_event(service)
    original = service.db.upsert_group_memory(
        "100",
        "preference:42:language",
        "preference",
        "小陈偏好 Rust",
        {"source_event_id": event["id"], "evidence_text": "优先用 Rust"},
        subject="小陈（QQ 42）",
        predicate="编程语言偏好",
        object_value="Rust",
    )
    corrected = await service.moderate_group_memory(
        "100",
        original["id"],
        "correct",
        replacement_text="小陈目前偏好 Rust，但工作项目按团队要求选择语言。",
        note="管理员依据本人说明修正",
    )
    assert corrected["active"] is True
    assert corrected["confidence_status"] == "confirmed"
    old = service.db.get_group_memory("100", original["id"])
    assert old is not None and old["active"] is False
    assert old["superseded_by_memory_id"] == corrected["id"]
    assert corrected["evidence"][0]["evidence_text"] == "优先用 Rust"


@pytest.mark.asyncio
async def test_successful_bot_send_becomes_memory_evidence(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(config, "keyring", None)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "机器人记忆群")
    seed = add_event(service, message_id="seed", text="请你记住自己的承诺")
    service.db.mark_events_memory_processed([seed["id"]])

    class Adapter:
        connected = True

        def __init__(self) -> None:
            self.sequence = 0

        async def call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
            assert action == "send_group_msg"
            self.sequence += 1
            return {"data": {"message_id": f"bot-{self.sequence}"}}

    service.adapter = Adapter()
    turn_id = service.db.create_turn("100", [seed["id"]])
    sent = await service._execute_tool(
        turn_id,
        "100",
        "send_group_message",
        {"text": "我承诺明天把完整代码发到群里。"},
        "model-send-1",
        operation_slot=0,
    )
    assert sent == {"ok": True, "message_id": "bot-1"}
    bot_events = service.db.memory_pending_events("100")
    assert len(bot_events) == 1
    assert bot_events[0]["is_self"] == 1
    assert bot_events[0]["event_type"] == "message.app_sent"

    class BotMemoryExtractor:
        async def extract_memory_proposals(self, **kwargs: Any) -> List[Dict[str, Any]]:
            event_id = kwargs["event_records"][0]["event_id"]
            assert kwargs["event_records"][0]["is_bot_message"] is True
            return [
                {
                    "proposal_id": "bot-promise",
                    "operation": "remember",
                    "memory_type": "commitment",
                    "subject_id": "",
                    "subject_name": "机器人",
                    "predicate": "承诺",
                    "value": "明天把完整代码发到群里",
                    "target_memory_id": "",
                    "temporal_status": "ongoing",
                    "source_event_ids": [str(event_id)],
                    "evidence": [
                        {"event_id": str(event_id), "quote": "明天把完整代码发到群里"}
                    ],
                    "confidence": 0.99,
                    "verification_reason": "机器人自己的明确承诺",
                }
            ]

    assert await service._process_group_memory_batch(
        BotMemoryExtractor(), "100", bot_events, "high"
    ) == 1
    memories = service.db.search_group_memories("100", "完整代码")
    assert len(memories) == 1
    assert memories[0]["evidence"][0]["source_message_id"] == "bot-1"
