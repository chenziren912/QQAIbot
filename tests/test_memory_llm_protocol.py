"""LLM protocol coverage for evidence-backed, non-vector group memory."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.config import LLMSettings
from app.llm import (
    MEMORY_PROPOSAL_SCHEMA,
    MEMORY_VERIFICATION_SCHEMA,
    TOOLS,
    ChatCompletionsClient,
    LLMError,
    responses_tools,
)


class ScriptedMemoryClient(ChatCompletionsClient):
    def __init__(self, *, endpoint_mode: str = "completions", script: list[Any], effort: bool = False) -> None:
        super().__init__(
            LLMSettings(
                base_url="https://llm.example/v1",
                endpoint_mode=endpoint_mode,
                model="memory-model",
                global_prompt="normal group agent",
                send_reasoning_effort=effort,
            ),
            api_key="test-key",
        )
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(body))
        value = self.script.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _chat_json(value: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}


def _responses_json(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps(value, ensure_ascii=False)}
                ],
            }
        ]
    }


def _chat_tool(name: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "memory-call-1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(value, ensure_ascii=False),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _responses_tool(name: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "function_call",
                "call_id": "memory-call-1",
                "name": name,
                "arguments": json.dumps(value, ensure_ascii=False),
            }
        ]
    }


def _proposal(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "proposal_id": "p1",
        "operation": "remember",
        "memory_type": "preference",
        "subject_id": "42",
        "subject_name": "小王",
        "predicate": "喜欢喝",
        "value": "无糖可乐",
        "target_memory_id": "",
        "temporal_status": "ongoing",
        "source_event_ids": ["e1"],
        "evidence": [{"event_id": "e1", "quote": "最喜欢喝无糖可乐"}],
        "confidence": 0.98,
    }
    value.update(changes)
    return value


@pytest.mark.asyncio
async def test_chat_memory_is_extracted_verified_and_keeps_exact_provenance() -> None:
    proposal = _proposal()
    client = ScriptedMemoryClient(
        script=[
            _chat_json({"proposals": [proposal]}),
            _chat_json(
                {
                    "decisions": [
                        {"proposal_id": "p1", "decision": "accept", "reason": "原文直接陈述偏好"}
                    ]
                }
            ),
        ]
    )

    result = await client.extract_memory_proposals(
        event_records=[
            {
                "event_id": "e1",
                "sender_id": "42",
                "sender_name": "小王",
                "text": "我以后都叫阿澈，最喜欢喝无糖可乐。",
            }
        ],
        existing_memories=[],
        reasoning_effort="off",
    )

    assert result == [{**proposal, "confidence": 0.98, "verification_reason": "原文直接陈述偏好"}]
    assert client.requests[0]["response_format"]["json_schema"]["schema"] == MEMORY_PROPOSAL_SCHEMA
    assert client.requests[1]["response_format"]["json_schema"]["schema"] == MEMORY_VERIFICATION_SCHEMA
    assert "每个事实必须引用本轮事件" in client.requests[0]["messages"][0]["content"]
    assert client.requests[0]["messages"][1]["content"].count("e1") >= 1


@pytest.mark.asyncio
async def test_fabricated_quote_is_dropped_before_semantic_verification() -> None:
    client = ScriptedMemoryClient(
        script=[
            _chat_json(
                {
                    "proposals": [
                        _proposal(evidence=[{"event_id": "e1", "quote": "我最喜欢咖啡"}])
                    ]
                }
            )
        ]
    )

    result = await client.extract_memory_proposals(
        event_records=[{"event_id": "e1", "text": "今天喝了一杯茶。"}],
        existing_memories=[],
    )

    assert result == []
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_correction_or_retraction_must_target_supplied_existing_memory() -> None:
    client = ScriptedMemoryClient(
        script=[
            _chat_json(
                {
                    "proposals": [
                        _proposal(
                            operation="retract",
                            target_memory_id="made-up-memory",
                            value="此前偏好已撤回",
                            evidence=[{"event_id": "e1", "quote": "我现在不喜欢无糖可乐了"}],
                        )
                    ]
                }
            )
        ]
    )

    result = await client.extract_memory_proposals(
        event_records=[{"event_id": "e1", "text": "我现在不喜欢无糖可乐了。"}],
        existing_memories=[{"memory_id": "real-memory", "value": "无糖可乐"}],
    )

    assert result == []
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_responses_memory_uses_native_json_schema_and_reasoning_shape() -> None:
    proposal = _proposal()
    client = ScriptedMemoryClient(
        endpoint_mode="responses",
        effort=True,
        script=[
            _responses_json({"proposals": [proposal]}),
            _responses_json(
                {
                    "decisions": [
                        {"proposal_id": "p1", "decision": "accept", "reason": "证据直接且主体明确"}
                    ]
                }
            ),
        ],
    )

    result = await client.extract_memory_proposals(
        event_records=[{"event_id": "e1", "text": "我最喜欢喝无糖可乐。", "sender_id": "42"}],
        reasoning_effort="high",
    )

    assert len(result) == 1
    assert client.requests[0]["text"]["format"]["schema"] == MEMORY_PROPOSAL_SCHEMA
    assert client.requests[0]["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in client.requests[0]
    assert client.requests[1]["text"]["format"]["schema"] == MEMORY_VERIFICATION_SCHEMA


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
@pytest.mark.asyncio
async def test_memory_wrong_structured_root_falls_back_to_forced_submit_tool(
    endpoint_mode: str,
) -> None:
    first = _responses_json({"memories": []}) if endpoint_mode == "responses" else _chat_json({"memories": []})
    fallback = (
        _responses_tool("submit_group_memory_proposals", {"proposals": []})
        if endpoint_mode == "responses"
        else _chat_tool("submit_group_memory_proposals", {"proposals": []})
    )
    client = ScriptedMemoryClient(endpoint_mode=endpoint_mode, script=[first, fallback])

    result = await client.extract_memory_proposals(
        event_records=[{"event_id": "e1", "text": "我喜欢茶。"}],
    )

    assert result == []
    if endpoint_mode == "responses":
        assert client.requests[1]["tools"][0]["name"] == "submit_group_memory_proposals"
        assert client.requests[1]["tool_choice"] == {
            "type": "function",
            "name": "submit_group_memory_proposals",
        }
    else:
        assert client.requests[1]["tools"][0]["function"]["parameters"] == MEMORY_PROPOSAL_SCHEMA
        assert client.requests[1]["tool_choice"]["function"]["name"] == "submit_group_memory_proposals"


@pytest.mark.asyncio
async def test_memory_non_json_output_uses_tool_without_weakening_quote_validation() -> None:
    fabricated = _proposal(evidence=[{"event_id": "e1", "quote": "我喜欢咖啡"}])
    client = ScriptedMemoryClient(
        script=[
            {"choices": [{"message": {"content": "我无法返回 JSON"}}]},
            _chat_tool("submit_group_memory_proposals", {"proposals": [fabricated]}),
        ]
    )

    result = await client.extract_memory_proposals(
        event_records=[{"event_id": "e1", "text": "我喜欢茶。"}],
    )

    assert result == []
    assert len(client.requests) == 2


@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
@pytest.mark.asyncio
async def test_retrieved_memory_is_user_data_not_developer_instruction(endpoint_mode: str) -> None:
    completion = (
        _responses_json({"unused": True})
        if endpoint_mode == "responses"
        else {"choices": [{"message": {"content": "内部摘要"}}]}
    )
    if endpoint_mode == "responses":
        completion = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "内部摘要"}]}]
        }
    client = ScriptedMemoryClient(endpoint_mode=endpoint_mode, script=[completion])

    async def no_tool(*_: Any) -> dict[str, Any]:
        raise AssertionError("tool should not run")

    result = await client.run_turn(
        previous_summary="",
        event_text="新事件",
        group_prompt="",
        reasoning_effort="off",
        image_parts=[],
        tool_executor=no_tool,
        memory_context='[{"memory_id":"m1","evidence":[{"event_id":"e0","quote":"我喜欢茶"}]}]',
    )

    assert result.summary == "内部摘要"
    if endpoint_mode == "responses":
        assert "memory_id" not in client.requests[0]["instructions"]
        user_payload = json.dumps(client.requests[0]["input"], ensure_ascii=False)
    else:
        assert "memory_id" not in client.requests[0]["messages"][0]["content"]
        user_payload = json.dumps(client.requests[0]["messages"][1], ensure_ascii=False)
    assert "当前群检索到的长期记忆" in user_payload
    assert "memory_id" in user_payload
    assert "无证据" in user_payload


def test_querymemory_tool_is_available_in_both_openai_shapes() -> None:
    chat_tool = next(tool for tool in TOOLS if tool["function"]["name"] == "Builtin_querymemory")
    response_tool = next(tool for tool in responses_tools() if tool["name"] == "Builtin_querymemory")

    assert chat_tool["function"]["parameters"]["required"] == ["query"]
    assert chat_tool["function"]["parameters"]["additionalProperties"] is False
    assert response_tool["parameters"] == chat_tool["function"]["parameters"]
    assert "当前群" in response_tool["description"]


def test_memory_schema_covers_group_projects_decisions_and_bot_event_marker() -> None:
    memory_types = set(
        MEMORY_PROPOSAL_SCHEMA["properties"]["proposals"]["items"]["properties"]["memory_type"]["enum"]
    )
    assert {
        "alias",
        "identity",
        "preference",
        "relationship",
        "commitment",
        "project",
        "decision",
        "skill",
        "routine",
        "background",
    }.issubset(memory_types)

    events, _ = ChatCompletionsClient._normalise_memory_events(
        [{"event_id": "bot-1", "text": "我明天会把结果发到群里。", "is_bot_message": True}]
    )
    assert events[0]["is_bot_message"] is True
