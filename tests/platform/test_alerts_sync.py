"""Task M-26: medsupply/services/alerts.py의 sync_alerts(conn) 계약 테스트.

sync_alerts는 conn을 직접 받는 계약(``sync_alerts(conn) -> dict``)이라, 다른 서비스
함수(compute_order_proposal 등, settings.DB_PATH를 스스로 여는 계약)와 달리 monkeypatch
없이 온디스크 소형 픽스처 DB에 직접 커넥션을 열어 검증할 수 있다. medsupply.data.db로
스키마만 적용한 뒤 최소 행을 직접 INSERT한다(표준 스냅샷이 아니라 결정적 손검산용
픽스처 DB — 브리프 표현 그대로).

두 개의 독립 픽스처 DB를 쓴다:
- 두 run(동일 패밀리) DB: 등급 상승 3분기·하강·receipt_delay·notice_map 규칙을 한
  DB 안에서 함께 검증한다(멱등·dedupe 롤백도 이 DB로 검증).
- 단일 run DB: "직전 run 부재 시 규칙 1 전체 스킵"을 독립적으로 검증한다(동시에 규칙
  2는 직전 run과 무관하게 여전히 동작함을 함께 확인한다).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from medsupply.data import db
from medsupply.services import alerts as alerts_service

# ---------------------------------------------------------------------------
# 두 run(동일 패밀리) 픽스처 — 등급 상승/하강/receipt_delay/notice_map
# ---------------------------------------------------------------------------

BASE_DATE = "2026-08-15"
RUN_LATEST = f"{BASE_DATE}#aaaa1111"
RUN_PREV = "2026-08-14#aaaa1111"

ITM_A = "ITM-A"  # 경고 → 위험(긴급) + receipt_delay 신호 동시 보유
ITM_B = "ITM-B"  # 주의 → 경고(높음) + 공고 매핑 대상
ITM_C = "ITM-C"  # 정상 → 주의(생성 안 함)
ITM_D = "ITM-D"  # 위험 → 경고(하강, 생성 안 함)

ITEM_NAMES = {
    ITM_A: "테스트품목A",
    ITM_B: "테스트품목B",
    ITM_C: "테스트품목C",
    ITM_D: "테스트품목D",
}

NOTICE_ID = "NTC-A"


def _insert_item(conn: sqlite3.Connection, item_id: str) -> None:
    conn.execute(
        "INSERT INTO items(item_id, item_name, ingredient_code, supplier, is_essential,"
        " substitute_group_id) VALUES (?, ?, NULL, '대한제약', 0, NULL)",
        (item_id, ITEM_NAMES[item_id]),
    )


def _insert_risk(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    as_of: str,
    item_id: str,
    grade: str,
    risk_type: str = "general",
    factors: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade,"
        " escalated_by_notice, risk_type, score, days_to_stockout, depletion_date,"
        " factors_json) VALUES (?, ?, ?, ?, ?, 0, ?, 50, NULL, NULL, ?)",
        (run_id, item_id, as_of, grade, grade, risk_type, json.dumps(factors or {})),
    )


def _build_two_run_db(db_path: Path) -> sqlite3.Connection:
    """RUN_PREV → RUN_LATEST(동일 패밀리) 등급 전이 4품목 + 공고 매핑 1건."""
    conn = db.get_connection(str(db_path))
    db.init_db(conn, drop=False)

    for item_id in (ITM_A, ITM_B, ITM_C, ITM_D):
        _insert_item(conn, item_id)

    # 직전 run(RUN_PREV) — 등급 상승/하강 비교 기준.
    _insert_risk(conn, run_id=RUN_PREV, as_of="2026-08-14", item_id=ITM_A, grade="경고")
    _insert_risk(conn, run_id=RUN_PREV, as_of="2026-08-14", item_id=ITM_B, grade="주의")
    _insert_risk(conn, run_id=RUN_PREV, as_of="2026-08-14", item_id=ITM_C, grade="정상")
    _insert_risk(conn, run_id=RUN_PREV, as_of="2026-08-14", item_id=ITM_D, grade="위험")

    # 최신 run(RUN_LATEST).
    _insert_risk(
        conn, run_id=RUN_LATEST, as_of=BASE_DATE, item_id=ITM_A, grade="위험",
        risk_type="supply_halt",
        factors={
            "anomalies": [
                {
                    "kind": "receipt_delay", "detected_on": BASE_DATE, "metric": 5.0,
                    "detail": "입고 예정 2026-08-10 대비 5일 지연 (예정 수량 100)",
                }
            ]
        },
    )
    _insert_risk(conn, run_id=RUN_LATEST, as_of=BASE_DATE, item_id=ITM_B, grade="경고", risk_type="demand_surge")
    _insert_risk(conn, run_id=RUN_LATEST, as_of=BASE_DATE, item_id=ITM_C, grade="주의")
    _insert_risk(conn, run_id=RUN_LATEST, as_of=BASE_DATE, item_id=ITM_D, grade="경고")  # 하강

    # 활성 공고 매핑 1건(ITM_B) — 신규 공고 매핑 규칙.
    conn.execute(
        "INSERT INTO notices(notice_id, published_date, title, source, source_url,"
        " raw_text, notice_type, collected_at) VALUES (?, '2026-08-10', ?, 'TEST', NULL,"
        " NULL, '공급중단', '2026-08-10T09:00:00')",
        (NOTICE_ID, "테스트 공급중단 공고"),
    )
    conn.execute(
        "INSERT INTO notice_extractions(notice_id, payload_json, status)"
        " VALUES (?, ?, '확인 필요')",
        (NOTICE_ID, json.dumps({"expected_restart_date": None})),
    )
    conn.execute(
        "INSERT INTO notice_item_map(notice_id, item_id, substitute_group_id, match_basis,"
        " needs_review) VALUES (?, ?, NULL, 'test', 0)",
        (NOTICE_ID, ITM_B),
    )

    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        [("base_date", BASE_DATE), ("data_version", "0")],
    )
    conn.commit()
    return conn


@pytest.fixture()
def two_run_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = _build_two_run_db(tmp_path / "two_run.db")
    yield conn
    conn.close()


def _alert_row(conn: sqlite3.Connection, dedupe_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM alerts WHERE dedupe_key = ?", (dedupe_key,)).fetchone()


def _data_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    return int(row["value"])


# ---------------------------------------------------------------------------
# 규칙 1 — 등급 상승 3분기 + 하강 미생성
# ---------------------------------------------------------------------------


class TestGradeEscalation:
    def test_escalation_to_danger_creates_urgent_alert(self, two_run_conn: sqlite3.Connection) -> None:
        alerts_service.sync_alerts(two_run_conn)

        row = _alert_row(two_run_conn, f"grade_up:{ITM_A}:{RUN_LATEST}")
        assert row is not None
        assert row["alert_type"] == "grade_up"
        assert row["severity"] == "긴급"
        assert row["title"] == f"{ITEM_NAMES[ITM_A]} 위험등급 상승"
        assert row["body"] == "경고 → 위험 · 공급 중단"
        assert row["item_id"] == ITM_A

    def test_escalation_to_warning_creates_high_alert(self, two_run_conn: sqlite3.Connection) -> None:
        alerts_service.sync_alerts(two_run_conn)

        row = _alert_row(two_run_conn, f"grade_up:{ITM_B}:{RUN_LATEST}")
        assert row is not None
        assert row["severity"] == "높음"
        assert row["body"] == "주의 → 경고 · 수요 급증"

    def test_escalation_to_watch_creates_no_alert(self, two_run_conn: sqlite3.Connection) -> None:
        alerts_service.sync_alerts(two_run_conn)

        assert _alert_row(two_run_conn, f"grade_up:{ITM_C}:{RUN_LATEST}") is None

    def test_de_escalation_creates_no_alert(self, two_run_conn: sqlite3.Connection) -> None:
        alerts_service.sync_alerts(two_run_conn)

        assert _alert_row(two_run_conn, f"grade_up:{ITM_D}:{RUN_LATEST}") is None
        count = two_run_conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE item_id = ?", (ITM_D,)
        ).fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# 규칙 2 — 입고 지연(receipt_delay)
# ---------------------------------------------------------------------------


class TestReceiptDelay:
    def test_receipt_delay_anomaly_creates_high_alert(self, two_run_conn: sqlite3.Connection) -> None:
        alerts_service.sync_alerts(two_run_conn)

        row = _alert_row(two_run_conn, f"receipt_delay:{ITM_A}:{RUN_LATEST}")
        assert row is not None
        assert row["alert_type"] == "receipt_delay"
        assert row["severity"] == "높음"
        assert row["title"] == f"{ITEM_NAMES[ITM_A]} 입고 지연"
        assert "5일 지연" in row["body"]

    def test_no_anomaly_creates_no_receipt_delay_alert(self, two_run_conn: sqlite3.Connection) -> None:
        alerts_service.sync_alerts(two_run_conn)

        assert _alert_row(two_run_conn, f"receipt_delay:{ITM_B}:{RUN_LATEST}") is None


# ---------------------------------------------------------------------------
# 규칙 3 — 신규 공고 매핑
# ---------------------------------------------------------------------------


class TestNoticeMap:
    def test_active_notice_mapping_creates_confirm_alert(self, two_run_conn: sqlite3.Connection) -> None:
        alerts_service.sync_alerts(two_run_conn)

        row = _alert_row(two_run_conn, f"notice_map:{NOTICE_ID}:{ITM_B}")
        assert row is not None
        assert row["alert_type"] == "notice_map"
        assert row["severity"] == "확인"
        assert row["title"] == f"{ITEM_NAMES[ITM_B]} 공급 공고 매핑"
        assert row["item_id"] == ITM_B


# ---------------------------------------------------------------------------
# 직전 run 부재 — 규칙 1 전체 스킵(규칙 2는 무관하게 동작)
# ---------------------------------------------------------------------------


class TestPrevRunAbsent:
    def test_single_run_skips_grade_up_entirely(self, tmp_path: Path) -> None:
        db_path = tmp_path / "single_run.db"
        conn = db.get_connection(str(db_path))
        db.init_db(conn, drop=False)

        _insert_item(conn, ITM_A)
        run_only = "2026-08-20#cccc3333"
        _insert_risk(
            conn, run_id=run_only, as_of="2026-08-20", item_id=ITM_A, grade="위험",
            factors={
                "anomalies": [
                    {
                        "kind": "receipt_delay", "detected_on": "2026-08-20", "metric": 3.0,
                        "detail": "입고 예정 2026-08-17 대비 3일 지연 (예정 수량 50)",
                    }
                ]
            },
        )
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [("base_date", "2026-08-20"), ("data_version", "0")],
        )
        conn.commit()

        result = alerts_service.sync_alerts(conn)

        grade_up_count = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE alert_type = 'grade_up'"
        ).fetchone()[0]
        assert grade_up_count == 0
        # 규칙 2(입고 지연)는 직전 run과 무관하게 여전히 동작한다.
        assert _alert_row(conn, f"receipt_delay:{ITM_A}:{run_only}") is not None
        assert result["created"] == 1
        conn.close()


# ---------------------------------------------------------------------------
# 멱등성 — 2회 실행 시 created=0, dedupe 롤백 후 data_version 불변
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_second_run_creates_nothing_and_all_skipped(
        self, two_run_conn: sqlite3.Connection
    ) -> None:
        first = alerts_service.sync_alerts(two_run_conn)
        assert first["created"] > 0

        version_after_first = _data_version(two_run_conn)

        second = alerts_service.sync_alerts(two_run_conn)

        assert second["created"] == 0
        assert second["skipped"] == first["created"]
        # dedupe 충돌은 create_alert 내부에서 롤백되므로 data_version은 그대로다.
        assert _data_version(two_run_conn) == version_after_first

    def test_total_alert_row_count_stable_after_second_run(
        self, two_run_conn: sqlite3.Connection
    ) -> None:
        alerts_service.sync_alerts(two_run_conn)
        count_after_first = two_run_conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

        alerts_service.sync_alerts(two_run_conn)
        count_after_second = two_run_conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

        assert count_after_second == count_after_first


# ---------------------------------------------------------------------------
# 반환값 형태
# ---------------------------------------------------------------------------


class TestReturnShape:
    def test_returns_created_and_skipped_counts(self, two_run_conn: sqlite3.Connection) -> None:
        result = alerts_service.sync_alerts(two_run_conn)

        assert set(result.keys()) == {"created", "skipped"}
        assert isinstance(result["created"], int)
        assert isinstance(result["skipped"], int)
        # 이 픽스처는 위험등급 상승 2건(A·B) + receipt_delay 1건(A) + notice_map 1건(B) = 4건.
        assert result["created"] == 4
        assert result["skipped"] == 0
