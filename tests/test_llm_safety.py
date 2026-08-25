"""Safety and retry regression tests for the Chat Completions orchestration."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.config import LLMSettings
from app.llm import ChatCompletionsClient, LLMError


class ScriptedClient(ChatCompletionsClient):
    def __init__(self, script: list[Any]) -> None:
        super().__init__(
            LLMSettings(base_url="https://llm.example/v1", model="test", global_prompt="custom global rule"),
            api_key="test-key",
        )
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(body))
        next_item = self.script.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


def _completion(message: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": message}]}


@pytest.mark.asyncio
async def test_fixed_tool_boundary_is_retained_after_editable_prompts() -> None:
    client = ScriptedClient([_completion({"content": "摘要"})])

    async def unused_tool(*_: Any) -> dict[str, Any]:
        raise AssertionError("tool should not execute")

    result = await client.run_turn("", "群消息", "group rule", "off", [], unused_tool)

    developer = client.requests[0]["messages"][0]["content"]
    assert result.summary == "摘要"
    assert "custom global rule" in developer
    assert "group rule" in developer
    assert developer.rfind("不可变服务边界") > developer.find("group rule")


@pytest.mark.asyncio
async def test_final_summary_failure_does_not_repeat_executed_tool() -> None:
    first = _completion(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call-once",
                    "type": "function",
                    "function": {"name": "send_group_message", "arguments": '{"text":"once"}'},
                }
            ],
        }
    )
    client = ScriptedClient([first, LLMError("final completion unavailable")])
    executions: list[str] = []

    async def execute(name: str, _: dict[str, Any], __: str) -> dict[str, Any]:
        executions.append(name)
        return {"ok": True, "message_id": "1"}

    result = await client.run_turn("old", "event", "", "off", [], execute)

    assert executions == ["send_group_message"]
    assert "安全回退摘要" in result.warning
    assert "本轮工具已执行" in result.summary
