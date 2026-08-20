"""Task M-18: history.py 실데이터 렌더 테스트.

AppTest.from_function(history.render 래퍼)을 온디스크 소형 표준 스냅샷(subprocess로
--baseline-only 생성)에 대해 실행한다(tests/platform/test_situation_live.py와 동일 관례).
--baseline-only 스냅샷은 이력 시드를 적재하지 않는다(이력 시드 적재는 "전체 빌드"인 주입
경로에서만 의미가 있다 — scripts/generate_dataset.py 문서 참조) — 정상 경로 테스트는
writer.save_action_history(쓰기 단일 경로)로 직접 이력을 심는다.

st.cache_data/st.cache_resource는 프로세스 전역이라 각 테스트에서 반드시 clear()해야
이전 테스트의 커넥션·결과가 재사용되지 않는다.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from medsupply import settings
from medsupply.data import writer
from medsupply.services import history as history_service
from medsupply.services import inventory

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"

SEED = 20260801
BASE_DATE = "2026-08-01"


def _run_history() -> None:
    from medsupply import theme
    from medsupply.views import history

    theme.inject_css()
    history.render()


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


def _seed_history_rows(db_path: Path) -> tuple[str, str]:
    """상태·위험 유형이 다른 이력 2건을 writer.save_action_history(쓰기 단일 경로)로 심는다.

    --baseline-only 스냅샷은 이력 시드를 적재하지 않으므로 정상 경로 테스트가 직접 심는다.
    Returns (item_id, item_name).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        item_id, item_name = conn.execute(
            "SELECT item_id, item_name FROM items ORDER BY item_id LIMIT 1"
        ).fetchone()
        writer.save_action_history(
            conn, item_id, "대체 발주", "김약사", "AppTest 시드 — 완료 건",
            status="완료", risk_type="demand_surge",
        )
        writer.save_action_history(
            conn, item_id, "입고 재확인", "김약사", "AppTest 시드 — 진행 중 건",
            status="진행 중", risk_type="supply_halt",
        )
    finally:
        conn.close()
    return item_id, item_name


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 스냅샷 — 모듈 1회만 생성(비용 절감)."""
    db_path = tmp_path_factory.mktemp("history_live_base") / "base.db"
    _generate_snapshot(db_path)
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


def test_history_renders_real_snapshot_without_exception(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _item_id, item_name = _seed_history_rows(live_db)

    monkeypatch.setattr(settings, "DB_PATH", live_db)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_history)
    at.run()

    assert not at.exception
    table = at.dataframe[0].value
    assert item_name in set(table["품목"])


# ---------------------------------------------------------------------------
# DB 부재 경로
# ---------------------------------------------------------------------------


def test_history_missing_db_shows_warning_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()

    monkeypatch.setattr(settings, "DB_PATH", missing)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_history)
    at.run()

    assert not at.exception
    assert len(at.warning) >= 1
    assert any("표준 스냅샷이 없습니다" in w.value for w in at.warning)


def test_history_smoke_still_passes_with_no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """기존 test_views_smoke.py의 history 스모크(DB 부재 → 경고 경로)가 여전히
    통과하는지 이 파일에서도 같은 조건으로 재확인한다(회귀 방지)."""
    missing = Path("/nonexistent/medsupply-m18-test/medsupply.db")
    assert not missing.exists()

    monkeypatch.setattr(settings, "DB_PATH", missing)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_history)
    at.run()

    assert not at.exception


# ---------------------------------------------------------------------------
# services.history.load_history — 함수 직접 호출로 신규 로직(검색·risk_type) 검증
# ---------------------------------------------------------------------------


def test_load_history_search_filters_by_item_name_or_note(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_history_rows(live_db)

    monkeypatch.setattr(settings, "DB_PATH", live_db)
    st.cache_data.clear()
    st.cache_resource.clear()

    data_version = inventory.current_data_version()

    matched = history_service.load_history(search="완료 건", data_version=data_version)
    unmatched = history_service.load_history(
        search="존재하지-않는-검색어", data_version=data_version
    )

    assert len(matched) == 1
    assert "완료 건" in matched.iloc[0]["note"]
    assert unmatched.empty


def test_load_history_risk_type_filter_matches_seeded_rows(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_history_rows(live_db)

    monkeypatch.setattr(settings, "DB_PATH", live_db)
    st.cache_data.clear()
    st.cache_resource.clear()

    data_version = inventory.current_data_version()

    demand_surge_only = history_service.load_history(
        risk_type="demand_surge", data_version=data_version
    )

    assert list(demand_surge_only["risk_type"]) == ["demand_surge"]
