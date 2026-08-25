"""Focused persistence coverage for the local administrator chat and rules."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import Database, MAX_ADMIN_HISTORY_LIMIT, MAX_ADMIN_MESSAGE_CHARS
from app.rules import MAX_RULES_CHARS, RulesContentTooLargeError, RulesStore, RulesStoreError


def test_rules_store_uses_only_the_fixed_project_local_rules_file(tmp_path) -> None:
    data_dir = tmp_path / "project-data"
    unrelated = tmp_path / "do-not-touch.md"
    unrelated.write_text("keep", encoding="utf-8")
    store = RulesStore(data_dir)

    assert store.path == data_dir.resolve() / "rules.md"
    assert store.read() == ""

    content = "# 机器人规则\n\n始终用中文。\n"
    store.write(content)

    assert store.read() == content
    assert store.path.read_text(encoding="utf-8") == content
    assert unrelated.read_text(encoding="utf-8") == "keep"

    store.clear()
    assert store.read() == ""


def test_rules_store_keeps_old_file_when_atomic_replace_fails(tmp_path, monkeypatch) -> None:
    store = RulesStore(tmp_path / "data")
    store.write("旧规则")

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("app.rules.os.replace", fail_replace)
    with pytest.raises(RulesStoreError, match="原子写入"):
        store.write("新规则")

    assert store.read() == "旧规则"
    assert not list(store.path.parent.glob(".rules.*.tmp"))


def test_rules_store_bounds_and_reports_non_utf8_manual_files(tmp_path) -> None:
    store = RulesStore(tmp_path / "data")
    store.write("保留的规则")
    with pytest.raises(RulesContentTooLargeError):
        store.write("x" * (MAX_RULES_CHARS + 1))
    assert store.read() == "保留的规则"

    store.path.write_bytes(b"\xff\xfe\x80")
    with pytest.raises(RulesStoreError, match="UTF-8"):
        store.read()


def test_admin_messages_are_single_local_chronological_history(tmp_path) -> None:
    db = Database(tmp_path / "data" / "agent.sqlite3")
    try:
        user_id = db.append_admin_message("user", "以后回答要简洁")
        assistant_id = db.append_admin_message("assistant", "好的，我会遵守。")
        tool_id = db.append_admin_message(
            "tool",
            "规则已写入。",
            tool_name="write_rules",
            tool_result={"ok": True, "characters": 12},
        )

        assert [user_id, assistant_id, tool_id] == sorted([user_id, assistant_id, tool_id])
        messages = db.list_recent_admin_messages(2)
        assert [message["role"] for message in messages] == ["assistant", "tool"]
        assert messages[0]["tool_name"] == ""
        assert messages[0]["tool_result"] is None
        assert messages[1]["tool_name"] == "write_rules"
        assert messages[1]["tool_result"] == {"ok": True, "characters": 12}
        assert db.list_recent_admin_messages(0) == []
    finally:
        db.close()


def test_admin_message_validation_and_history_bound(tmp_path) -> None:
    db = Database(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="role"):
            db.append_admin_message("system", "不允许")
        with pytest.raises(TypeError, match="内容"):
            db.append_admin_message("user", 123)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="最多允许"):
            db.append_admin_message("user", "x" * (MAX_ADMIN_MESSAGE_CHARS + 1))

        for index in range(MAX_ADMIN_HISTORY_LIMIT + 3):
            db.append_admin_message("user", str(index))
        history = db.list_recent_admin_messages(MAX_ADMIN_HISTORY_LIMIT + 100)
        assert len(history) == MAX_ADMIN_HISTORY_LIMIT
        assert history[0]["content"] == "3"
        assert history[-1]["content"] == str(MAX_ADMIN_HISTORY_LIMIT + 2)
    finally:
        db.close()


def test_old_database_migrates_admin_history_without_losing_existing_data(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(str(db_path))
    try:
        legacy.execute(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES ('legacy', 'true', 'before')"
        )
        legacy.commit()
    finally:
        legacy.close()

    db = Database(db_path)
    try:
        assert db.get_json_setting("legacy", False) is True
        db.append_admin_message("user", "迁移后仍可对话")
        assert db.list_recent_admin_messages()[-1]["content"] == "迁移后仍可对话"
    finally:
        db.close()
