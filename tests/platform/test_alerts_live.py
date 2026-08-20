"""Task M-26: views/alerts.py 실데이터 렌더 테스트(AppTest).

tests/platform/test_situation_live.py·test_review_live.py와 동일 관례 — 온디스크
소형 표준 스냅샷(subprocess로 --baseline-only 생성, 필요 시 + 위험 평가 배치)에 대해
실행한다. st.cache_data/st.cache_resource는 프로세스 전역이라 각 테스트에서 반드시
clear()해야 이전 테스트의 커넥션·결과가 재사용되지 않는다.

등급 상승 알림을 실 스냅샷에서 결정적으로 재현하기 위해 tests/platform/
test_situation_live.py의 `_force_danger_item`과 동일한 기법(raw SQL로 risk_results
직접 조작)을 쓴다 — 배치가 만든 실제 run_id의 params_hash 패밀리를 그대로 이어받는
전일 run 행 1개를 추가로 심어 "경고 → 위험" 상승을 보장한다(생성 데이터의 우연한
등급 변동에 의존하지 않는다).
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
from medsupply.services import alerts as alerts_service
from medsupply.services import inventory, workbench

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"
BATCH_SCRIPT = REPO_ROOT / "scripts" / "run_risk_batch.py"

SEED = 20260801
BASE_DATE = "2026-08-01"
PREV_DATE = "2026-07-31"


def _run_alerts() -> None:
    from medsupply import theme
    from medsupply.views import alerts

    theme.inject_css()
    alerts.render()


def _activate(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    st.cache_data.clear()
    st.cache_resource.clear()


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


def _force_grade_escalation(db_path: Path) -> str:
    """첫 품목의 최신 run 등급을 '위험'으로 고정하고, 같은 params_hash 패밀리의 전일
    run에 '경고' 등급 행 1개를 추가로 심어 결정적 등급 상승(경고→위험)을 만든다.

    test_situation_live.py의 `_force_danger_item`과 동일 기법(raw SQL 직접 조작) —
    생성된 시나리오 데이터의 우연한 등급 변동에 기대지 않기 위함이다. item_name을
    반환한다(제목 단언용).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT item_id, item_name FROM items ORDER BY item_id LIMIT 1"
        ).fetchone()
        item_id, item_name = row["item_id"], row["item_name"]

        latest_run_id = conn.execute(
            "SELECT run_id FROM risk_results ORDER BY as_of DESC, run_id DESC LIMIT 1"
        ).fetchone()["run_id"]
        _, sep, family = latest_run_id.partition("#")
        assert sep, f"unexpected run_id format without '#': {latest_run_id!r}"

        conn.execute(
            "UPDATE risk_results SET grade = '위험', base_grade = '위험'"
            " WHERE run_id = ? AND item_id = ?",
            (latest_run_id, item_id),
        )
        prev_run_id = f"{PREV_DATE}#{family}"
        conn.execute(
            "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade,"
            " escalated_by_notice, risk_type, score, days_to_stockout, depletion_date,"
            " factors_json) VALUES (?, ?, ?, '경고', '경고', 0, 'general', 50, NULL, NULL,"
            " '{}')",
            (prev_run_id, item_id, PREV_DATE),
        )
        conn.commit()
    finally:
        conn.close()
    return item_name


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def batch_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 스냅샷 + 위험 평가 배치 1 run — 모듈 1회만 생성(비용 절감)."""
    db_path = tmp_path_factory.mktemp("alerts_live_batch") / "base.db"
    _generate_snapshot(db_path)
    _run_batch(db_path)
    return db_path


@pytest.fixture()
def escalated_db(batch_snapshot: Path, tmp_path: Path) -> Path:
    """batch_snapshot을 함수별 tmp_path로 복사한 뒤 등급 상승을 강제 주입한다."""
    dest = tmp_path / "escalated.db"
    shutil.copy(batch_snapshot, dest)
    _force_grade_escalation(dest)
    return dest


@pytest.fixture(scope="module")
def no_run_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only만(배치 미실행, 공고 없음) — 알림 0건 경로 전용."""
    db_path = tmp_path_factory.mktemp("alerts_live_no_run") / "base.db"
    _generate_snapshot(db_path)
    return db_path


@pytest.fixture()
def empty_alerts_db(no_run_snapshot: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "empty.db"
    shutil.copy(no_run_snapshot, dest)
    return dest


# ---------------------------------------------------------------------------
# 정상 스냅샷 경로 — 등급 상승 알림이 자동 sync로 생성·렌더된다
# ---------------------------------------------------------------------------


def test_alerts_renders_grade_up_alert_after_auto_sync(
    escalated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item_name = None
    conn = sqlite3.connect(escalated_db)
    try:
        item_name = conn.execute(
            "SELECT item_name FROM items ORDER BY item_id LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    _activate(monkeypatch, escalated_db)

    # 렌더 전에는 alerts 테이블이 비어 있다(뷰의 자동 sync가 이번 렌더에서 처음 만든다).
    conn = sqlite3.connect(escalated_db)
    try:
        before_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    finally:
        conn.close()
    assert before_count == 0

    at = AppTest.from_function(_run_alerts)
    at.run()

    assert not at.exception
    rendered = "\n".join(md.value for md in at.markdown)
    assert f"{item_name} 위험등급 상승" in rendered

    conn = sqlite3.connect(escalated_db)
    try:
        after_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    finally:
        conn.close()
    assert after_count >= 1


def test_alerts_second_render_does_not_duplicate(
    escalated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """자동 sync는 멱등이라 같은 스냅샷을 다시 렌더해도 알림 행이 늘지 않는다."""
    _activate(monkeypatch, escalated_db)

    at1 = AppTest.from_function(_run_alerts)
    at1.run()
    assert not at1.exception

    conn = sqlite3.connect(escalated_db)
    try:
        count_after_first = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    finally:
        conn.close()

    st.cache_data.clear()
    st.cache_resource.clear()
    at2 = AppTest.from_function(_run_alerts)
    at2.run()
    assert not at2.exception

    conn = sqlite3.connect(escalated_db)
    try:
        count_after_second = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    finally:
        conn.close()

    assert count_after_second == count_after_first


# ---------------------------------------------------------------------------
# 0건 안내 — 배치 미실행 + 공고 없음(--baseline-only 자연 상태)
# ---------------------------------------------------------------------------


def test_alerts_shows_empty_state_info_when_no_alerts(
    empty_alerts_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate(monkeypatch, empty_alerts_db)

    at = AppTest.from_function(_run_alerts)
    at.run()

    assert not at.exception
    assert len(at.info) >= 1
    assert any("알림이 없습니다" in i.value for i in at.info)


# ---------------------------------------------------------------------------
# DB 부재 경로
# ---------------------------------------------------------------------------


def test_alerts_missing_db_shows_warning_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()
    _activate(monkeypatch, missing)

    at = AppTest.from_function(_run_alerts)
    at.run()

    assert not at.exception
    assert len(at.warning) >= 1
    assert any("표준 스냅샷이 없습니다" in w.value for w in at.warning)


def test_alerts_smoke_still_passes_with_no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """기존 test_views_smoke.py의 alerts 스모크(DB 부재 → 경고 경로)가 여전히
    통과하는지 이 파일에서도 같은 조건으로 재확인한다(회귀 방지)."""
    missing = Path("/nonexistent/medsupply-m26-test/medsupply.db")
    assert not missing.exists()
    _activate(monkeypatch, missing)

    at = AppTest.from_function(_run_alerts)
    at.run()

    assert not at.exception


# ---------------------------------------------------------------------------
# 읽음 처리 — 버튼 클릭이 아니라 함수 직접 호출로 검증(브리프 명시) + 캐시 clear
# ---------------------------------------------------------------------------


def test_mark_alert_read_updates_row_and_bumps_version(
    escalated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate(monkeypatch, escalated_db)

    write_conn = workbench.open_write_conn()
    try:
        sync_result = alerts_service.sync_alerts(write_conn)
    finally:
        write_conn.close()
    assert sync_result["created"] >= 1
    st.cache_data.clear()

    conn = sqlite3.connect(escalated_db)
    try:
        alert_id = conn.execute(
            "SELECT alert_id FROM alerts WHERE is_read = 0 ORDER BY alert_id LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    before_version = inventory.current_data_version()

    write_conn = workbench.open_write_conn()
    try:
        writer.mark_alert_read(write_conn, alert_id)
    finally:
        write_conn.close()
    st.cache_data.clear()

    conn = sqlite3.connect(escalated_db)
    try:
        is_read = conn.execute(
            "SELECT is_read FROM alerts WHERE alert_id = ?", (alert_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    after_version = inventory.current_data_version()

    assert is_read == 1
    assert after_version == before_version + 1

    # 캐시 clear 후 load_alerts(unread_only=True)가 이 알림을 더 이상 반환하지 않는다.
    unread = alerts_service.load_alerts(unread_only=True, data_version=after_version)
    assert alert_id not in set(unread["alert_id"])
