"""Regression coverage for bounded sequential multi-tool QQ decisions."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.config import LLMSettings
from app.llm import ChatCompletionsClient, MAX_TOOL_CALLS_PER_DECISION


class ScriptedClient(ChatCompletionsClient):
    def __init__(self, mode: str, script: list[dict[str, Any]]) -> None:
        super().__init__(
            LLMSettings(
                base_url="https://llm.example/v1",
                endpoint_mode=mode,
                model="test-model",
                global_prompt="test prompt",
            ),
            api_key="test-key",
        )
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(body))
        return self.script.pop(0)


def _chat_tools(calls: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "send_group_message",
                                "arguments": json.dumps({"text": text}, ensure_ascii=False),
                            },
                        }
                        for call_id, text in calls
                    ],
                }
            }
        ]
    }


def _chat_text(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


def _responses_tools(calls: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": "send_group_message",
                "arguments": json.dumps({"text": text}, ensure_ascii=False),
            }
            for call_id, text in calls
        ]
    }


def _responses_text(text: str) -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


def _tool_decision(mode: str, calls: list[tuple[str, str]]) -> dict[str, Any]:
    return _responses_tools(calls) if mode == "responses" else _chat_tools(calls)


def _text_decision(mode: str, text: str) -> dict[str, Any]:
    return _responses_text(text) if mode == "responses" else _chat_text(text)


def _tool_outputs(request: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    if mode == "responses":
        return [
            json.loads(item["output"])
            for item in request["input"]
            if item.get("type") == "function_call_output"
        ]
    return [
        json.loads(item["content"])
        for item in request["messages"]
        if item.get("role") == "tool"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completions", "responses"])
async def test_multiple_successful_tools_execute_in_order_and_all_results_reach_model(mode: str) -> None:
    client = ScriptedClient(
        mode,
        [
            _tool_decision(mode, [("one", "第一条\n保留换行"), ("two", "第二条")]),
            _text_decision(mode, "最终内部摘要"),
        ],
    )
    executions: list[tuple[str, str, int]] = []

    async def execute(name: str, arguments: dict[str, Any], call_id: str, slot: int) -> dict[str, Any]:
        executions.append((call_id, arguments["text"], slot))
        return {"ok": True, "message_id": "m-%s" % slot}

    result = await client.run_turn("旧摘要", "群消息", "", "off", [], execute)

    assert result.summary == "最终内部摘要"
    assert executions == [("one", "第一条\n保留换行", 0), ("two", "第二条", 1)]
    assert [item["result"] for item in result.tool_results] == [
        {"ok": True, "message_id": "m-0"},
        {"ok": True, "message_id": "m-1"},
    ]
    assert _tool_outputs(client.requests[1], mode) == [
        {"ok": True, "message_id": "m-0"},
        {"ok": True, "message_id": "m-1"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completions", "responses"])
async def test_tool_failure_stops_later_actions_but_returns_full_results_to_model(mode: str) -> None:
    client = ScriptedClient(
        mode,
        [
            _tool_decision(mode, [("first", "第一条"), ("bad", "第二条"), ("later", "第三条")]),
            _text_decision(mode, "失败后的内部摘要"),
        ],
    )
    executions: list[tuple[str, int]] = []

    async def execute(_: str, __: dict[str, Any], call_id: str, slot: int) -> dict[str, Any]:
        executions.append((call_id, slot))
        if call_id == "bad":
            return {"ok": False, "error": "NapCat 503：上游拒绝发送；request-id=local-77"}
        return {"ok": True, "message_id": "m-%s" % slot}

    result = await client.run_turn("旧摘要", "群消息", "", "off", [], execute)

    assert result.summary == "失败后的内部摘要"
    assert executions == [("first", 0), ("bad", 1)]
    assert result.tool_results[1]["result"]["error"].endswith("request-id=local-77")
    assert result.tool_results[2]["result"]["skipped"] is True
    assert "前一工具调用失败" in result.tool_results[2]["result"]["error"]
    outputs = _tool_outputs(client.requests[1], mode)
    assert outputs[1]["error"].endswith("request-id=local-77")
    assert outputs[2]["skipped"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completions", "responses"])
async def test_safe_repair_reuses_failed_slot_and_can_make_multiple_calls(mode: str) -> None:
    client = ScriptedClient(
        mode,
        [
            _tool_decision(mode, [("ok", "已发送"), ("invalid", "坏参数"), ("skipped", "不应执行")]),
            _tool_decision(mode, [("repair", "修正"), ("repair-next", "修正后第二条")]),
            _text_decision(mode, "修正后的内部摘要"),
        ],
    )
    executions: list[tuple[str, int]] = []

    async def execute(_: str, __: dict[str, Any], call_id: str, slot: int) -> dict[str, Any]:
        executions.append((call_id, slot))
        if call_id == "invalid":
            return {
                "ok": False,
                "error": "text 不能为空",
                "retry_safe": True,
                "retry_safe_reason": "本地校验拒绝，未发送 QQ。",
            }
        return {"ok": True, "message_id": "m-%s" % slot}

    result = await client.run_turn("旧摘要", "群消息", "", "off", [], execute)

    assert result.summary == "修正后的内部摘要"
    # Slot 1 was never reserved by the local validation failure, so the repair
    # correctly owns slot 1; slot 2 follows it while the old skipped call stays
    # unexecuted.
    assert executions == [("ok", 0), ("invalid", 1), ("repair", 1), ("repair-next", 2)]
    assert result.tool_results[2]["result"]["skipped"] is True
    assert [request["tool_choice"] for request in client.requests] == ["auto", "auto", "none"]


@pytest.mark.asyncio
async def test_tool_decision_has_explicit_hard_cap() -> None:
    calls = [("c-%s" % index, "消息%s" % index) for index in range(MAX_TOOL_CALLS_PER_DECISION + 2)]
    client = ScriptedClient("completions", [_chat_tools(calls), _chat_text("最终摘要")])
    executions: list[int] = []

    async def execute(_: str, __: dict[str, Any], ___: str, slot: int) -> dict[str, Any]:
        executions.append(slot)
        return {"ok": True, "message_id": str(slot)}

    result = await client.run_turn("", "群消息", "", "off", [], execute)

    assert executions == list(range(MAX_TOOL_CALLS_PER_DECISION))
    assert len(result.tool_results) == len(calls)
    assert result.tool_results[-1]["result"]["skipped"] is True
    assert "最多允许执行" in result.tool_results[-1]["result"]["error"]
