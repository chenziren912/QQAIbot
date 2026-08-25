"""Regression coverage for the no-tools busy-reply mini agent."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest

from app.config import LLMSettings
from app.llm import ChatCompletionsClient, LLMResult
from app.onebot import OneBotActionTimeoutError
from app.service import (
    AgentService,
    BUSY_REPLY_OPERATION_NAMESPACE,
    _event_explicitly_requires_busy_agent_reply,
    normalise_onebot_event,
)


class Secrets:
    def get_llm_api_key(self) -> str:
        return "test-key"


class Adapter:
    connected = True

    def __init__(self, *, timeout_send: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.timeout_send = timeout_send

    async def call(self, action: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append((action, copy.deepcopy(params)))
        if self.timeout_send and action in {"send_group_msg", "send_private_msg"}:
            raise OneBotActionTimeoutError("NapCat did not confirm the send")
        return {"data": {"message_id": "sent-%s" % len(self.calls)}}

    async def disconnect(self, **_: Any) -> None:
        self.connected = False


def _message(message_id: str, text: str, timestamp: int = 1) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": "100",
        "message_id": message_id,
        "time": timestamp,
        "self_id": "bot",
        "user_id": "member",
        "sender": {"nickname": "成员"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


async def _wait_for(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for background busy reply")


class ScriptedBusyClient(ChatCompletionsClient):
    def __init__(self, settings: LLMSettings, script: list[dict[str, Any]]) -> None:
        super().__init__(settings, "test-key")
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(body))
        return self.script.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint_mode", "payload"),
    [
        ("completions", {"choices": [{"message": {"content": "我正在处理上一条消息，这条已收到。"}}]}),
        (
            "responses",
            {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "我正在处理上一条消息，这条已收到。"}],
                    }
                ]
            },
        ),
    ],
)
async def test_busy_reply_client_uses_no_tools_for_both_endpoint_modes(
    endpoint_mode: str,
    payload: dict[str, Any],
) -> None:
    client = ScriptedBusyClient(
        LLMSettings(base_url="https://llm.example/v1", model="test", endpoint_mode=endpoint_mode),
        [payload],
    )

    text = await client.run_busy_reply(
        worker_snapshot={"main_phase": "正在搜索资料", "active_tool": "Builtin_Websearch"},
        incoming_event_text="新问题",
    )

    assert text == "我正在处理上一条消息，这条已收到。"
    request = client.requests[0]
    assert request["tools"] == []
    assert request["tool_choice"] == "none"
    assert "Builtin_Websearch" in str(request)
    assert "新问题" in str(request)
    if endpoint_mode == "responses":
        assert "messages" not in request
        assert "主 Agent 的轻量辅助回复" in request["instructions"]
    else:
        assert request["messages"][0]["role"] == "developer"
        assert "主 Agent 的轻量辅助回复" in request["messages"][0]["content"]


@pytest.mark.asyncio
async def test_new_message_while_primary_worker_active_stays_queued_without_auxiliary_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots: list[dict[str, Any]] = []

    class BusyClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_busy_reply(self, **kwargs: Any) -> str:
            snapshots.append(copy.deepcopy(kwargs))
            return "我正在查资料，这条消息已收到，请稍等。"

    monkeypatch.setattr("app.service.ChatCompletionsClient", BusyClient)
    service = AgentService(tmp_path, secret_store=Secrets())
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    primary_gate = asyncio.Event()
    primary = asyncio.create_task(primary_gate.wait())
    service._workers["100"] = primary  # type: ignore[assignment]
    service._set_worker_activity(
        "100",
        "正在搜索资料",
        turn_id=73,
        event_ids=[70, 71],
        active_tool="Builtin_Websearch",
        turn_context="当前主任务正在核对前一条提问。",
        previous_summary="较早上下文摘要。",
        recent_context="最近会话原文。",
    )
    try:
        await service.handle_onebot_event(_message("new-1", "AI，看看这个呢？", 2))
        await asyncio.sleep(0.08)

        # A queued direct request must not cause a second mini-Agent status
        # message.  It remains pending for the actual serial primary worker.
        assert snapshots == []
        assert adapter.calls == []
        assert service.db.get_summary("100") == ""

        # The same incoming event is handled only by the primary Agent.
        incoming = next(item for item in service.db.pending_events("100") if item["message_id"] == "new-1")
        main_turn = service.db.create_turn("100", [incoming["id"]])
        main_result = await service._execute_tool(
            main_turn,
            "100",
            "send_group_message",
            {"text": "主 Agent 的正式回复"},
            "main-send",
        )
        assert main_result["ok"] is True
        assert len(adapter.calls) == 1
    finally:
        primary_gate.set()
        await primary
        await service.stop()


@pytest.mark.asyncio
async def test_busy_agent_is_not_scheduled_even_for_direct_or_explicit_ai_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary conversation must not receive an unsolicited busy message."""

    calls_to_mini_agent: list[dict[str, Any]] = []

    class BusyClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_busy_reply(self, **kwargs: Any) -> str:
            calls_to_mini_agent.append(copy.deepcopy(kwargs))
            return "我正在处理上一条消息，这条已收到，请稍等。"

    monkeypatch.setattr("app.service.ChatCompletionsClient", BusyClient)
    service = AgentService(tmp_path, secret_store=Secrets())
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    primary_gate = asyncio.Event()
    primary = asyncio.create_task(primary_gate.wait())
    service._workers["100"] = primary  # type: ignore[assignment]
    service._set_worker_activity("100", "主 Agent 正在分析上一条消息", turn_id=9, event_ids=[8])
    try:
        # These are the kinds of messages shown in the report: ordinary chat,
        # a generic group call, and discussion *about* AI.  None is directed
        # to this bot, so the mini-agent must remain silent.
        for message_id, text in (
            ("ordinary", "那这个呢？"),
            ("generic", "有人吗？"),
            ("about-ai", "这个 AI 看起来挺厉害"),
        ):
            await service.handle_onebot_event(_message(message_id, text, 2))
        await asyncio.sleep(0.08)
        assert calls_to_mini_agent == []
        assert adapter.calls == []

        # A QQ @ to the actual bot also waits for the primary Agent.  It must
        # not receive a boilerplate status acknowledgement first.
        direct = _message("direct", "帮我看这个", 3)
        direct["message"] = [
            {"type": "at", "data": {"qq": "bot"}},
            {"type": "text", "data": {"text": "帮我看这个"}},
        ]
        await service.handle_onebot_event(direct)
        await asyncio.sleep(0.08)
        assert calls_to_mini_agent == []
        assert adapter.calls == []
    finally:
        primary_gate.set()
        await primary
        await service.stop()


def test_busy_reply_predicate_requires_verified_or_explicit_targeting() -> None:
    assert not _event_explicitly_requires_busy_agent_reply(
        normalise_onebot_event(_message("ordinary", "那这个呢？"))
    )
    assert not _event_explicitly_requires_busy_agent_reply(
        normalise_onebot_event(_message("ai-topic", "这个 AI 模型很强"))
    )
    assert _event_explicitly_requires_busy_agent_reply(
        normalise_onebot_event(_message("explicit", "请 AI 回答一下这个问题"))
    )
    direct = _message("at-bot", "帮我看一下")
    direct["message"] = [
        {"type": "at", "data": {"qq": "bot"}},
        {"type": "text", "data": {"text": "帮我看一下"}},
    ]
    assert _event_explicitly_requires_busy_agent_reply(normalise_onebot_event(direct))
    reply_to_bot = normalise_onebot_event(_message("reply-bot", "继续说"))
    reply_to_bot["content"]["live_reply_to_bot"] = True
    assert _event_explicitly_requires_busy_agent_reply(reply_to_bot)


@pytest.mark.asyncio
async def test_live_primary_worker_does_not_start_a_busy_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_started = asyncio.Event()
    release_main = asyncio.Event()
    snapshots: list[dict[str, Any]] = []
    main_calls = 0

    class ConcurrentClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_turn(self, **_: Any) -> LLMResult:
            nonlocal main_calls
            main_calls += 1
            if main_calls == 1:
                main_started.set()
                await release_main.wait()
            return LLMResult("内部摘要", [])

        async def run_busy_reply(self, **kwargs: Any) -> str:
            snapshots.append(copy.deepcopy(kwargs))
            return "我正在处理上一条消息，这条也收到啦，请稍等。"

    monkeypatch.setattr("app.service.ChatCompletionsClient", ConcurrentClient)
    service = AgentService(tmp_path, secret_store=Secrets())
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    try:
        await service.handle_onebot_event(_message("first", "先处理这个问题", 1))
        await asyncio.wait_for(main_started.wait(), timeout=1)
        primary = service._workers["100"]
        await service.handle_onebot_event(_message("second", "AI，这条也看一下", 2))
        await asyncio.sleep(0.08)

        assert snapshots == []
        assert adapter.calls == []
        # The primary turn is still waiting and remains the only Agent that
        # will handle the second message.
        assert not primary.done()

        release_main.set()
        await primary
        assert main_calls >= 2
    finally:
        release_main.set()
        await service.stop()


def test_busy_reply_text_that_looks_like_an_answer_is_replaced_by_fallback() -> None:
    assert AgentService._normalise_busy_reply_text("答案是 42，我正在处理。") == ""
    assert AgentService._normalise_busy_reply_text("```python\nprint(1)\n```") == ""
    assert AgentService._normalise_busy_reply_text("我正在处理上一条消息，这条已收到。")


@pytest.mark.asyncio
async def test_busy_reply_is_never_scheduled_while_primary_worker_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BusyClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_busy_reply(self, **_: Any) -> str:
            return "我正在处理上一条消息，这条也已收到，请稍等。"

    monkeypatch.setattr("app.service.ChatCompletionsClient", BusyClient)
    service = AgentService(tmp_path, secret_store=Secrets())
    adapter = Adapter(timeout_send=True)
    service.adapter = adapter  # type: ignore[assignment]
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    primary_gate = asyncio.Event()
    primary = asyncio.create_task(primary_gate.wait())
    service._workers["100"] = primary  # type: ignore[assignment]
    service._set_worker_activity("100", "主 Agent 正在分析上一条消息", turn_id=9, event_ids=[8])
    try:
        await service.handle_onebot_event(_message("timeout-event", "AI，看看又来这条", 2))
        await asyncio.sleep(0.08)
        incoming = next(item for item in service.db.pending_events("100") if item["message_id"] == "timeout-event")

        # The auxiliary namespace is no longer used in production: direct
        # requests remain queued for the primary Agent rather than receiving
        # a boilerplate acknowledgement that might look like a reply.
        assert bool(incoming["pending"]) is True
        assert adapter.calls == []
        assert service._busy_reply_event_ids == set()
        operations = service.db._connection.execute(  # type: ignore[attr-defined]
            "SELECT operation_key, tool_name FROM tool_operations"
        ).fetchall()
        assert operations == []
    finally:
        primary_gate.set()
        await primary
        await service.stop()
