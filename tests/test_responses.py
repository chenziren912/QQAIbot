"""Native Responses endpoint regression tests."""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest

from app.config import LLMSettings
from app.llm import ChatCompletionsClient, LLMError, endpoint_url


class ScriptedClient(ChatCompletionsClient):
    """Capture generated JSON without making an HTTP request."""

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
        "endpoint_mode": "responses",
        "model": "test-model",
        "global_prompt": "System prompt",
    }
    values.update(changes)
    return LLMSettings(**values)


def _response_text(text: str) -> dict[str, Any]:
    return {
        "id": "resp-text",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


def _response_tool_call(name: str = "send_group_message", call_id: str = "call-send") -> dict[str, Any]:
    return {
        "id": "resp-call",
        "output": [
            {
                "type": "function_call",
                "id": "fc_123",
                "call_id": call_id,
                "name": name,
                "arguments": '{"text":"已收到"}',
            }
        ],
    }


@pytest.mark.asyncio
async def test_responses_uses_native_input_tools_images_and_reasoning_shape() -> None:
    client = ScriptedClient(_settings(send_reasoning_effort=True), [_response_text("新的摘要")])

    async def unused_tool(*_: Any) -> dict[str, Any]:
        raise AssertionError("tool should not execute")

    result = await client.run_turn(
        "旧摘要",
        "新事件",
        "",
        "high",
        [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA==", "detail": "auto"}}],
        unused_tool,
    )

    assert result.summary == "新的摘要"
    request = client.requests[0]
    assert "messages" not in request
    assert request["instructions"].startswith("【管理员配置的全局规则】")
    assert request["reasoning"] == {"effort": "high"}
    assert request["input"][0]["role"] == "user"
    assert request["input"][0]["content"][0]["type"] == "input_text"
    assert request["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,AA==",
        "detail": "auto",
    }
    assert request["tools"][0]["type"] == "function"
    assert request["tools"][0]["name"] == "send_group_message"
    assert "function" not in request["tools"][0]


@pytest.mark.asyncio
async def test_responses_function_call_output_is_sent_before_final_summary() -> None:
    client = ScriptedClient(_settings(), [_response_tool_call(), _response_text("工具后的摘要")])
    executed: list[tuple[str, dict[str, Any], str]] = []

    async def execute(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        executed.append((name, arguments, call_id))
        return {"ok": True, "message_id": "9001"}

    result = await client.run_turn("旧摘要", "新事件", "", "off", [], execute)

    assert result.summary == "工具后的摘要"
    assert executed == [("send_group_message", {"text": "已收到"}, "call-send")]
    assert result.tool_results == [
        {
            "tool_call_id": "call-send",
            "tool_name": "send_group_message",
            "result": {"ok": True, "message_id": "9001"},
        }
    ]
    assert [request["tool_choice"] for request in client.requests] == ["auto", "auto"]
    second_input = client.requests[1]["input"]
    assert any(item.get("type") == "function_call" and item.get("call_id") == "call-send" for item in second_input)
    assert {
        "type": "function_call_output",
        "call_id": "call-send",
        "output": '{"ok": true, "message_id": "9001"}',
    } in second_input


@pytest.mark.asyncio
async def test_responses_reasoning_compatibility_retry_omits_nested_field() -> None:
    client = ScriptedClient(
        _settings(send_reasoning_effort=True),
        [LLMError("HTTP 400: unsupported reasoning.effort"), _response_text("兼容摘要")],
    )

    async def unused_tool(*_: Any) -> dict[str, Any]:
        raise AssertionError("tool should not execute")

    result = await client.run_turn("", "新事件", "", "high", [], unused_tool)

    assert result.summary == "兼容摘要"
    assert client.requests[0]["reasoning"] == {"effort": "high"}
    assert "reasoning" not in client.requests[1]
    assert "reasoning.effort" in result.warning


@pytest.mark.asyncio
async def test_responses_image_compatibility_retry_uses_text_placeholder() -> None:
    client = ScriptedClient(
        _settings(),
        [LLMError("HTTP 400: unsupported input_image"), _response_text("仅文字摘要")],
    )

    async def unused_tool(*_: Any) -> dict[str, Any]:
        raise AssertionError("tool should not execute")

    result = await client.run_turn(
        "",
        "新事件",
        "",
        "off",
        [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}],
        unused_tool,
    )

    first_content = client.requests[0]["input"][0]["content"]
    second_content = client.requests[1]["input"][0]["content"]
    assert any(part.get("type") == "input_image" for part in first_content)
    assert not any(part.get("type") == "input_image" for part in second_content)
    assert any("图片视觉输入因兼容性问题被省略" in str(part) for part in second_content)
    assert result.summary == "仅文字摘要"
    assert "视觉内容" in result.warning


@pytest.mark.asyncio
async def test_responses_direct_mention_forces_flat_function_choice() -> None:
    client = ScriptedClient(_settings(), [_response_tool_call(), _response_text("最新摘要")])

    async def execute(*_: Any) -> dict[str, Any]:
        return {"ok": True, "message_id": "9002"}

    result = await client.run_turn(
        "", "服务生成的实时直接提及标记", "", "off", [], execute, direct_mention_reply_required=True
    )

    assert result.summary == "最新摘要"
    assert client.requests[0]["tool_choice"] == {"type": "function", "name": "send_group_message"}


def test_endpoint_url_selects_paths_and_base_preserves_full_url() -> None:
    assert endpoint_url("https://llm.example/v1", "completions") == "https://llm.example/v1/chat/completions"
    assert endpoint_url("https://llm.example/v1", "responses") == "https://llm.example/v1/responses"
    assert endpoint_url("https://llm.example/v1/chat/completions/", "responses") == "https://llm.example/v1/responses"
    assert endpoint_url("https://llm.example/v1/responses", "completions") == "https://llm.example/v1/chat/completions"
    assert endpoint_url("  https://relay.example/custom/  ", "base") == "https://relay.example/custom/"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "configured_url", "expected_url"),
    [
        ("base", "https://relay.example/full/", "https://relay.example/full/"),
        ("completions", "https://llm.example/v1/responses", "https://llm.example/v1/chat/completions"),
        ("responses", "https://llm.example/v1/chat/completions", "https://llm.example/v1/responses"),
    ],
)
async def test_post_uses_the_resolved_selected_endpoint(
    monkeypatch: pytest.MonkeyPatch, mode: str, configured_url: str, expected_url: str
) -> None:
    seen_urls: list[str] = []

    class CapturingAsyncClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> "CapturingAsyncClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, url: str, **_: Any) -> httpx.Response:
            seen_urls.append(url)
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("app.llm.httpx.AsyncClient", CapturingAsyncClient)
    client = ChatCompletionsClient(_settings(endpoint_mode=mode, base_url=configured_url), api_key="test-key")

    assert await client._post({"model": "test-model"}) == {"ok": True}
    assert seen_urls == [expected_url]
