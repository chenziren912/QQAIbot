"""Built-in loopback terminal command coverage."""

from __future__ import annotations

import app.config as config
from fastapi.testclient import TestClient

from app.terminal import help_text, parse_command
from app.web import create_app


def test_terminal_parser_and_help_are_chinese() -> None:
    parsed = parse_command('/reset-memory 123 reset')
    assert parsed.name == "reset-memory"
    assert parsed.args == ("123", "reset")
    text = help_text()
    assert "/status" in text
    assert "/reset-memory <会话ID> reset" in text
    assert "未知命令" not in text


def test_terminal_endpoint_dispatches_bounded_commands(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        service.db.upsert_group("123", "测试群")

        assert client.post("/terminal/command", json={"command": "/status"}).status_code == 403
        response = client.post(
            "/terminal/command",
            headers={"X-QQAI-Terminal": "1"},
            json={"command": "/groups"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert "测试群" in payload["output"]

        help_response = client.get("/terminal/help")
        assert help_response.status_code == 200
        assert "/status" in help_response.json()["help"]

        unknown = client.post(
            "/terminal/command",
            headers={"X-QQAI-Terminal": "1"},
            json={"command": "/不存在"},
        )
        assert unknown.status_code == 200
        assert unknown.json()["ok"] is False
        assert "未知命令" in unknown.json()["error"]


def test_terminal_reset_requires_confirmation_and_rewinds_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        service.db.upsert_group("456", "重算群")
        service.db.save_summary("456", "旧摘要", 1)
        blocked = client.post(
            "/terminal/command",
            headers={"X-QQAI-Terminal": "1"},
            json={"command": "/reset-memory 456"},
        )
        assert blocked.json()["ok"] is False
        assert "reset" in blocked.json()["error"]

        completed = client.post(
            "/terminal/command",
            headers={"X-QQAI-Terminal": "1"},
            json={"command": "/reset-memory 456 reset"},
        )
        assert completed.json()["ok"] is True
        assert "旧摘要" not in service.db.get_summary("456")

