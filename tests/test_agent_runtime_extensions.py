"""Coverage for raw-window retention, agent tools, image refresh and progress."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.config import LLMSettings
from app.db import Database
from app.llm import ChatCompletionsClient, LLMResult
from app.media import MediaError
from app.service import AgentService, normalise_onebot_event


class MemorySecrets:
    def __init__(self) -> None:
        self.api_key = "test-key"
        self.onebot_token = ""

    def get_llm_api_key(self) -> str:
        return self.api_key

    def set_llm_api_key(self, value: str) -> None:
        self.api_key = value

    def get_onebot_token(self) -> str:
        return self.onebot_token

    def set_onebot_token(self, value: str) -> None:
        self.onebot_token = value


def _message(message_id: str, text: str, timestamp: int, *, group_id: str = "100") -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "message_id": message_id,
        "time": timestamp,
        "user_id": "member",
        "sender": {"nickname": "成员"},
        "raw_message": text,
    }


def test_existing_database_migrates_archive_cursor_before_creating_index(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    connection = sqlite3.connect(str(path))
    connection.execute(
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL UNIQUE,
            group_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            sub_type TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            occurred_at INTEGER NOT NULL DEFAULT 0,
            sender_id TEXT NOT NULL DEFAULT '',
            sender_name TEXT NOT NULL DEFAULT '',
            self_id TEXT NOT NULL DEFAULT '',
            normalized_text TEXT NOT NULL DEFAULT '',
            content_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL DEFAULT '{}',
            is_self INTEGER NOT NULL DEFAULT 0,
            pending INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        "INSERT INTO events(dedupe_key, group_id, event_type, pending, created_at) VALUES ('old', '100', 'message.group', 0, 'now')"
    )
    connection.commit()
    connection.close()

    db = Database(path)
    try:
        columns = {row["name"] for row in db._connection.execute("PRAGMA table_info(events)").fetchall()}
        assert "archived" in columns
        row = db._connection.execute("SELECT archived FROM events WHERE dedupe_key = 'old'").fetchone()
        assert row["archived"] == 1
        index_names = {row["name"] for row in db._connection.execute("PRAGMA index_list(events)").fetchall()}
        assert "idx_events_archive" in index_names
    finally:
        db.close()


@pytest.mark.asyncio
async def test_latest_raw_window_stays_verbatim_while_only_older_event_is_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []

    class CapturingClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_turn(self, **kwargs: Any) -> LLMResult:
            captured.append(kwargs)
            return LLMResult("已经归档的旧上下文", [])

    monkeypatch.setattr("app.service.ChatCompletionsClient", CapturingClient)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    old_id = service.db.insert_event(normalise_onebot_event(_message("old", "旧" * 30_000, 1)))
    new_id = service.db.insert_event(normalise_onebot_event(_message("new", "新" * 30_000, 2)))
    assert old_id and new_id
    try:
        await service._run_group_worker("100")
        # Pending-event batching is still capped at 50K, so the two live
        # events require two agent decisions.  Both decisions retain the same
        # newest verbatim raw window; only the first archives the old record.
        assert len(captured) == 2
        assert "旧" * 100 in captured[0]["event_text"]
        assert "新" * 100 in captured[0]["recent_context_text"]
        assert "旧" * 100 not in captured[0]["recent_context_text"]
        assert "没有新的旧消息需要写入滚动摘要" in captured[1]["event_text"]
        unarchived_ids = [item["id"] for item in service.db.unarchived_events("100")]
        assert unarchived_ids == [new_id]
        assert service.db.get_summary("100") == "已经归档的旧上下文"
        assert service.db.pending_events("100") == []
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_read_only_tools_are_group_scoped_and_do_not_need_onebot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    event_id = service.db.insert_event(normalise_onebot_event(_message("g1", "Tarjan 的根节点", 1)))
    assert event_id
    service.db.insert_event(normalise_onebot_event(_message("g2", "其他群的 Tarjan", 1, group_id="200")))
    turn_id = service.db.create_turn("100", [event_id])

    async def fake_search(query: Any, *, max_results: Any = 5) -> dict[str, Any]:
        return {"ok": True, "query": query, "results": [{"title": "结果", "url": "https://example.com"}]}

    async def fake_fetch(url: Any, *, max_chars: Any = 12_000) -> dict[str, Any]:
        return {"ok": True, "url": url, "text": "网页正文"}

    monkeypatch.setattr("app.service.google_search", fake_search)
    monkeypatch.setattr("app.service.fetch_link", fake_fetch)
    try:
        history = await service._execute_tool(turn_id, "100", "Builtin_querymessage", {"query": "Tarjan"}, "q")
        search = await service._execute_tool(turn_id, "100", "Builtin_Websearch", {"query": "Tarjan"}, "s")
        fetch = await service._execute_tool(turn_id, "100", "Builtin_patch", {"url": "https://example.com"}, "p")
        assert history["ok"] is True
        assert [item["text"] for item in history["matches"]] == ["Tarjan 的根节点"]
        assert search["results"][0]["title"] == "结果"
        assert fetch["text"] == "网页正文"
        assert [audit["tool_name"] for audit in service.db.list_recent_audits(3)] == [
            "Builtin_patch",
            "Builtin_Websearch",
            "Builtin_querymessage",
        ]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_expired_image_uses_authenticated_get_image_cache_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A valid 1x1 transparent PNG.  It mimics the file returned by NapCat's
    # get_image cache action after a QQ rkey CDN URL has expired.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360f8cfc0000004010100b5d4a3b10000000049454e44ae426082"
    )
    cache = tmp_path / "napcat-cache.png"
    cache.write_bytes(png)

    class ImageAdapter:
        connected = True

        async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
            assert action == "get_image"
            assert params == {"file": "image-token"}
            return {"data": {"file": str(cache)}}

    service = AgentService(tmp_path / "data", secret_store=MemorySecrets())
    service.adapter = ImageAdapter()  # type: ignore[assignment]
    raw = _message("image", "", 1)
    raw["message"] = [
        {
            "type": "image",
            "data": {"url": "https://gchat.qpic.cn/download?expired=1", "file": "image-token"},
        }
    ]
    event = normalise_onebot_event(raw)
    event_id = service.db.insert_event(event)
    assert event_id
    event["id"] = event_id

    async def expired_download(*_: Any, **__: Any) -> Any:
        raise MediaError("HTTP 400 expired rkey")

    monkeypatch.setattr(service.media, "download_image", expired_download)
    try:
        stored = await service._persist_event_images(event)
        assert len(stored) == 1
        image = event["content"]["images"][0]
        assert Path(image["stored_path"]).exists()
        assert image.get("refreshed_url") is True
        assert "storage_error" not in image
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_slow_turn_never_sends_automatic_thinking_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowClient:
        def __init__(self, *_: Any) -> None:
            pass

        async def run_turn(self, **_: Any) -> LLMResult:
            await asyncio.sleep(0.05)
            return LLMResult("内部摘要", [])

    class Adapter:
        connected = True

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
            if action == "send_group_msg":
                self.sent.append(params["message"][-1]["data"]["text"])
                return {"data": {"message_id": "thinking-1"}}
            raise AssertionError(action)

    monkeypatch.setattr("app.service.ChatCompletionsClient", SlowClient)
    service = AgentService(tmp_path, secret_store=MemorySecrets())
    adapter = Adapter()
    service.adapter = adapter  # type: ignore[assignment]
    service.db.upsert_group("100", "测试群")
    service.db.set_group_config("100", True)
    service.db.set_group_initialized("100", True)
    assert service.db.insert_event(normalise_onebot_event(_message("live", "请看一下", 1)))
    try:
        await service._run_group_worker("100")
        assert adapter.sent == []
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_clear_live_group_call_forces_a_short_current_group_reply() -> None:
    class ScriptedClient(ChatCompletionsClient):
        def __init__(self) -> None:
            super().__init__(
                LLMSettings(base_url="https://llm.example/v1", model="test", global_prompt="test"),
                api_key="test-key",
            )
            self.requests: list[dict[str, Any]] = []

        async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
            self.requests.append(body)
            if len(self.requests) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "reply",
                                        "type": "function",
                                        "function": {
                                            "name": "send_group_message",
                                            "arguments": '{"text":"我在。"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "内部摘要"}}]}

    event = normalise_onebot_event(_message("call", "有人吗？是人的发1", 1))
    assert event["content"]["live_clear_group_call"] is True
    client = ScriptedClient()
    sent: list[str] = []

    async def execute(name: str, arguments: dict[str, Any], _: str) -> dict[str, Any]:
        sent.append(name + ":" + arguments["text"])
        return {"ok": True, "message_id": "answer"}

    result = await client.run_turn(
        "", "实时群消息", "", "off", [], execute, direct_clear_group_call_reply_required=True
    )
    assert result.summary == "内部摘要"
    assert sent == ["send_group_message:我在。"]
    assert client.requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "send_group_message"},
    }
    assert "实时群内召唤规则" in client.requests[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_agent_can_chain_read_tools_then_send_a_group_message() -> None:
    def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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

    class ScriptedClient(ChatCompletionsClient):
        def __init__(self) -> None:
            super().__init__(
                LLMSettings(base_url="https://llm.example/v1", model="test", global_prompt="test"),
                api_key="test-key",
            )
            self.script = [
                tool_call("search", "Builtin_Websearch", {"query": "Tarjan 点双"}),
                tool_call("page", "Builtin_patch", {"url": "https://example.com/tarjan"}),
                tool_call("send", "send_group_message", {"text": "我查到资料了\n核心是 low 值判定。"}),
                {"choices": [{"message": {"content": "旧消息已归档；最新原文仍保留。"}}]},
            ]
            self.requests: list[dict[str, Any]] = []

        async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
            self.requests.append(body)
            return self.script.pop(0)

    client = ScriptedClient()
    executed: list[tuple[str, int]] = []

    async def execute(name: str, _: dict[str, Any], __: str, slot: int) -> dict[str, Any]:
        executed.append((name, slot))
        return {"ok": True, "tool": name}

    result = await client.run_turn("旧摘要", "旧事件", "", "off", [], execute)
    assert result.summary.startswith("旧消息已归档")
    assert executed == [
        ("Builtin_Websearch", 0),
        ("Builtin_patch", 1),
        ("send_group_message", 2),
    ]
    assert [request["tool_choice"] for request in client.requests] == ["auto", "auto", "auto", "auto"]
