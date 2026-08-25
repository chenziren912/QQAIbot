"""Regression coverage for terminal Agent finalization fallbacks.

These tests model the failure shown by a real QQ task: a member asks the Agent
to deeply understand a just-uploaded video, the video tool returns a concrete
local error, then an OpenAI-compatible relay returns no final assistant text.
The Agent must not replay the video work, but it also must not leave the
requester with only an invisible dashboard audit row.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.config import LLMSettings
from app.llm import ChatCompletionsClient
from app.service import AgentService, normalise_onebot_event


class ScriptedClient(ChatCompletionsClient):
    def __init__(self, script: list[dict[str, Any]], *, endpoint_mode: str = "completions") -> None:
        super().__init__(
            LLMSettings(
                base_url="https://llm.example/v1",
                endpoint_mode=endpoint_mode,
                model="test-model",
                global_prompt="test",
            ),
            api_key="test-key",
        )
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(body))
        return self.script.pop(0)


def _chat_tool(name: str, arguments: dict[str, Any], call_id: str = "call-tool") -> dict[str, Any]:
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
                    ],
                }
            }
        ]
    }


def _chat_text(text: str | None) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


def _responses_tool(name: str, arguments: dict[str, Any], call_id: str = "call-tool") -> dict[str, Any]:
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


def _responses_text(text: str | None) -> dict[str, Any]:
    content: list[dict[str, str]] = []
    if text is not None:
        content.append({"type": "output_text", "text": text})
    return {"output": [{"type": "message", "role": "assistant", "content": content}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_explicit_video_task_gets_one_honest_failure_reply_when_finalization_is_blank(
    endpoint_mode: str,
) -> None:
    if endpoint_mode == "responses":
        script = [
            _responses_tool("Builtin_video_understanding", {"path": "movie.mp4"}),
            _responses_text(None),
        ]
    else:
        script = [
            _chat_tool("Builtin_video_understanding", {"path": "movie.mp4"}),
            _chat_text(None),
        ]
    client = ScriptedClient(script, endpoint_mode=endpoint_mode)
    calls: list[tuple[str, dict[str, Any], str]] = []

    async def execute(name: str, arguments: dict[str, Any], call_id: str, *_: Any) -> dict[str, Any]:
        calls.append((name, arguments, call_id))
        if name == "Builtin_video_understanding":
            return {
                "ok": False,
                "safe_to_notify_user": True,
                "error": "<urlopen error [Errno 2] No such file or directory>",
            }
        assert name == "send_group_message"
        return {"ok": True, "message_id": "failure-notice"}

    result = await client.run_turn(
        "旧摘要",
        "实时任务",
        "",
        "off",
        [],
        execute,
        direct_explicit_task_reply_required=True,
    )

    assert [name for name, _, _ in calls] == ["Builtin_video_understanding", "send_group_message"]
    notice = calls[-1][1]["text"]
    assert "视频分析失败" in notice
    assert "No such file or directory" in notice
    assert "本轮工具已执行，但生成最终摘要失败" in result.summary
    assert "安全回退摘要" in result.warning
    assert result.tool_results[-1]["result"] == {"ok": True, "message_id": "failure-notice"}
    # Initial tool call plus the no-tools finalization only: finalization
    # fallback is local and never asks the provider to replay the video tool.
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_safe_terminal_tool_failure_is_explained_even_when_internal_summary_arrives() -> None:
    client = ScriptedClient(
        [
            _chat_tool("Builtin_video_understanding", {"path": "movie.mp4"}),
            _chat_text("这是可保存的内部摘要。"),
        ]
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any], _: str, *__: Any) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "Builtin_video_understanding":
            return {"ok": False, "safe_to_notify_user": True, "error": "视频文件不存在：movie.mp4"}
        return {"ok": True, "message_id": "failure-notice"}

    result = await client.run_turn(
        "旧摘要",
        "实时任务",
        "",
        "off",
        [],
        execute,
        direct_explicit_task_reply_required=True,
    )

    assert result.summary == "这是可保存的内部摘要。"
    assert [name for name, _ in calls] == ["Builtin_video_understanding", "send_group_message"]
    assert "视频文件不存在：movie.mp4" in calls[-1][1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_mode", ["completions", "responses"])
async def test_blank_forced_video_failure_repair_still_sends_service_failure_text(
    endpoint_mode: str,
) -> None:
    if endpoint_mode == "responses":
        script = [
            _responses_tool("Builtin_video_understanding", {"file_id": "video-1"}),
            _responses_text(None),
        ]
    else:
        script = [
            _chat_tool("Builtin_video_understanding", {"file_id": "video-1"}),
            _chat_text(None),
        ]
    client = ScriptedClient(script, endpoint_mode=endpoint_mode)
    calls: list[tuple[str, int, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any], _: str, slot: int) -> dict[str, Any]:
        calls.append((name, slot, arguments))
        if name == "Builtin_video_understanding":
            return {
                "ok": False,
                "retry_safe": True,
                "repair_uses_next_slot": True,
                "required_tool": "send_group_message",
                "error": "无法读取当前视频：临时地址已失效",
                "user_visible_text": "这段视频暂时无法下载解析，请重新发送原视频或稍后再试。",
            }
        return {"ok": True, "message_id": "video-failure-notice"}

    result = await client.run_turn("旧摘要", "成员要求分析视频", "", "off", [], execute)

    assert calls == [
        ("Builtin_video_understanding", 0, {"file_id": "video-1"}),
        (
            "send_group_message",
            1,
            {"text": "这段视频暂时无法下载解析，请重新发送原视频或稍后再试。"},
        ),
    ]
    assert "本轮工具已执行，但生成最终摘要失败" in result.summary
    assert result.tool_results[-1]["result"]["ok"] is True
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_unknown_qq_send_failure_never_causes_another_automatic_send() -> None:
    client = ScriptedClient(
        [
            _chat_tool("send_group_message", {"text": "正在处理"}),
            _chat_text(None),
        ]
    )
    calls: list[str] = []

    async def execute(name: str, _: dict[str, Any], __: str, *___: Any) -> dict[str, Any]:
        calls.append(name)
        return {"ok": False, "error": "OneBot request timed out; QQ result unknown"}

    result = await client.run_turn(
        "旧摘要",
        "实时任务",
        "",
        "off",
        [],
        execute,
        direct_explicit_task_reply_required=True,
    )

    assert calls == ["send_group_message"]
    assert len(result.tool_results) == 1
    assert "本轮工具已执行，但生成最终摘要失败" in result.summary


def _task_message(text: str) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": "100",
        "message_id": "task-1",
        "time": 1,
        "user_id": "member",
        "sender": {"nickname": "成员"},
        "raw_message": text,
    }


def test_explicit_agent_task_marker_matches_video_request_but_not_normal_chat() -> None:
    task = normalise_onebot_event(_task_message("深度理解这个视频讲述的内容，生成文字稿markdown并渲染图片"))
    ordinary = normalise_onebot_event(_task_message("这个视频真好看"))

    assert task["content"]["live_explicit_agent_task_request"] is True
    assert ordinary["content"]["live_explicit_agent_task_request"] is False
    assert "实时明确任务标记" in AgentService._format_events([task])


class _ConnectedAdapter:
    connected = True

    async def call(self, *_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("missing local video should fail before OneBot action")


@pytest.mark.asyncio
async def test_video_tool_failure_is_service_marked_safe_for_finalization_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("100", "测试群")
    service.adapter = _ConnectedAdapter()  # type: ignore[assignment]
    turn_id = service.db.create_turn("100", [])

    result = await service._execute_tool(
        turn_id,
        "100",
        "Builtin_video_understanding",
        {"path": "missing.mp4"},
        "video-missing",
    )

    assert result["ok"] is False
    assert result["safe_to_notify_user"] is True
    assert "视频文件不存在" in result["error"]
    await service.stop()
