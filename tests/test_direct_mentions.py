"""Regression tests for replies to verified, live @-mentions."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from app.config import LLMSettings
from app.llm import ChatCompletionsClient, LLMError, LLMResult
from app.service import AgentService, DIRECT_MENTION_CONTEXT_MARKER, normalise_onebot_event


class MemorySecrets:
    def get_llm_api_key(self) -> str:
        return "test-key"


def _mention_event(*, message_id: str = "1") -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": "100",
        "message_id": message_id,
        "time": 1,
        "self_id": "3436861606",
        "user_id": "42",
        "sender": {"nickname": "测试成员"},
        "message": [
            {"type": "at", "data": {"qq": "3436861606"}},
            {"type": "text", "data": {"text": " 你好"}},
        ],
    }


def test_only_live_structured_at_segment_sets_server_direct_mention_marker() -> None:
    live = normalise_onebot_event(_mention_event())
    assert live["content"]["live_direct_mention"] is True
    assert "@3436861606" in live["normalized_text"]

    # Text that merely looks like a mention is not an authoritative segment.
    text_only = _mention_event(message_id="text")
    text_only["message"] = "@3436861606 你好"
    assert normalise_onebot_event(text_only)["content"]["live_direct_mention"] is False

    wrong_target = _mention_event(message_id="wrong")
    wrong_target["message"][0]["data"]["qq"] = "999"
    assert normalise_onebot_event(wrong_target)["content"]["live_direct_mention"] is False


@pytest.mark.asyncio
async def test_initial_or_reconnect_history_mention_never_requests_a_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[dict[str, Any]] = []

    class CapturingClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_turn(self, **kwargs: Any) -> LLMResult:
            received.append(kwargs)
            return LLMResult("历史摘要", [])

    monkeypatch.setattr("app.service.ChatCompletionsClient", CapturingClient)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)

    initial_history = normalise_onebot_event(_mention_event(), history=True)
    reconnect_history = normalise_onebot_event(_mention_event(message_id="2"), live=False)
    assert initial_history["content"]["live_direct_mention"] is False
    assert reconnect_history["content"]["live_direct_mention"] is False
    assert service.db.insert_event(initial_history)
    assert service.db.insert_event(reconnect_history)

    await service._run_group_worker("100")

    assert len(received) == 1
    assert received[0]["direct_mention_reply_required"] is False
    assert DIRECT_MENTION_CONTEXT_MARKER not in received[0]["event_text"]
    await service.stop()


class ScriptedClient(ChatCompletionsClient):
    def __init__(self, script: list[Any]) -> None:
        super().__init__(
            LLMSettings(base_url="https://llm.example/v1", model="test", global_prompt="saved old prompt"),
            api_key="test-key",
        )
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(body))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _completion(message: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": message}]}


def _send_tool_call(call_id: str = "send-1") -> dict[str, Any]:
    return _completion(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "send_group_message", "arguments": '{"text":"收到"}'},
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_live_direct_mention_forces_send_tool_choice_and_old_prompt_cannot_disable_it() -> None:
    client = ScriptedClient([_send_tool_call(), _completion({"content": "最新摘要"})])
    calls: list[str] = []

    async def execute(name: str, _: dict[str, Any], __: str) -> dict[str, Any]:
        calls.append(name)
        return {"ok": True, "message_id": "900"}

    result = await client.run_turn(
        "旧摘要", DIRECT_MENTION_CONTEXT_MARKER + "\n[1] message / 成员: @机器人 你好", "", "off", [], execute,
        direct_mention_reply_required=True,
    )

    assert result.summary == "最新摘要"
    assert calls == ["send_group_message"]
    assert client.requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "send_group_message"},
    }
    developer = client.requests[0]["messages"][0]["content"]
    assert "服务生成的实时直接提及规则" in developer
    assert "saved old prompt" in developer


@pytest.mark.asyncio
async def test_forced_tool_choice_rejects_then_safely_falls_back_to_auto() -> None:
    client = ScriptedClient(
        [
            LLMError("HTTP 400: unsupported tool_choice function object"),
            _send_tool_call("send-fallback"),
            _completion({"content": "最新摘要"}),
        ]
    )

    async def execute(_: str, __: dict[str, Any], ___: str) -> dict[str, Any]:
        return {"ok": True, "message_id": "901"}

    result = await client.run_turn(
        "", DIRECT_MENTION_CONTEXT_MARKER, "", "off", [], execute, direct_mention_reply_required=True
    )

    assert client.requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "send_group_message"},
    }
    assert client.requests[1]["tool_choice"] == "auto"
    assert client.requests[2]["tool_choice"] == "auto"
    assert "已回退为自动工具选择" in result.warning
