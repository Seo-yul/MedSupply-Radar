"""Task M-16: medsupply/services/workbench.py 계약 테스트.

test_inventory_service.py(M-15)와 동일한 픽스처 관례를 재사용한다: 온디스크 소형
표준 스냅샷(module-scope, subprocess로 --baseline-only 생성 + 위험 평가 배치 1 run
실행)을 만든 뒤, 각 테스트는 함수별 tmp_path로 복사해 격리한다. "위험 평가 배치 run
부재" 케이스는 배치를 실행하지 않은 별도 module-scope 스냅샷을 쓴다.

st.cache_data/st.cache_resource는 프로세스 전역이라 monkeypatch로 settings.DB_PATH를
바꾼 뒤에는 반드시 두 캐시를 모두 clear()해야 한다 — _activate()가 이를 담당한다.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st

from medsupply import settings
from medsupply.data import writer
from medsupply.services import inventory, workbench

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"
BATCH_SCRIPT = REPO_ROOT / "scripts" / "run_risk_batch.py"

SEED = 20260801
BASE_DATE = "2026-08-01"

EXPECTED_KEYS = {
    "item", "risk", "prev_risk", "series", "forecast", "current_stock",
    "avg_daily_usage", "avg_prev", "next_shipment", "has_active_notice",
    "substitutes", "ingredient_name_kr", "ingredient_name_en",
}


# ---------------------------------------------------------------------------
# 서브프로세스·DB 헬퍼(tests/platform/test_inventory_service.py와 동일 관례)
# ---------------------------------------------------------------------------


def _generate_snapshot(db_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable, str(GENERATE_SCRIPT),
            "--baseline-only", "--seed", str(SEED), "--base-date", BASE_DATE,
            "--out", str(db_path),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _run_batch(db_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(BATCH_SCRIPT), "--db", str(db_path), "--as-of", BASE_DATE],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _activate(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    """settings.DB_PATH를 db_path로 바꾸고 캐시를 초기화한다(모든 테스트의 필수 선행 단계)."""
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    st.cache_data.clear()
    st.cache_resource.clear()


def _direct_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _first_item_id(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT item_id FROM items ORDER BY item_id LIMIT 1").fetchone()["item_id"]


def _latest_run_id(conn: sqlite3.Connection) -> str:
    return conn.execute(
        "SELECT run_id FROM risk_results ORDER BY as_of DESC, run_id DESC LIMIT 1"
    ).fetchone()["run_id"]


def _item_with_both_substitute_kinds(conn: sqlite3.Connection) -> str:
    """같은 대체군 형제(same_condition=True)와 다른 대체군 동일 성분 후보(False)를 모두
    가진 품목 1개(대체 후보 탭 렌더 검증용 — 참조 마스터가 고정이므로 결정적이다)."""
    row = conn.execute(
        """
        SELECT i.item_id
        FROM items AS i
        WHERE EXISTS (
                SELECT 1 FROM items AS s
                WHERE s.item_id != i.item_id AND s.substitute_group_id = i.substitute_group_id
            )
            AND EXISTS (
                SELECT 1 FROM items AS m
                WHERE m.item_id != i.item_id AND m.ingredient_code = i.ingredient_code
                    AND (m.substitute_group_id IS NULL OR m.substitute_group_id != i.substitute_group_id)
            )
        ORDER BY i.item_id
        LIMIT 1
        """
    ).fetchone()
    assert row is not None, "same_condition True/False 후보를 모두 가진 품목을 찾지 못했다"
    return row["item_id"]


def _clear_pending_shipments(conn: sqlite3.Connection, item_id: str) -> None:
    conn.execute(
        "DELETE FROM incoming_shipments WHERE item_id = ? AND actual_date IS NULL", (item_id,)
    )
    conn.commit()


def _insert_pending_shipment(
    conn: sqlite3.Connection, item_id: str, expected_date: str, qty: int
) -> None:
    conn.execute(
        "INSERT INTO incoming_shipments(item_id, order_date, expected_date, expected_qty,"
        " actual_date, actual_qty, status) VALUES (?, ?, ?, ?, NULL, NULL, '예정')",
        (item_id, BASE_DATE, expected_date, qty),
    )
    conn.commit()


def _insert_active_halt_notice(conn: sqlite3.Connection, item_id: str, notice_id: str) -> None:
    conn.execute(
        "INSERT INTO notices(notice_id, published_date, title, source, source_url, raw_text,"
        " notice_type, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            notice_id, "2026-07-01", "테스트 공급중단 공고", "TEST", None, None,
            "공급중단", "2026-07-01T00:00:00",
        ),
    )
    conn.execute(
        "INSERT INTO notice_extractions(notice_id, payload_json, confidence, status,"
        " prompt_version, provider, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            notice_id, json.dumps({"expected_restart_date": None}), 0.9, "자동확정",
            "test@v1", "test", "test",
        ),
    )
    conn.execute(
        "INSERT INTO notice_item_map(notice_id, item_id, substitute_group_id, match_basis,"
        " needs_review) VALUES (?, ?, ?, ?, ?)",
        (notice_id, item_id, None, "test", 0),
    )
    conn.commit()


def _truncate_history_before(conn: sqlite3.Connection, item_id: str, keep_from: str) -> None:
    conn.execute(
        "DELETE FROM stock_usage_daily WHERE item_id = ? AND date < ?", (item_id, keep_from)
    )
    conn.commit()


def _set_ingredient_name_en_null(conn: sqlite3.Connection, ingredient_code: str) -> None:
    conn.execute(
        "UPDATE ingredients SET ingredient_name_en = NULL WHERE ingredient_code = ?",
        (ingredient_code,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 스냅샷 + 위험 평가 배치 1 run — 모듈 1회만 생성(비용 절감)."""
    db_path = tmp_path_factory.mktemp("workbench_service_base") / "base.db"
    _generate_snapshot(db_path)
    _run_batch(db_path)
    return db_path


@pytest.fixture()
def db_path(base_snapshot: Path, tmp_path: Path) -> Path:
    """base_snapshot을 함수별 tmp_path로 복사해 테스트 간 쓰기 격리를 보장한다."""
    dest = tmp_path / "t.db"
    shutil.copy(base_snapshot, dest)
    return dest


@pytest.fixture(scope="module")
def no_run_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """위험 평가 배치를 실행하지 않은 스냅샷(run 부재 경로 전용)."""
    db_path = tmp_path_factory.mktemp("workbench_service_no_run") / "base.db"
    _generate_snapshot(db_path)
    return db_path


@pytest.fixture()
def no_run_db_path(no_run_snapshot: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "no_run.db"
    shutil.copy(no_run_snapshot, dest)
    return dest


# ---------------------------------------------------------------------------
# load_item_detail — 키 완비
# ---------------------------------------------------------------------------


class TestLoadItemDetailShape:
    def test_returns_all_contract_keys(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert EXPECTED_KEYS <= set(detail.keys())


# ---------------------------------------------------------------------------
# risk/forecast 조인
# ---------------------------------------------------------------------------


class TestRiskAndForecastJoin:
    def test_risk_matches_latest_run_and_parses_factors(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        run_id = _latest_run_id(conn)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["risk"] is not None
        assert detail["risk"]["run_id"] == run_id
        assert detail["risk"]["item_id"] == item_id
        assert isinstance(detail["risk"]["factors"], dict)
        assert "anomalies" in detail["risk"]["factors"]

    def test_prev_risk_is_none_with_single_run_fixture(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["prev_risk"] is None

    def test_forecast_present_with_daily_list(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["forecast"] is not None
        assert isinstance(detail["forecast"]["daily"], list)
        assert len(detail["forecast"]["daily"]) > 0


# ---------------------------------------------------------------------------
# current_stock / series
# ---------------------------------------------------------------------------


class TestCurrentStock:
    def test_current_stock_equals_series_last_closing_stock(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        expected = conn.execute(
            "SELECT closing_stock FROM stock_usage_daily WHERE item_id = ?"
            " ORDER BY date DESC LIMIT 1",
            (item_id,),
        ).fetchone()["closing_stock"]
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["current_stock"] == expected
        assert detail["series"].iloc[-1]["closing_stock"] == expected


# ---------------------------------------------------------------------------
# 사용량 평균(최근 28일 / 그 직전 28일)
# ---------------------------------------------------------------------------


class TestUsageAverages:
    def test_avg_daily_usage_matches_manual_mean_of_last_28_days(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        rows = conn.execute(
            "SELECT usage_qty FROM stock_usage_daily WHERE item_id = ? ORDER BY date DESC LIMIT 28",
            (item_id,),
        ).fetchall()
        conn.close()
        expected = round(sum(r["usage_qty"] for r in rows) / len(rows), 1)

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["avg_daily_usage"] == expected

    def test_avg_prev_populated_when_full_history_available(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--baseline-only는 base_date 기준 364일 전부터 생성하므로 56일 창이 항상 꽉 찬다."""
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["avg_prev"] is not None
        assert isinstance(detail["avg_prev"], float)

    def test_avg_prev_none_when_history_truncated(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        keep_from = (date.fromisoformat(BASE_DATE) - timedelta(days=29)).isoformat()
        _truncate_history_before(conn, item_id, keep_from)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["avg_prev"] is None
        assert detail["avg_daily_usage"] is not None


# ---------------------------------------------------------------------------
# next_shipment — 최근접 미입고 건
# ---------------------------------------------------------------------------


class TestNextShipment:
    def test_next_shipment_is_earliest_pending(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        _clear_pending_shipments(conn, item_id)
        _insert_pending_shipment(conn, item_id, "2026-09-20", 500)
        _insert_pending_shipment(conn, item_id, "2026-08-10", 120)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["next_shipment"] is not None
        assert detail["next_shipment"]["expected_date"] == "2026-08-10"
        assert detail["next_shipment"]["qty"] == 120

    def test_next_shipment_none_when_no_pending(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        _clear_pending_shipments(conn, item_id)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["next_shipment"] is None


# ---------------------------------------------------------------------------
# next_shipment — as_of(meta.base_date) 인지(F2): 연체 건은 후보에서 제외
# ---------------------------------------------------------------------------


class TestNextShipmentAsOfAware:
    """BASE_DATE = 2026-08-01. 연체 = expected_date <= BASE_DATE인데 미입고(동결 모델의
    overdue_cutoff가 그런 건을 소진 추정에서 배제하는 것과 일관 — 화면도 그 건을 '다음
    입고'로 보여주지 않아야 한다, F2)."""

    def test_next_shipment_none_when_only_overdue_pending(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        _clear_pending_shipments(conn, item_id)
        _insert_pending_shipment(conn, item_id, "2026-07-18", 300)  # BASE_DATE보다 이전(연체)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["next_shipment"] is None

    def test_next_shipment_picks_future_pending_over_overdue(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        _clear_pending_shipments(conn, item_id)
        _insert_pending_shipment(conn, item_id, "2026-07-18", 300)  # 연체(제외 대상)
        _insert_pending_shipment(conn, item_id, "2026-08-10", 120)  # 미래 예정(선정 대상)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["next_shipment"] is not None
        assert detail["next_shipment"]["expected_date"] == "2026-08-10"
        assert detail["next_shipment"]["qty"] == 120


# ---------------------------------------------------------------------------
# has_active_notice
# ---------------------------------------------------------------------------


class TestHasActiveNotice:
    def test_true_when_active_halt_notice_mapped(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        _insert_active_halt_notice(conn, item_id, "TEST-HALT-WB-1")
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["has_active_notice"] is True

    def test_false_when_no_active_notice(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["has_active_notice"] is False


# ---------------------------------------------------------------------------
# substitutes — same_condition 분리
# ---------------------------------------------------------------------------


class TestSubstitutes:
    def test_same_condition_split_present(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _item_with_both_substitute_kinds(conn)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        substitutes = detail["substitutes"]
        assert isinstance(substitutes, pd.DataFrame)
        assert (substitutes["same_condition"] == True).any()  # noqa: E712
        assert (substitutes["same_condition"] == False).any()  # noqa: E712
        assert item_id not in set(substitutes["item_id"])


# ---------------------------------------------------------------------------
# ingredient_name_kr/en — 결측 폴백
# ---------------------------------------------------------------------------


class TestIngredientFallback:
    def test_ingredient_name_en_defaults_to_dash_when_missing(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        ingredient_code = conn.execute(
            "SELECT ingredient_code FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()["ingredient_code"]
        _set_ingredient_name_en_null(conn, ingredient_code)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["ingredient_name_en"] == "-"
        assert detail["ingredient_name_kr"] != "-"


# ---------------------------------------------------------------------------
# 위험 평가 배치 run 부재 — risk=None 무예외
# ---------------------------------------------------------------------------


class TestNoRiskRun:
    def test_risk_forecast_prev_risk_all_none_without_exception(
        self, no_run_db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, no_run_db_path)
        conn = _direct_conn(no_run_db_path)
        item_id = _first_item_id(conn)
        conn.close()

        detail = workbench.load_item_detail(item_id, data_version=inventory.current_data_version())
        assert detail["risk"] is None
        assert detail["prev_risk"] is None
        assert detail["forecast"] is None
        # run이 없어도 재고 파생값은 여전히 계산된다(무예외 전제).
        assert detail["current_stock"] is not None


# ---------------------------------------------------------------------------
# open_write_conn — 쓰기 전용 커넥션
# ---------------------------------------------------------------------------


class TestOpenWriteConn:
    def test_can_save_action_history_and_bumps_data_version(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        before_count = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
        conn.close()

        before_version = inventory.current_data_version()

        write_conn = workbench.open_write_conn()
        try:
            history_id = writer.save_action_history(
                write_conn, item_id, "대체 품목 검토", "김약사", "테스트 조치", status="진행 중",
            )
        finally:
            write_conn.close()

        assert isinstance(history_id, int)

        conn = _direct_conn(db_path)
        after_count = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
        conn.close()
        assert after_count == before_count + 1

        st.cache_data.clear()
        after_version = inventory.current_data_version()
        assert after_version == before_version + 1
