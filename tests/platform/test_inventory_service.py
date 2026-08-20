"""Task M-15: medsupply/services/inventory.py 계약 테스트.

온디스크 소형 표준 스냅샷(module-scope, subprocess로 --baseline-only 생성 + 위험 평가
배치 1 run 실행)을 만든 뒤, 각 테스트는 함수별 tmp_path로 복사해 격리한다(tests/platform/
test_risk_batch.py와 동일한 픽스처 관례). supply_status 4분기 중 자연 데이터로 만들기
어려운 케이스(재고 0, 활성 공급중단 공고, 특정 등급)는 브리프가 명시한 대로 raw SQL로
직접 조작해 결정적으로 검증한다.

st.cache_data/st.cache_resource는 프로세스 전역이라 monkeypatch로 settings.DB_PATH를
바꾼 뒤에는 반드시 두 캐시를 모두 clear()해야 한다(그러지 않으면 이전 테스트가 연 커넥션·
결과가 재사용되어 이번 테스트의 DB 조작이 반영되지 않는다) — _activate()가 이를 담당한다.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import streamlit as st

from medsupply import settings
from medsupply.services import inventory

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"
BATCH_SCRIPT = REPO_ROOT / "scripts" / "run_risk_batch.py"

SEED = 20260801
BASE_DATE = "2026-08-01"

EXPECTED_COLUMNS = {
    "item_id", "item_name", "ingredient_code", "ingredient_name_kr", "strength",
    "form", "route", "supplier", "is_essential", "substitute_group_id",
    "grade", "score", "days_to_stockout", "risk_type",
    "current_stock", "supply_status", "form_code",
}


# ---------------------------------------------------------------------------
# 서브프로세스·DB 헬퍼
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
    """서비스 캐시와 무관하게 DB를 직접 조작·조회하기 위한 커넥션(테스트 전용)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _first_item_id(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT item_id FROM items ORDER BY item_id LIMIT 1").fetchone()["item_id"]


def _latest_run_id(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT run_id FROM risk_results LIMIT 1").fetchone()["run_id"]


def _item_with_positive_stock(conn: sqlite3.Connection) -> str:
    """최신 날짜 closing_stock > 0인 품목 1개(공급중단·등급 조작 테스트가 재고 0과 겹치지
    않게 하기 위한 선행 조건)."""
    row = conn.execute(
        """
        SELECT s.item_id
        FROM stock_usage_daily AS s
        INNER JOIN (
            SELECT item_id, MAX(date) AS max_date FROM stock_usage_daily GROUP BY item_id
        ) AS latest ON latest.item_id = s.item_id AND latest.max_date = s.date
        WHERE s.closing_stock > 0
        ORDER BY s.item_id
        LIMIT 1
        """
    ).fetchone()
    assert row is not None, "재고 > 0인 품목을 찾지 못했다(baseline 생성 가정이 깨졌다)"
    return row["item_id"]


def _set_closing_stock_zero(conn: sqlite3.Connection, item_id: str) -> None:
    conn.execute(
        "UPDATE stock_usage_daily SET closing_stock = 0 WHERE item_id = ? AND date ="
        " (SELECT MAX(date) FROM stock_usage_daily WHERE item_id = ?)",
        (item_id, item_id),
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


def _set_grade(conn: sqlite3.Connection, run_id: str, item_id: str, grade: str) -> None:
    conn.execute(
        "UPDATE risk_results SET grade = ? WHERE run_id = ? AND item_id = ?",
        (grade, run_id, item_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 스냅샷 + 위험 평가 배치 1 run — 모듈 1회만 생성(비용 절감)."""
    db_path = tmp_path_factory.mktemp("inventory_service_base") / "base.db"
    _generate_snapshot(db_path)
    _run_batch(db_path)
    return db_path


@pytest.fixture()
def db_path(base_snapshot: Path, tmp_path: Path) -> Path:
    """base_snapshot을 함수별 tmp_path로 복사해 테스트 간 쓰기 격리를 보장한다."""
    dest = tmp_path / "t.db"
    shutil.copy(base_snapshot, dest)
    return dest


# ---------------------------------------------------------------------------
# load_overview — 컬럼·정렬·필터
# ---------------------------------------------------------------------------


class TestLoadOverviewShape:
    def test_columns_complete(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _activate(monkeypatch, db_path)
        data_version = inventory.current_data_version()
        df = inventory.load_overview(data_version=data_version)
        assert EXPECTED_COLUMNS <= set(df.columns)

    def test_row_count_matches_items_table(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        expected = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()

        data_version = inventory.current_data_version()
        df = inventory.load_overview(data_version=data_version)
        assert len(df) == expected

    def test_sorted_by_score_descending(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        data_version = inventory.current_data_version()
        df = inventory.load_overview(data_version=data_version)
        scores = df["score"].dropna().tolist()
        assert scores == sorted(scores, reverse=True)


class TestLoadOverviewFilters:
    def test_filter_by_grade(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _activate(monkeypatch, db_path)
        data_version = inventory.current_data_version()
        baseline = inventory.load_overview(data_version=data_version)
        sample_grade = baseline["grade"].dropna().iloc[0]

        filtered = inventory.load_overview(grade=sample_grade, data_version=data_version)
        assert len(filtered) == int((baseline["grade"] == sample_grade).sum())
        assert (filtered["grade"] == sample_grade).all()

    def test_filter_by_status(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _activate(monkeypatch, db_path)
        data_version = inventory.current_data_version()
        baseline = inventory.load_overview(data_version=data_version)
        sample_status = baseline["supply_status"].iloc[0]

        filtered = inventory.load_overview(status=sample_status, data_version=data_version)
        assert len(filtered) == int((baseline["supply_status"] == sample_status).sum())
        assert (filtered["supply_status"] == sample_status).all()

    def test_filter_by_search(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _activate(monkeypatch, db_path)
        data_version = inventory.current_data_version()
        baseline = inventory.load_overview(data_version=data_version)
        sample = baseline.iloc[0]
        term = str(sample["item_name"])[:3]

        filtered = inventory.load_overview(search=term, data_version=data_version)
        assert sample["item_id"] in set(filtered["item_id"])
        lowered = term.lower()
        assert all(
            lowered in str(name).lower() or lowered in str(ing).lower()
            for name, ing in zip(filtered["item_name"], filtered["ingredient_name_kr"])
        )


# ---------------------------------------------------------------------------
# supply_status 4분기(확정 규칙, 우선순위순) — 브리프 명시 기법: 자연 데이터로 만들기
# 어려운 케이스는 raw SQL로 직접 조작한다.
# ---------------------------------------------------------------------------


class TestSupplyStatusBuckets:
    def test_stockout_when_current_stock_zero(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _first_item_id(conn)
        _set_closing_stock_zero(conn, item_id)
        conn.close()

        data_version = inventory.current_data_version()
        df = inventory.load_overview(data_version=data_version)
        row = df[df["item_id"] == item_id].iloc[0]
        assert row["supply_status"] == inventory.STATUS_STOCKOUT

    def test_halted_when_active_halt_notice_mapped(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _item_with_positive_stock(conn)
        _insert_active_halt_notice(conn, item_id, "TEST-HALT-1")
        conn.close()

        data_version = inventory.current_data_version()
        df = inventory.load_overview(data_version=data_version)
        row = df[df["item_id"] == item_id].iloc[0]
        assert row["supply_status"] == inventory.STATUS_HALTED

    def test_expected_stockout_when_grade_danger_or_warning(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _item_with_positive_stock(conn)
        run_id = _latest_run_id(conn)
        _set_grade(conn, run_id, item_id, "경고")
        conn.close()

        data_version = inventory.current_data_version()
        df = inventory.load_overview(data_version=data_version)
        row = df[df["item_id"] == item_id].iloc[0]
        assert row["supply_status"] == inventory.STATUS_EXPECTED

    def test_normalized_is_default_when_no_signal(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _item_with_positive_stock(conn)
        run_id = _latest_run_id(conn)
        _set_grade(conn, run_id, item_id, "정상")
        conn.close()

        data_version = inventory.current_data_version()
        df = inventory.load_overview(data_version=data_version)
        row = df[df["item_id"] == item_id].iloc[0]
        assert row["supply_status"] == inventory.STATUS_NORMAL

    def test_stockout_outranks_halt_and_grade(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """우선순위 검증: 재고 0 + 활성 공급중단 공고 + 위험 등급이 겹쳐도 '현재 품절'이 이긴다."""
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _item_with_positive_stock(conn)
        run_id = _latest_run_id(conn)
        _set_grade(conn, run_id, item_id, "위험")
        _insert_active_halt_notice(conn, item_id, "TEST-HALT-PRIORITY")
        _set_closing_stock_zero(conn, item_id)
        conn.close()

        data_version = inventory.current_data_version()
        df = inventory.load_overview(data_version=data_version)
        row = df[df["item_id"] == item_id].iloc[0]
        assert row["supply_status"] == inventory.STATUS_STOCKOUT

    def test_halted_outranks_grade(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """우선순위 검증: 재고는 정상이지만 활성 공급중단 공고 + 위험 등급이 겹치면 '공급중단'이
        '품절 예상'보다 이긴다."""
        _activate(monkeypatch, db_path)
        conn = _direct_conn(db_path)
        item_id = _item_with_positive_stock(conn)
        run_id = _latest_run_id(conn)
        _set_grade(conn, run_id, item_id, "위험")
        _insert_active_halt_notice(conn, item_id, "TEST-HALT-VS-GRADE")
        conn.close()

        data_version = inventory.current_data_version()
        df = inventory.load_overview(data_version=data_version)
        row = df[df["item_id"] == item_id].iloc[0]
        assert row["supply_status"] == inventory.STATUS_HALTED
