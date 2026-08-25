from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.llm import LLMResult
from app.service import AgentService, normalise_onebot_event


class Secrets:
    def get_llm_api_key(self) -> str:
        return "test-key"


def _message(message_id: str, text: str, timestamp: int) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": "100",
        "message_id": message_id,
        "time": timestamp,
        "self_id": "bot",
        "user_id": "member",
        "sender": {"nickname": "成员"},
        "raw_message": text,
    }


@pytest.mark.asyncio
async def test_invalid_model_summary_rolls_back_and_recomputes_archive_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        [
            LLMResult("The prompt could not be submitted. The prompt contains sensitive words.", []),
            LLMResult("重算后的连续摘要", []),
        ]
    )
    calls: list[dict[str, Any]] = []

    class Client:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_turn(self, **kwargs: Any) -> LLMResult:
            calls.append(kwargs)
            return next(responses)

    monkeypatch.setattr("app.service.ChatCompletionsClient", Client)
    service = AgentService(tmp_path, secret_store=Secrets())
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    service.db.save_summary("100", "上一次可用摘要", 0, turn_id=1)

    # The old event is outside the newest raw window, while the current event
    # remains a live decision.  The failed turn must consume the live event
    # (no action replay) and recompute the old interval as archive-only.
    old_id = service.db.insert_event(
        normalise_onebot_event(_message("old", "旧消息" * 20_000, 1))
    )
    current_id = service.db.insert_event(
        normalise_onebot_event(_message("current", "当前消息", 2))
    )
    assert old_id and current_id
    service.db.mark_events_processed([old_id])

    await service._run_group_worker("100")

    assert len(calls) == 2
    assert service.db.get_summary("100") == "重算后的连续摘要"
    assert service.db.pending_events("100") == []
    unarchived = service.db.unarchived_events("100")
    assert [item["message_id"] for item in unarchived] == ["current"]
    turn_errors = [turn for turn in service.db.list_group_turns("100") if turn["status"] == "failed"]
    assert turn_errors
    assert "自动回退到上一次可用记忆" in turn_errors[0]["error"]
    await service.stop()


def test_summary_snapshots_are_persisted_and_restore_the_live_pointer(tmp_path: Path) -> None:
    service = AgentService(tmp_path, secret_store=Secrets())
    service.db.save_summary("100", "第一份摘要", 10, turn_id=1)
    service.db.save_summary("100", "第二份摘要", 20, turn_id=2)
    snapshots = service.db.list_summary_snapshots("100")
    assert [item["content"] for item in snapshots[:2]] == ["第二份摘要", "第一份摘要"]

    service.db.restore_summary_snapshot("100", "第一份摘要", 10, reason="测试回退")
    record = service.db.get_summary_record("100")
    assert record["content"] == "第一份摘要"
    assert record["last_event_id"] == 10
    service.db.close()


@pytest.mark.asyncio
async def test_google_safety_refusal_retries_without_raw_content_or_qq_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        [
            LLMResult(
                "The prompt could not be submitted. The prompt contains sensitive words.",
                [],
            ),
            LLMResult("仅根据事件元数据生成的安全降级摘要", []),
        ]
    )
    calls: list[dict[str, Any]] = []

    class Client:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_turn(self, **kwargs: Any) -> LLMResult:
            calls.append(kwargs)
            return next(responses)

    monkeypatch.setattr("app.service.ChatCompletionsClient", Client)
    service = AgentService(tmp_path, secret_store=Secrets())
    service.db.upsert_group("100", "安全降级群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)

    old = normalise_onebot_event(_message("old", "敏感原文" * 20_000, 1))
    old["pending"] = False
    old_id = service.db.insert_event(old)
    current_id = service.db.insert_event(normalise_onebot_event(_message("current", "当前消息", 2)))
    assert old_id and current_id
    service.db.mark_events_processed([old_id])
    service.db.mark_events_memory_processed([old_id, current_id])

    await service._run_group_worker("100")

    assert len(calls) == 2
    assert "敏感原文" in calls[0]["event_text"]
    fallback = calls[1]
    assert "敏感原文" not in fallback["event_text"]
    assert "敏感原文" not in fallback["recent_context_text"]
    assert "敏感原文" not in fallback["current_event_text"]
    assert fallback["image_parts"] == []
    assert fallback["allow_group_actions"] is False
    assert fallback["group_prompt"] == ""
    assert fallback["persistent_rules"] == ""
    assert service.db.get_summary("100") == "仅根据事件元数据生成的安全降级摘要"
    assert "安全降级" in service.db.get_group("100")["last_error"]
    assert service.db.get_group("100")["last_error"].count("上游模型拒绝接收本群原文") == 1
    await service.stop()
