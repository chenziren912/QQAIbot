"""Regression tests for one bounded, pre-action tool correction."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.config import LLMSettings
from app.llm import ChatCompletionsClient


class ScriptedClient(ChatCompletionsClient):
    def __init__(self, settings: LLMSettings, script: list[dict[str, Any]]) -> None:
        super().__init__(settings, api_key="test-key")
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(body))
        return self.script.pop(0)


def _settings(*, endpoint_mode: str = "completions") -> LLMSettings:
    return LLMSettings(
        base_url="https://llm.example/v1",
        endpoint_mode=endpoint_mode,
        model="test-model",
        global_prompt="local test rule",
    )


def _chat_tool(call_id: str, text: str) -> dict[str, Any]:
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
                    ],
                }
            }
        ]
    }


def _chat_named_tool(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
                                "name": name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _chat_text(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


def _response_tool(call_id: str, text: str) -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": "send_group_message",
                "arguments": json.dumps({"text": text}, ensure_ascii=False),
            }
        ]
    }


def _response_named_tool(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            }
        ]
    }


def _chat_renderer_tool(call_id: str, markdown: str) -> dict[str, Any]:
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
                                "name": "Builtin_render_markdown_image",
                                "arguments": json.dumps({"markdown": markdown}, ensure_ascii=False),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _response_renderer_tool(call_id: str, markdown: str) -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": "Builtin_render_markdown_image",
                "arguments": json.dumps({"markdown": markdown}, ensure_ascii=False),
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


def _safe_rejection() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "text 不能为空",
        "retry_safe": True,
        "retry_safe_reason": "本地参数校验或工具权限检查已拒绝该请求；未向 QQ 发起操作。",
    }


def _video_source_rejection() -> dict[str, Any]:
    return {
        "ok": False,
        "retry_safe": True,
        "retry_safe_reason": "视频在下载阶段失败，尚未发送 QQ 回复；可安全说明失败。",
        "repair_uses_next_slot": True,
        "required_tool": "send_group_message",
        "failure_kind": "video_source_unavailable",
        "error": "无法读取当前视频：QQ 临时地址不可用",
        "user_visible_text": "这段视频暂时无法下载解析，请重新发送原视频或稍后再试。",
    }


@pytest.mark.asyncio
async def test_chat_safe_tool_rejection_gets_one_repair_then_no_tools_summary() -> None:
    client = ScriptedClient(
        _settings(),
        [_chat_tool("initial", ""), _chat_tool("repair", "修正后的短回复"), _chat_text("最终内部摘要")],
    )
    executions: list[tuple[str, dict[str, Any], str]] = []

    async def execute(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        executions.append((name, arguments, call_id))
        return _safe_rejection() if call_id == "initial" else {"ok": True, "message_id": "9001"}

    result = await client.run_turn("旧摘要", "群消息", "", "off", [], execute)

    assert result.summary == "最终内部摘要"
    assert [request["tool_choice"] for request in client.requests] == ["auto", "auto", "none"]
    assert executions == [
        ("send_group_message", {"text": ""}, "initial"),
        ("send_group_message", {"text": "修正后的短回复"}, "repair"),
    ]
    repair_messages = client.requests[1]["messages"]
    assert json.loads(repair_messages[-2]["content"]) == _safe_rejection()
    assert "工具修正规则" in repair_messages[-1]["content"]
    assert result.tool_results[0]["result"] == _safe_rejection()
    assert result.tool_results[1]["result"] == {"ok": True, "message_id": "9001"}


@pytest.mark.asyncio
async def test_responses_safe_tool_rejection_gets_one_repair_then_no_tools_summary() -> None:
    client = ScriptedClient(
        _settings(endpoint_mode="responses"),
        [_response_tool("initial", ""), _response_tool("repair", "修正后的短回复"), _response_text("最终内部摘要")],
    )
    executions: list[tuple[str, dict[str, Any], str]] = []

    async def execute(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        executions.append((name, arguments, call_id))
        return _safe_rejection() if call_id == "initial" else {"ok": True, "message_id": "9002"}

    result = await client.run_turn("旧摘要", "群消息", "", "off", [], execute)

    assert result.summary == "最终内部摘要"
    assert [request["tool_choice"] for request in client.requests] == ["auto", "auto", "none"]
    assert executions == [
        ("send_group_message", {"text": ""}, "initial"),
        ("send_group_message", {"text": "修正后的短回复"}, "repair"),
    ]
    repair_input = client.requests[1]["input"]
    safe_outputs = [item for item in repair_input if item.get("type") == "function_call_output"]
    assert len(safe_outputs) == 1
    assert json.loads(safe_outputs[0]["output"]) == _safe_rejection()
    assert "工具修正规则" in client.requests[1]["instructions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_unsafe_tool_failure_is_returned_to_model_but_same_send_is_not_retried(
    endpoint_mode: str,
) -> None:
    if endpoint_mode == "responses":
        script = [_response_tool("first", "会造成歧义"), _response_text("安全结束摘要")]
    else:
        script = [_chat_tool("first", "会造成歧义"), _chat_text("安全结束摘要")]
    client = ScriptedClient(_settings(endpoint_mode=endpoint_mode), script)
    executions: list[str] = []

    async def execute(name: str, _: dict[str, Any], __: str) -> dict[str, Any]:
        executions.append(name)
        # An adapter/network result may have reached QQ, so it is intentionally
        # not retry-safe even if it says ``ok: false``.
        return {"ok": False, "error": "OneBot request timed out; result unknown"}

    result = await client.run_turn("旧摘要", "群消息", "", "off", [], execute)

    assert result.summary == "安全结束摘要"
    assert executions == ["send_group_message"]
    # The second request receives the actual error JSON and gets an Agent
    # recovery opportunity, but an unknown QQ send is removed from its tools
    # so it cannot blindly duplicate the message.
    assert [request["tool_choice"] for request in client.requests] == ["auto", "auto"]
    if endpoint_mode == "responses":
        recovery_outputs = [
            item
            for item in client.requests[1]["input"]
            if item.get("type") == "function_call_output"
        ]
        assert json.loads(recovery_outputs[0]["output"])["error"] == "OneBot request timed out; result unknown"
        assert "工具错误恢复规则" in client.requests[1]["instructions"]
        assert "send_group_message" not in {tool["name"] for tool in client.requests[1]["tools"]}
    else:
        recovery_messages = client.requests[1]["messages"]
        tool_message = next(message for message in recovery_messages if message.get("role") == "tool")
        assert json.loads(tool_message["content"])["error"] == "OneBot request timed out; result unknown"
        assert "工具错误恢复规则" in recovery_messages[-1]["content"]
        assert "send_group_message" not in {
            tool["function"]["name"] for tool in client.requests[1]["tools"]
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_renderer_error_returns_full_result_to_agent_then_allows_short_explanation(
    endpoint_mode: str,
) -> None:
    """A failed MarkFlow/Edge render must not strand the Agent at its audit row."""

    render_error = {
        "ok": False,
        "error": "图片渲染失败：Edge connection context was destroyed.",
    }
    if endpoint_mode == "responses":
        script = [
            _response_renderer_tool("render", "# 很长的说明"),
            _response_tool("explain", "刚才图片渲染失败了，我先用简短文字说明。"),
            _response_text("内部摘要：已如实告知渲染失败。"),
        ]
    else:
        script = [
            _chat_renderer_tool("render", "# 很长的说明"),
            _chat_tool("explain", "刚才图片渲染失败了，我先用简短文字说明。"),
            _chat_text("内部摘要：已如实告知渲染失败。"),
        ]
    client = ScriptedClient(_settings(endpoint_mode=endpoint_mode), script)
    executions: list[tuple[str, dict[str, Any], str]] = []

    async def execute(name: str, arguments: dict[str, Any], call_id: str, *_: Any) -> dict[str, Any]:
        executions.append((name, arguments, call_id))
        if name == "Builtin_render_markdown_image":
            return render_error
        assert name == "send_group_message"
        return {"ok": True, "message_id": "recovery-explained"}

    result = await client.run_turn(
        "旧摘要",
        "成员要求写完整题解",
        "",
        "off",
        [],
        execute,
        direct_mention_reply_required=True,
    )

    assert result.summary == "内部摘要：已如实告知渲染失败。"
    assert executions == [
        ("Builtin_render_markdown_image", {"markdown": "# 很长的说明"}, "render"),
        ("send_group_message", {"text": "刚才图片渲染失败了，我先用简短文字说明。"}, "explain"),
    ]
    initial_choice = (
        {"type": "function", "name": "send_group_message"}
        if endpoint_mode == "responses"
        else {"type": "function", "function": {"name": "send_group_message"}}
    )
    assert [request["tool_choice"] for request in client.requests] == [initial_choice, "auto", "none"]
    if endpoint_mode == "responses":
        recovery_outputs = [
            item
            for item in client.requests[1]["input"]
            if item.get("type") == "function_call_output"
        ]
        assert json.loads(recovery_outputs[0]["output"]) == render_error
        assert "工具错误恢复规则" in client.requests[1]["instructions"]
        recovery_tool_names = {tool["name"] for tool in client.requests[1]["tools"]}
    else:
        recovery_messages = client.requests[1]["messages"]
        tool_message = next(message for message in recovery_messages if message.get("role") == "tool")
        assert json.loads(tool_message["content"]) == render_error
        assert "工具错误恢复规则" in recovery_messages[-1]["content"]
        recovery_tool_names = {
            tool["function"]["name"] for tool in client.requests[1]["tools"]
        }
    # The model can explain through a different tool, but the failed image
    # renderer is intentionally absent to avoid a blind duplicate render.
    assert "send_group_message" in recovery_tool_names
    assert "Builtin_render_markdown_image" not in recovery_tool_names


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_direct_recovery_prose_after_renderer_failure_uses_safe_text_fallback(
    endpoint_mode: str,
) -> None:
    """The recovery model may answer in prose; it must not remain internal."""

    apology = "刚才图片渲染失败了，我暂时无法生成长图，请稍后再试。"
    if endpoint_mode == "responses":
        script = [
            _response_renderer_tool("render", "# 长内容"),
            _response_text(apology),
        ]
    else:
        script = [
            _chat_renderer_tool("render", "# 长内容"),
            _chat_text(apology),
        ]
    client = ScriptedClient(_settings(endpoint_mode=endpoint_mode), script)
    executions: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any], *_: Any) -> dict[str, Any]:
        executions.append((name, arguments))
        if name == "Builtin_render_markdown_image":
            return {"ok": False, "error": "图片渲染失败：Edge context was destroyed."}
        assert name == "send_group_message"
        return {"ok": True, "message_id": "prose-recovery"}

    result = await client.run_turn(
        "旧摘要",
        "成员 @机器人：把这个题解渲染成长图",
        "",
        "off",
        [],
        execute,
        direct_mention_reply_required=True,
    )

    assert result.summary == apology
    assert executions == [
        ("Builtin_render_markdown_image", {"markdown": "# 长内容"}),
        ("send_group_message", {"text": apology}),
    ]
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_direct_mention_repair_keeps_send_tool_forced() -> None:
    client = ScriptedClient(
        _settings(),
        [_chat_tool("initial", ""), _chat_tool("repair", "收到，我来看看"), _chat_text("内部摘要")],
    )

    async def execute(_: str, __: dict[str, Any], call_id: str) -> dict[str, Any]:
        return _safe_rejection() if call_id == "initial" else {"ok": True, "message_id": "9003"}

    await client.run_turn("", "@机器人", "", "off", [], execute, direct_mention_reply_required=True)

    forced = {"type": "function", "function": {"name": "send_group_message"}}
    assert [request["tool_choice"] for request in client.requests] == [forced, forced, "none"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_video_source_failure_forces_one_visible_failure_reply_without_direct_mention(
    endpoint_mode: str,
) -> None:
    """A video request cannot silently degrade into an internal-only turn."""

    if endpoint_mode == "responses":
        script = [
            _response_named_tool("video", "Builtin_video_understanding", {"file_id": "video-1"}),
            _response_tool("explain", "这段视频暂时无法下载解析，请重新发送原视频或稍后再试。"),
            _response_text("最终内部摘要"),
        ]
    else:
        script = [
            _chat_named_tool("video", "Builtin_video_understanding", {"file_id": "video-1"}),
            _chat_tool("explain", "这段视频暂时无法下载解析，请重新发送原视频或稍后再试。"),
            _chat_text("最终内部摘要"),
        ]
    client = ScriptedClient(_settings(endpoint_mode=endpoint_mode), script)
    executions: list[tuple[str, int, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any], _call_id: str, slot: int) -> dict[str, Any]:
        executions.append((name, slot, arguments))
        if name == "Builtin_video_understanding":
            return _video_source_rejection()
        assert name == "send_group_message"
        return {"ok": True, "message_id": "video-failure-visible"}

    result = await client.run_turn("旧摘要", "成员要求分析视频", "", "off", [], execute)

    assert result.summary == "最终内部摘要"
    # The failed download held operation slot 0, so its visible explanation
    # uses a distinct durable slot and cannot be collapsed into the old audit.
    assert executions == [
        ("Builtin_video_understanding", 0, {"file_id": "video-1"}),
        (
            "send_group_message",
            1,
            {"text": "这段视频暂时无法下载解析，请重新发送原视频或稍后再试。"},
        ),
    ]
    if endpoint_mode == "responses":
        assert [request["tool_choice"] for request in client.requests] == [
            "auto",
            {"type": "function", "name": "send_group_message"},
            "none",
        ]
        assert "user_visible_text" in client.requests[1]["instructions"]
    else:
        forced = {"type": "function", "function": {"name": "send_group_message"}}
        assert [request["tool_choice"] for request in client.requests] == ["auto", forced, "none"]
        assert "user_visible_text" in client.requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_video_failure_still_notifies_when_provider_ignores_forced_repair_tool(
    endpoint_mode: str,
) -> None:
    """A relay's prose fallback must not turn an explicit task into silence."""

    if endpoint_mode == "responses":
        script = [
            _response_named_tool("video", "Builtin_video_understanding", {"file_id": "video-1"}),
            _response_text("内部摘要：视频下载失败。"),
        ]
    else:
        script = [
            _chat_named_tool("video", "Builtin_video_understanding", {"file_id": "video-1"}),
            _chat_text("内部摘要：视频下载失败。"),
        ]
    client = ScriptedClient(_settings(endpoint_mode=endpoint_mode), script)
    executions: list[tuple[str, int, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any], _call_id: str, slot: int) -> dict[str, Any]:
        executions.append((name, slot, arguments))
        if name == "Builtin_video_understanding":
            return _video_source_rejection()
        return {"ok": True, "message_id": "fallback-visible"}

    result = await client.run_turn("旧摘要", "成员要求分析视频", "", "off", [], execute)

    assert result.summary == "内部摘要：视频下载失败。"
    assert executions == [
        ("Builtin_video_understanding", 0, {"file_id": "video-1"}),
        (
            "send_group_message",
            1,
            {"text": "这段视频暂时无法下载解析，请重新发送原视频或稍后再试。"},
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_structured_text_can_stay_in_send_group_message_when_model_chooses_it(
    endpoint_mode: str,
) -> None:
    """Code-like structured text no longer triggers a presentation repair."""

    text = "# 题解\\n\\nint main() { return 0; }"
    script = (
        [_response_tool("initial-send", text), _response_text("最终内部摘要")]
        if endpoint_mode == "responses"
        else [_chat_tool("initial-send", text), _chat_text("最终内部摘要")]
    )
    client = ScriptedClient(_settings(endpoint_mode=endpoint_mode), script)
    executions: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any], *_: Any) -> dict[str, Any]:
        executions.append((name, arguments))
        assert name == "send_group_message"
        assert arguments == {"text": text}
        return {"ok": True, "message_id": "plain-code"}

    result = await client.run_turn(
        "旧摘要",
        "成员 @机器人：给我这题的完整代码",
        "",
        "off",
        [],
        execute,
        direct_mention_reply_required=True,
    )

    assert result.summary == "最终内部摘要"
    assert executions == [("send_group_message", {"text": text})]
    if endpoint_mode == "responses":
        assert [request["tool_choice"] for request in client.requests] == [
            {"type": "function", "name": "send_group_message"},
            "auto",
        ]
    else:
        forced = {"type": "function", "function": {"name": "send_group_message"}}
        assert [request["tool_choice"] for request in client.requests] == [forced, "auto"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_direct_text_fallback_never_auto_renders_a_structured_reply(
    endpoint_mode: str,
) -> None:
    """Provider prose remains text unless the provider itself picks renderer."""

    text = "# 题解\\n\\nint main() { return 0; }"
    script = [_response_text(text)] if endpoint_mode == "responses" else [_chat_text(text)]
    client = ScriptedClient(_settings(endpoint_mode=endpoint_mode), script)
    executions: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any], *_: Any) -> dict[str, Any]:
        executions.append((name, arguments))
        assert name == "send_group_message"
        return {"ok": True, "message_id": "plain-direct-text"}

    result = await client.run_turn(
        "旧摘要",
        "成员 @机器人：给我完整代码",
        "",
        "off",
        [],
        execute,
        direct_mention_reply_required=True,
    )

    assert result.summary == text
    assert executions == [("send_group_message", {"text": text})]
    assert result.tool_results[0]["result"]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_direct_text_fallback_never_auto_renders_a_blocked_private_summary(
    endpoint_mode: str,
) -> None:
    """A prose-only relay must not leak its rolling summary through MarkFlow."""

    leaked_summary = (
        "群内此前进行海龟汤推理，随后群友围绕 Docker 部署、Python 语法、题解排版和签到话题展开讨论；"
        "机器人已分别澄清互动规则并配合发送签到指令。"
    )
    script = (
        [_response_text(leaked_summary), _response_text("收到，我在。")]
        if endpoint_mode == "responses"
        else [_chat_text(leaked_summary), _chat_text("收到，我在。")]
    )
    client = ScriptedClient(_settings(endpoint_mode=endpoint_mode), script)
    executions: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any], *_: Any) -> dict[str, Any]:
        executions.append((name, arguments))
        assert name == "send_group_message"
        if arguments["text"] == "收到，我在。":
            return {"ok": True, "message_id": "fresh-direct-reply"}
        return {
            "ok": False,
            "retry_safe": True,
            "error": "该内容属于本机滚动内部摘要，禁止发送。",
            "internal_summary_outbound_blocked": True,
            "suppress_direct_text_fallback": True,
            "suppress_direct_reply_repair": True,
        }

    result = await client.run_turn(
        "旧摘要",
        "成员 @机器人：在吗",
        "",
        "off",
        [],
        execute,
        direct_mention_reply_required=True,
    )

    assert result.summary == leaked_summary
    # The private prose is blocked before QQ, then exactly one new provider
    # correction produces a fresh direct reply.  It must never render/send the
    # original rolling-summary body.
    assert executions == [
        ("send_group_message", {"text": leaked_summary}),
        ("send_group_message", {"text": "收到，我在。"}),
    ]
    assert [item["tool_name"] for item in result.tool_results] == [
        "send_group_message",
        "send_group_message",
    ]
    assert result.tool_results[0]["result"]["internal_summary_outbound_blocked"] is True
    assert result.tool_results[1]["result"]["ok"] is True
    assert "受限实时回复修正" in result.warning
    if endpoint_mode == "responses":
        assert [request["tool_choice"] for request in client.requests] == [
            {"type": "function", "name": "send_group_message"},
            "auto",
        ]
        assert {tool["name"] for tool in client.requests[1]["tools"]} == {
            "send_group_message",
            "Builtin_render_markdown_image",
        }
    else:
        forced = {"type": "function", "function": {"name": "send_group_message"}}
        assert [request["tool_choice"] for request in client.requests] == [forced, "auto"]
        assert {
            item["function"]["name"]
            for item in client.requests[1]["tools"]
        } == {"send_group_message", "Builtin_render_markdown_image"}


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
@pytest.mark.parametrize("second_reply", ["再次输出的内部摘要", ""])
async def test_private_summary_reply_correction_stops_after_second_block_or_empty(
    endpoint_mode: str,
    second_reply: str,
) -> None:
    first_reply = "群内此前围绕 Docker、题解和签到展开讨论，机器人已完成对应处理。"
    responses = [_response_text(first_reply), _response_text(second_reply)]
    chats = [_chat_text(first_reply), _chat_text(second_reply)]
    client = ScriptedClient(_settings(endpoint_mode=endpoint_mode), responses if endpoint_mode == "responses" else chats)
    executions: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any], *_: Any) -> dict[str, Any]:
        executions.append((name, arguments))
        assert name == "send_group_message"
        return {
            "ok": False,
            "retry_safe": True,
            "error": "私有滚动摘要不能发送",
            "internal_summary_outbound_blocked": True,
            "suppress_direct_text_fallback": True,
            "suppress_direct_reply_repair": True,
        }

    result = await client.run_turn(
        "旧摘要",
        "成员 @机器人：在吗",
        "",
        "off",
        [],
        execute,
        direct_mention_reply_required=True,
    )

    assert result.summary == first_reply
    # First block + exactly one correction request, never an unbounded loop.
    assert len(client.requests) == 2
    assert all(
        name == "send_group_message"
        for name, _arguments in executions
    )
    assert not any(name == "Builtin_render_markdown_image" for name, _arguments in executions)
    if second_reply:
        assert len(executions) == 2
        assert "再次命中内部摘要边界" in result.warning
    else:
        assert len(executions) == 1
