"""medsupply/data/schema.sql (v1) 계약 검증.

이 테스트는 스키마가 이후 모든 태스크가 소비하는 '데이터 계약'임을 고정한다.
- 테이블 16종 존재
- FK 강제(PRAGMA foreign_keys=ON)
- 상태값 CHECK 제약(위험등급 / 공고 확인상태)
- 유니크·복합 PK 제약
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "medsupply" / "data" / "schema.sql"

EXPECTED_TABLES = {
    # 마스터
    "ingredients",
    "ingredient_aliases",
    "substitute_groups",
    "items",
    # 재고·입고
    "stock_usage_daily",
    "incoming_shipments",
    # 공고
    "notices",
    "notice_extractions",
    "notice_item_map",
    # 분석 결과
    "risk_results",
    "forecasts",
    "llm_explanations",
    # 액션·운영
    "alerts",
    "action_history",
    "order_requests",
    "meta",
}


def _schema_sql() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _schema_statements_only() -> str:
    """`--` 주석을 제거한 실행 SQL 본문(주석 문구가 검사에 걸리지 않도록)."""
    return "\n".join(line.split("--", 1)[0] for line in _schema_sql().splitlines())


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """스키마가 적용되고 FK가 켜진 in-memory 커넥션."""
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(_schema_sql())
    yield connection
    connection.close()


def _seed_item(conn: sqlite3.Connection, item_id: str = "ITEM-0001") -> str:
    """FK를 만족하는 최소 마스터 데이터(성분 → 대체군 → 품목)를 넣는다."""
    conn.execute(
        "INSERT INTO ingredients(ingredient_code, ingredient_name_kr, ingredient_name_en, atc_code)"
        " VALUES (?, ?, ?, ?)",
        ("ING-001", "세프트리악손나트륨", "Ceftriaxone Sodium", "J01DD04"),
    )
    conn.execute(
        "INSERT INTO substitute_groups(substitute_group_id, ingredient_code, strength, form, route,"
        " group_label) VALUES (?, ?, ?, ?, ?, ?)",
        ("SG-001", "ING-001", "1g", "주사제", "정맥", "세프트리악손 1g 주사"),
    )
    conn.execute(
        "INSERT INTO items(item_id, item_name, standard_code, ingredient_code, strength, form,"
        " route, pack_size, supplier, is_essential, substitute_group_id, atc_code)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item_id,
            "세프트리악손주 1g",
            "8806001234567",
            "ING-001",
            "1g",
            "주사제",
            "정맥",
            10,
            "한국제약",
            1,
            "SG-001",
            "J01DD04",
        ),
    )
    conn.commit()
    return item_id


def _seed_notice(conn: sqlite3.Connection, notice_id: str = "NTC-0001") -> str:
    conn.execute(
        "INSERT INTO notices(notice_id, published_date, title, source, source_url, raw_text,"
        " notice_type, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            notice_id,
            "2026-07-20",
            "세프트리악손주 공급중단 안내",
            "의약품통합정보시스템",
            "https://example.invalid/notice/1",
            "제조소 사정으로 공급이 중단됩니다.",
            "공급중단",
            "2026-07-20T09:00:00",
        ),
    )
    conn.commit()
    return notice_id


# --- 1. 스크립트 적용 -------------------------------------------------------


def test_schema_file_exists() -> None:
    assert SCHEMA_PATH.is_file(), f"스키마 파일이 없다: {SCHEMA_PATH}"


def test_schema_applies_to_memory_db() -> None:
    """schema.sql 전체가 :memory: DB에 오류 없이 적용된다."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_schema_sql())
    finally:
        connection.close()


def test_schema_has_version_header() -> None:
    """서두에 스키마 버전과 마이그레이션 없음 원칙이 명기된다."""
    head = "\n".join(_schema_sql().splitlines()[:12])
    assert "schema v1" in head
    assert "init_db" in head


def test_schema_uses_plain_create_statements() -> None:
    """재생성은 init_db(drop=True) 담당 — IF NOT EXISTS를 쓰지 않는다."""
    body = _schema_statements_only().upper()
    assert "IF NOT EXISTS" not in body
    assert "PRAGMA" not in body, "PRAGMA는 커넥션 책임(db.get_connection) — schema.sql은 CREATE문만 담는다"
    assert "INSERT" not in body, "시드 데이터는 데이터 생성 태스크 담당 — schema.sql은 CREATE문만 담는다"


# --- 2. 테이블 16종 ---------------------------------------------------------


def test_sixteen_tables_exist(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    names = {row[0] for row in rows}
    assert names == EXPECTED_TABLES
    assert len(names) == 16


# --- 3. FK 강제 -------------------------------------------------------------


def test_foreign_key_rejects_unknown_item(conn: sqlite3.Connection) -> None:
    """존재하지 않는 item_id로 stock_usage_daily INSERT 시 IntegrityError."""
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
            " VALUES (?, ?, ?, ?, ?)",
            ("NO-SUCH-ITEM", "2026-08-01", 10, 0, 100),
        )


def test_foreign_key_accepts_known_item(conn: sqlite3.Connection) -> None:
    item_id = _seed_item(conn)
    conn.execute(
        "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
        " VALUES (?, ?, ?, ?, ?)",
        (item_id, "2026-08-01", 10, 0, 100),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM stock_usage_daily").fetchone()[0] == 1


# --- 4. 상태값 CHECK --------------------------------------------------------


def _insert_risk_result(conn: sqlite3.Connection, item_id: str, grade: str) -> None:
    conn.execute(
        "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade, escalated_by_notice,"
        " risk_type, score, days_to_stockout, depletion_date, factors_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-08-01#a1b2c3d4",
            item_id,
            "2026-08-01",
            grade,
            "경고",
            1,
            "supply_halt",
            88,
            5,
            "2026-08-06",
            "{}",
        ),
    )


def test_risk_grade_check_rejects_unknown_label(conn: sqlite3.Connection) -> None:
    item_id = _seed_item(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_risk_result(conn, item_id, "매우 높음")


def test_risk_grade_check_accepts_contract_label(conn: sqlite3.Connection) -> None:
    item_id = _seed_item(conn)
    _insert_risk_result(conn, item_id, "위험")
    conn.commit()
    assert conn.execute("SELECT grade FROM risk_results").fetchone()[0] == "위험"


def test_risk_type_check_rejects_unknown_type(conn: sqlite3.Connection) -> None:
    item_id = _seed_item(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade, risk_type,"
            " factors_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-08-01#a1b2c3d4", item_id, "2026-08-01", "정상", "정상", "unknown_type", "{}"),
        )


def test_risk_type_rejects_explicit_null(conn: sqlite3.Connection) -> None:
    """미분류를 NULL로 표현할 수 없다(NOT NULL) — 'general'과의 이중 표현 차단."""
    item_id = _seed_item(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade, risk_type,"
            " factors_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-08-01#a1b2c3d4", item_id, "2026-08-01", "정상", "정상", None, "{}"),
        )


def test_risk_type_defaults_to_general(conn: sqlite3.Connection) -> None:
    """risk_type 미지정 시 기본값은 'general'이다."""
    item_id = _seed_item(conn)
    conn.execute(
        "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade, factors_json)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-08-01#a1b2c3d4", item_id, "2026-08-01", "정상", "정상", "{}"),
    )
    conn.commit()
    assert conn.execute("SELECT risk_type FROM risk_results").fetchone()[0] == "general"


def _insert_extraction(conn: sqlite3.Connection, notice_id: str, status: str) -> None:
    conn.execute(
        "INSERT INTO notice_extractions(notice_id, payload_json, confidence, status,"
        " prompt_version, provider, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            notice_id,
            '{"product_names": []}',
            0.91,
            status,
            "notice_extract@v1",
            "anthropic",
            "claude-opus-5",
            "2026-08-01T09:30:00",
        ),
    )


def test_extraction_status_check_rejects_unknown_status(conn: sqlite3.Connection) -> None:
    notice_id = _seed_notice(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_extraction(conn, notice_id, "검토중")


def test_extraction_status_check_accepts_contract_status(conn: sqlite3.Connection) -> None:
    notice_id = _seed_notice(conn)
    _insert_extraction(conn, notice_id, "확인 필요")
    conn.commit()
    assert conn.execute("SELECT status FROM notice_extractions").fetchone()[0] == "확인 필요"


def test_notice_type_check_rejects_unknown_type(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO notices(notice_id, published_date, title, notice_type)"
            " VALUES (?, ?, ?, ?)",
            ("NTC-9999", "2026-07-20", "임의 공고", "회수"),
        )


def test_action_history_status_check(conn: sqlite3.Connection) -> None:
    item_id = _seed_item(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO action_history(item_id, action_type, owner, note, status)"
            " VALUES (?, ?, ?, ?, ?)",
            (item_id, "대체 검토", "약제부", "메모", "보류"),
        )


# --- 5. 유니크·복합 PK ------------------------------------------------------


def test_alert_dedupe_key_is_unique(conn: sqlite3.Connection) -> None:
    item_id = _seed_item(conn)
    conn.execute(
        "INSERT INTO alerts(created_at, alert_type, item_id, title, body, severity, dedupe_key,"
        " is_read) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-08-01T09:30:00",
            "risk_escalation",
            item_id,
            "위험 등급 상향",
            "공고로 등급이 상향되었다.",
            "위험",
            "risk_escalation:ITEM-0001:2026-08-01",
            0,
        ),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO alerts(created_at, alert_type, item_id, title, body, severity,"
            " dedupe_key, is_read) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-08-01T10:00:00",
                "risk_escalation",
                item_id,
                "위험 등급 상향(중복)",
                "동일 dedupe_key",
                "위험",
                "risk_escalation:ITEM-0001:2026-08-01",
                0,
            ),
        )


def test_stock_usage_daily_composite_pk(conn: sqlite3.Connection) -> None:
    item_id = _seed_item(conn)
    conn.execute(
        "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
        " VALUES (?, ?, ?, ?, ?)",
        (item_id, "2026-08-01", 10, 0, 100),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
            " VALUES (?, ?, ?, ?, ?)",
            (item_id, "2026-08-01", 99, 0, 50),
        )


def test_risk_results_composite_pk(conn: sqlite3.Connection) -> None:
    item_id = _seed_item(conn)
    _insert_risk_result(conn, item_id, "위험")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_risk_result(conn, item_id, "정상")


# --- 6. 자동 증가 PK --------------------------------------------------------


def test_rowid_pks_autoincrement(conn: sqlite3.Connection) -> None:
    """alert_id·history_id·order_id·shipment_id는 INTEGER AUTOINCREMENT(rowid 반환 계약)."""
    item_id = _seed_item(conn)
    cur = conn.execute(
        "INSERT INTO order_requests(item_id, supplier, quantity, desired_date, owner, reason)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, "한국제약", 200, "2026-08-05", "약제부", "소진 임박"),
    )
    order_id = cur.lastrowid
    assert isinstance(order_id, int) and order_id > 0

    cur = conn.execute(
        "INSERT INTO incoming_shipments(item_id, order_date, expected_date, expected_qty, status)"
        " VALUES (?, ?, ?, ?, ?)",
        (item_id, "2026-07-25", "2026-08-03", 200, "예정"),
    )
    assert isinstance(cur.lastrowid, int) and cur.lastrowid > 0

    cur = conn.execute(
        "INSERT INTO action_history(item_id, action_type, owner, note, order_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (item_id, "발주 요청", "약제부", "긴급 발주", order_id),
    )
    assert isinstance(cur.lastrowid, int) and cur.lastrowid > 0

    cur = conn.execute(
        "INSERT INTO alerts(alert_type, item_id, title, body, severity, dedupe_key)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            "risk_escalation",
            item_id,
            "위험 등급 상향",
            "공고로 등급이 상향되었다.",
            "위험",
            f"risk_escalation:{item_id}:2026-08-01",
        ),
    )
    assert isinstance(cur.lastrowid, int) and cur.lastrowid > 0
    conn.commit()


# --- 7. 인덱스 계약 ---------------------------------------------------------


def test_required_indexes_exist(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ).fetchall()
    indexed_tables = {tbl for _, tbl in rows}
    for table in (
        "stock_usage_daily",
        "incoming_shipments",
        "notice_item_map",
        "risk_results",
        "alerts",
        "action_history",
    ):
        assert table in indexed_tables, f"{table} 인덱스가 없다"


# --- 8. meta 키-값 ----------------------------------------------------------


def test_meta_is_key_value_store(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        [("seed", "20260819"), ("base_date", "2026-08-01"), ("data_version", "1")],
    )
    conn.commit()
    assert conn.execute("SELECT value FROM meta WHERE key='base_date'").fetchone()[0] == "2026-08-01"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO meta(key, value) VALUES ('seed', '999')")
