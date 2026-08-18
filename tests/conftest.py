"""tests/ 전역 공용 픽스처.

medsupply/data/queries.py(읽기 전용 조회 계층)를 검증하는 tests/data/test_queries.py가
소비하는 소형 픽스처 DB를 제공한다.

- empty_conn: 스키마만 적용되고 데이터가 없는 :memory: 커넥션(빈 결과 경로 테스트용).
- fixture_conn: empty_conn 위에 최소 시드 데이터를 채운 :memory: 커넥션(대표 경로·필터 테스트용).

시드 데이터 구성(모듈 상수는 다른 테스트 모듈에서 재사용할 수 있도록 공개한다):
- 품목 3개 — ITEM_1·ITEM_2는 같은 대체군(SUBSTITUTE_GROUP_1), ITEM_3은 같은 성분(INGREDIENT_1)의
  다른 대체군(SUBSTITUTE_GROUP_2). INGREDIENT_2는 어떤 품목도 쓰지 않는 성분으로,
  "유효한 값이지만 매칭 품목이 없는" 필터 빈 결과 경로를 만드는 데 쓴다.
- stock_usage_daily: 품목별 2026-07-30~08-01 3일치.
- incoming_shipments: ITEM_1에 미입고 1건.
- notices: NOTICE_HALT(공급중단, 재개예정일 NULL) 1건 + NOTICE_NORMALIZED(정상화, 재개예정일
  '2026-07-01') 1건. NOTICE_HALT만 활성 규칙을 만족해야 get_active_notice_map의
  "정상화 제외·null 포함" 경로를 동시에 검증할 수 있다.
- notice_item_map: NOTICE_HALT → ITEM_1(needs_review=0), ITEM_2(needs_review=1).
- risk_results: ITEM_1에 대해 RUN_YESTERDAY(경고) → RUN_TODAY(위험) 2개 run.
- forecasts: RUN_TODAY·ITEM_1 1건.
- action_history: ITEM_1·ITEM_2 각 1건(시각 다름 → 정렬 검증 가능).
- alerts: ITEM_1에 unread 1건 + read 1건.
- order_requests: 0건(계약상 queries.py가 이 테이블을 조회하지 않음).
- meta: seed·base_date·item_count·data_version 4키.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest

from medsupply.data import db

ITEM_1 = "ITEM-0001"
ITEM_2 = "ITEM-0002"
ITEM_3 = "ITEM-0003"
INGREDIENT_1 = "ING-1"
INGREDIENT_2 = "ING-2"
SUBSTITUTE_GROUP_1 = "SG-1"
SUBSTITUTE_GROUP_2 = "SG-2"
NOTICE_HALT = "NTC-0001"
NOTICE_NORMALIZED = "NTC-0002"
RUN_TODAY = "2026-08-01#a1b2c3d4"
RUN_YESTERDAY = "2026-07-31#e5f6a7b8"
AS_OF_TODAY = "2026-08-01"


@pytest.fixture()
def empty_conn() -> Iterator[sqlite3.Connection]:
    """스키마만 적용된 빈 :memory: 커넥션."""
    conn = db.get_connection(":memory:")
    db.init_db(conn, drop=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def fixture_conn(empty_conn: sqlite3.Connection) -> sqlite3.Connection:
    """최소 시드 데이터가 채워진 :memory: 커넥션(모듈 docstring 참조)."""
    conn = empty_conn

    conn.executemany(
        "INSERT INTO ingredients(ingredient_code, ingredient_name_kr, ingredient_name_en, atc_code)"
        " VALUES (?, ?, ?, ?)",
        [
            (INGREDIENT_1, "세프트리악손나트륨", "Ceftriaxone Sodium", "J01DD04"),
            (INGREDIENT_2, "반코마이신염산염", "Vancomycin Hydrochloride", "J01XA01"),
        ],
    )

    conn.executemany(
        "INSERT INTO substitute_groups(substitute_group_id, ingredient_code, strength, form,"
        " route, group_label) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (SUBSTITUTE_GROUP_1, INGREDIENT_1, "1g", "주사제", "정맥", "세프트리악손 1g 주사"),
            (SUBSTITUTE_GROUP_2, INGREDIENT_1, "500mg", "주사제", "정맥", "세프트리악손 500mg 주사"),
        ],
    )

    conn.executemany(
        "INSERT INTO items(item_id, item_name, standard_code, ingredient_code, strength, form,"
        " route, pack_size, supplier, is_essential, substitute_group_id, atc_code)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                ITEM_1, "세프트리악손주 1g(한국제약)", "8806001234567", INGREDIENT_1, "1g",
                "주사제", "정맥", 10, "한국제약", 1, SUBSTITUTE_GROUP_1, "J01DD04",
            ),
            (
                ITEM_2, "세프트리악손주 1g(대한제약)", "8806001234574", INGREDIENT_1, "1g",
                "주사제", "정맥", 10, "대한제약", 0, SUBSTITUTE_GROUP_1, "J01DD04",
            ),
            (
                ITEM_3, "세프트리악손주 500mg", "8806001234581", INGREDIENT_1, "500mg",
                "주사제", "정맥", 10, "한국제약", 0, SUBSTITUTE_GROUP_2, "J01DD04",
            ),
        ],
    )

    conn.executemany(
        "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (ITEM_1, "2026-07-30", 10, 0, 100),
            (ITEM_1, "2026-07-31", 12, 0, 88),
            (ITEM_1, "2026-08-01", 8, 0, 80),
            (ITEM_2, "2026-07-30", 5, 0, 50),
            (ITEM_2, "2026-07-31", 5, 0, 45),
            (ITEM_2, "2026-08-01", 5, 0, 40),
            (ITEM_3, "2026-07-30", 3, 20, 60),
            (ITEM_3, "2026-07-31", 4, 0, 56),
            (ITEM_3, "2026-08-01", 4, 0, 52),
        ],
    )

    conn.execute(
        "INSERT INTO incoming_shipments(item_id, order_date, expected_date, expected_qty,"
        " actual_date, actual_qty, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ITEM_1, "2026-07-25", "2026-08-05", 200, None, None, "예정"),
    )

    conn.executemany(
        "INSERT INTO notices(notice_id, published_date, title, source, source_url, raw_text,"
        " notice_type, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                NOTICE_HALT, "2026-07-15", "세프트리악손주 공급중단 안내", "의약품통합정보시스템",
                "https://example.invalid/notice/1",
                "제조소 사정으로 2026년 7월 15일부터 공급이 중단됩니다.",
                "공급중단", "2026-07-15T09:00:00",
            ),
            (
                NOTICE_NORMALIZED, "2026-07-28", "세프트리악손주 공급 정상화 안내",
                "의약품통합정보시스템", "https://example.invalid/notice/2",
                "2026년 7월 1일부로 공급이 정상화되었습니다.",
                "정상화", "2026-07-28T09:00:00",
            ),
        ],
    )

    conn.execute(
        "INSERT INTO notice_extractions(notice_id, payload_json, confidence, status,"
        " prompt_version, provider, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            NOTICE_HALT,
            json.dumps(
                {
                    "product_names": ["세프트리악손주 1g"],
                    "ingredient_names": ["세프트리악손나트륨"],
                    "reason": "제조소 설비 점검",
                    "halt_start_date": "2026-07-15",
                    "expected_restart_date": None,
                    "notice_type": "공급중단",
                    "evidence_quotes": ["제조소 사정으로 2026년 7월 15일부터 공급이 중단됩니다."],
                }
            ),
            0.6,
            "확인 필요",
            "notice_extract@v1",
            "anthropic",
            "claude-opus-5",
            "2026-07-15T09:30:00",
        ),
    )
    conn.execute(
        "INSERT INTO notice_extractions(notice_id, payload_json, confidence, status,"
        " prompt_version, provider, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            NOTICE_NORMALIZED,
            json.dumps(
                {
                    "product_names": ["세프트리악손주 1g"],
                    "ingredient_names": ["세프트리악손나트륨"],
                    "reason": "정상화",
                    "halt_start_date": None,
                    "expected_restart_date": "2026-07-01",
                    "notice_type": "정상화",
                    "evidence_quotes": ["2026년 7월 1일부로 공급이 정상화되었습니다."],
                }
            ),
            0.95,
            "자동확정",
            "notice_extract@v1",
            "anthropic",
            "claude-opus-5",
            "2026-07-28T09:30:00",
        ),
    )

    conn.executemany(
        "INSERT INTO notice_item_map(notice_id, item_id, substitute_group_id, match_basis,"
        " needs_review) VALUES (?, ?, ?, ?, ?)",
        [
            (NOTICE_HALT, ITEM_1, SUBSTITUTE_GROUP_1, "standard_code", 0),
            (NOTICE_HALT, ITEM_2, SUBSTITUTE_GROUP_1, "ingredient+strength+form", 1),
        ],
    )

    conn.executemany(
        "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade,"
        " escalated_by_notice, risk_type, score, days_to_stockout, depletion_date, factors_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                RUN_TODAY, ITEM_1, "2026-08-01", "위험", "경고", 1, "supply_halt", 92, 5,
                "2026-08-06", "{}",
            ),
            (
                RUN_YESTERDAY, ITEM_1, "2026-07-31", "경고", "경고", 0, "general", 55, 12,
                "2026-08-12", "{}",
            ),
        ],
    )

    conn.execute(
        "INSERT INTO forecasts(run_id, item_id, as_of, horizon_days, avg_daily_forecast,"
        " total_forecast, daily_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (RUN_TODAY, ITEM_1, "2026-08-01", 5, 9.6, 48.0, json.dumps([9, 10, 9, 10, 9])),
    )

    conn.executemany(
        "INSERT INTO action_history(created_at, item_id, action_type, owner, note, status)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("2026-07-20T10:00:00", ITEM_1, "대체 검토", "약제부", "대체 후보 확인", "진행 중"),
            ("2026-08-01T09:00:00", ITEM_2, "발주 요청", "약제부", "긴급 발주 완료", "완료"),
        ],
    )

    conn.executemany(
        "INSERT INTO alerts(created_at, alert_type, item_id, title, body, severity, dedupe_key,"
        " is_read) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "2026-08-01T09:35:00", "risk_escalation", ITEM_1, "위험 등급 상향",
                "공고로 등급이 상향되었다.", "긴급", f"risk_escalation:{ITEM_1}:2026-08-01", 0,
            ),
            (
                "2026-07-15T09:10:00", "notice_new", ITEM_1, "신규 공급중단 공고",
                "세프트리악손주 공급중단 공고가 등록되었다.", "확인", f"notice_new:{NOTICE_HALT}", 1,
            ),
        ],
    )

    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        [
            ("seed", "20260819"),
            ("base_date", "2026-08-01"),
            ("item_count", "3"),
            ("data_version", "1"),
        ],
    )

    conn.commit()
    return conn
