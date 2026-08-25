"""Recover a readable SQLite database whose table B-tree traversal is damaged.

The source is opened immutable/read-only and is never modified.  The caller
must provide a non-existent destination path.  Ordinary tables are copied in
stable row order; the ``turns`` table has an additional primary-key probe path
because that is the table affected by the known local corruption incident.
The destination is accepted only after row-count, row-content, foreign-key and
full SQLite integrity checks all pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import Database  # noqa: E402


TABLE_ORDER = (
    "settings",
    "groups",
    "events",
    "summaries",
    "turns",
    "tool_audits",
    "tool_operations",
    "sent_messages",
    "admin_messages",
    "group_memories",
    "group_memory_evidence",
    "group_memory_conflicts",
    "group_memory_changes",
)


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def row_digest(rows: Sequence[Sequence[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        payload = json.dumps(list(row), ensure_ascii=False, default=str, separators=(",", ":"))
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def table_columns(connection: sqlite3.Connection, table: str) -> List[str]:
    return [str(row[1]) for row in connection.execute("PRAGMA table_info(" + quote_identifier(table) + ")")]


def sqlite_sequence_value(connection: sqlite3.Connection, table: str) -> int:
    try:
        row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0]) if row else 0


def read_table_rows(connection: sqlite3.Connection, table: str, columns: Sequence[str]) -> List[Tuple[Any, ...]]:
    column_sql = ",".join(quote_identifier(column) for column in columns)
    table_sql = quote_identifier(table)
    scan_error: Exception | None = None
    try:
        scanned_rows = [
            tuple(row)
            for row in connection.execute(
                "SELECT " + column_sql + " FROM " + table_sql + " ORDER BY rowid"
            )
        ]
    except sqlite3.DatabaseError as exc:
        scan_error = exc
        scanned_rows = []
        if "id" not in columns:
            raise

    expected_count = -1
    try:
        # Force the canonical table B-tree.  A corrupt database can retain
        # dangling secondary-index entries after the underlying row is gone;
        # SQLite may otherwise choose that covering index for count(*) and
        # falsely report unrecoverable phantom rows as real data.
        count_clause = " NOT INDEXED" if "id" in columns else ""
        expected_count = int(
            connection.execute(
                "SELECT count(*) FROM " + table_sql + count_clause
            ).fetchone()[0]
        )
    except sqlite3.DatabaseError:
        pass
    if scan_error is None and (expected_count < 0 or expected_count == len(scanned_rows)):
        return scanned_rows
    if "id" not in columns:
        if scan_error is not None:
            raise scan_error
        raise sqlite3.DatabaseError(
            "%s 全表扫描返回 %s 行，但 count(*) 为 %s，且没有可探测的 id 主键"
            % (table, len(scanned_rows), expected_count)
        )

    # A malformed interior B-tree page may either fail a full scan or silently
    # omit leaf pages even though every row remains addressable by INTEGER
    # PRIMARY KEY.  Probe the durable sequence one ID at a time and retain
    # gaps exactly as gaps.
    maximum_id = sqlite_sequence_value(connection, table)
    if maximum_id <= 0:
        try:
            maximum_id = int(
                connection.execute("SELECT max(id) FROM " + table_sql).fetchone()[0] or 0
            )
        except sqlite3.DatabaseError as exc:
            raise sqlite3.DatabaseError(
                "%s 无法可靠全表读取，且无法确定主键恢复范围" % table
            ) from exc
    rows: List[Tuple[Any, ...]] = []
    for row_id in range(1, maximum_id + 1):
        row = connection.execute(
            "SELECT " + column_sql + " FROM " + table_sql + " WHERE id = ?", (row_id,)
        ).fetchone()
        if row is not None:
            rows.append(tuple(row))
    if expected_count >= 0 and len(rows) != expected_count:
        raise sqlite3.DatabaseError(
            "%s 逐主键恢复得到 %s 行，但 count(*) 为 %s；拒绝生成不完整恢复库"
            % (table, len(rows), expected_count)
        )
    return rows


def recover(source: Path, destination: Path) -> Dict[str, int]:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=False)
    if source == destination:
        raise ValueError("恢复目标不能覆盖源数据库")
    if destination.exists():
        raise FileExistsError("恢复目标已存在，拒绝覆盖：%s" % destination)
    if not destination.parent.is_dir():
        raise FileNotFoundError("恢复目标目录不存在：%s" % destination.parent)

    # immutable=1 deliberately ignores SQLite journals.  Refuse to use it
    # when sidecars exist: otherwise a caller could recover an older main-file
    # snapshot while silently discarding committed rows that only live in WAL.
    source_sidecars = [Path(str(source) + suffix) for suffix in ("-wal", "-shm")]
    present_sidecars = [path for path in source_sidecars if path.exists()]
    if present_sidecars:
        raise RuntimeError(
            "源数据库仍有 WAL/SHM；请先停止服务并保留完整文件组：%s"
            % ", ".join(str(path) for path in present_sidecars)
        )

    source_connection = sqlite3.connect(
        source.as_uri() + "?mode=ro&immutable=1", uri=True
    )
    source_connection.row_factory = sqlite3.Row
    source_tables = {
        str(row[0])
        for row in source_connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }

    # Build the destination from the current application's migrations, then
    # reopen it for a single controlled import transaction.
    Database(destination).close()
    target_connection = sqlite3.connect(str(destination))
    target_connection.execute("PRAGMA foreign_keys = OFF")
    copied: Dict[str, int] = {}
    source_snapshots: Dict[str, Tuple[List[str], List[Tuple[Any, ...]], str]] = {}
    try:
        target_connection.execute("BEGIN IMMEDIATE")
        for table in TABLE_ORDER:
            if table not in source_tables:
                continue
            source_columns = table_columns(source_connection, table)
            target_columns = table_columns(target_connection, table)
            missing_columns = [column for column in source_columns if column not in target_columns]
            if missing_columns:
                raise RuntimeError(
                    "%s 的目标表缺少列：%s" % (table, ", ".join(missing_columns))
                )
            rows = read_table_rows(source_connection, table, source_columns)
            source_snapshots[table] = (source_columns, rows, row_digest(rows))
            target_connection.execute("DELETE FROM " + quote_identifier(table))
            if rows:
                column_sql = ",".join(quote_identifier(column) for column in source_columns)
                placeholders = ",".join("?" for _ in source_columns)
                target_connection.executemany(
                    "INSERT INTO " + quote_identifier(table) + "(" + column_sql + ") VALUES (" + placeholders + ")",
                    rows,
                )
            copied[table] = len(rows)

        # Preserve AUTOINCREMENT high-water marks so a recovered database
        # never reuses a historical event/turn/tool ID that is absent today.
        sequence_rows = source_connection.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
        target_connection.execute("DELETE FROM sqlite_sequence")
        for row in sequence_rows:
            if str(row[0]) in TABLE_ORDER:
                target_connection.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                    (str(row[0]), int(row[1])),
                )
        target_connection.commit()

        # FTS content is derived.  Rebuild it after the canonical memory rows
        # exist; failure is acceptable only when this Python lacks FTS5.
        try:
            target_connection.execute(
                "INSERT INTO group_memories_fts(group_memories_fts) VALUES ('rebuild')"
            )
            target_connection.commit()
        except sqlite3.OperationalError:
            target_connection.rollback()

        for table, (columns, expected_rows, expected_digest) in source_snapshots.items():
            actual_rows = read_table_rows(target_connection, table, columns)
            if len(actual_rows) != len(expected_rows) or row_digest(actual_rows) != expected_digest:
                raise RuntimeError("%s 恢复后的行内容校验失败" % table)

        foreign_key_rows = target_connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_rows:
            raise RuntimeError("恢复库 foreign_key_check 失败：%s" % (foreign_key_rows[:10],))
        integrity_rows = target_connection.execute("PRAGMA integrity_check").fetchall()
        if [str(row[0]) for row in integrity_rows] != ["ok"]:
            raise RuntimeError("恢复库 integrity_check 失败：%s" % (integrity_rows[:20],))
    except Exception:
        target_connection.rollback()
        target_connection.close()
        source_connection.close()
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    target_connection.close()
    source_connection.close()
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description="无损迁移可读取的 SQLite 数据到完整新库")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    copied = recover(args.source, args.destination)
    print("恢复库已通过完整性校验：")
    for table, count in copied.items():
        print("  %s: %s 行" % (table, count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
