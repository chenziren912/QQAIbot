"""Focused coverage for evidence-backed, non-vector group memory storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import Database


def _event(
    group_id: str,
    message_id: str,
    text: str,
    occurred_at: int,
    *,
    memory_processed: bool = False,
) -> dict:
    return {
        "dedupe_key": f"message:{group_id}:{message_id}",
        "group_id": group_id,
        "event_type": "message.group",
        "message_id": message_id,
        "occurred_at": occurred_at,
        "sender_id": "member",
        "sender_name": "成员",
        "normalized_text": text,
        "content": {"text": text},
        "raw": {},
        "memory_processed": memory_processed,
    }


def test_memory_requires_same_group_source_and_never_crosses_group_queries(tmp_path: Path) -> None:
    db = Database(tmp_path / "agent.sqlite3")
    try:
        db.upsert_group("100", "一群")
        db.upsert_group("200", "二群")
        event_100 = db.insert_event(_event("100", "m100", "小明喜欢蓝色", 1))
        event_200 = db.insert_event(_event("200", "m200", "另一个群的消息", 2))
        assert event_100 and event_200

        memory = db.upsert_group_memory(
            "100",
            "preference:member:color",
            "preference",
            "小明喜欢蓝色。",
            [{"event_id": event_100, "message_id": "m100", "quote": "小明喜欢蓝色"}],
            subject="小明",
            predicate="喜欢的颜色",
            object_value="蓝色",
            confidence_status="confirmed",
        )

        assert memory["active"] is True
        assert memory["confidence_status"] == "confirmed"
        assert memory["evidence"][0]["source_event_id"] == event_100
        assert memory["evidence"][0]["source_message_id"] == "m100"
        assert db.get_group_memory("200", memory["id"]) is None
        assert db.list_group_memories("200") == []
        assert db.search_group_memories("200", "蓝色") == []

        message_only = db.upsert_group_memory(
            "100",
            "fact:message-only-source",
            "fact",
            "消息正文确实提到蓝色。",
            {"message_id": "m100", "quote": "喜欢蓝色"},
        )
        assert message_only["evidence"][0]["source_event_id"] is None
        assert message_only["evidence"][0]["source_message_id"] == "m100"

        with pytest.raises(ValueError, match="逐字片段"):
            db.upsert_group_memory(
                "100",
                "fact:invented-quote",
                "fact",
                "模型编造的说法",
                [{"event_id": event_100, "quote": "原消息里从未出现的证据"}],
            )

        with pytest.raises(ValueError, match="不属于当前群"):
            db.upsert_group_memory(
                "100",
                "fact:cross-group",
                "fact",
                "不应写入",
                [{"event_id": event_200, "quote": "另一个群的消息"}],
            )
        with pytest.raises(ValueError, match="至少需要一条来源证据"):
            db.upsert_group_memory("100", "fact:no-source", "fact", "无来源", [])
    finally:
        db.close()


def test_reinforce_correct_and_retract_preserve_revision_and_audit(tmp_path: Path) -> None:
    db = Database(tmp_path / "agent.sqlite3")
    try:
        first_event = db.insert_event(_event("100", "1", "小明说他喜欢蓝色", 1))
        second_event = db.insert_event(_event("100", "2", "小明再次确认喜欢蓝色", 2))
        correction_event = db.insert_event(_event("100", "3", "小明说现在最喜欢绿色", 3))
        assert first_event and second_event and correction_event

        first = db.upsert_group_memory(
            "100",
            "preference:小明:颜色",
            "preference",
            "小明喜欢蓝色。",
            {"event_id": first_event, "quote": "喜欢蓝色"},
            subject="小明",
            predicate="喜欢",
            object_value="蓝色",
            confidence_status="uncertain",
            metadata={"extractor": "test"},
        )
        reinforced = db.upsert_group_memory(
            "100",
            "preference:小明:颜色",
            "preference",
            "小明喜欢蓝色。",
            {"event_id": second_event, "quote": "再次确认喜欢蓝色"},
            subject="小明",
            predicate="喜欢",
            object_value="蓝色",
            confidence_status="confirmed",
        )
        assert reinforced["id"] == first["id"]
        assert reinforced["confidence_status"] == "confirmed"
        assert len(reinforced["evidence"]) == 2
        assert reinforced["metadata"] == {"extractor": "test"}

        corrected = db.correct_group_memory(
            "100",
            first["id"],
            statement="小明现在最喜欢绿色。",
            evidence={"event_id": correction_event, "quote": "现在最喜欢绿色"},
            object_value="绿色",
        )
        assert corrected["id"] != first["id"]
        assert corrected["active"] is True
        assert corrected["evidence"][0]["evidence_role"] == "correction"

        old = db.get_group_memory("100", first["id"])
        assert old is not None
        assert old["active"] is False
        assert old["superseded_by_memory_id"] == corrected["id"]
        assert [item["id"] for item in db.list_group_memories("100")] == [corrected["id"]]
        assert {item["id"] for item in db.list_group_memories("100", active_only=False)} == {
            first["id"],
            corrected["id"],
        }

        retracted = db.retract_group_memory("100", corrected["id"], "本人说明前述说法是玩笑")
        assert retracted["active"] is False
        assert retracted["confidence_status"] == "retracted"
        assert retracted["retraction_reason"] == "本人说明前述说法是玩笑"
        assert db.list_group_memories("100") == []

        actions = {
            item["action"] for item in db.list_group_memory_changes("100", limit=50)
        }
        assert {"created", "confirmed", "superseded", "corrected", "retracted"} <= actions
    finally:
        db.close()


def test_conflicts_are_explicit_resolvable_and_group_scoped(tmp_path: Path) -> None:
    db = Database(tmp_path / "agent.sqlite3")
    try:
        event_a = db.insert_event(_event("100", "a", "比赛周六举行", 1))
        event_b = db.insert_event(_event("100", "b", "比赛改到周日", 2))
        event_other = db.insert_event(_event("200", "c", "二群事实", 3))
        assert event_a and event_b and event_other
        a = db.upsert_group_memory(
            "100", "schedule:a", "fact", "比赛周六举行。", {"event_id": event_a, "quote": "周六举行"}
        )
        b = db.upsert_group_memory(
            "100", "schedule:b", "fact", "比赛周日举行。", {"event_id": event_b, "quote": "改到周日"}
        )
        other = db.upsert_group_memory(
            "200", "other", "fact", "二群事实。", {"event_id": event_other, "quote": "二群事实"}
        )

        conflict = db.record_group_memory_conflict("100", a["id"], b["id"], "时间说法冲突")
        assert conflict["status"] == "open"
        assert db.get_group_memory("100", a["id"])["conflicts_with_memory_ids"] == [b["id"]]
        with pytest.raises(ValueError, match="当前群中不存在"):
            db.record_group_memory_conflict("100", a["id"], other["id"], "跨群不允许")

        resolved = db.resolve_group_memory_conflict(
            "100", conflict["id"], "采用较新的周日说法", resolution_memory_id=b["id"]
        )
        assert resolved["status"] == "resolved"
        assert resolved["resolution_memory_id"] == b["id"]
        assert db.get_group_memory("100", a["id"])["open_conflict_ids"] == []
    finally:
        db.close()


def test_search_uses_fts_when_possible_and_like_for_cjk_substrings(tmp_path: Path) -> None:
    db = Database(tmp_path / "agent.sqlite3")
    try:
        event_id = db.insert_event(_event("100", "1", "陈梓仁最喜欢深海蓝配色", 1))
        assert event_id
        memory = db.upsert_group_memory(
            "100",
            "preference:陈梓仁:配色",
            "preference",
            "陈梓仁最喜欢深海蓝配色。",
            {"event_id": event_id, "quote": "最喜欢深海蓝配色"},
            subject="陈梓仁",
            object_value="深海蓝",
        )
        assert db.memory_search_backend in {"fts5", "like"}
        assert [item["id"] for item in db.search_group_memories("100", "海蓝")] == [memory["id"]]

        db.memory_search_backend = "like"
        assert [item["id"] for item in db.search_group_memories("100", "陈梓仁")] == [memory["id"]]
        assert db.search_group_memories("100", "%' OR 1=1 --") == []
    finally:
        db.close()


def test_legacy_events_get_retryable_memory_cursor_and_explicit_skip(tmp_path: Path) -> None:
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
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        "INSERT INTO events(dedupe_key, group_id, event_type, message_id, created_at) "
        "VALUES ('old', '100', 'message.group', 'old-message', 'now')"
    )
    connection.commit()
    connection.close()

    db = Database(path)
    try:
        columns = {row["name"] for row in db._connection.execute("PRAGMA table_info(events)")}
        assert "memory_processed" in columns
        pending = db.memory_pending_events("100")
        assert [item["message_id"] for item in pending] == ["old-message"]

        skipped_id = db.insert_event(_event("100", "skip", "系统思考提示", 2, memory_processed=True))
        assert skipped_id
        assert [item["message_id"] for item in db.memory_pending_events("100")] == ["old-message"]
        db.mark_events_memory_processed([pending[0]["id"]])
        assert db.memory_pending_events("100") == []
    finally:
        db.close()


def test_group_memory_reset_erases_derived_state_and_rewinds_all_events(tmp_path: Path) -> None:
    db = Database(tmp_path / "agent.sqlite3")
    try:
        db.upsert_group("100", "可重算群")
        db.set_group_config("100", True, "本群旧规则", "inherit")
        first = db.insert_event(_event("100", "1", "小明喜欢蓝色", 1, memory_processed=True))
        second = db.insert_event(_event("100", "2", "小明改喜欢绿色", 2, memory_processed=True))
        assert first and second
        db.mark_events_processed([first, second])
        db.mark_events_archived([first, second])
        db.save_summary("100", "旧的滚动摘要", second, turn_id=7)
        memory = db.upsert_group_memory(
            "100",
            "preference:x:color",
            "preference",
            "小明喜欢绿色。",
            {"event_id": second, "quote": "小明改喜欢绿色"},
        )
        assert memory["id"]

        counts = db.reset_group_memory_and_recompute("100")

        assert counts["events"] == 2
        assert counts["memories"] == 1
        assert db.list_group_memories("100", active_only=False) == []
        assert db.list_summary_snapshots("100") == []
        assert db.get_summary_record("100")["content"] == ""
        assert db.get_group("100")["prompt_override"] == ""
        events = db.unarchived_events("100")
        assert [event["message_id"] for event in events] == ["1", "2"]
        assert all(event["archived"] == 0 for event in events)
        assert all(event["pending"] == 0 for event in events)
        assert db.memory_pending_events("100")
    finally:
        db.close()
