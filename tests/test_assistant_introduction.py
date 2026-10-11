"""The introduction tool exposes a document, not the settings database."""

from pathlib import Path

import pytest

from app.llm import TOOLS
from app.service import AgentService


class MemorySecrets:
    def get_llm_api_key(self):
        return "private-test-key"

    def get_onebot_token(self):
        return "private-onebot-token"


def test_introduction_tool_schema():
    tools = [tool["function"] for tool in TOOLS if tool["function"]["name"] == "Builtin_settings"]
    assert len(tools) == 1
    assert tools[0]["parameters"]["properties"] == {}
    assert tools[0]["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["100", "private:42"])
async def test_introduction_dispatch_is_read_only_and_audited(tmp_path, group_id):
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    try:
        service.db.upsert_group(group_id, "当前会话")
        turn = service.db.create_turn(group_id, [])
        assert not service.onebot_connected
        result = await service._execute_tool(turn, group_id, "Builtin_settings", {}, "intro")
        assert result["ok"] is True
        assert result["read_only"] is True
        assert result["text"] == Path("app/assistant_introduction.md").read_text(encoding="utf-8").strip()
        assert "private-test-key" not in str(result)
        assert "private-onebot-token" not in str(result)
        audit = service.db.get_tool_audit(turn, "intro")
        assert audit["status"] == "success"
        assert await service._execute_tool(turn, group_id, "Builtin_settings", {}, "intro") == result
    finally:
        service.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"path": "../settings.json"}, {"action": "write"}, None])
async def test_introduction_rejects_parameters(tmp_path, arguments):
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    try:
        result = await service._execute_read_only_tool("100", "Builtin_settings", arguments)
        assert result["ok"] is False
        assert "text" not in result
    finally:
        service.db.close()


@pytest.mark.asyncio
async def test_introduction_reports_missing_document(tmp_path, monkeypatch):
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == "assistant_introduction.md":
            raise FileNotFoundError("介绍文档不存在")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    try:
        result = await service._execute_read_only_tool("100", "Builtin_settings", {})
        assert result["ok"] is False
        assert "介绍文档不存在" in result["error"]
    finally:
        service.db.close()
