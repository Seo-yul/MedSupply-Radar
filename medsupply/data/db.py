"""연결 관리 + 스키마 초기화.

medsupply/data/ 계층의 유일한 커넥션 진입점이다. WAL·FK 등 커넥션 단위 PRAGMA는 여기서만
설정한다 — schema.sql은 PRAGMA·INSERT를 포함하지 않는다(파일 서두 주석 참조).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from medsupply import settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# init_db(drop=True)의 DROP 순서. FK 역순(자식 → 부모)이어야 한다 — 부모를 먼저 지우면
# 남은 자식 테이블의 스키마 선언이 참조 무결성 상 모순되는 상태로 잠깐 존재하게 된다.
# (SQLite는 DROP TABLE 자체에서 FK를 강제하지 않지만, 순서를 지켜 어떤 SQLite 버전/설정에서도
# 안전하도록 방어적으로 둔다.) meta는 참조 관계가 없어 아무 위치나 무방하다.
_TABLES_CHILD_TO_PARENT: list[str] = [
    "action_history",
    "order_requests",
    "alerts",
    "llm_explanations",
    "forecasts",
    "risk_results",
    "notice_item_map",
    "notice_extractions",
    "notices",
    "incoming_shipments",
    "stock_usage_daily",
    "items",
    "substitute_groups",
    "ingredient_aliases",
    "ingredients",
    "meta",
]


def get_connection(db_path: str | Path = settings.DB_PATH) -> sqlite3.Connection:
    """WAL + FK ON + Row factory가 설정된 SQLite 커넥션을 연다.

    ``:memory:``로 열면 WAL은 SQLite가 자체적으로 'memory' 저널 모드로 무시한다(정상 동작).
    """
    conn = sqlite3.connect(db_path, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection, *, drop: bool = False) -> None:
    """schema.sql(테이블 16종)을 커넥션에 적용한다.

    drop=True: 기존 테이블을 자식→부모 순으로 DROP(존재하지 않아도 안전)한 뒤 재적용한다.
    drop=False(기본): 빈 DB에만 적용한다. schema.sql은 ``CREATE TABLE IF NOT EXISTS``를 쓰지
    않으므로, 테이블이 이미 있으면 ``sqlite3.OperationalError``가 그대로 전파된다.
    """
    if drop:
        for table in _TABLES_CHILD_TO_PARENT:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    conn.commit()
