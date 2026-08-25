"""Dashboard coverage for evidence-backed, group-scoped memory controls."""

from __future__ import annotations

import app.config as config
from fastapi.testclient import TestClient

from app.web import create_app
from tests.test_web import csrf_from


def _memory(group_id: str = "123") -> dict:
    return {
        "id": "memory-1",
        "group_id": group_id,
        "kind": "preference",
        "statement": "陈梓仁明确偏好先给结论，再解释原因。",
        "confidence_status": "verified",
        "active": True,
        "updated_at": "2026-08-18T12:00:00+00:00",
        "evidence": [
            {
                "message_id": "msg-88",
                "quote": "以后回答我时先给结论，再解释原因",
                "sender_name": "陈梓仁",
                "created_at": "2026-08-18T11:59:00+00:00",
            }
        ],
    }


def test_dashboard_renders_group_memory_status_and_traceable_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        service.db.upsert_group("123", "记忆测试群")

        def list_memories(group_id: str, *, limit: int, include_inactive: bool) -> list[dict]:
            assert limit == 100
            assert include_inactive is True
            return [_memory(group_id)]

        monkeypatch.setattr(service, "list_group_memories", list_memories, raising=False)
        page = client.get("/")

        assert page.status_code == 200
        assert "群长期记忆" in page.text
        assert "记忆按群完全隔离" in page.text
        assert "不依赖向量模型" in page.text
        assert "陈梓仁明确偏好先给结论" in page.text
        assert "以后回答我时先给结论，再解释原因" in page.text
        assert "msg-88" in page.text
        assert "已人工确认" in page.text


def test_memory_moderation_is_csrf_protected_group_scoped_and_uses_prg(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        service.db.upsert_group("123", "来源群")
        service.db.upsert_group("456", "其他群")

        def list_memories(group_id: str, *, limit: int, include_inactive: bool) -> list[dict]:
            return [_memory("123")] if group_id == "123" else []

        calls = []

        async def moderate(
            group_id: str,
            memory_id: str,
            action: str,
            *,
            replacement_text: str,
            note: str,
        ) -> dict:
            calls.append((group_id, memory_id, action, replacement_text, note))
            return {"ok": True}

        monkeypatch.setattr(service, "list_group_memories", list_memories, raising=False)
        monkeypatch.setattr(service, "moderate_group_memory", moderate, raising=False)
        token = csrf_from(client.get("/").text)
        endpoint = "/groups/123/memories/memory-1/moderate"

        assert client.post(endpoint, data={"action": "confirm"}).status_code == 403
        response = client.post(
            endpoint,
            data={
                "csrf_token": token,
                "action": "correct",
                "replacement_text": "陈梓仁现在偏好先给完整代码。",
                "note": "本人在群内明确更正",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/#group-memory-123"
        assert calls == [
            (
                "123",
                "memory-1",
                "correct",
                "陈梓仁现在偏好先给完整代码。",
                "本人在群内明确更正",
            )
        ]

        # A valid id from group 123 cannot be smuggled through group 456's URL.
        cross_group = client.post(
            "/groups/456/memories/memory-1/moderate",
            data={"csrf_token": token, "action": "retract", "note": "wrong group"},
            follow_redirects=False,
        )
        assert cross_group.status_code == 404
        assert len(calls) == 1


def test_memory_delete_is_presented_as_soft_delete_and_empty_correction_is_rejected(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        service.db.upsert_group("123", "软删除群")
        monkeypatch.setattr(
            service,
            "list_group_memories",
            lambda group_id, *, limit, include_inactive: [_memory(group_id)],
            raising=False,
        )
        calls = []
        monkeypatch.setattr(
            service,
            "moderate_group_memory",
            lambda *args, **kwargs: calls.append((args, kwargs)),
            raising=False,
        )

        page = client.get("/")
        assert "软删除：隐藏使用但保留审计" in page.text
        assert "不会物理抹去记录" in page.text
        token = csrf_from(page.text)
        response = client.post(
            "/groups/123/memories/memory-1/moderate",
            data={"csrf_token": token, "action": "correct", "replacement_text": "   "},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert calls == []
        assert "更正后的内容不能为空" in client.get("/").text


def test_group_memory_reset_is_csrf_protected_and_group_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        service.db.upsert_group("123", "重算群")
        service.db.upsert_group("456", "其他群")
        calls = []

        async def reset(group_id: str) -> dict:
            calls.append(group_id)
            return {"scheduled": True}

        monkeypatch.setattr(service, "reset_group_memory", reset, raising=False)
        page = client.get("/")
        assert "清空本群记忆与规则并重算历史" in page.text
        token = csrf_from(page.text)

        assert client.post("/groups/123/reset-memory", data={}).status_code == 403
        assert client.post(
            "/groups/123/reset-memory",
            data={"csrf_token": token, "confirm_reset": "no"},
        ).status_code == 400
        response = client.post(
            "/groups/123/reset-memory",
            data={"csrf_token": token, "confirm_reset": "reset"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/#group-memory-123"
        assert calls == ["123"]

        cross_group = client.post(
            "/groups/999/reset-memory",
            data={"csrf_token": token, "confirm_reset": "reset"},
            follow_redirects=False,
        )
        assert cross_group.status_code == 404
