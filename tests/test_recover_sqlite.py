"""Safety coverage for the offline SQLite recovery helper."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import Database
from scripts.recover_sqlite import recover


def test_recovery_supports_a_separate_unicode_destination_directory(tmp_path) -> None:
    source_dir = tmp_path / "损坏 源目录"
    destination_dir = tmp_path / "健康 目标目录"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "agent.sqlite3"
    destination = destination_dir / "恢复 数据.sqlite3"

    database = Database(source)
    database.close()

    copied = recover(source, destination)

    assert destination.is_file()
    assert "events" in copied
    with sqlite3.connect(str(destination)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_recovery_refuses_to_ignore_a_source_wal_or_shm(tmp_path) -> None:
    source = tmp_path / "agent.sqlite3"
    destination = tmp_path / "recovered.sqlite3"
    database = Database(source)
    database.close()
    wal = Path(str(source) + "-wal")
    wal.write_bytes(b"uncheckpointed-test-data")

    with pytest.raises(RuntimeError, match="WAL/SHM"):
        recover(source, destination)

    assert not destination.exists()
