import json

import pytest

from app.llm import TOOLS, parse_tool_calls, parse_responses_tool_calls, _direct_visible_reply_tools
from app.tool_facade import model_tools, normalise_call, prepare_model_request, REVERSE


def test_model_only_sees_18_tools_and_no_legacy_duplicates():
    functions = [t["function"] for t in model_tools(TOOLS)]
    names = {t["name"] for t in functions}
    assert len(names) == 18
    assert not names.intersection(REVERSE)
    assert {"Builtin_send", "Builtin_video_download", "Builtin_video_generation"} <= names


@pytest.mark.parametrize("public,args,legacy,expected", [
    ("Builtin_send", {"type": "text", "text": "你好"}, "send_group_message", {"text": "你好"}),
    ("Builtin_send", {"type": "markdown", "markdown": "# 标题"}, "Builtin_render_markdown_image", {"markdown": "# 标题"}),
    ("Builtin_send", {"type": "file", "path": "video.mp4"}, "send_group_file", {"path": "video.mp4"}),
    ("Builtin_video_download", {"action": "download", "channel": "youtube", "query": "猫"}, "Builtin_youtube_download", {"query": "猫"}),
    ("Builtin_video_download", {"action": "download", "channel": "bilibili", "url": "https://bilibili.com"}, "Builtin_bilibili_download", {"url": "https://bilibili.com"}),
    ("Builtin_video_download", {"action": "confirm", "confirmation_id": "abc", "decision": "approve"}, "Builtin_video_download_confirmation", {"confirmation_id": "abc", "decision": "approve"}),
    ("Builtin_video_generation", {"action": "create", "prompt": "猫"}, "Builtin_video_generation_create", {"prompt": "猫"}),
    ("Builtin_video_generation", {"action": "list"}, "Builtin_video_generation_list", {}),
    ("Builtin_video_generation", {"action": "result", "job_id": "123"}, "Builtin_video_generation_result", {"job_id": "123"}),
])
def test_routes_in_both_protocols(public, args, legacy, expected):
    assert normalise_call(public, args, TOOLS) == (legacy, expected)
    call = {"id": "call1", "function": {"name": public, "arguments": json.dumps(args)}}
    chat = parse_tool_calls({"tool_calls": [call]})[0]
    responses = parse_responses_tool_calls({"output": [{"type": "function_call", "call_id": "call1", **call["function"]}]})[0]
    for result in [chat, responses]:
        assert result.name == legacy
        assert result.arguments == expected
        assert result.call_id == "call1"


@pytest.mark.parametrize("name,args", [
    ("Builtin_send", {"type": "file", "text": "不是文件"}),
    ("Builtin_send", {"type": "text", "text": "hi", "path": "secret"}),
    ("Builtin_send", {"type": "unknown"}),
    ("Builtin_video_download", {"action": "download", "url": "https://youtube.com"}),
    ("Builtin_video_download", {"action": "download", "channel": []}),
    ("Builtin_video_download", {"action": "download", "channel": {}}),
    ("Builtin_video_download", {"action": "confirm", "confirmation_id": "abc", "decision": "approve", "url": "https://youtube.com"}),
    ("Builtin_video_generation", {"action": "list", "prompt": "猫"}),
    ("Builtin_video_generation", {"action": "result"}),
])
def test_rejects_missing_and_cross_action_parameters(name, args):
    with pytest.raises(ValueError):
        normalise_call(name, args, TOOLS)


def test_recovery_schema_cannot_expose_file_sending():
    functions = model_tools(_direct_visible_reply_tools())
    assert len(functions) == 1
    schema = functions[0]["function"]["parameters"]
    assert schema["properties"]["type"]["enum"] == ["text", "markdown"]
    assert "path" not in schema["properties"]


def test_wire_rewrites_forced_choice_and_history_without_changing_user_text():
    original = {"tools": TOOLS, "tool_choice": {"type": "function", "function": {"name": "send_group_message"}},
        "messages": [{"role": "developer", "content": "使用 send_group_message"},
                     {"role": "user", "content": "send_group_message 不要改我的原文"},
                     {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "send_group_message", "arguments": '{"text":"hi"}'}}]}]}
    body = prepare_model_request(original)
    assert body["tool_choice"]["function"]["name"] == "Builtin_send"
    assert 'Builtin_send(type="text")' in body["messages"][0]["content"]
    assert body["messages"][1] == original["messages"][1]
    call = body["messages"][2]["tool_calls"][0]
    assert call["id"] == "1"
    assert json.loads(call["function"]["arguments"])["type"] == "text"
    assert original["tool_choice"]["function"]["name"] == "send_group_message"


def test_responses_wire_and_idempotence():
    body = prepare_model_request({"tools": [{"type": "function", **t["function"]} for t in TOOLS],
        "instructions": "用 Builtin_video_generation_list",
        "input": [{"type": "function_call", "call_id": "id1", "name": "Builtin_video_generation_list", "arguments": "{}"}]})
    assert len(body["tools"]) == 18
    assert body["input"][0]["name"] == "Builtin_video_generation"
    assert json.loads(body["input"][0]["arguments"])["action"] == "list"
    assert prepare_model_request(body) == body


@pytest.mark.parametrize("arguments", ["{bad json", "null", "[]", None])
def test_malformed_legacy_history_does_not_break_error_recovery(arguments):
    body = prepare_model_request({"messages": [{"role": "assistant", "tool_calls": [{
        "id": "bad", "function": {"name": "send_group_message", "arguments": arguments}}]}]})
    function = body["messages"][0]["tool_calls"][0]["function"]
    assert function["name"] == "Builtin_send"
    assert function["arguments"] == arguments


@pytest.mark.asyncio
async def test_service_public_routes_validate_before_any_side_effect(tmp_path):
    from app.service import AgentService

    service = AgentService(tmp_path)
    try:
        service.db.upsert_group("private:42", "当前私聊")
        turn = service.db.create_turn("private:42", [])
        result = await service._execute_tool(turn, "private:42", "Builtin_video_download",
            {"action": "download", "channel": []}, "invalid")
        assert result["ok"] is False and result["retry_safe"] is True
        assert service.db.get_tool_audit(turn, "invalid")["status"] == "failed"
        result = await service._execute_tool(turn, "private:42", "Builtin_video_generation",
            {"action": "list"}, "list")
        assert result["ok"] is True and result["tasks"] == []
        assert result["current_conversation_only"] is True
    finally:
        service.db.close()


@pytest.mark.asyncio
async def test_public_send_reuses_legacy_deduplication(tmp_path):
    from app.service import AgentService

    class Adapter:
        connected = True
        connection_id = 1
        calls = []

        async def call(self, action, params):
            self.calls.append((action, params))
            return {"data": {"message_id": "sent1"}}

    service = AgentService(tmp_path)
    adapter = Adapter()
    service.adapter = adapter
    try:
        service.db.upsert_group("100", "当前群")
        turn = service.db.create_turn("100", [])
        first = await service._execute_tool(turn, "100", "Builtin_send", {"type": "text", "text": "你好"}, "public")
        assert first["ok"] is True
        await service._execute_tool(turn, "100", "send_group_message", {"text": "你好"}, "legacy")
        assert len(adapter.calls) == 1
        assert adapter.calls[0][0] == "send_group_msg"
    finally:
        service.db.close()


@pytest.mark.asyncio
async def test_actual_http_boundary_exposes_merged_tools(monkeypatch):
    import httpx
    from app.config import LLMSettings
    from app.llm import ChatCompletionsClient

    requests = []

    class HttpClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            requests.append(kwargs["json"])
            return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": {"content": "完成"}}]})

    monkeypatch.setattr("app.llm.httpx.AsyncClient", HttpClient)
    client = ChatCompletionsClient(LLMSettings(base_url="http://localhost:8045/v1", model="test"), "test-key")
    await client._post({"model": "test", "messages": [{"role": "user", "content": "hi"}], "tools": TOOLS})
    assert len(requests[0]["tools"]) == 18
    assert not {t["function"]["name"] for t in requests[0]["tools"]}.intersection(REVERSE)
