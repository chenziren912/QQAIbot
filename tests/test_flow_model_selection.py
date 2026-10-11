"""Browser model isolation checks; no Chromium or real provider calls."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from app.config import SecretStore


def bridge_module():
    source = Path(__file__).resolve().parents[1] / "scripts" / "flow-ai-model.py"
    spec = importlib.util.spec_from_file_location("flow_model_selection_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_settings(data_dir, **overrides):
    llm = {
        "base_url": "http://127.0.0.1:8045/v1", "endpoint_mode": "completions",
        "model": "gemini-3.8-flash-tiered", "vision_enabled": True,
        "send_reasoning_effort": True, "global_reasoning_effort": "xhigh",
    }
    llm.update(overrides)
    values = {"llm": llm}
    with sqlite3.connect(data_dir / "agent.sqlite3") as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO settings VALUES (?, ?)", ("app_settings", json.dumps(values)))
    return values


@pytest.mark.parametrize("main_model", ["gemini-3.8-flash-tiered", "gemini-3.8-flash-high", "some-future-reply-model"])
def test_browser_always_uses_lite_without_changing_main_settings(tmp_path, monkeypatch, main_model):
    module = bridge_module()
    original = make_settings(tmp_path, model=main_model)
    monkeypatch.setattr(SecretStore, "get_llm_api_key", lambda self: "private-test-key")
    client = module.read_client(tmp_path)
    assert client.settings.model == "gemini-3.5-flash-lite"
    assert client.settings.base_url == original["llm"]["base_url"]
    assert client.settings.endpoint_mode == "completions"
    assert client.settings.vision_enabled is True
    assert client.settings.send_reasoning_effort is False
    assert client.settings.global_reasoning_effort == "off"
    assert client.api_key == "private-test-key"
    with sqlite3.connect(tmp_path / "agent.sqlite3") as conn:
        persisted = json.loads(conn.execute("SELECT value FROM settings WHERE key='app_settings'").fetchone()[0])
    assert persisted == original


@pytest.mark.parametrize("mode", ["base", "responses"])
def test_browser_retains_endpoint_protocol(tmp_path, monkeypatch, mode):
    module = bridge_module()
    make_settings(tmp_path, endpoint_mode=mode)
    monkeypatch.setattr(SecretStore, "get_llm_api_key", lambda self: "private-test-key")
    assert module.read_client(tmp_path).settings.endpoint_mode == mode


def test_browser_still_requires_vision_and_credentials(tmp_path, monkeypatch):
    module = bridge_module()
    make_settings(tmp_path, vision_enabled=False)
    monkeypatch.setattr(SecretStore, "get_llm_api_key", lambda self: "private-test-key")
    with pytest.raises(ValueError, match="视觉"):
        module.read_client(tmp_path)
    monkeypatch.setattr(SecretStore, "get_llm_api_key", lambda self: None)
    with pytest.raises(ValueError, match="API Key"):
        module.read_client(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completions", "responses"])
async def test_decisions_report_browser_model_not_main_and_keep_tools_and_images(tmp_path, monkeypatch, mode):
    module = bridge_module()
    make_settings(tmp_path, endpoint_mode=mode)
    monkeypatch.setattr(SecretStore, "get_llm_api_key", lambda self: "private-test-key")
    client = module.read_client(tmp_path)
    decision = {"action": "wait", "reason": "mock only"}
    seen = []
    async def completion(**kwargs):
        seen.append(kwargs)
        return {"choices": [{"message": {"tool_calls": [{"function": {"name": "flow_browser_action", "arguments": json.dumps(decision)}}]}}]}, ""
    async def responses(**kwargs):
        seen.append(kwargs)
        return {"output": [{"type": "function_call", "name": "flow_browser_action", "arguments": json.dumps(decision)}]}, ""
    monkeypatch.setattr(client, "_create_completion", completion)
    monkeypatch.setattr(client, "_create_responses_completion", responses)
    result = await module.decide({"phase": "review", "screenshot": "data:image/jpeg;base64,mock"}, client)
    assert result["model"] == "gemini-3.5-flash-lite"
    assert result["decision"] == decision
    assert "private-test-key" not in json.dumps(result)
    assert seen[0]["contains_images"] is True
    assert seen[0]["reasoning_effort"] == "off"
    assert seen[0]["tool_choice"]["function"]["name"] == "flow_browser_action"
