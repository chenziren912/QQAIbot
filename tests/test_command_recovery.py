"""Regression coverage for non-terminal local command diagnostics."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.config import LLMSettings
from app.llm import ChatCompletionsClient
from app.service import AgentService


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


def _tool_decision(mode: str, calls: list[tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
    if mode == "responses":
        return {
            "output": [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                }
                for call_id, name, arguments in calls
            ]
        }
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
                        for call_id, name, arguments in calls
                    ],
                }
            }
        ]
    }


def _text_decision(mode: str, text: str) -> dict[str, Any]:
    if mode == "responses":
        return {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        }
    return {"choices": [{"message": {"content": text}}]}


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


def _failed_command_result() -> dict[str, Any]:
    return {
        "ok": False,
        "returncode": 1,
        "command": "python -c \"import matplotlib\"",
        "output": "Traceback (most recent call last):\nModuleNotFoundError: No module named 'matplotlib'\n",
        "truncated": False,
        "error": "命令返回非零退出码",
        "agent_continue": True,
        "failure_kind": "command_nonzero_exit",
        "recovery": "请阅读 output 后继续诊断；不得自动安装软件包。",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completions", "responses"])
async def test_failed_command_does_not_block_later_tool_in_same_decision(mode: str) -> None:
    client = ScriptedClient(
        mode,
        [
            _tool_decision(
                mode,
                [
                    ("command", "execute_command", {"command": "python -c \"import matplotlib\""}),
                    ("write", "write_workspace_file", {"path": "diagnosis.txt", "content": "missing matplotlib"}),
                ],
            ),
            _text_decision(mode, "已记录依赖缺失，等待用户决定是否安装。"),
        ],
    )
    executions: list[str] = []

    async def execute(name: str, _: dict[str, Any], __: str, ___: int) -> dict[str, Any]:
        executions.append(name)
        if name == "execute_command":
            return _failed_command_result()
        return {"ok": True, "path": "diagnosis.txt", "bytes": 18}

    result = await client.run_turn("旧摘要", "群消息", "", "off", [], execute)

    assert result.summary == "已记录依赖缺失，等待用户决定是否安装。"
    assert executions == ["execute_command", "write_workspace_file"]
    assert result.tool_results[0]["result"]["agent_continue"] is True
    assert result.tool_results[1]["result"]["ok"] is True
    # The following agent decision receives the entire command traceback and
    # the trusted recovery rule rather than being forced into finalization.
    assert "ModuleNotFoundError" in _tool_outputs(client.requests[1], mode)[0]["output"]
    if mode == "responses":
        assert "命令恢复规则" in client.requests[1]["instructions"]
    else:
        assert any(
            item.get("role") == "developer" and "命令恢复规则" in str(item.get("content"))
            for item in client.requests[1]["messages"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completions", "responses"])
async def test_failed_command_opens_next_agent_decision_for_recovery(mode: str) -> None:
    client = ScriptedClient(
        mode,
        [
            _tool_decision(mode, [("command", "execute_command", {"command": "python -c \"import matplotlib\""})]),
            _tool_decision(mode, [("inspect", "list_workspace_files", {"path": "."})]),
            _text_decision(mode, "已查看工作目录；缺少 matplotlib，未自动安装。"),
        ],
    )
    executions: list[str] = []

    async def execute(name: str, _: dict[str, Any], __: str, ___: int) -> dict[str, Any]:
        executions.append(name)
        if name == "execute_command":
            return _failed_command_result()
        return {"ok": True, "files": []}

    result = await client.run_turn("旧摘要", "群消息", "", "off", [], execute)

    assert result.summary == "已查看工作目录；缺少 matplotlib，未自动安装。"
    assert executions == ["execute_command", "list_workspace_files"]
    # Initial request + recovery tool decision + ordinary no-tools conclusion.
    assert [request["tool_choice"] for request in client.requests] == ["auto", "auto", "auto"]
    assert "ModuleNotFoundError" in _tool_outputs(client.requests[1], mode)[0]["output"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completions", "responses"])
async def test_blank_recovery_response_keeps_agent_loop_alive(mode: str) -> None:
    """An empty relay response after a recoverable command must not end the task."""

    if mode == "responses":
        blank = {"output": [{"type": "message", "role": "assistant", "content": []}]}
    else:
        blank = {"choices": [{"message": {"content": None}}]}
    client = ScriptedClient(
        mode,
        [
            _tool_decision(mode, [("command", "execute_command", {"command": "yt-dlp old-command"})]),
            blank,
            _tool_decision(mode, [("music", "Builtin_music_download", {"url": "https://music.example/track"})]),
            _text_decision(mode, "已改用音乐下载工具并完成处理。"),
        ],
    )
    executions: list[str] = []

    async def execute(name: str, _: dict[str, Any], __: str, ___: int) -> dict[str, Any]:
        executions.append(name)
        if name == "execute_command":
            return _failed_command_result()
        if name == "Builtin_music_download":
            return {"ok": True, "message_id": "voice-1"}
        raise AssertionError(name)

    result = await client.run_turn("旧摘要", "下载音乐", "", "off", [], execute)

    assert result.summary == "已改用音乐下载工具并完成处理。"
    assert executions == ["execute_command", "Builtin_music_download"]
    assert len(client.requests) == 4
    recovery_request = client.requests[2]
    if mode == "responses":
        assert "空回复恢复规则" in recovery_request["instructions"]
    else:
        assert any(
            item.get("role") == "developer" and "空回复恢复规则" in str(item.get("content"))
            for item in recovery_request["messages"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completions", "responses"])
async def test_empty_final_answer_keeps_repairing_until_agent_returns_text(mode: str) -> None:
    """An empty finalization must not strand a successful tool task."""

    blank = (
        {"output": [{"type": "message", "role": "assistant", "content": []}]} 
        if mode == "responses"
        else {"choices": [{"message": {"content": None}}]}
    )
    script = [
        _tool_decision(mode, [("search", "Builtin_Websearch", {"query": "Sharks official MV"})]),
        blank,
        blank,
        blank,
        blank,
        _text_decision(mode, "已找到视频并完成处理。"),
    ]
    client = ScriptedClient(mode, script)

    async def execute(name: str, _: dict[str, Any], __: str, ___: int) -> dict[str, Any]:
        assert name == "Builtin_Websearch"
        return {"ok": True, "results": []}

    result = await client.run_turn("旧摘要", "下载 Sharks official MV", "", "off", [], execute)

    assert result.summary == "已找到视频并完成处理。"
    assert len(client.requests) == 6
    assert "空回复恢复规则" in (
        client.requests[-1]["instructions"]
        if mode == "responses"
        else str(client.requests[-1]["messages"])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completions", "responses"])
async def test_stateful_failure_still_blocks_command_after_it(mode: str) -> None:
    client = ScriptedClient(
        mode,
        [
            _tool_decision(
                mode,
                [
                    ("qq-failed", "send_group_message", {"text": "可能已发送"}),
                    ("must-skip", "execute_command", {"command": "python -c \"print('no')\""}),
                ],
            ),
            _text_decision(mode, "QQ 发送结果未知，未继续执行命令。"),
        ],
    )
    executions: list[str] = []

    async def execute(name: str, _: dict[str, Any], __: str, ___: int) -> dict[str, Any]:
        executions.append(name)
        return {"ok": False, "error": "OneBot request timed out; result unknown"}

    result = await client.run_turn("旧摘要", "群消息", "", "off", [], execute)

    assert result.summary == "QQ 发送结果未知，未继续执行命令。"
    assert executions == ["send_group_message"]
    assert result.tool_results[1]["result"]["skipped"] is True
    # The error and skipped command are fed back to a bounded Agent recovery
    # turn, but the ambiguous send action is removed so it cannot be repeated.
    assert [request["tool_choice"] for request in client.requests] == ["auto", "auto"]
    assert _tool_outputs(client.requests[1], mode)[0]["error"] == "OneBot request timed out; result unknown"
    if mode == "responses":
        recovery_names = {tool["name"] for tool in client.requests[1]["tools"]}
        assert "工具错误恢复规则" in client.requests[1]["instructions"]
    else:
        recovery_names = {tool["function"]["name"] for tool in client.requests[1]["tools"]}
        assert "工具错误恢复规则" in client.requests[1]["messages"][-1]["content"]
    assert "send_group_message" not in recovery_names


@pytest.mark.asyncio
async def test_workspace_nonzero_command_returns_full_recoverable_diagnostic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(tmp_path / "Workspace"))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "测试群")
    turn_id = service.db.create_turn("123", [])

    result = await service._execute_tool(
        turn_id,
        "123",
        "execute_command",
        {"command": "python -c \"import definitely_missing_qq_agent_package\""},
        "nonzero-command",
    )

    assert result["ok"] is False
    assert result["agent_continue"] is True
    assert result["failure_kind"] == "command_nonzero_exit"
    assert result["returncode"] != 0
    assert "ModuleNotFoundError" in result["output"]
    assert "缺少依赖" in result["recovery"]


@pytest.mark.asyncio
async def test_music_shell_search_is_routed_to_music_tool(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(tmp_path / "Workspace"))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "测试群")
    turn_id = service.db.create_turn("123", [])

    result = await service._execute_tool(
        turn_id,
        "123",
        "execute_command",
        {"command": 'yt-dlp "bilisearch3:Alan Walker Alone" -x --audio-format mp3'},
        "music-shell",
    )

    assert result["ok"] is False
    assert result["retry_safe"] is True
    assert result["required_tool"] == "Builtin_music_download"
    assert "未执行" in result["error"]
    await service.stop()


@pytest.mark.asyncio
async def test_youtube_shell_search_is_routed_to_youtube_tool(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QQ_AI_WORKSPACE_ROOT", str(tmp_path / "Workspace"))
    service = AgentService(tmp_path / "data")
    service.db.upsert_group("123", "测试群")
    turn_id = service.db.create_turn("123", [])

    result = await service._execute_tool(
        turn_id,
        "123",
        "execute_command",
        {"command": 'yt-dlp "ytsearch1:Sharks official MV 144p" -f "bv*[height<=144]+ba"'},
        "youtube-shell",
    )

    assert result["ok"] is False
    assert result["retry_safe"] is True
    assert result["required_tool"] == "Builtin_youtube_download"
    assert "未执行" in result["error"]
    await service.stop()
