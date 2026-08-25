"""Regression coverage for quiet, useful QQ replies and direct @ replies."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.config import DEFAULT_PROMPT, LLMSettings
from app.llm import ChatCompletionsClient


class ScriptedClient(ChatCompletionsClient):
    """Capture API bodies while exercising the real prompt construction."""

    def __init__(self, settings: LLMSettings, script: list[dict[str, Any]]) -> None:
        super().__init__(settings, api_key="test-key")
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(body))
        return self.script.pop(0)


def _chat_text(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


def _chat_send_then_summary() -> list[dict[str, Any]]:
    return [
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "send-1",
                                "type": "function",
                                "function": {
                                    "name": "send_group_message",
                                    "arguments": '{"text":"简短回复"}',
                                },
                            }
                        ],
                    }
                }
            ]
        },
        _chat_text("内部摘要"),
    ]


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


def _response_send_then_summary() -> list[dict[str, Any]]:
    return [
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "send-1",
                    "name": "send_group_message",
                    "arguments": '{"text":"简短回复"}',
                }
            ]
        },
        _response_text("内部摘要"),
    ]


async def _unused_tool(*_: Any) -> dict[str, Any]:
    raise AssertionError("this turn should not execute a tool")


async def _successful_send(*_: Any) -> dict[str, Any]:
    return {"ok": True, "message_id": "1"}


def _settings(*, endpoint_mode: str = "completions") -> LLMSettings:
    return LLMSettings(
        base_url="https://llm.example/v1",
        endpoint_mode=endpoint_mode,
        model="test",
        # Simulates an already-persisted older instruction.  The immutable
        # policy must still be appended after it rather than overwriting it.
        global_prompt="旧全局提示词：每一条群消息都发一个很长的总结。",
    )


def _chat_developer(request: dict[str, Any]) -> str:
    return str(request["messages"][0]["content"])


def _responses_developer(request: dict[str, Any]) -> str:
    return str(request["instructions"])


@pytest.mark.asyncio
async def test_chat_normal_turn_keeps_internal_summary_and_allows_bounded_proactive_autonomy() -> None:
    client = ScriptedClient(_settings(), [_chat_text("内部摘要")])

    result = await client.run_turn("旧摘要", "成员：ok", "群级规则", "off", [], _unused_tool)

    request = client.requests[0]
    developer = _chat_developer(request)
    user_text = request["messages"][1]["content"][0]["text"]
    assert result.summary == "内部摘要"
    # ``auto`` deliberately remains: the bot can proactively help when useful
    # without turning every group event into a reply.
    assert request["tool_choice"] == "auto"
    assert developer.find("旧全局提示词") < developer.find("不可变群聊发言策略")
    assert developer.rfind("不可变群聊发言策略") > developer.find("群级规则")
    assert "闲聊、表情、单字确认" in developer
    assert "可以像克制、有帮助的群成员一样自主发言" in developer
    assert "不是每条消息都回复" in developer
    assert "可以保持沉默" in developer
    assert "服务生成的实时直接提及规则" not in developer
    assert "你可自主决定是否调用工具" in user_text
    send_tool = request["tools"][0]["function"]
    assert "单次决策可多次调用" in send_tool["description"]
    assert "服务生成的可信消息元数据" in send_tool["parameters"]["properties"]["reply_to_message_id"]["description"]


@pytest.mark.asyncio
async def test_chat_live_direct_mention_rule_overrides_old_prompt_and_requires_plain_bounded_reply() -> None:
    client = ScriptedClient(_settings(), _chat_send_then_summary())

    await client.run_turn(
        "旧摘要", "[服务标记] @机器人 这个怎么做？", "群级规则", "off", [], _successful_send,
        direct_mention_reply_required=True,
    )

    request = client.requests[0]
    developer = _chat_developer(request)
    user_text = request["messages"][1]["content"][0]["text"]
    assert request["tool_choice"] == {
        "type": "function",
        "function": {"name": "send_group_message"},
    }
    assert developer.find("旧全局提示词") < developer.rfind("服务生成的实时直接提及规则")
    assert developer.rfind("服务生成的实时直接提及规则") > developer.find("不可变群聊发言策略")
    assert "必须先调用 send_group_message" in developer
    assert "绝不能发送背景摘要" in developer
    assert "可信消息元数据中明确提供" in developer
    assert "Markdown 图片渲染完全是可选工具" in developer
    assert "代码、题解、公式、表格和较长回答并不强制渲染" in developer
    assert "必须先调用 send_group_message" in user_text


@pytest.mark.asyncio
async def test_chat_live_reply_to_bot_forces_natural_send_without_pretending_it_was_an_at() -> None:
    """A verified reply chain is an interaction even if the user did not @ the bot."""

    client = ScriptedClient(_settings(), _chat_send_then_summary())

    await client.run_turn(
        "旧摘要",
        "【服务生成的实时回复机器人标记】\n成员：你太棒了",
        "群级规则",
        "off",
        [],
        _successful_send,
        direct_reply_to_bot_message_required=True,
    )

    request = client.requests[0]
    developer = _chat_developer(request)
    assert request["tool_choice"] == {
        "type": "function",
        "function": {"name": "send_group_message"},
    }
    assert "服务生成的实时回复机器人规则" in developer
    assert "致谢、夸奖或轻松互动" in developer
    assert "服务生成的实时直接提及规则" not in developer


@pytest.mark.asyncio
async def test_rolling_summary_prompt_keeps_rules_and_demands_full_continuous_replacement() -> None:
    client = ScriptedClient(_settings(), [_chat_text("完整的新累计摘要")])

    result = await client.run_turn(
        "旧的累计上下文：正在讨论 Tarjan。",
        "成员：谢谢。",
        "群级规则",
        "off",
        [],
        _unused_tool,
        persistent_rules="长期规则：算法回答先给结论。",
    )

    request = client.requests[0]
    developer = _chat_developer(request)
    user_text = request["messages"][1]["content"][0]["text"]
    assert result.summary == "完整的新累计摘要"
    assert developer.find("群级规则") < developer.find("本机管理员长期规则")
    assert developer.find("本机管理员长期规则") < developer.find("不可变服务边界")
    assert "完整替换，而不是只概括本轮新消息" in user_text
    assert "不要因此丢失此前仍重要的上下文" in user_text


@pytest.mark.asyncio
async def test_responses_normal_turn_has_the_same_internal_summary_and_tool_boundaries() -> None:
    client = ScriptedClient(_settings(endpoint_mode="responses"), [_response_text("内部摘要")])

    result = await client.run_turn("旧摘要", "成员：收到", "群级规则", "off", [], _unused_tool)

    request = client.requests[0]
    developer = _responses_developer(request)
    user_text = request["input"][0]["content"][0]["text"]
    assert result.summary == "内部摘要"
    assert request["tool_choice"] == "auto"
    assert developer.find("旧全局提示词") < developer.find("不可变群聊发言策略")
    assert "同一话题的重复回复" in developer
    assert "你可自主决定是否调用工具" in user_text
    send_tool = request["tools"][0]
    assert "单次决策可多次调用" in send_tool["description"]
    assert "服务生成的可信消息元数据" in send_tool["parameters"]["properties"]["reply_to_message_id"]["description"]


@pytest.mark.asyncio
async def test_responses_live_direct_mention_has_the_same_plain_bounded_reply_rule() -> None:
    client = ScriptedClient(_settings(endpoint_mode="responses"), _response_send_then_summary())

    await client.run_turn(
        "旧摘要", "[服务标记] @机器人 帮我看看", "群级规则", "off", [], _successful_send,
        direct_mention_reply_required=True,
    )

    request = client.requests[0]
    developer = _responses_developer(request)
    user_text = request["input"][0]["content"][0]["text"]
    assert request["tool_choice"] == {"type": "function", "name": "send_group_message"}
    assert developer.find("旧全局提示词") < developer.rfind("服务生成的实时直接提及规则")
    assert "必须先调用 send_group_message" in developer
    assert "绝不能发送背景摘要" in developer
    assert "可信消息元数据中明确提供" in developer
    assert "必须先调用 send_group_message" in user_text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_direct_code_request_uses_renderer_and_preserves_markdown_source(
    endpoint_mode: str,
) -> None:
    """A direct @ code reply is rendered rather than flattened into QQ text."""

    markdown = "# 修改后的代码\n\n```cpp\nvoid solve() {\n    dfs(1);\n}\n```\n"
    if endpoint_mode == "responses":
        script = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "code-render",
                        "name": "Builtin_render_markdown_image",
                        "arguments": json.dumps({"markdown": markdown}, ensure_ascii=False),
                    }
                ]
            },
            _response_text("内部摘要"),
        ]
    else:
        script = [
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "code-render",
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
            },
            _chat_text("内部摘要"),
        ]

    client = ScriptedClient(_settings(endpoint_mode=endpoint_mode), script)
    sent: list[dict[str, Any]] = []

    async def execute(name: str, arguments: dict[str, Any], *_: Any) -> dict[str, Any]:
        assert name == "Builtin_render_markdown_image"
        sent.append(arguments)
        return {"ok": True, "message_id": "code-image"}

    await client.run_turn(
        "旧摘要",
        "成员 @机器人：请只改我贴出的 DFS 代码错误部分。\nvoid solve(){ dfs(0); }",
        "",
        "off",
        [],
        execute,
        direct_mention_reply_required=True,
    )

    request = client.requests[0]
    developer = (
        _responses_developer(request) if endpoint_mode == "responses" else _chat_developer(request)
    )
    assert sent == [{"markdown": markdown}]
    assert "```cpp" in sent[0]["markdown"]
    assert "\n" in sent[0]["markdown"]
    assert "Markdown 图片渲染完全是可选工具" in developer
    assert "代码、题解、公式、表格和较长回答并不强制渲染" in developer
    assert "Builtin_render_markdown_image" in developer


def test_fresh_default_prompt_describes_bounded_autonomous_qq_behavior() -> None:
    assert "可以自主发言" in DEFAULT_PROMPT
    assert "内部摘要、逐条群聊回顾、群情概览" in DEFAULT_PROMPT
    assert "完全由你根据当前对话和表达效果自然选择" in DEFAULT_PROMPT
    assert "渲染工具不是强制格式" in DEFAULT_PROMPT
    assert "Builtin_render_markdown_image" in DEFAULT_PROMPT
    assert "Markdown 源码" in DEFAULT_PROMPT
    assert "可信消息元数据" in DEFAULT_PROMPT
    assert "JSON 中的 \\n 解析后应成为真正换行" in DEFAULT_PROMPT
