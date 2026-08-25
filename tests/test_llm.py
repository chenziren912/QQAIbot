"""Focused unit tests for the OpenAI-compatible turn client."""

from __future__ import annotations

import copy
import json
from typing import Any, List

import httpx
import pytest

from app.config import LLMSettings
from app.llm import ChatCompletionsClient, LLMError, bypass_environment_proxy


class ScriptedClient(ChatCompletionsClient):
    """Avoid network I/O while retaining the real turn/fallback implementation."""

    def __init__(self, settings: LLMSettings, script: List[Any]) -> None:
        super().__init__(settings, api_key="test-key")
        self.script = list(script)
        self.requests: List[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(body))
        next_result = self.script.pop(0)
        if isinstance(next_result, Exception):
            raise next_result
        return next_result


def _settings(**changes: Any) -> LLMSettings:
    values: dict[str, Any] = {
        "base_url": "https://llm.example/v1",
        "model": "test-model",
        "global_prompt": "System prompt",
    }
    values.update(changes)
    return LLMSettings(**values)


def _completion(message: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": message}]}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8045/v1/chat/completions", True),
        ("http://localhost:8045/v1", True),
        ("http://[::1]:8045/v1", True),
        ("https://api.example.com/v1", False),
        ("http://192.168.1.20:8080/v1", False),
    ],
)
def test_loopback_endpoint_bypasses_system_proxy(url: str, expected: bool) -> None:
    assert bypass_environment_proxy(url) is expected


@pytest.mark.asyncio
async def test_tool_result_is_sent_to_second_request_and_final_summary_is_used() -> None:
    first = _completion(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "send_group_message",
                        "arguments": '{"text":"已收到"}',
                    },
                }
            ],
        }
    )
    final = _completion({"content": "最终群聊摘要"})
    client = ScriptedClient(_settings(), [first, final])
    executions: list[tuple[str, dict[str, Any], str]] = []

    async def execute(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        executions.append((name, arguments, call_id))
        return {"ok": True, "message_id": "9001"}

    result = await client.run_turn(
        previous_summary="旧摘要",
        event_text="群成员发来一条消息",
        group_prompt="",
        reasoning_effort="off",
        image_parts=[],
        tool_executor=execute,
    )

    assert result.summary == "最终群聊摘要"
    assert executions == [("send_group_message", {"text": "已收到"}, "call-1")]
    assert result.tool_results == [
        {
            "tool_call_id": "call-1",
            "tool_name": "send_group_message",
            "result": {"ok": True, "message_id": "9001"},
        }
    ]
    # A successful tool result now opens one bounded Agent follow-up with
    # tools still available; the scripted final response chooses no tool.
    assert [request["tool_choice"] for request in client.requests] == ["auto", "auto"]
    second_messages = client.requests[1]["messages"]
    assert second_messages[-2]["role"] == "assistant"
    assert second_messages[-1]["role"] == "tool"
    assert second_messages[-1]["tool_call_id"] == "call-1"
    assert json.loads(second_messages[-1]["content"]) == {"ok": True, "message_id": "9001"}


@pytest.mark.asyncio
async def test_reasoning_effort_compatibility_error_retries_without_the_field() -> None:
    client = ScriptedClient(
        _settings(send_reasoning_effort=True),
        [LLMError("LLM request error 400: unsupported reasoning_effort"), _completion({"content": "摘要"})],
    )

    async def unused_execute(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        raise AssertionError("tool executor should not be called")

    result = await client.run_turn(
        previous_summary="",
        event_text="新事件",
        group_prompt="",
        reasoning_effort="high",
        image_parts=[],
        tool_executor=unused_execute,
    )

    assert result.summary == "摘要"
    assert "reasoning_effort" in client.requests[0]
    assert "reasoning_effort" not in client.requests[1]
    assert "reasoning_effort" in result.warning


@pytest.mark.asyncio
async def test_image_compatibility_error_retries_without_image_content() -> None:
    image_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    client = ScriptedClient(
        _settings(),
        [LLMError("LLM request error 400: unsupported image input"), _completion({"content": "仅文字摘要"})],
    )

    async def unused_execute(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        raise AssertionError("tool executor should not be called")

    result = await client.run_turn(
        previous_summary="旧摘要",
        event_text="新事件",
        group_prompt="",
        reasoning_effort="off",
        image_parts=[image_part],
        tool_executor=unused_execute,
    )

    first_content = client.requests[0]["messages"][1]["content"]
    second_content = client.requests[1]["messages"][1]["content"]
    assert any(part.get("type") == "image_url" for part in first_content)
    assert not any(part.get("type") == "image_url" for part in second_content)
    assert any("图片视觉输入因兼容性问题被省略" in str(part) for part in second_content)
    assert result.summary == "仅文字摘要"
    assert "视觉内容" in result.warning


@pytest.mark.asyncio
async def test_service_error_diagnostic_has_phase_retries_and_redacts_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingAsyncClient:
        calls = 0

        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> "FailingAsyncClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> httpx.Response:
            type(self).calls += 1
            return httpx.Response(
                503,
                headers={"content-type": "application/json", "x-request-id": "req-local-123", "retry-after": "5"},
                content=(
                    b'{"error":{"message":"authorization: Bearer api-key-very-secret-123; '
                    b'api_key=other-secret; upstream unavailable"}}'
                ),
            )

    async def skip_backoff(_: float) -> None:
        return None

    monkeypatch.setattr("app.llm.httpx.AsyncClient", FailingAsyncClient)
    monkeypatch.setattr("app.llm.asyncio.sleep", skip_backoff)
    client = ChatCompletionsClient(_settings(), api_key="api-key-very-secret-123")

    async def unused_execute(*_: Any) -> dict[str, Any]:
        raise AssertionError("tool executor should not be called")

    with pytest.raises(LLMError) as caught:
        await client.run_turn("", "新事件", "", "off", [], unused_execute)

    detail = str(caught.value)
    assert FailingAsyncClient.calls == 3
    assert "阶段：初始摘要与工具决策" in detail
    assert "HTTP 503 Service Unavailable" in detail
    assert "本轮已尝试 3 次请求" in detail
    assert "Request ID：req-local-123" in detail
    assert "Retry-After：5" in detail
    assert "upstream unavailable" in detail
    assert "api-key-very-secret-123" not in detail
    assert "other-secret" not in detail
    assert "[已隐藏]" in detail


@pytest.mark.asyncio
async def test_empty_provider_error_body_is_explicitly_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyAsyncClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> "EmptyAsyncClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> httpx.Response:
            return httpx.Response(401, headers={"content-type": "application/json"}, content=b"")

    monkeypatch.setattr("app.llm.httpx.AsyncClient", EmptyAsyncClient)
    client = ChatCompletionsClient(_settings(), api_key="test-key")

    async def unused_execute(*_: Any) -> dict[str, Any]:
        raise AssertionError("tool executor should not be called")

    with pytest.raises(LLMError) as caught:
        await client.run_turn("", "新事件", "", "off", [], unused_execute)

    detail = str(caught.value)
    assert "阶段：初始摘要与工具决策" in detail
    assert "HTTP 401 Unauthorized" in detail
    assert "服务端未返回错误正文。" in detail
