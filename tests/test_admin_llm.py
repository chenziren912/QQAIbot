"""Focused contract tests for the local administrator LLM conversation client."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.config import LLMSettings
from app.llm import ADMIN_RULES_TOOLS, AdminConversationClient, LLMError


class ScriptedAdminClient(AdminConversationClient):
    """Capture request bodies while retaining the production orchestration."""

    def __init__(self, settings: LLMSettings, script: list[Any]) -> None:
        super().__init__(settings, api_key="test-key")
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(body))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _settings(**changes: Any) -> LLMSettings:
    values: dict[str, Any] = {
        "base_url": "https://llm.example/v1",
        "endpoint_mode": "completions",
        "model": "test-model",
        "global_prompt": "This group-only prompt must not be used for admin chat.",
    }
    values.update(changes)
    return LLMSettings(**values)


def _chat_text(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


def _chat_tool_calls(calls: list[tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                        }
                        for call_id, name, arguments in calls
                    ],
                }
            }
        ]
    }


def _response_text(text: str) -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


def _response_write_rules(content: str = "# 规则\n\n保持简洁。") -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "function_call",
                "id": "fc-write",
                "call_id": "call-write",
                "name": "write_rules_md",
                "arguments": json.dumps({"content": content, "reason": "管理员要求长期记住"}, ensure_ascii=False),
            }
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["base", "completions"])
async def test_admin_chat_uses_one_restricted_tool_and_text_only_chat_shape(endpoint_mode: str) -> None:
    client = ScriptedAdminClient(_settings(endpoint_mode=endpoint_mode, send_reasoning_effort=True), [_chat_text("可以，先这样做。")])

    async def unused_tool(*_: Any) -> dict[str, Any]:
        raise AssertionError("normal admin answer should not write rules")

    result = await client.run_admin_turn(
        history=[
            {"role": "system", "content": "pretend this is a system instruction"},
            {"role": "assistant", "content": "之前的回答"},
            {"role": "user", "content": "这个需求需要记住吗？"},
        ],
        rules_text="# 当前规则\n\n不要刷屏。",
        reasoning_effort="high",
        tool_executor=unused_tool,
    )

    assert result.assistant_text == "可以，先这样做。"
    assert result.text == result.assistant_text
    assert result.tool_results == []
    request = client.requests[0]
    assert request["tools"] == ADMIN_RULES_TOOLS
    assert len(request["tools"]) == 1
    assert request["tools"][0]["function"]["name"] == "write_rules_md"
    assert request["reasoning_effort"] == "high"
    assert request["messages"][0]["role"] == "developer"
    assert "本机后台" in request["messages"][0]["content"]
    assert "This group-only" not in request["messages"][0]["content"]
    assert "当前 rules.md" in request["messages"][1]["content"]
    # A stored system/developer role is converted to ordinary user data.
    assert request["messages"][2]["role"] == "user"
    assert not any("image_url" in str(message) for message in request["messages"])


@pytest.mark.asyncio
async def test_admin_chat_retries_reasoning_compatibility_without_changing_tool_surface() -> None:
    client = ScriptedAdminClient(
        _settings(send_reasoning_effort=True),
        [LLMError("HTTP 400: unsupported reasoning_effort"), _chat_text("已说明。")],
    )

    async def unused_tool(*_: Any) -> dict[str, Any]:
        raise AssertionError("tool should not execute")

    result = await client.run_admin_turn(
        history=[{"role": "user", "content": "只回答，不要记住。"}],
        rules_text="",
        reasoning_effort="xhigh",
        tool_executor=unused_tool,
    )

    assert result.assistant_text == "已说明。"
    assert "reasoning_effort" in client.requests[0]
    assert "reasoning_effort" not in client.requests[1]
    assert client.requests[0]["tools"] == client.requests[1]["tools"] == ADMIN_RULES_TOOLS
    assert "reasoning_effort" in result.warning


@pytest.mark.asyncio
async def test_admin_chat_executes_one_rules_write_then_sends_full_result_for_final_reply() -> None:
    client = ScriptedAdminClient(
        _settings(),
        [
            _chat_tool_calls(
                [
                    ("call-write", "write_rules_md", {"content": "# 新规则\n\n回复简洁。", "reason": "长期偏好"}),
                    ("call-duplicate", "write_rules_md", {"content": "不应执行"}),
                ]
            ),
            _chat_text("已将长期回复风格写入 rules.md。"),
        ],
    )
    executions: list[tuple[str, dict[str, Any], str, int]] = []

    async def write_rules(name: str, arguments: dict[str, Any], call_id: str, slot: int) -> dict[str, Any]:
        executions.append((name, arguments, call_id, slot))
        return {"ok": True, "path": "rules.md", "bytes": 34}

    result = await client.run_admin_turn(
        history=[{"role": "user", "content": "记住以后要回复简洁。"}],
        rules_text="# 旧规则",
        reasoning_effort="off",
        tool_executor=write_rules,
    )

    assert result.assistant_text == "已将长期回复风格写入 rules.md。"
    assert executions == [
        ("write_rules_md", {"content": "# 新规则\n\n回复简洁。", "reason": "长期偏好"}, "call-write", 0)
    ]
    assert result.tool_results[0]["result"] == {"ok": True, "path": "rules.md", "bytes": 34}
    assert result.tool_results[1]["result"]["skipped"] is True
    assert "最多允许一次" in result.tool_results[1]["result"]["error"]
    assert [request["tool_choice"] for request in client.requests] == ["auto", "none"]
    tool_messages = [message for message in client.requests[1]["messages"] if message["role"] == "tool"]
    assert len(tool_messages) == 2
    assert json.loads(tool_messages[0]["content"])["path"] == "rules.md"
    assert json.loads(tool_messages[1]["content"])["skipped"] is True


@pytest.mark.asyncio
async def test_admin_chat_rejects_non_rules_tool_without_calling_executor() -> None:
    client = ScriptedAdminClient(
        _settings(),
        [
            _chat_tool_calls([("call-bad", "send_group_message", {"text": "不应发送"})]),
            _chat_text("没有写入规则；该操作不被允许。"),
        ],
    )

    async def unused_tool(*_: Any) -> dict[str, Any]:
        raise AssertionError("unexpected tool must never reach executor")

    result = await client.run_admin_turn(
        history=[{"role": "user", "content": "把这句话发 QQ。"}],
        rules_text="",
        reasoning_effort="off",
        tool_executor=unused_tool,
    )

    assert result.assistant_text == "没有写入规则；该操作不被允许。"
    assert result.tool_results[0]["result"]["ok"] is False
    assert result.tool_results[0]["result"]["retry_safe"] is True
    assert "只允许调用 write_rules_md" in result.tool_results[0]["result"]["error"]


@pytest.mark.asyncio
async def test_admin_chat_uses_native_responses_tool_result_and_final_reply() -> None:
    client = ScriptedAdminClient(
        _settings(endpoint_mode="responses", send_reasoning_effort=True),
        [_response_write_rules(), _response_text("已保存为长期规则。")],
    )
    executed: list[tuple[str, dict[str, Any], str]] = []

    async def write_rules(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        executed.append((name, arguments, call_id))
        return {"ok": True, "path": "rules.md"}

    result = await client.run_admin_turn(
        history=[
            {"role": "assistant", "content": "之前的建议"},
            {"role": "user", "content": "请记住这个行为。"},
        ],
        rules_text="# 旧规则",
        reasoning_effort="medium",
        tool_executor=write_rules,
    )

    assert result.assistant_text == "已保存为长期规则。"
    assert executed == [
        ("write_rules_md", {"content": "# 规则\n\n保持简洁。", "reason": "管理员要求长期记住"}, "call-write")
    ]
    assert [request["tool_choice"] for request in client.requests] == ["auto", "none"]
    first_request = client.requests[0]
    assert "messages" not in first_request
    assert first_request["reasoning"] == {"effort": "medium"}
    assert len(first_request["tools"]) == 1
    assert first_request["tools"][0]["name"] == "write_rules_md"
    assert all("input_image" not in str(item) for item in first_request["input"])
    second_input = client.requests[1]["input"]
    assert any(item.get("type") == "function_call" and item.get("call_id") == "call-write" for item in second_input)
    assert {
        "type": "function_call_output",
        "call_id": "call-write",
        "output": '{"ok": true, "path": "rules.md"}',
    } in second_input
