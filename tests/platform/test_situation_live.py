"""Task M-15: situation.py 실데이터 렌더 테스트.

AppTest.from_function(situation.render 래퍼)을 온디스크 소형 스냅샷(subprocess로
--baseline-only 생성 + 위험 평가 배치 1 run)에 대해 실행한다. AppTest.from_function은
함수 소스를 별도 스크립트로 추출해 같은 프로세스의 별도 스레드에서 실행하므로(streamlit.
testing.v1.LocalScriptRunner), monkeypatch로 바꾼 medsupply.settings.DB_PATH는
sys.modules에 캐시된 같은 모듈 객체를 통해 그대로 보인다 — 단, st.cache_data/
st.cache_resource는 프로세스 전역이라 각 테스트에서 반드시 clear()해야 이전 테스트의
커넥션·결과가 재사용되지 않는다.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from medsupply import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"
BATCH_SCRIPT = REPO_ROOT / "scripts" / "run_risk_batch.py"

SEED = 20260801
BASE_DATE = "2026-08-01"


def _run_situation() -> None:
    from medsupply import theme
    from medsupply.views import situation

    theme.inject_css()
    situation.render()


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


def _force_single_essential_risk_item(db_path: Path, days_to_stockout: int) -> None:
    """모든 품목의 is_essential을 0으로 내린 뒤 1개만 필수의약품+경고 등급·지정
    days_to_stockout으로 고정한다 — "필수의약품 위험·경고" 집계 대상을 결정론적으로
    만든다(F7 ② 검증용)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE items SET is_essential = 0")
        item_id = conn.execute(
            "SELECT item_id FROM items ORDER BY item_id LIMIT 1"
        ).fetchone()[0]
        run_id = conn.execute("SELECT run_id FROM risk_results LIMIT 1").fetchone()[0]
        conn.execute("UPDATE items SET is_essential = 1 WHERE item_id = ?", (item_id,))
        conn.execute(
            "UPDATE risk_results SET grade = '경고', days_to_stockout = ?"
            " WHERE run_id = ? AND item_id = ?",
            (days_to_stockout, run_id, item_id),
        )
        conn.commit()
    finally:
        conn.close()


def _force_danger_item(db_path: Path) -> str:
    """1개 품목을 결정적으로 '위험' 등급으로 만들고 item_name을 반환한다(마크업 표본 단언용).

    baseline-only 생성이 자연히 '위험' 등급을 만들어내는지는 시드에 의존해 불확실하므로,
    브리프가 test_inventory_service에 명시한 것과 같은 raw SQL 조작 기법을 그대로 쓴다.
    """
    conn = sqlite3.connect(db_path)
    try:
        item_id, item_name = conn.execute(
            "SELECT item_id, item_name FROM items ORDER BY item_id LIMIT 1"
        ).fetchone()
        run_id = conn.execute("SELECT run_id FROM risk_results LIMIT 1").fetchone()[0]
        conn.execute(
            "UPDATE risk_results SET grade = '위험', score = 95, days_to_stockout = 3"
            " WHERE run_id = ? AND item_id = ?",
            (run_id, item_id),
        )
        conn.commit()
    finally:
        conn.close()
    return item_name


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 스냅샷 + 위험 평가 배치 1 run — 모듈 1회만 생성(비용 절감)."""
    db_path = tmp_path_factory.mktemp("situation_live_base") / "base.db"
    _generate_snapshot(db_path)
    _run_batch(db_path)
    return db_path


@pytest.fixture()
def live_db(base_snapshot: Path, tmp_path: Path) -> Path:
    """base_snapshot을 함수별 tmp_path로 복사해 테스트 간 쓰기 격리를 보장한다."""
    dest = tmp_path / "t.db"
    shutil.copy(base_snapshot, dest)
    return dest


# ---------------------------------------------------------------------------
# 정상 스냅샷 경로
# ---------------------------------------------------------------------------


def test_situation_renders_real_snapshot_without_exception(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    danger_name = _force_danger_item(live_db)

    monkeypatch.setattr(settings, "DB_PATH", live_db)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_situation)
    at.run()

    assert not at.exception
    rendered = "\n".join(md.value for md in at.markdown)
    assert danger_name in rendered


def test_situation_smoke_still_passes_with_no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """기존 test_views_smoke.py의 situation 스모크(DB 부재 → 경고 경로)가 여전히
    통과하는지 이 파일에서도 같은 조건으로 재확인한다(회귀 방지)."""
    missing = Path("/nonexistent/medsupply-m15-test/medsupply.db")
    assert not missing.exists()

    monkeypatch.setattr(settings, "DB_PATH", missing)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_situation)
    at.run()

    assert not at.exception


# ---------------------------------------------------------------------------
# DB 부재 경로
# ---------------------------------------------------------------------------


def test_situation_missing_db_shows_warning_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()

    monkeypatch.setattr(settings, "DB_PATH", missing)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_situation)
    at.run()

    assert not at.exception
    assert len(at.warning) >= 1
    assert any("표준 스냅샷이 없습니다" in w.value for w in at.warning)


# ---------------------------------------------------------------------------
# F7 — 문구 2건 실데이터 바인딩(마크업·클래스 불변, f-string 값만)
# ---------------------------------------------------------------------------


def test_situation_series_range_binds_to_meta_base_date(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """① "2026.01.01~08.01" 하드코딩 대신 base_date−365일~base_date를 같은 표기
    형식(%Y.%m.%d)으로 렌더한다."""
    monkeypatch.setattr(settings, "DB_PATH", live_db)
    st.cache_data.clear()
    st.cache_resource.clear()

    conn = sqlite3.connect(live_db)
    base_date = date.fromisoformat(
        conn.execute("SELECT value FROM meta WHERE key = 'base_date'").fetchone()[0]
    )
    conn.close()
    series_start = base_date - timedelta(days=365)
    expected = f"{series_start:%Y.%m.%d}~{base_date:%Y.%m.%d}"

    at = AppTest.from_function(_run_situation)
    at.run()

    assert not at.exception
    rendered = "\n".join(md.value for md in at.markdown)
    assert expected in rendered
    assert "2026.01.01~08.01" not in rendered


def test_situation_essential_risk_window_binds_to_live_max_days(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """② "10일 이내 소진" 하드코딩 대신, 필수의약품 위험·경고 집계 대상의 실제 최대
    days_to_stockout을 "{n}일 이내"로 바인딩한다."""
    _force_single_essential_risk_item(live_db, 6)

    monkeypatch.setattr(settings, "DB_PATH", live_db)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_situation)
    at.run()

    assert not at.exception
    rendered = "\n".join(md.value for md in at.markdown)
    assert "필수의약품 1종이 6일 이내 소진될 수 있습니다" in rendered
    assert "10일 이내 소진" not in rendered


def test_situation_essential_risk_window_shows_dash_when_no_target(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """대상 0건이면 문장 구조는 유지한 채 '-'로 표시한다."""
    conn = sqlite3.connect(live_db)
    conn.execute("UPDATE items SET is_essential = 0")
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "DB_PATH", live_db)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_situation)
    at.run()

    assert not at.exception
    rendered = "\n".join(md.value for md in at.markdown)
    assert "필수의약품 0종이 - 소진될 수 있습니다" in rendered
