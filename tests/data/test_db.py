"""medsupply/data/db.py 계약 검증 — 커넥션 관리(WAL·FK·Row)와 init_db 재생성.

get_connection/init_db는 조회(queries.py)·쓰기(writer.py) 계층이 공유하는 유일한 커넥션
진입점이다. 이 테스트는 그 커넥션 단위 계약(PRAGMA·재생성 규칙)을 고정한다.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from medsupply import settings
from medsupply.data import db

EXPECTED_TABLE_COUNT = 16


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


# --- get_connection ----------------------------------------------------------


def test_get_connection_default_db_path_is_settings_db_path() -> None:
    """기본 인자는 settings.DB_PATH다(호출부가 매번 명시할 필요가 없어야 한다)."""
    default = inspect.signature(db.get_connection).parameters["db_path"].default
    assert default == settings.DB_PATH


def test_get_connection_sets_sqlite_row_factory(tmp_path: Path) -> None:
    conn = db.get_connection(tmp_path / "test.db")
    try:
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_get_connection_enables_foreign_keys(tmp_path: Path) -> None:
    conn = db.get_connection(tmp_path / "test.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_get_connection_enables_wal_journal_mode(tmp_path: Path) -> None:
    """WAL은 파일 기반 DB에서 확인한다(:memory:는 SQLite가 'memory' 모드로 무시한다)."""
    conn = db.get_connection(tmp_path / "test.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_get_connection_accepts_str_path(tmp_path: Path) -> None:
    conn = db.get_connection(str(tmp_path / "test.db"))
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_get_connection_foreign_keys_actually_enforced(tmp_path: Path) -> None:
    """PRAGMA만 켜진 게 아니라 실제로 FK 위반을 막는지 스키마까지 적용해 확인한다."""
    conn = db.get_connection(tmp_path / "test.db")
    try:
        db.init_db(conn, drop=False)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty,"
                " closing_stock) VALUES (?, ?, ?, ?, ?)",
                ("NO-SUCH-ITEM", "2026-08-01", 1, 0, 1),
            )
    finally:
        conn.close()


# --- init_db -------------------------------------------------------------------


def test_init_db_drop_false_creates_sixteen_tables() -> None:
    conn = db.get_connection(":memory:")
    try:
        db.init_db(conn, drop=False)
        assert len(_table_names(conn)) == EXPECTED_TABLE_COUNT
    finally:
        conn.close()


def test_init_db_drop_false_on_already_initialized_db_raises_operational_error() -> None:
    """drop=False는 빈 DB 전용이다 — 테이블이 이미 있으면 OperationalError를 그대로 전파한다."""
    conn = db.get_connection(":memory:")
    try:
        db.init_db(conn, drop=False)
        with pytest.raises(sqlite3.OperationalError):
            db.init_db(conn, drop=False)
    finally:
        conn.close()


def test_init_db_drop_true_recreates_and_clears_existing_data() -> None:
    conn = db.get_connection(":memory:")
    try:
        db.init_db(conn, drop=False)
        conn.execute("INSERT INTO meta(key, value) VALUES ('seed', '1')")
        conn.commit()

        db.init_db(conn, drop=True)

        assert len(_table_names(conn)) == EXPECTED_TABLE_COUNT
        assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0
    finally:
        conn.close()


def test_init_db_drop_true_on_fresh_db_does_not_raise() -> None:
    """아직 테이블이 없는 새 커넥션에도 drop=True를 그대로 써서 초기화할 수 있어야 한다."""
    conn = db.get_connection(":memory:")
    try:
        db.init_db(conn, drop=True)
        assert len(_table_names(conn)) == EXPECTED_TABLE_COUNT
    finally:
        conn.close()


def test_init_db_default_drop_is_false() -> None:
    """drop 기본값은 False다 — 두 번째 호출이 실수로 데이터를 지우지 않아야 한다."""
    default = inspect.signature(db.init_db).parameters["drop"].default
    assert default is False
