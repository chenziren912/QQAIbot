"""Small, transactional SQLite persistence layer for the local agent."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

from .memory import (
    MAX_MEMORY_FIELD_CHARS,
    MAX_MEMORY_KEY_CHARS,
    MAX_MEMORY_KIND_CHARS,
    MAX_MEMORY_METADATA_JSON_CHARS,
    MAX_MEMORY_REASON_CHARS,
    MAX_MEMORY_STATEMENT_CHARS,
    bounded_text,
    make_fts_query,
    normalize_confidence_status,
    normalize_memory_evidence,
)


ADMIN_MESSAGE_ROLES = frozenset({"user", "assistant", "tool"})
MAX_ADMIN_MESSAGE_CHARS = 60_000
MAX_ADMIN_TOOL_NAME_CHARS = 128
MAX_ADMIN_TOOL_RESULT_CHARS = 60_000
MAX_ADMIN_HISTORY_LIMIT = 200


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_loads(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.journal_mode = ""
        self.journal_mode_warning = ""
        self.memory_search_backend = "like"
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._configure_journal_mode()
        self.initialize()

    def _configure_journal_mode(self) -> None:
        """Prefer WAL but keep a local service bootable when it is unavailable.

        Some Windows folders (notably sync-protected, read-only-attributed, or
        aggressively scanned project directories) reject SQLite's sidecar
        ``-wal`` / ``-shm`` creation with ``disk I/O error``.  There is no
        reason for the dashboard to crash before it can explain that problem:
        fall back to SQLite's single-file DELETE journal mode.  We never
        delete, recreate, or silently repair the user's database here.
        """

        try:
            row = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
            self.journal_mode = str(row[0]).lower() if row else "wal"
            if self.journal_mode == "wal":
                return
            # SQLite can decline WAL without raising (for example on a
            # special filesystem) and report the mode it kept.  Surface that
            # fact instead of falsely claiming WAL is active.
            self.journal_mode_warning = (
                "SQLite 请求 WAL 日志后实际仍为 %s 日志模式；服务可继续运行，"
                "但请检查 data 目录是否被同步盘、杀毒软件或文件权限限制。"
                % self.journal_mode.upper()
            )
            return
        except sqlite3.OperationalError as wal_error:
            try:
                row = self._connection.execute("PRAGMA journal_mode = DELETE").fetchone()
                self.journal_mode = str(row[0]).lower() if row else "delete"
            except sqlite3.OperationalError as fallback_error:
                raise sqlite3.OperationalError(
                    "SQLite 无法设置 WAL 或 DELETE 日志模式（数据库：%s）。"
                    "请确认 data 目录和 agent.sqlite3 可写、未被同步盘/杀毒软件锁定；"
                    "不会自动删除任何数据库文件。WAL 错误：%s；DELETE 错误：%s"
                    % (self.path, wal_error, fallback_error)
                ) from fallback_error
            self.journal_mode_warning = (
                "SQLite 无法启用 WAL 日志（%s），已安全降级为 %s 日志模式；"
                "服务可继续运行，但请检查 data 目录是否被同步盘、杀毒软件或文件权限锁定。"
                % (wal_error, self.journal_mode.upper())
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def initialize(self) -> None:
        with self.transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY,
                    group_name TEXT NOT NULL DEFAULT '',
                    conversation_type TEXT NOT NULL DEFAULT 'group',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    prompt_override TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL DEFAULT 'inherit',
                    initialized INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
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
                    memory_processed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_pending
                    ON events(group_id, pending, id);
                CREATE INDEX IF NOT EXISTS idx_events_message
                    ON events(group_id, message_id);
                CREATE INDEX IF NOT EXISTS idx_events_message_any_group
                    ON events(message_id);
                CREATE TABLE IF NOT EXISTS summaries (
                    group_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL DEFAULT '',
                    last_event_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                -- Immutable rolling-summary checkpoints.  The live
                -- ``summaries`` row is only a pointer; checkpoints let the
                -- service restore the last usable state after a provider
                -- refusal or malformed model response without losing the
                -- original events.
                CREATE TABLE IF NOT EXISTS summary_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    turn_id INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL DEFAULT '',
                    last_event_id INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_summary_snapshots_group
                    ON summary_snapshots(group_id, id DESC);
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    event_ids_json TEXT NOT NULL DEFAULT '[]',
                    summary_text TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS tool_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id INTEGER NOT NULL,
                    group_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL DEFAULT '',
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(turn_id, tool_call_id)
                );
                -- A durable reservation sits in front of every QQ-changing
                -- action.  It deliberately spans a processing batch instead
                -- of a model-generated call id, so an interrupted/retried
                -- turn cannot send the same batch's action twice.
                CREATE TABLE IF NOT EXISTS tool_operations (
                    operation_key TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    event_ids_json TEXT NOT NULL DEFAULT '[]',
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sent_messages (
                    message_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    turn_id INTEGER NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    recalled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                -- There is deliberately no group/conversation identifier:
                -- this is the one local operator-to-agent conversation shown
                -- in the loopback dashboard.  It never mixes with QQ data.
                CREATE TABLE IF NOT EXISTS admin_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    tool_name TEXT NOT NULL DEFAULT '',
                    tool_result_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_admin_messages_id
                    ON admin_messages(id);

                -- Long-term memory is revisioned and evidence-backed.  An
                -- inactive row is retained forever so a correction,
                -- retraction, or supersede never erases what the model once
                -- believed or why it believed it.
                CREATE TABLE IF NOT EXISTS group_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    predicate TEXT NOT NULL DEFAULT '',
                    object_value TEXT NOT NULL DEFAULT '',
                    statement TEXT NOT NULL,
                    confidence_status TEXT NOT NULL DEFAULT 'uncertain'
                        CHECK(confidence_status IN ('confirmed', 'uncertain', 'retracted')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                    valid_from TEXT NOT NULL DEFAULT '',
                    valid_until TEXT NOT NULL DEFAULT '',
                    superseded_by_memory_id INTEGER,
                    retraction_reason TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(id, group_id),
                    FOREIGN KEY(superseded_by_memory_id) REFERENCES group_memories(id),
                    CHECK(confidence_status <> 'retracted' OR active = 0)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_group_memories_current_key
                    ON group_memories(group_id, memory_key) WHERE active = 1;
                CREATE INDEX IF NOT EXISTS idx_group_memories_lookup
                    ON group_memories(group_id, active, kind, confidence_status, updated_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS group_memory_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER NOT NULL,
                    group_id TEXT NOT NULL,
                    evidence_role TEXT NOT NULL DEFAULT 'support'
                        CHECK(evidence_role IN ('support', 'confirmation', 'correction', 'retraction', 'conflict')),
                    source_event_id INTEGER,
                    source_message_id TEXT NOT NULL DEFAULT '',
                    evidence_text TEXT NOT NULL,
                    observed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id, group_id) REFERENCES group_memories(id, group_id),
                    FOREIGN KEY(source_event_id) REFERENCES events(id),
                    CHECK(source_event_id IS NOT NULL OR source_message_id <> '')
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_group_memory_evidence_dedupe
                    ON group_memory_evidence(
                        memory_id,
                        evidence_role,
                        COALESCE(source_event_id, -1),
                        source_message_id,
                        evidence_text
                    );
                CREATE INDEX IF NOT EXISTS idx_group_memory_evidence_memory
                    ON group_memory_evidence(group_id, memory_id, id);

                CREATE TABLE IF NOT EXISTS group_memory_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    memory_id_low INTEGER NOT NULL,
                    memory_id_high INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved')),
                    resolution TEXT NOT NULL DEFAULT '',
                    resolution_memory_id INTEGER,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(group_id, memory_id_low, memory_id_high),
                    CHECK(memory_id_low < memory_id_high),
                    FOREIGN KEY(memory_id_low, group_id) REFERENCES group_memories(id, group_id),
                    FOREIGN KEY(memory_id_high, group_id) REFERENCES group_memories(id, group_id),
                    FOREIGN KEY(resolution_memory_id, group_id) REFERENCES group_memories(id, group_id)
                );
                CREATE INDEX IF NOT EXISTS idx_group_memory_conflicts_lookup
                    ON group_memory_conflicts(group_id, status, id DESC);

                CREATE TABLE IF NOT EXISTS group_memory_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER NOT NULL,
                    group_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id, group_id) REFERENCES group_memories(id, group_id)
                );
                CREATE INDEX IF NOT EXISTS idx_group_memory_changes_lookup
                    ON group_memory_changes(group_id, memory_id, id DESC);
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
            group_columns = {row["name"] for row in conn.execute("PRAGMA table_info(groups)").fetchall()}
            if "conversation_type" not in group_columns:
                conn.execute(
                    "ALTER TABLE groups ADD COLUMN conversation_type TEXT NOT NULL DEFAULT 'group'"
                )
            if "message_id" not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN message_id TEXT NOT NULL DEFAULT ''")
            if "archived" not in columns:
                # Existing rows with pending=0 have already contributed to the
                # legacy rolling summary.  Treat them as archived during the
                # migration so merely upgrading does not make the agent replay
                # years of old QQ messages or issue old tool actions again.
                conn.execute("ALTER TABLE events ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
                conn.execute("UPDATE events SET archived = 1 WHERE pending = 0")
            if "memory_processed" not in columns:
                # Deliberately leave legacy events at zero.  The independent
                # extraction worker can backfill them with the same retryable
                # cursor used for new messages; no old evidence is forgotten.
                conn.execute("ALTER TABLE events ADD COLUMN memory_processed INTEGER NOT NULL DEFAULT 0")
            # This must happen after the legacy-column migration: SQLite runs
            # CREATE INDEX immediately, and an old installation otherwise
            # fails before it reaches the safe ALTER TABLE above.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_archive "
                "ON events(group_id, archived, occurred_at, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_memory_pending "
                "ON events(group_id, memory_processed, occurred_at, id)"
            )
            self._initialize_memory_fts(conn)

    def _initialize_memory_fts(self, conn: sqlite3.Connection) -> None:
        """Create a derived FTS5 index, safely retaining a LIKE fallback."""

        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS group_memories_fts USING fts5("
                "statement, subject, predicate, object_value, "
                "content='group_memories', content_rowid='id', tokenize='unicode61'"
                ")"
            )
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS group_memories_fts_insert
                AFTER INSERT ON group_memories BEGIN
                    INSERT INTO group_memories_fts(rowid, statement, subject, predicate, object_value)
                    VALUES (new.id, new.statement, new.subject, new.predicate, new.object_value);
                END;
                CREATE TRIGGER IF NOT EXISTS group_memories_fts_delete
                AFTER DELETE ON group_memories BEGIN
                    INSERT INTO group_memories_fts(group_memories_fts, rowid, statement, subject, predicate, object_value)
                    VALUES ('delete', old.id, old.statement, old.subject, old.predicate, old.object_value);
                END;
                CREATE TRIGGER IF NOT EXISTS group_memories_fts_update
                AFTER UPDATE OF statement, subject, predicate, object_value ON group_memories BEGIN
                    INSERT INTO group_memories_fts(group_memories_fts, rowid, statement, subject, predicate, object_value)
                    VALUES ('delete', old.id, old.statement, old.subject, old.predicate, old.object_value);
                    INSERT INTO group_memories_fts(rowid, statement, subject, predicate, object_value)
                    VALUES (new.id, new.statement, new.subject, new.predicate, new.object_value);
                END;
                """
            )
            # The index is derived data.  Rebuild also covers installations
            # upgraded from a version that had memory rows but no FTS table.
            conn.execute("INSERT INTO group_memories_fts(group_memories_fts) VALUES ('rebuild')")
            self.memory_search_backend = "fts5"
        except sqlite3.OperationalError:
            # Minimal/system SQLite builds may omit FTS5.  Memory persistence
            # and exact group isolation remain fully functional via LIKE.
            self.memory_search_backend = "like"

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # Settings
    def get_json_setting(self, key: str, default: Any) -> Any:
        with self._lock:
            row = self._connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json_loads(row["value"] if row else None, default)

    def set_json_setting(self, key: str, value: Any) -> None:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, json_dumps(value), now),
            )

    # Local administrator conversation
    def append_admin_message(
        self,
        role: str,
        content: str,
        tool_name: str = "",
        tool_result: Any = None,
    ) -> int:
        """Persist one message in the single local administrator conversation.

        The content and optional tool result are separate from all QQ event
        tables.  Results are stored as JSON so a future UI/LLM layer can
        render structured rule-write outcomes without parsing a display
        string.  The returned auto-increment ID is stable for UI ordering.
        """

        normalized_role = str(role or "").strip().lower()
        if normalized_role not in ADMIN_MESSAGE_ROLES:
            allowed = ", ".join(sorted(ADMIN_MESSAGE_ROLES))
            raise ValueError(f"管理员对话 role 必须是以下之一：{allowed}")
        if not isinstance(content, str):
            raise TypeError("管理员对话内容必须是字符串")
        if len(content) > MAX_ADMIN_MESSAGE_CHARS:
            raise ValueError(f"管理员对话内容最多允许 {MAX_ADMIN_MESSAGE_CHARS} 个字符")
        if not isinstance(tool_name, str):
            raise TypeError("管理员工具名称必须是字符串")
        normalized_tool_name = tool_name.strip()
        if len(normalized_tool_name) > MAX_ADMIN_TOOL_NAME_CHARS:
            raise ValueError(f"管理员工具名称最多允许 {MAX_ADMIN_TOOL_NAME_CHARS} 个字符")

        result_json = "" if tool_result is None else json_dumps(tool_result)
        if len(result_json) > MAX_ADMIN_TOOL_RESULT_CHARS:
            raise ValueError(f"管理员工具结果最多允许 {MAX_ADMIN_TOOL_RESULT_CHARS} 个字符")

        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO admin_messages(role, content, tool_name, tool_result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (normalized_role, content, normalized_tool_name, result_json, utc_now()),
            )
            return int(cursor.lastrowid)

    def list_recent_admin_messages(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the newest bounded admin history in chronological order."""

        try:
            requested_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("管理员对话历史条数必须是整数") from exc
        if requested_limit <= 0:
            return []
        safe_limit = min(requested_limit, MAX_ADMIN_HISTORY_LIMIT)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM admin_messages ORDER BY id DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        messages: List[Dict[str, Any]] = []
        for row in reversed(rows):
            message = dict(row)
            message["tool_result"] = json_loads(message.pop("tool_result_json", ""), None)
            messages.append(message)
        return messages

    # Groups
    def upsert_group(
        self,
        group_id: str,
        group_name: str = "",
        conversation_type: str = "",
    ) -> None:
        now = utc_now()
        kind = str(conversation_type or "").strip().lower()
        if kind not in {"group", "private"}:
            kind = ""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO groups(group_id, group_name, conversation_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET group_name = "
                "CASE WHEN excluded.group_name <> '' THEN excluded.group_name ELSE groups.group_name END, "
                "conversation_type = CASE WHEN excluded.conversation_type = 'private' THEN 'private' ELSE groups.conversation_type END, "
                "updated_at = excluded.updated_at",
                (str(group_id), group_name or "", kind or "group", now, now),
            )

    def list_groups(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT g.*, COALESCE(s.content, '') AS summary, COALESCE(s.updated_at, '') AS summary_updated_at, "
                "(SELECT COUNT(1) FROM events e WHERE e.group_id = g.group_id AND e.pending = 1) AS pending_count "
                "FROM groups g LEFT JOIN summaries s ON s.group_id = g.group_id "
                "ORDER BY g.enabled DESC, g.group_name COLLATE NOCASE, g.group_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_event_count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(1) AS value FROM events WHERE pending = 1").fetchone()
        return int(row["value"] if row else 0)

    def enabled_group_count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(1) AS value FROM groups WHERE enabled = 1").fetchone()
        return int(row["value"] if row else 0)

    def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM groups WHERE group_id = ?", (str(group_id),)).fetchone()
        return dict(row) if row else None

    def set_group_config(
        self,
        group_id: str,
        enabled: bool,
        prompt_override: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        self.upsert_group(group_id)
        current = self.get_group(group_id) or {}
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE groups SET enabled = ?, prompt_override = ?, reasoning_effort = ?, updated_at = ? WHERE group_id = ?",
                (
                    int(enabled),
                    current.get("prompt_override", "") if prompt_override is None else prompt_override,
                    current.get("reasoning_effort", "inherit") if reasoning_effort is None else reasoning_effort,
                    now,
                    str(group_id),
                ),
            )

    def set_group_initialized(self, group_id: str, initialized: bool) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE groups SET initialized = ?, updated_at = ? WHERE group_id = ?",
                (int(initialized), utc_now(), str(group_id)),
            )

    def set_group_error(self, group_id: str, error: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE groups SET last_error = ?, updated_at = ? WHERE group_id = ?",
                (error[:2000], utc_now(), str(group_id)),
            )

    # Events and summaries
    def insert_event(self, event: Dict[str, Any]) -> Optional[int]:
        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO events(
                    dedupe_key, group_id, event_type, sub_type, message_id, occurred_at, sender_id, sender_name,
                    self_id, normalized_text, content_json, raw_json, is_self, pending, archived,
                    memory_processed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["dedupe_key"],
                    str(event["group_id"]),
                    event.get("event_type", "unknown"),
                    event.get("sub_type", ""),
                    str(event.get("message_id", "")),
                    int(event.get("occurred_at", 0) or 0),
                    str(event.get("sender_id", "")),
                    event.get("sender_name", ""),
                    str(event.get("self_id", "")),
                    event.get("normalized_text", ""),
                    json_dumps(event.get("content", {})),
                    json_dumps(event.get("raw", {})),
                    int(bool(event.get("is_self", False))),
                    int(bool(event.get("pending", True))),
                    int(bool(event.get("archived", False))),
                    int(bool(event.get("memory_processed", False))),
                    now,
                ),
            )
            if cursor.rowcount == 0:
                return None
            return int(cursor.lastrowid)

    def update_event_content(self, event_id: int, content: Dict[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE events SET content_json = ? WHERE id = ?", (json_dumps(content), event_id))

    def pending_events(self, group_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE group_id = ? AND pending = 1 ORDER BY occurred_at ASC, id ASC LIMIT ?",
                (str(group_id), limit),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def memory_pending_events(self, group_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """Return the current group's events awaiting durable memory extraction."""

        safe_limit = max(1, min(int(limit), 5_000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE group_id = ? AND memory_processed = 0 "
                "ORDER BY occurred_at ASC, id ASC LIMIT ?",
                (str(group_id), safe_limit),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def mark_events_memory_processed(self, event_ids: Iterable[int]) -> None:
        """Advance the memory cursor only after extraction is fully committed."""

        ids = sorted({int(item) for item in event_ids if int(item) > 0})
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.transaction() as conn:
            conn.execute(
                "UPDATE events SET memory_processed = 1 WHERE id IN (" + placeholders + ")",
                ids,
            )

    def unarchived_events(self, group_id: str, limit: int = 5_000) -> List[Dict[str, Any]]:
        """Return stored events which have not entered the rolling summary.

        ``pending`` tracks whether a live event still needs an agent decision;
        ``archived`` independently tracks whether an older event has been
        folded into the durable summary.  Keeping those two cursors separate
        lets the newest raw transcript remain verbatim in every LLM request.
        """

        safe_limit = max(1, min(int(limit), 20_000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE group_id = ? AND archived = 0 "
                "ORDER BY occurred_at ASC, id ASC LIMIT ?",
                (str(group_id), safe_limit),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def list_recent_events(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._event_row(row) for row in rows]

    def recent_group_message_events(
        self,
        group_id: str,
        limit: int = 10,
        *,
        include_self: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return the latest recorded group-message events in chronological order.

        This intentionally reads raw normalized event text, not summaries.  A
        small, stable recent window gives a model enough conversational context
        after long-lived summaries have compressed older discussion.  Bot/self
        echoes are omitted by default so the prompt cannot teach the bot to
        recursively imitate its own earlier replies.
        """

        safe_limit = max(1, min(int(limit), 5_000))
        clauses = ["group_id = ?", "event_type LIKE 'message%'"]
        params: List[Any] = [str(group_id)]
        if not include_self:
            clauses.append("is_self = 0")
        query = (
            "SELECT * FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY occurred_at DESC, id DESC LIMIT ?"
        )
        params.append(safe_limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        # SQL reads newest first for efficient LIMIT; expose oldest-to-newest
        # so the model sees the same natural order as a QQ transcript.
        return [self._event_row(row) for row in reversed(rows)]

    def recent_group_message_context_events(self, group_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the latest visible group-message context, including this app's replies.

        NapCat is normally configured with ``reportSelfMessage=false`` so an
        app-sent QQ message often has no matching OneBot event.  It is still a
        real part of the conversation, though, and the next model turn needs
        to see it.  This query therefore merges ordinary/self message events
        with durable ``sent_messages`` rows that have not echoed as an event.
        Recalled app messages are deliberately omitted: they are no longer
        visible to people in the group and should not influence the model's
        current conversational context.
        """

        # The normal UI/model call uses a tiny window, while the rolling
        # transcript mode may need many short messages to reach 50,000 text
        # characters.  This remains bounded to avoid an untrusted group
        # turning one request into an unbounded database read.
        safe_limit = max(1, min(int(limit), 60_000))
        query = """
            WITH context_messages AS (
                SELECT
                    e.id, e.dedupe_key, e.group_id, e.event_type, e.sub_type,
                    e.message_id, e.occurred_at, e.sender_id, e.sender_name,
                    e.self_id, e.normalized_text, e.content_json, e.raw_json,
                    e.is_self, e.pending, e.archived, e.created_at, 0 AS context_source
                FROM events AS e
                WHERE e.group_id = ?
                  AND e.event_type LIKE 'message%'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM sent_messages AS recalled
                    WHERE recalled.group_id = e.group_id
                      AND recalled.message_id = e.message_id
                      AND recalled.recalled = 1
                  )

                UNION ALL

                SELECT
                    -sent.rowid AS id,
                    'app-sent:' || sent.message_id AS dedupe_key,
                    sent.group_id,
                    'message.app_sent' AS event_type,
                    '' AS sub_type,
                    sent.message_id,
                    CAST(strftime('%s', sent.created_at) AS INTEGER) AS occurred_at,
                    '' AS sender_id,
                    '机器人' AS sender_name,
                    '' AS self_id,
                    sent.content AS normalized_text,
                    '{"app_sent":true}' AS content_json,
                    '{}' AS raw_json,
                    1 AS is_self,
                    0 AS pending,
                    0 AS archived,
                    sent.created_at,
                    1 AS context_source
                FROM sent_messages AS sent
                WHERE sent.group_id = ?
                  AND sent.recalled = 0
                  AND NOT EXISTS (
                    SELECT 1
                    FROM events AS echoed
                    WHERE echoed.group_id = sent.group_id
                      AND echoed.message_id = sent.message_id
                  )
            )
            SELECT *
            FROM context_messages
            ORDER BY occurred_at DESC, context_source DESC, id DESC
            LIMIT ?
        """
        with self._lock:
            rows = self._connection.execute(query, (str(group_id), str(group_id), safe_limit)).fetchall()
        # SQL reads newest first for an efficient LIMIT; present an ordinary
        # transcript to the model in chronological order.
        result: List[Dict[str, Any]] = []
        for row in reversed(rows):
            event = self._event_row(row)
            event.pop("context_source", None)
            result.append(event)
        return result

    def latest_group_raw_context_events(
        self,
        group_id: str,
        *,
        limit: int = 60_000,
    ) -> List[Dict[str, Any]]:
        """Return a broad group-event source window for raw-context selection.

        Character budgeting belongs to the service because it knows the exact
        transcript syntax and service-generated metadata.  Unlike the small
        conversational helper above, this includes recorded group notices and
        dynamics as well as messages, plus unsuppressed app sends.  A group
        dynamic should not disappear merely because it has no ``message``
        event type.
        """

        safe_limit = max(1, min(int(limit), 60_000))
        query = """
            WITH context_events AS (
                SELECT
                    e.id, e.dedupe_key, e.group_id, e.event_type, e.sub_type,
                    e.message_id, e.occurred_at, e.sender_id, e.sender_name,
                    e.self_id, e.normalized_text, e.content_json, e.raw_json,
                    e.is_self, e.pending, e.archived, e.created_at, 0 AS context_source
                FROM events AS e
                WHERE e.group_id = ?
                  AND NOT EXISTS (
                    SELECT 1
                    FROM sent_messages AS recalled
                    WHERE recalled.group_id = e.group_id
                      AND recalled.message_id = e.message_id
                      AND recalled.recalled = 1
                  )

                UNION ALL

                SELECT
                    -sent.rowid AS id,
                    'app-sent:' || sent.message_id AS dedupe_key,
                    sent.group_id,
                    'message.app_sent' AS event_type,
                    '' AS sub_type,
                    sent.message_id,
                    CAST(strftime('%s', sent.created_at) AS INTEGER) AS occurred_at,
                    '' AS sender_id,
                    '机器人' AS sender_name,
                    '' AS self_id,
                    sent.content AS normalized_text,
                    '{"app_sent":true}' AS content_json,
                    '{}' AS raw_json,
                    1 AS is_self,
                    0 AS pending,
                    0 AS archived,
                    sent.created_at,
                    1 AS context_source
                FROM sent_messages AS sent
                WHERE sent.group_id = ?
                  AND sent.recalled = 0
                  AND NOT EXISTS (
                    SELECT 1
                    FROM events AS echoed
                    WHERE echoed.group_id = sent.group_id
                      AND echoed.message_id = sent.message_id
                  )
            )
            SELECT * FROM context_events
            ORDER BY occurred_at DESC, context_source DESC, id DESC
            LIMIT ?
        """
        with self._lock:
            rows = self._connection.execute(query, (str(group_id), str(group_id), safe_limit)).fetchall()
        result: List[Dict[str, Any]] = []
        for row in reversed(rows):
            event = self._event_row(row)
            event.pop("context_source", None)
            result.append(event)
        return result

    def search_group_message_context(
        self,
        group_id: str,
        query_text: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search only the current group's persisted visible message text.

        This powers a read-only agent tool.  It deliberately never searches
        another group, raw OneBot payloads, settings, administrator chat, or
        files.  The app's own durable sends are included even when NapCat is
        configured not to echo self messages.
        """

        text = str(query_text or "").strip()
        if not text:
            return []
        safe_limit = max(1, min(int(limit), 50))
        # Backslash is the explicit escape character so `%` and `_` from a
        # model's query remain literal Chinese/chat text rather than wildcards.
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = "%" + escaped + "%"
        with self._lock:
            event_rows = self._connection.execute(
                "SELECT * FROM events WHERE group_id = ? AND event_type LIKE 'message%' "
                "AND normalized_text LIKE ? ESCAPE '\\' ORDER BY occurred_at DESC, id DESC LIMIT ?",
                (str(group_id), pattern, safe_limit),
            ).fetchall()
            sent_rows = self._connection.execute(
                "SELECT message_id, group_id, content, recalled, created_at "
                "FROM sent_messages WHERE group_id = ? AND recalled = 0 "
                "AND content LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT ?",
                (str(group_id), pattern, safe_limit),
            ).fetchall()
        items = [self._event_row(row) for row in event_rows]
        for row in sent_rows:
            item = dict(row)
            item.update(
                {
                    "id": -abs(hash("sent-search:" + str(row["message_id"]))) or -1,
                    "dedupe_key": "app-sent:" + str(row["message_id"]),
                    "event_type": "message.app_sent",
                    "sub_type": "",
                    "occurred_at": 0,
                    "sender_id": "",
                    "sender_name": "机器人",
                    "self_id": "",
                    "normalized_text": str(row["content"]),
                    "content": {"app_sent": True},
                    "raw": {},
                    "is_self": 1,
                    "pending": 0,
                    "archived": 0,
                }
            )
            # created_at is ISO UTC; an approximate seconds sort is enough
            # for mixed old local records and never becomes model authority.
            try:
                item["occurred_at"] = int(datetime.fromisoformat(str(row["created_at"])).timestamp())
            except (TypeError, ValueError):
                pass
            items.append(item)
        # De-duplicate an app message that was also received as an OneBot echo.
        seen_message_ids = set()
        newest_first: List[Dict[str, Any]] = []
        for item in sorted(
            items,
            key=lambda value: (int(value.get("occurred_at") or 0), int(value.get("id") or 0)),
            reverse=True,
        ):
            message_id = str(item.get("message_id") or "")
            key = message_id or str(item.get("dedupe_key") or item.get("id"))
            if key in seen_message_ids:
                continue
            seen_message_ids.add(key)
            item["normalized_text"] = str(item.get("normalized_text") or "")[:2_000]
            newest_first.append(item)
            if len(newest_first) >= safe_limit:
                break
        return list(reversed(newest_first))

    # Evidence-backed long-term group memory
    def upsert_group_memory(
        self,
        group_id: str,
        memory_key: str,
        kind: str,
        statement: str,
        evidence: Iterable[Mapping[str, Any]] | Mapping[str, Any],
        *,
        subject: str = "",
        predicate: str = "",
        object_value: str = "",
        confidence_status: str = "uncertain",
        valid_from: str = "",
        valid_until: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        supersedes_memory_ids: Iterable[int] = (),
        conflicts_with_ids: Iterable[int] = (),
        conflict_reason: str = "",
    ) -> Dict[str, Any]:
        """Insert or reinforce one current memory without erasing history.

        ``memory_key`` identifies the logical slot inside *one* group.  If the
        semantic fields change, the prior current row becomes inactive and a
        new immutable revision supersedes it.  An identical proposal simply
        appends evidence and may strengthen ``uncertain`` to ``confirmed``.
        Every write requires locally resolvable event/message evidence.
        """

        target_group = bounded_text(group_id, name="群号", maximum=128, required=True)
        normalized_key = bounded_text(
            memory_key, name="记忆键", maximum=MAX_MEMORY_KEY_CHARS, required=True
        )
        normalized_kind = bounded_text(
            kind, name="记忆类型", maximum=MAX_MEMORY_KIND_CHARS, required=True
        ).lower()
        normalized_statement = bounded_text(
            statement, name="记忆陈述", maximum=MAX_MEMORY_STATEMENT_CHARS, required=True
        )
        normalized_subject = bounded_text(subject, name="记忆主体", maximum=MAX_MEMORY_FIELD_CHARS)
        normalized_predicate = bounded_text(predicate, name="记忆关系", maximum=MAX_MEMORY_FIELD_CHARS)
        normalized_object = bounded_text(object_value, name="记忆客体", maximum=MAX_MEMORY_FIELD_CHARS)
        normalized_status = normalize_confidence_status(confidence_status)
        normalized_valid_from = bounded_text(valid_from, name="记忆有效起点", maximum=128)
        normalized_valid_until = bounded_text(valid_until, name="记忆有效终点", maximum=128)
        normalized_evidence = normalize_memory_evidence(evidence)
        metadata_value: Dict[str, Any] = dict(metadata or {})
        metadata_json = json_dumps(metadata_value)
        if len(metadata_json) > MAX_MEMORY_METADATA_JSON_CHARS:
            raise ValueError(f"记忆 metadata 最多允许 {MAX_MEMORY_METADATA_JSON_CHARS} 个 JSON 字符")
        supersedes = self._normalize_memory_ids(supersedes_memory_ids, "supersedes_memory_ids")
        conflicts = self._normalize_memory_ids(conflicts_with_ids, "conflicts_with_ids")
        normalized_conflict_reason = bounded_text(
            conflict_reason, name="记忆冲突说明", maximum=MAX_MEMORY_REASON_CHARS
        )

        now = utc_now()
        with self.transaction() as conn:
            verified_evidence = self._verify_memory_evidence(conn, target_group, normalized_evidence)
            current = conn.execute(
                "SELECT * FROM group_memories WHERE group_id = ? AND memory_key = ? AND active = 1",
                (target_group, normalized_key),
            ).fetchone()
            semantic = (
                normalized_kind,
                normalized_subject,
                normalized_predicate,
                normalized_object,
                normalized_statement,
                normalized_valid_from,
                normalized_valid_until,
            )
            current_semantic = None
            if current:
                current_semantic = (
                    str(current["kind"]),
                    str(current["subject"]),
                    str(current["predicate"]),
                    str(current["object_value"]),
                    str(current["statement"]),
                    str(current["valid_from"]),
                    str(current["valid_until"]),
                )

            if current and current_semantic == semantic:
                memory_id = int(current["id"])
                before = self._memory_snapshot(current)
                additional_superseded_ids = set(supersedes) - {memory_id}
                additional_superseded_rows = self._memory_rows_for_update(
                    conn, target_group, additional_superseded_ids
                )
                # Weak evidence never silently downgrades a confirmed fact.
                effective_status = (
                    "confirmed"
                    if normalized_status == "confirmed" or current["confidence_status"] == "confirmed"
                    else "uncertain"
                )
                merged_metadata = json_loads(str(current["metadata_json"]), {})
                if not isinstance(merged_metadata, dict):
                    merged_metadata = {}
                merged_metadata.update(metadata_value)
                merged_metadata_json = json_dumps(merged_metadata)
                if len(merged_metadata_json) > MAX_MEMORY_METADATA_JSON_CHARS:
                    raise ValueError(
                        f"合并后的记忆 metadata 最多允许 {MAX_MEMORY_METADATA_JSON_CHARS} 个 JSON 字符"
                    )
                conn.execute(
                    "UPDATE group_memories SET confidence_status = ?, metadata_json = ?, updated_at = ? "
                    "WHERE id = ? AND group_id = ?",
                    (effective_status, merged_metadata_json, now, memory_id, target_group),
                )
                evidence_role = "confirmation" if effective_status == "confirmed" else "support"
                self._insert_memory_evidence(
                    conn, target_group, memory_id, verified_evidence, evidence_role=evidence_role
                )
                after_row = conn.execute("SELECT * FROM group_memories WHERE id = ?", (memory_id,)).fetchone()
                self._record_memory_change(
                    conn,
                    target_group,
                    memory_id,
                    "confirmed" if before["confidence_status"] != effective_status else "reinforced",
                    before,
                    self._memory_snapshot(after_row),
                )
                if additional_superseded_ids:
                    placeholders = ",".join("?" for _ in additional_superseded_ids)
                    conn.execute(
                        "UPDATE group_memories SET active = 0, superseded_by_memory_id = ?, "
                        "updated_at = ? WHERE group_id = ? AND id IN (" + placeholders + ")",
                        [memory_id, now, target_group, *sorted(additional_superseded_ids)],
                    )
                    self._resolve_superseded_memory_conflicts(
                        conn,
                        target_group,
                        additional_superseded_ids,
                        replacement_memory_id=memory_id,
                        resolved_at=now,
                    )
                    for old_row in additional_superseded_rows:
                        refreshed_old = conn.execute(
                            "SELECT * FROM group_memories WHERE id = ?", (int(old_row["id"]),)
                        ).fetchone()
                        self._record_memory_change(
                            conn,
                            target_group,
                            int(old_row["id"]),
                            "superseded",
                            self._memory_snapshot(old_row),
                            self._memory_snapshot(refreshed_old),
                            reason=f"由记忆 #{memory_id} 取代",
                        )
            else:
                superseded_ids = set(supersedes)
                if current:
                    superseded_ids.add(int(current["id"]))
                superseded_rows = self._memory_rows_for_update(conn, target_group, superseded_ids)
                if superseded_ids:
                    placeholders = ",".join("?" for _ in superseded_ids)
                    conn.execute(
                        "UPDATE group_memories SET active = 0, updated_at = ? "
                        "WHERE group_id = ? AND id IN (" + placeholders + ")",
                        [now, target_group, *sorted(superseded_ids)],
                    )
                cursor = conn.execute(
                    "INSERT INTO group_memories("
                    "group_id, memory_key, kind, subject, predicate, object_value, statement, "
                    "confidence_status, active, valid_from, valid_until, metadata_json, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                    (
                        target_group,
                        normalized_key,
                        normalized_kind,
                        normalized_subject,
                        normalized_predicate,
                        normalized_object,
                        normalized_statement,
                        normalized_status,
                        normalized_valid_from,
                        normalized_valid_until,
                        metadata_json,
                        now,
                        now,
                    ),
                )
                memory_id = int(cursor.lastrowid)
                if superseded_ids:
                    placeholders = ",".join("?" for _ in superseded_ids)
                    conn.execute(
                        "UPDATE group_memories SET superseded_by_memory_id = ?, updated_at = ? "
                        "WHERE group_id = ? AND id IN (" + placeholders + ")",
                        [memory_id, now, target_group, *sorted(superseded_ids)],
                    )
                    self._resolve_superseded_memory_conflicts(
                        conn,
                        target_group,
                        superseded_ids,
                        replacement_memory_id=memory_id,
                        resolved_at=now,
                    )
                    for old_row in superseded_rows:
                        refreshed_old = conn.execute(
                            "SELECT * FROM group_memories WHERE id = ?", (int(old_row["id"]),)
                        ).fetchone()
                        self._record_memory_change(
                            conn,
                            target_group,
                            int(old_row["id"]),
                            "superseded",
                            self._memory_snapshot(old_row),
                            self._memory_snapshot(refreshed_old),
                            reason=f"由记忆 #{memory_id} 取代",
                        )
                self._insert_memory_evidence(
                    conn,
                    target_group,
                    memory_id,
                    verified_evidence,
                    evidence_role="correction" if superseded_ids else "support",
                )
                new_row = conn.execute("SELECT * FROM group_memories WHERE id = ?", (memory_id,)).fetchone()
                self._record_memory_change(
                    conn,
                    target_group,
                    memory_id,
                    "corrected" if superseded_ids else "created",
                    {},
                    self._memory_snapshot(new_row),
                )

            for conflict_id in conflicts:
                if conflict_id == memory_id:
                    raise ValueError("记忆不能与自身建立冲突")
                self._record_memory_conflict_tx(
                    conn,
                    target_group,
                    memory_id,
                    conflict_id,
                    normalized_conflict_reason,
                )

        result = self.get_group_memory(target_group, memory_id, include_evidence=True)
        if result is None:  # pragma: no cover - the transaction just inserted it
            raise RuntimeError("记忆写入成功后无法读取")
        return result

    def get_group_memory(
        self,
        group_id: str,
        memory_id: int,
        *,
        include_evidence: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Fetch by both group and ID so callers cannot cross group scope."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM group_memories WHERE group_id = ? AND id = ?",
                (str(group_id), int(memory_id)),
            ).fetchone()
        if not row:
            return None
        item = self._memory_row(row)
        if include_evidence:
            self._attach_memory_evidence(str(group_id), [item])
        self._attach_memory_conflict_ids(str(group_id), [item])
        return item

    def list_group_memories(
        self,
        group_id: str,
        *,
        active_only: bool = True,
        kinds: Optional[Sequence[str]] = None,
        confidence_status: Optional[str] = None,
        limit: int = 200,
        include_evidence: bool = True,
    ) -> List[Dict[str, Any]]:
        """List bounded memories from exactly one group, newest first."""

        safe_limit = max(1, min(int(limit), 1_000))
        clauses = ["group_id = ?"]
        params: List[Any] = [str(group_id)]
        if active_only:
            clauses.append("active = 1")
        normalized_kinds = [
            bounded_text(kind, name="记忆类型", maximum=MAX_MEMORY_KIND_CHARS, required=True).lower()
            for kind in (kinds or [])
        ]
        if normalized_kinds:
            clauses.append("kind IN (" + ",".join("?" for _ in normalized_kinds) + ")")
            params.extend(normalized_kinds)
        if confidence_status:
            normalized_status = normalize_confidence_status(confidence_status, allow_retracted=True)
            clauses.append("confidence_status = ?")
            params.append(normalized_status)
        params.append(safe_limit)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM group_memories WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        items = [self._memory_row(row) for row in rows]
        if include_evidence:
            self._attach_memory_evidence(str(group_id), items)
        self._attach_memory_conflict_ids(str(group_id), items)
        return items

    def search_group_memories(
        self,
        group_id: str,
        query: str,
        *,
        active_only: bool = True,
        limit: int = 20,
        include_evidence: bool = True,
    ) -> List[Dict[str, Any]]:
        """Search one group's text via FTS5, falling back safely to LIKE."""

        query_text = bounded_text(query, name="记忆搜索词", maximum=2_000)
        if not query_text:
            return self.list_group_memories(
                group_id,
                active_only=active_only,
                limit=limit,
                include_evidence=include_evidence,
            )
        safe_limit = max(1, min(int(limit), 200))
        rows: List[sqlite3.Row] = []
        fts_expression = make_fts_query(query_text)
        use_like = self.memory_search_backend != "fts5"
        if self.memory_search_backend == "fts5" and fts_expression:
            active_clause = " AND gm.active = 1" if active_only else ""
            try:
                with self._lock:
                    rows = self._connection.execute(
                        "SELECT gm.*, bm25(group_memories_fts) AS search_rank "
                        "FROM group_memories_fts "
                        "JOIN group_memories AS gm ON gm.id = group_memories_fts.rowid "
                        "WHERE group_memories_fts MATCH ? AND gm.group_id = ?"
                        + active_clause
                        + " ORDER BY search_rank ASC, gm.updated_at DESC, gm.id DESC LIMIT ?",
                        (fts_expression, str(group_id), safe_limit),
                    ).fetchall()
            except sqlite3.OperationalError:
                # A corrupt/unsupported derived index must not make memories
                # disappear.  Degrade this process to the ordinary text path.
                self.memory_search_backend = "like"
                rows = []
                use_like = True
            # unicode61 tokenization does not provide substring matching for
            # every CJK build.  A zero-result literal FTS query therefore
            # gets the deterministic LIKE path too, without disabling FTS
            # for later exact/token searches.
            if not rows:
                use_like = True
        if use_like:
            escaped = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = "%" + escaped + "%"
            active_clause = " AND active = 1" if active_only else ""
            with self._lock:
                rows = self._connection.execute(
                    "SELECT * FROM group_memories WHERE group_id = ?"
                    + active_clause
                    + " AND (memory_key LIKE ? ESCAPE '\\' OR kind LIKE ? ESCAPE '\\' "
                    "OR statement LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\' "
                    "OR predicate LIKE ? ESCAPE '\\' OR object_value LIKE ? ESCAPE '\\') "
                    "ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (str(group_id), pattern, pattern, pattern, pattern, pattern, pattern, safe_limit),
                ).fetchall()
        items = [self._memory_row(row) for row in rows]
        if include_evidence:
            self._attach_memory_evidence(str(group_id), items)
        self._attach_memory_conflict_ids(str(group_id), items)
        return items

    def confirm_group_memory(
        self,
        group_id: str,
        memory_id: int,
        *,
        evidence: Optional[Iterable[Mapping[str, Any]] | Mapping[str, Any]] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Mark an active memory confirmed, optionally adding fresh evidence."""

        target_group = str(group_id)
        normalized_reason = bounded_text(reason, name="确认说明", maximum=MAX_MEMORY_REASON_CHARS)
        normalized_evidence = normalize_memory_evidence(evidence) if evidence is not None else []
        with self.transaction() as conn:
            row = self._require_group_memory(conn, target_group, int(memory_id))
            if not int(row["active"]):
                raise ValueError("只能确认当前有效的记忆")
            before = self._memory_snapshot(row)
            if normalized_evidence:
                verified = self._verify_memory_evidence(conn, target_group, normalized_evidence)
                self._insert_memory_evidence(
                    conn, target_group, int(memory_id), verified, evidence_role="confirmation"
                )
            conn.execute(
                "UPDATE group_memories SET confidence_status = 'confirmed', updated_at = ? "
                "WHERE group_id = ? AND id = ?",
                (utc_now(), target_group, int(memory_id)),
            )
            after = conn.execute("SELECT * FROM group_memories WHERE id = ?", (int(memory_id),)).fetchone()
            self._record_memory_change(
                conn,
                target_group,
                int(memory_id),
                "confirmed",
                before,
                self._memory_snapshot(after),
                normalized_reason,
            )
        result = self.get_group_memory(target_group, int(memory_id))
        if result is None:  # pragma: no cover
            raise RuntimeError("确认后的记忆无法读取")
        return result

    def correct_group_memory(
        self,
        group_id: str,
        memory_id: int,
        *,
        statement: str,
        evidence: Iterable[Mapping[str, Any]] | Mapping[str, Any],
        kind: Optional[str] = None,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        object_value: Optional[str] = None,
        confidence_status: str = "confirmed",
        valid_from: Optional[str] = None,
        valid_until: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a corrected revision and keep the replaced row auditable."""

        old = self.get_group_memory(group_id, memory_id, include_evidence=False)
        if old is None:
            raise ValueError("当前群中不存在该记忆")
        return self.upsert_group_memory(
            str(group_id),
            str(old["memory_key"]),
            str(old["kind"] if kind is None else kind),
            statement,
            evidence,
            subject=str(old["subject"] if subject is None else subject),
            predicate=str(old["predicate"] if predicate is None else predicate),
            object_value=str(old["object_value"] if object_value is None else object_value),
            confidence_status=confidence_status,
            valid_from=str(old["valid_from"] if valid_from is None else valid_from),
            valid_until=str(old["valid_until"] if valid_until is None else valid_until),
            metadata=dict(old.get("metadata") or {}) if metadata is None else metadata,
            supersedes_memory_ids=[int(memory_id)],
        )

    def retract_group_memory(
        self,
        group_id: str,
        memory_id: int,
        reason: str,
        *,
        evidence: Optional[Iterable[Mapping[str, Any]] | Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Soft-retract a memory; no evidence, revision, or audit is deleted."""

        target_group = str(group_id)
        normalized_reason = bounded_text(
            reason, name="撤回原因", maximum=MAX_MEMORY_REASON_CHARS, required=True
        )
        normalized_evidence = normalize_memory_evidence(evidence) if evidence is not None else []
        with self.transaction() as conn:
            row = self._require_group_memory(conn, target_group, int(memory_id))
            if str(row["confidence_status"]) == "retracted":
                result = self._memory_row(row)
            else:
                before = self._memory_snapshot(row)
                if normalized_evidence:
                    verified = self._verify_memory_evidence(conn, target_group, normalized_evidence)
                    self._insert_memory_evidence(
                        conn, target_group, int(memory_id), verified, evidence_role="retraction"
                    )
                now = utc_now()
                conn.execute(
                    "UPDATE group_memories SET confidence_status = 'retracted', active = 0, "
                    "retraction_reason = ?, updated_at = ? WHERE group_id = ? AND id = ?",
                    (normalized_reason, now, target_group, int(memory_id)),
                )
                conn.execute(
                    "UPDATE group_memory_conflicts SET status = 'resolved', "
                    "resolution = ?, resolved_at = ? WHERE group_id = ? AND status = 'open' "
                    "AND (memory_id_low = ? OR memory_id_high = ?)",
                    (
                        "冲突中的记忆 #%s 已撤回：%s" % (int(memory_id), normalized_reason),
                        now,
                        target_group,
                        int(memory_id),
                        int(memory_id),
                    ),
                )
                after = conn.execute("SELECT * FROM group_memories WHERE id = ?", (int(memory_id),)).fetchone()
                self._record_memory_change(
                    conn,
                    target_group,
                    int(memory_id),
                    "retracted",
                    before,
                    self._memory_snapshot(after),
                    normalized_reason,
                )
                result = self._memory_row(after)
        full = self.get_group_memory(target_group, int(memory_id))
        return full if full is not None else result

    def record_group_memory_conflict(
        self,
        group_id: str,
        memory_id: int,
        conflicting_memory_id: int,
        reason: str,
    ) -> Dict[str, Any]:
        """Record two same-group assertions that cannot both be trusted."""

        normalized_reason = bounded_text(
            reason, name="冲突说明", maximum=MAX_MEMORY_REASON_CHARS, required=True
        )
        with self.transaction() as conn:
            conflict_id = self._record_memory_conflict_tx(
                conn,
                str(group_id),
                int(memory_id),
                int(conflicting_memory_id),
                normalized_reason,
            )
        conflicts = self.list_group_memory_conflicts(str(group_id), conflict_id=conflict_id)
        return conflicts[0]

    def list_group_memory_conflicts(
        self,
        group_id: str,
        *,
        status: Optional[str] = None,
        conflict_id: Optional[int] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        clauses = ["c.group_id = ?"]
        params: List[Any] = [str(group_id)]
        if status:
            normalized_status = str(status).strip().lower()
            if normalized_status not in {"open", "resolved"}:
                raise ValueError("冲突状态必须是 open 或 resolved")
            clauses.append("c.status = ?")
            params.append(normalized_status)
        if conflict_id is not None:
            clauses.append("c.id = ?")
            params.append(int(conflict_id))
        params.append(max(1, min(int(limit), 1_000)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT c.*, low.statement AS memory_statement, "
                "high.statement AS conflicting_statement "
                "FROM group_memory_conflicts AS c "
                "JOIN group_memories AS low ON low.id = c.memory_id_low AND low.group_id = c.group_id "
                "JOIN group_memories AS high ON high.id = c.memory_id_high AND high.group_id = c.group_id "
                "WHERE "
                + " AND ".join(clauses)
                + " ORDER BY c.id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_group_memory_conflict(
        self,
        group_id: str,
        conflict_id: int,
        resolution: str,
        *,
        resolution_memory_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        normalized_resolution = bounded_text(
            resolution, name="冲突解决说明", maximum=MAX_MEMORY_REASON_CHARS, required=True
        )
        target_group = str(group_id)
        with self.transaction() as conn:
            conflict = conn.execute(
                "SELECT * FROM group_memory_conflicts WHERE group_id = ? AND id = ?",
                (target_group, int(conflict_id)),
            ).fetchone()
            if not conflict:
                raise ValueError("当前群中不存在该记忆冲突")
            if resolution_memory_id is not None:
                self._require_group_memory(conn, target_group, int(resolution_memory_id))
            conn.execute(
                "UPDATE group_memory_conflicts SET status = 'resolved', resolution = ?, "
                "resolution_memory_id = ?, resolved_at = ? WHERE group_id = ? AND id = ?",
                (
                    normalized_resolution,
                    int(resolution_memory_id) if resolution_memory_id is not None else None,
                    utc_now(),
                    target_group,
                    int(conflict_id),
                ),
            )
        return self.list_group_memory_conflicts(target_group, conflict_id=int(conflict_id))[0]

    def list_group_memory_changes(
        self,
        group_id: str,
        *,
        memory_id: Optional[int] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        clauses = ["group_id = ?"]
        params: List[Any] = [str(group_id)]
        if memory_id is not None:
            clauses.append("memory_id = ?")
            params.append(int(memory_id))
        params.append(max(1, min(int(limit), 1_000)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM group_memory_changes WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["before"] = json_loads(item.pop("before_json", "{}"), {})
            item["after"] = json_loads(item.pop("after_json", "{}"), {})
            result.append(item)
        return result

    @staticmethod
    def _normalize_memory_ids(values: Iterable[int], name: str) -> List[int]:
        result = set()
        for value in values or ():
            try:
                memory_id = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} 中的记忆 ID 必须是整数") from exc
            if memory_id <= 0:
                raise ValueError(f"{name} 中的记忆 ID 必须为正数")
            result.add(memory_id)
        return sorted(result)

    def _verify_memory_evidence(
        self,
        conn: sqlite3.Connection,
        group_id: str,
        evidence: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        verified: List[Dict[str, Any]] = []
        for item in evidence:
            value = dict(item)
            event_id = value.get("source_event_id")
            message_id = str(value.get("source_message_id") or "")
            evidence_text = str(value.get("evidence_text") or "")
            source_texts: List[str] = []
            if event_id is not None:
                event_row = conn.execute(
                    "SELECT group_id, message_id, occurred_at, created_at, normalized_text "
                    "FROM events WHERE id = ?",
                    (int(event_id),),
                ).fetchone()
                if not event_row:
                    raise ValueError(f"证据事件 #{event_id} 不存在")
                if str(event_row["group_id"]) != group_id:
                    raise ValueError(f"证据事件 #{event_id} 不属于当前群")
                recorded_message_id = str(event_row["message_id"] or "")
                if message_id and recorded_message_id and message_id != recorded_message_id:
                    raise ValueError(f"证据事件 #{event_id} 与 message_id {message_id} 不匹配")
                if not message_id:
                    message_id = recorded_message_id
                if not value.get("observed_at"):
                    value["observed_at"] = str(event_row["created_at"] or "")
                source_texts.append(str(event_row["normalized_text"] or ""))
            if message_id:
                message_rows = conn.execute(
                    "SELECT normalized_text AS source_text FROM events "
                    "WHERE group_id = ? AND message_id = ? "
                    "UNION ALL "
                    "SELECT content AS source_text FROM sent_messages "
                    "WHERE group_id = ? AND message_id = ?",
                    (group_id, message_id, group_id, message_id),
                ).fetchall()
                if not message_rows and event_id is None:
                    raise ValueError(f"证据消息 {message_id} 不属于当前群或尚未记录")
                if event_id is None:
                    source_texts.extend(str(row["source_text"] or "") for row in message_rows)
            if not any(evidence_text in source_text for source_text in source_texts if source_text):
                source_label = f"事件 #{event_id}" if event_id is not None else f"消息 {message_id}"
                raise ValueError(f"证据文本不是{source_label}正文的逐字片段")
            value["source_message_id"] = message_id
            verified.append(value)
        return verified

    @staticmethod
    def _insert_memory_evidence(
        conn: sqlite3.Connection,
        group_id: str,
        memory_id: int,
        evidence: Sequence[Dict[str, Any]],
        *,
        evidence_role: str,
    ) -> None:
        for item in evidence:
            conn.execute(
                "INSERT OR IGNORE INTO group_memory_evidence("
                "memory_id, group_id, evidence_role, source_event_id, source_message_id, "
                "evidence_text, observed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(memory_id),
                    group_id,
                    evidence_role,
                    item.get("source_event_id"),
                    str(item.get("source_message_id") or ""),
                    str(item.get("evidence_text") or ""),
                    str(item.get("observed_at") or ""),
                    utc_now(),
                ),
            )

    @staticmethod
    def _memory_snapshot(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
        if row is None:
            return {}
        value = dict(row)
        value["metadata"] = json_loads(value.pop("metadata_json", "{}"), {})
        value["active"] = bool(value.get("active"))
        return value

    def _memory_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        value.pop("search_rank", None)
        value["metadata"] = json_loads(value.pop("metadata_json", "{}"), {})
        value["active"] = bool(value.get("active"))
        return value

    def _attach_memory_evidence(self, group_id: str, memories: List[Dict[str, Any]]) -> None:
        if not memories:
            return
        memory_ids = [int(item["id"]) for item in memories]
        placeholders = ",".join("?" for _ in memory_ids)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM group_memory_evidence WHERE group_id = ? AND memory_id IN ("
                + placeholders
                + ") ORDER BY id ASC",
                [group_id, *memory_ids],
            ).fetchall()
        by_memory: Dict[int, List[Dict[str, Any]]] = {memory_id: [] for memory_id in memory_ids}
        for row in rows:
            by_memory[int(row["memory_id"])].append(dict(row))
        for item in memories:
            item["evidence"] = by_memory.get(int(item["id"]), [])

    def _attach_memory_conflict_ids(self, group_id: str, memories: List[Dict[str, Any]]) -> None:
        if not memories:
            return
        memory_ids = [int(item["id"]) for item in memories]
        placeholders = ",".join("?" for _ in memory_ids)
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, memory_id_low, memory_id_high FROM group_memory_conflicts "
                "WHERE group_id = ? AND status = 'open' AND (memory_id_low IN ("
                + placeholders
                + ") OR memory_id_high IN ("
                + placeholders
                + "))",
                [group_id, *memory_ids, *memory_ids],
            ).fetchall()
        conflict_ids: Dict[int, List[int]] = {memory_id: [] for memory_id in memory_ids}
        conflicts_with: Dict[int, List[int]] = {memory_id: [] for memory_id in memory_ids}
        for row in rows:
            low = int(row["memory_id_low"])
            high = int(row["memory_id_high"])
            if low in conflict_ids:
                conflict_ids[low].append(int(row["id"]))
                conflicts_with[low].append(high)
            if high in conflict_ids:
                conflict_ids[high].append(int(row["id"]))
                conflicts_with[high].append(low)
        for item in memories:
            item["open_conflict_ids"] = conflict_ids.get(int(item["id"]), [])
            item["conflicts_with_memory_ids"] = conflicts_with.get(int(item["id"]), [])

    @staticmethod
    def _record_memory_change(
        conn: sqlite3.Connection,
        group_id: str,
        memory_id: int,
        action: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        reason: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO group_memory_changes("
            "memory_id, group_id, action, before_json, after_json, reason, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                int(memory_id),
                group_id,
                action,
                json_dumps(dict(before)),
                json_dumps(dict(after)),
                reason,
                utc_now(),
            ),
        )

    @staticmethod
    def _require_group_memory(
        conn: sqlite3.Connection,
        group_id: str,
        memory_id: int,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM group_memories WHERE group_id = ? AND id = ?",
            (group_id, int(memory_id)),
        ).fetchone()
        if not row:
            raise ValueError("当前群中不存在该记忆")
        return row

    def _memory_rows_for_update(
        self,
        conn: sqlite3.Connection,
        group_id: str,
        memory_ids: Iterable[int],
    ) -> List[sqlite3.Row]:
        ids = sorted(set(int(item) for item in memory_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            "SELECT * FROM group_memories WHERE group_id = ? AND id IN (" + placeholders + ")",
            [group_id, *ids],
        ).fetchall()
        found = {int(row["id"]) for row in rows}
        missing = [memory_id for memory_id in ids if memory_id not in found]
        if missing:
            raise ValueError("以下待取代记忆不属于当前群或不存在：%s" % ", ".join(map(str, missing)))
        return list(rows)

    def _record_memory_conflict_tx(
        self,
        conn: sqlite3.Connection,
        group_id: str,
        memory_id: int,
        conflicting_memory_id: int,
        reason: str,
    ) -> int:
        if int(memory_id) == int(conflicting_memory_id):
            raise ValueError("记忆不能与自身建立冲突")
        self._require_group_memory(conn, group_id, int(memory_id))
        self._require_group_memory(conn, group_id, int(conflicting_memory_id))
        low, high = sorted((int(memory_id), int(conflicting_memory_id)))
        now = utc_now()
        conn.execute(
            "INSERT INTO group_memory_conflicts("
            "group_id, memory_id_low, memory_id_high, reason, status, created_at"
            ") VALUES (?, ?, ?, ?, 'open', ?) "
            "ON CONFLICT(group_id, memory_id_low, memory_id_high) DO UPDATE SET "
            "reason = excluded.reason, status = 'open', resolution = '', "
            "resolution_memory_id = NULL, resolved_at = ''",
            (group_id, low, high, reason, now),
        )
        row = conn.execute(
            "SELECT id FROM group_memory_conflicts "
            "WHERE group_id = ? AND memory_id_low = ? AND memory_id_high = ?",
            (group_id, low, high),
        ).fetchone()
        conflict_id = int(row["id"])
        for target_id in (low, high):
            memory_row = conn.execute("SELECT * FROM group_memories WHERE id = ?", (target_id,)).fetchone()
            self._record_memory_change(
                conn,
                group_id,
                target_id,
                "conflict_recorded",
                self._memory_snapshot(memory_row),
                self._memory_snapshot(memory_row),
                reason,
            )
        return conflict_id

    @staticmethod
    def _resolve_superseded_memory_conflicts(
        conn: sqlite3.Connection,
        group_id: str,
        superseded_ids: Iterable[int],
        *,
        replacement_memory_id: int,
        resolved_at: str,
    ) -> None:
        """Close conflicts whose assertion was replaced by a newer revision."""

        ids = sorted({int(item) for item in superseded_ids})
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            "UPDATE group_memory_conflicts SET status = 'resolved', resolution = ?, "
            "resolution_memory_id = ?, resolved_at = ? "
            "WHERE group_id = ? AND status = 'open' AND (memory_id_low IN ("
            + placeholders
            + ") OR memory_id_high IN ("
            + placeholders
            + "))",
            [
                "冲突中的旧记忆已由记忆 #%s 取代。" % int(replacement_memory_id),
                int(replacement_memory_id),
                resolved_at,
                group_id,
                *ids,
                *ids,
            ],
        )

    def _event_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        value["content"] = json_loads(value.pop("content_json", "{}"), {})
        value["raw"] = json_loads(value.pop("raw_json", "{}"), {})
        return value

    def get_summary(self, group_id: str) -> str:
        with self._lock:
            row = self._connection.execute("SELECT content FROM summaries WHERE group_id = ?", (str(group_id),)).fetchone()
        return str(row["content"]) if row else ""

    def get_summary_record(self, group_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM summaries WHERE group_id = ?", (str(group_id),)
            ).fetchone()
        return dict(row) if row else {
            "group_id": str(group_id),
            "content": "",
            "last_event_id": 0,
            "updated_at": "",
        }

    def save_summary(
        self,
        group_id: str,
        content: str,
        last_event_id: int,
        *,
        turn_id: int = 0,
        snapshot_reason: str = "",
    ) -> None:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO summaries(group_id, content, last_event_id, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET content = excluded.content, last_event_id = excluded.last_event_id, updated_at = excluded.updated_at",
                (str(group_id), content, int(last_event_id), now),
            )
            if str(content).strip():
                conn.execute(
                    "INSERT INTO summary_snapshots(group_id, turn_id, content, last_event_id, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(group_id),
                        int(turn_id),
                        str(content),
                        int(last_event_id),
                        str(snapshot_reason or "success"),
                        now,
                    ),
                )

    def ensure_summary_snapshot(
        self,
        group_id: str,
        content: str,
        last_event_id: int,
        *,
        reason: str = "baseline",
    ) -> None:
        """Seed a checkpoint once for an older installation.

        Existing databases predate ``summary_snapshots``.  A valid live
        summary is safe to checkpoint, while an empty row is intentionally
        ignored so recovery can rebuild from durable events instead.
        """

        if not str(content).strip():
            return
        target_group = str(group_id)
        with self.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM summary_snapshots WHERE group_id = ? LIMIT 1",
                (target_group,),
            ).fetchone()
            if exists:
                return
            conn.execute(
                "INSERT INTO summary_snapshots(group_id, turn_id, content, last_event_id, reason, created_at) "
                "VALUES (?, 0, ?, ?, ?, ?)",
                (target_group, str(content), int(last_event_id), str(reason), utc_now()),
            )

    def list_summary_snapshots(self, group_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1_000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM summary_snapshots WHERE group_id = ? ORDER BY id DESC LIMIT ?",
                (str(group_id), safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def restore_summary_snapshot(
        self,
        group_id: str,
        content: str,
        last_event_id: int,
        *,
        reason: str = "automatic rollback",
    ) -> None:
        """Restore the live pointer and record the recovery as a checkpoint."""

        now = utc_now()
        target_group = str(group_id)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO summaries(group_id, content, last_event_id, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET content = excluded.content, "
                "last_event_id = excluded.last_event_id, updated_at = excluded.updated_at",
                (target_group, str(content), int(last_event_id), now),
            )
            conn.execute(
                "INSERT INTO summary_snapshots(group_id, turn_id, content, last_event_id, reason, created_at) "
                "VALUES (?, 0, ?, ?, ?, ?)",
                (target_group, str(content), int(last_event_id), str(reason), now),
            )

    def reset_summary_segment(
        self,
        group_id: str,
        after_event_id: int,
        through_event_id: int,
    ) -> int:
        """Unarchive a bounded interval for summary-only recomputation.

        Events are made non-pending deliberately: a rollback must never replay
        old QQ-changing tools.  They will be fed to archive-only turns, where
        the service exposes no live action batch, while new events arriving
        after ``through_event_id`` retain their normal pending state.
        """

        lower = int(after_event_id)
        upper = int(through_event_id)
        if upper <= lower:
            return 0
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE events SET archived = 0, pending = 0 "
                "WHERE group_id = ? AND id > ? AND id <= ?",
                (str(group_id), lower, upper),
            )
            return int(cursor.rowcount or 0)

    def max_event_id(self, group_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS max_id FROM events WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
        return int(row["max_id"] if row else 0)

    def mark_events_processed(self, event_ids: Iterable[int]) -> None:
        ids = [int(item) for item in event_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.transaction() as conn:
            conn.execute("UPDATE events SET pending = 0 WHERE id IN (" + placeholders + ")", ids)

    def mark_events_archived(self, event_ids: Iterable[int]) -> None:
        """Advance only the rolling-summary cursor for the supplied events."""

        ids = [int(item) for item in event_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.transaction() as conn:
            conn.execute("UPDATE events SET archived = 1 WHERE id IN (" + placeholders + ")", ids)

    def reset_pending(self, group_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE events SET pending = 1 WHERE group_id = ?", (str(group_id),))

    def reset_group_memory_and_recompute(self, group_id: str) -> Dict[str, int]:
        """Erase one group's derived memory and rewind all derived cursors.

        Raw events, sent-message records, tool audits and media are deliberately
        retained.  The operation only removes derived memory/revision rows and
        rolling-summary checkpoints, then makes every stored event eligible for
        a fresh summary/memory pass.  Events are kept non-pending so a reset can
        never replay an old QQ side effect such as sending or recalling a
        message.
        """

        target_group = str(group_id)
        counts: Dict[str, int] = {}
        with self.transaction() as conn:
            group = conn.execute(
                "SELECT 1 FROM groups WHERE group_id = ?", (target_group,)
            ).fetchone()
            if group is None:
                raise ValueError("未知群聊：" + target_group)

            for key, table in (
                ("conflicts", "group_memory_conflicts"),
                ("evidence", "group_memory_evidence"),
                ("changes", "group_memory_changes"),
                ("memories", "group_memories"),
                ("summary_snapshots", "summary_snapshots"),
            ):
                row = conn.execute(
                    "SELECT COUNT(1) AS value FROM %s WHERE group_id = ?" % table,
                    (target_group,),
                ).fetchone()
                counts[key] = int(row["value"] if row else 0)

            # Break the self-referential supersession links before deleting the
            # group rows. SQLite enforces this foreign key immediately.
            conn.execute(
                "UPDATE group_memories SET superseded_by_memory_id = NULL WHERE group_id = ?",
                (target_group,),
            )
            conn.execute("DELETE FROM group_memory_conflicts WHERE group_id = ?", (target_group,))
            conn.execute("DELETE FROM group_memory_evidence WHERE group_id = ?", (target_group,))
            conn.execute("DELETE FROM group_memory_changes WHERE group_id = ?", (target_group,))
            conn.execute("DELETE FROM group_memories WHERE group_id = ?", (target_group,))
            conn.execute("DELETE FROM summary_snapshots WHERE group_id = ?", (target_group,))
            conn.execute("DELETE FROM summaries WHERE group_id = ?", (target_group,))

            event_row = conn.execute(
                "SELECT COUNT(1) AS value FROM events WHERE group_id = ?", (target_group,)
            ).fetchone()
            counts["events"] = int(event_row["value"] if event_row else 0)
            conn.execute(
                "UPDATE events SET memory_processed = 0, archived = 0, pending = 0 "
                "WHERE group_id = ?",
                (target_group,),
            )
            # This is the group-scoped rules field shown in the dashboard. The
            # global administrator rules.md is intentionally not cleared here,
            # because doing so from one group would alter every conversation.
            conn.execute(
                "UPDATE groups SET prompt_override = '', last_error = '', updated_at = ? WHERE group_id = ?",
                (utc_now(), target_group),
            )
        return counts

    def message_groups(self, message_id: str) -> List[str]:
        """Return every locally recorded group that owns ``message_id``.

        A OneBot message normally has an account-wide unique ID, but this
        lookup deliberately does not rely on that assumption.  It lets the
        tool boundary distinguish an invented/stale optional reply target
        from a real message in a *different* group.  App-originated messages
        are included even when their OneBot echo has not arrived yet.
        """

        value = str(message_id)
        if not value:
            return []
        with self._lock:
            rows = self._connection.execute(
                "SELECT group_id FROM events WHERE message_id = ? "
                "UNION "
                "SELECT group_id FROM sent_messages WHERE message_id = ? "
                "ORDER BY group_id",
                (value, value),
            ).fetchall()
        return [str(row["group_id"]) for row in rows]

    def has_group_message(self, group_id: str, message_id: str) -> bool:
        """Whether a message is locally recorded as belonging to this group."""

        target_group = str(group_id)
        return target_group in self.message_groups(message_id)

    def mark_app_sent_event_ignored(self, group_id: str, message_id: str) -> None:
        """Suppress an echo that raced ahead of a tool's OneBot response."""

        if not message_id:
            return
        with self.transaction() as conn:
            conn.execute(
                "UPDATE events SET is_self = 1, pending = 0 WHERE group_id = ? AND message_id = ?",
                (str(group_id), str(message_id)),
            )

    # Turns and tool audit
    def create_turn(self, group_id: str, event_ids: Iterable[int]) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO turns(group_id, status, event_ids_json, started_at) VALUES (?, 'running', ?, ?)",
                (str(group_id), json_dumps(list(event_ids)), utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_turn(self, turn_id: int, status: str, summary_text: str = "", error: str = "") -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE turns SET status = ?, summary_text = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, summary_text, error[:4000], utc_now(), int(turn_id)),
            )

    def cancel_running_turns(self, reason: str = "service restarted") -> int:
        """Close turns left in ``running`` state by an interrupted process.

        Events remain pending, and the durable tool-operation journal still
        prevents replaying a QQ action whose outcome was already recorded.
        A fresh worker can therefore retry the unfinished batch without
        leaving the dashboard stuck on an impossible forever-running turn.
        """

        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE turns SET status = 'cancelled', error = ?, finished_at = ? "
                "WHERE status = 'running'",
                (str(reason)[:4000], now),
            )
            return int(cursor.rowcount or 0)

    def get_turn_event_ids(self, turn_id: int) -> List[int]:
        with self._lock:
            row = self._connection.execute("SELECT event_ids_json FROM turns WHERE id = ?", (int(turn_id),)).fetchone()
        values = json_loads(row["event_ids_json"] if row else None, [])
        if not isinstance(values, list):
            return []
        result: List[int] = []
        for value in values:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                continue
        return result

    def list_recent_turns(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM turns ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def list_group_turns(self, group_id: str, limit: int = 1_000) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 10_000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM turns WHERE group_id = ? ORDER BY id DESC LIMIT ?",
                (str(group_id), safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_tool_audit(self, turn_id: int, tool_call_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tool_audits WHERE turn_id = ? AND tool_call_id = ?", (int(turn_id), tool_call_id)
            ).fetchone()
        return dict(row) if row else None

    def add_tool_audit(
        self,
        turn_id: int,
        group_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Dict[str, Any],
        status: str,
        message_id: str = "",
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tool_audits(turn_id, group_id, tool_call_id, tool_name, arguments_json, result_json, status, message_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(turn_id), str(group_id), tool_call_id, tool_name, json_dumps(arguments), json_dumps(result), status,
                    str(message_id), utc_now(),
                ),
            )

    def list_recent_audits(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM tool_audits ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def reserve_tool_operation(
        self,
        operation_key: str,
        group_id: str,
        event_ids: Iterable[int],
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Reserve a state-changing action, returning an older reservation.

        A non-``None`` value means this batch has already reached the action
        boundary.  Callers must return its stored result rather than call QQ
        again.  A ``running`` reservation represents an ambiguous crash window
        and is intentionally treated as non-retryable for safety.
        """

        now = utc_now()
        unknown_result = {
            "ok": False,
            "error": "此前工具执行的结果未知；为避免重复影响 QQ，未再次执行。",
        }
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO tool_operations("
                "operation_key, group_id, event_ids_json, tool_name, arguments_json, status, result_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)",
                (
                    operation_key,
                    str(group_id),
                    json_dumps(list(event_ids)),
                    tool_name,
                    json_dumps(arguments),
                    json_dumps(unknown_result),
                    now,
                    now,
                ),
            )
            if cursor.rowcount:
                return None
            row = conn.execute(
                "SELECT * FROM tool_operations WHERE operation_key = ?", (operation_key,)
            ).fetchone()
        return dict(row) if row else {"result_json": json_dumps(unknown_result), "status": "running"}

    def finish_tool_operation(self, operation_key: str, status: str, result: Dict[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE tool_operations SET status = ?, result_json = ?, updated_at = ? WHERE operation_key = ?",
                (status, json_dumps(result), utc_now(), operation_key),
            )

    def add_sent_message(self, message_id: str, group_id: str, turn_id: int, content: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sent_messages(message_id, group_id, turn_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(message_id), str(group_id), int(turn_id), content, utc_now()),
            )

    def get_sent_message(self, message_id: str, group_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sent_messages WHERE message_id = ? AND group_id = ?", (str(message_id), str(group_id))
            ).fetchone()
        return dict(row) if row else None

    def mark_sent_message_recalled(self, message_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE sent_messages SET recalled = 1 WHERE message_id = ?", (str(message_id),))
