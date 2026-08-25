"""Loopback, CSRF, and local-dashboard smoke coverage."""

from __future__ import annotations

import re

import app.config as config
from fastapi.testclient import TestClient

from app.web import create_app


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_dashboard_is_loopback_guarded_and_forms_require_csrf(tmp_path, monkeypatch) -> None:
    # The application migrates a real user's legacy Credential Manager key on
    # first start.  Tests with a temporary data directory must not observe that
    # host-level state, or the first-run dashboard assertion becomes machine
    # dependent.
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/", headers={"host": "example.test"}).status_code == 403
        page = client.get("/")
        assert page.status_code == 200
        assert "首次配置向导" in page.text
        assert client.post("/settings", data={}).status_code == 403

        token = csrf_from(page.text)
        assert (
            client.post(
                "/settings",
                data={"csrf_token": token},
                headers={"origin": "https://evil.example"},
            ).status_code
            == 403
        )
        accepted = client.post(
            "/settings",
            data={"csrf_token": token},
            headers={"origin": "http://127.0.0.1:8765"},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        # Chromium extensions and opaque browser contexts can attach a
        # non-HTTP Origin to a same-machine form.  The CSRF token remains the
        # authorization check; only explicit off-loopback web origins reject.
        opaque_origin = client.post(
            "/settings",
            data={"csrf_token": token},
            headers={"origin": "null"},
            follow_redirects=False,
        )
        assert opaque_origin.status_code == 303
        assert client.get("/healthz").json()["ok"] is True


def test_group_toggle_uses_valid_csrf_and_keeps_unknown_groups_out(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        service.db.upsert_group("123", "可管理群")
        token = csrf_from(client.get("/").text)
        response = client.post(
            "/groups/123/toggle",
            data={
                "csrf_token": token,
                "enabled": "true",
                "prompt_override": "",
                "reasoning_effort": "inherit",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert service.db.get_group("123")["enabled"]
        unknown = client.post(
            "/groups/nope/toggle",
            data={"csrf_token": token, "enabled": "true"},
            follow_redirects=False,
        )
        assert unknown.status_code == 404


def test_dashboard_renders_group_and_turn_diagnostics(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        service.db.upsert_group("123", "诊断群")
        diagnostic = "模型调用失败\n阶段：初始摘要与工具决策\nHTTP 503 Service Unavailable"
        service.db.set_group_error("123", diagnostic)
        turn_id = service.db.create_turn("123", [])
        service.db.finish_turn(turn_id, "failed", error=diagnostic)

        page = client.get("/")

        assert page.status_code == 200
        assert "最近处理失败或告警" in page.text
        assert "处理失败详情" in page.text
        assert "阶段：初始摘要与工具决策" in page.text
        assert "HTTP 503 Service Unavailable" in page.text


def test_dashboard_renders_tool_request_and_return_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        turn_id = service.db.create_turn("123", [])
        service.db.add_tool_audit(
            turn_id,
            "123",
            "call-1",
            "Builtin_Websearch",
            {"query": "最新消息"},
            {"ok": True, "results": [{"title": "结果一", "url": "https://example.test"}]},
            "success",
        )

        page = client.get("/")

        assert page.status_code == 200
        assert "工具返回给 AI 的结果" in page.text
        assert "结果一" in page.text
        assert "最新消息" in page.text
