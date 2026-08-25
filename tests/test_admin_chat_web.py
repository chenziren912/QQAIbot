"""Focused control-plane coverage for the local administrator AI chat."""

from __future__ import annotations

import app.config as config
from fastapi.testclient import TestClient

from app.web import MAX_ADMIN_CHAT_MESSAGE_CHARS, create_app
from tests.test_web import csrf_from


def _install_admin_chat_doubles(monkeypatch, service):
    sent: list[str] = []

    async def fake_admin_chat(message: str) -> dict:
        sent.append(message)
        return {"assistant_text": "已收到", "warning": "记忆工具未调用"}

    def fake_history(limit: int = 40) -> list[dict]:
        assert limit == 40
        return [
            {
                "role": "user",
                "content": "以后回答先给结论。",
                "created_at": "2026-08-18T00:00:00+00:00",
            },
            {
                "role": "tool",
                "content": "已更新长期记忆。",
                "tool_name": "write_rules",
                "tool_result": {"ok": True, "path": "rules.md"},
                "created_at": "2026-08-18T00:00:01+00:00",
            },
        ]

    monkeypatch.setattr(service, "admin_chat", fake_admin_chat, raising=False)
    monkeypatch.setattr(service, "list_admin_messages", fake_history, raising=False)
    monkeypatch.setattr(service, "rules_text", lambda: "- 回答算法问题时先给结论", raising=False)
    monkeypatch.setattr(service, "rules_path_label", "data/rules.md", raising=False)
    return sent


def test_admin_chat_panel_renders_history_rules_and_tool_audit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        _install_admin_chat_doubles(monkeypatch, app.state.service)

        page = client.get("/")

        assert page.status_code == 200
        assert "与 AI 对话" in page.text
        assert "不会发送到 QQ 群" in page.text
        assert "以后回答先给结论。" in page.text
        assert "工具结果：write_rules" in page.text
        assert '"ok": true' in page.text
        assert "data/rules.md" in page.text
        assert "回答算法问题时先给结论" in page.text


def test_admin_chat_post_is_csrf_protected_validated_and_prg(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        sent = _install_admin_chat_doubles(monkeypatch, app.state.service)
        page = client.get("/")
        token = csrf_from(page.text)

        assert client.post("/admin/chat", data={"message": "未经验证"}).status_code == 403
        assert (
            client.post(
                "/admin/chat",
                data={"csrf_token": token, "message": "跨站请求"},
                headers={"origin": "https://evil.example"},
            ).status_code
            == 403
        )

        blank = client.post(
            "/admin/chat",
            data={"csrf_token": token, "message": " \n\t "},
            follow_redirects=False,
        )
        assert blank.status_code == 303
        assert blank.headers["location"] == "/#admin-chat"
        assert sent == []
        assert "管理员对话未发送：内容不能为空。" in client.get("/").text

        too_long = client.post(
            "/admin/chat",
            data={"csrf_token": token, "message": "x" * (MAX_ADMIN_CHAT_MESSAGE_CHARS + 1)},
            follow_redirects=False,
        )
        assert too_long.status_code == 303
        assert sent == []
        assert "单次内容最多 12,000 个字符。" in client.get("/").text

        accepted = client.post(
            "/admin/chat",
            data={"csrf_token": token, "message": "请评估是否应写入长期记忆。"},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert accepted.headers["location"] == "/#admin-chat"
        assert sent == ["请评估是否应写入长期记忆。"]
        assert "管理员对话告警：记忆工具未调用" in client.get("/").text
