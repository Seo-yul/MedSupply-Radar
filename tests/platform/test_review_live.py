"""Task M-16: review.py 실데이터 렌더 테스트 + trend_chart 시그니처 계약 테스트.

AppTest.from_function(review.render 래퍼)을 온디스크 소형 표준 스냅샷(subprocess로
--baseline-only 생성 + 위험 평가 배치 1 run 실행)에 대해 실행한다(tests/platform/
test_situation_live.py와 동일 관례). st.cache_data/st.cache_resource는 프로세스
전역이라 각 테스트에서 반드시 clear()해야 이전 테스트의 커넥션·결과가 재사용되지 않는다.

trend_chart의 새 시그니처(series_df, *, events=None, forecast=None)는 순수 함수라
Streamlit 컨텍스트 없이 직접 호출해 검증한다(호출처는 review.py뿐이라 이 파일에 둔다).
"""

from __future__ import annotations

import datetime as dt
import inspect
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from medsupply import settings
from medsupply.data import writer
from medsupply.services import inventory, workbench
from medsupply.ui import charts

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"
BATCH_SCRIPT = REPO_ROOT / "scripts" / "run_risk_batch.py"

SEED = 20260801
BASE_DATE = "2026-08-01"


def _run_review() -> None:
    from medsupply import theme
    from medsupply.views import review

    theme.inject_css()
    review.render()


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


def _top_score_item_name(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """review.py의 selectbox가 index 0으로 고르는 품목명 — inventory.load_overview()를
    직접 호출해 구한다(브리프: 품목 선택 목록은 M-15의 load_overview를 재사용). score
    동점 시 pandas.sort_values의 정렬 안정성에 의존하는 별도 SQL 재구현은 순서가
    어긋날 수 있어(quicksort는 stable하지 않다), 실제 호출부와 동일한 함수로 구한다."""
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    st.cache_data.clear()
    st.cache_resource.clear()
    data_version = inventory.current_data_version()
    overview = inventory.load_overview(data_version=data_version)
    return overview.iloc[0]["item_name"]


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 스냅샷 + 위험 평가 배치 1 run — 모듈 1회만 생성(비용 절감)."""
    db_path = tmp_path_factory.mktemp("review_live_base") / "base.db"
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


def test_review_renders_real_snapshot_without_exception(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    top_name = _top_score_item_name(live_db, monkeypatch)

    at = AppTest.from_function(_run_review)
    at.run()

    assert not at.exception
    rendered = "\n".join(md.value for md in at.markdown)
    assert top_name in rendered

    plotly_elements = at.get("plotly_chart")
    assert len(plotly_elements) == 2
    assert any("gauge+number" in el.proto.spec for el in plotly_elements)


# ---------------------------------------------------------------------------
# DB 부재 경로
# ---------------------------------------------------------------------------


def test_review_missing_db_shows_warning_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()

    monkeypatch.setattr(settings, "DB_PATH", missing)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_review)
    at.run()

    assert not at.exception
    assert len(at.warning) >= 1
    assert any("표준 스냅샷이 없습니다" in w.value for w in at.warning)


def test_review_smoke_still_passes_with_no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """기존 test_views_smoke.py의 review 스모크(DB 부재 → 경고 경로)가 여전히
    통과하는지 이 파일에서도 같은 조건으로 재확인한다(회귀 방지)."""
    missing = Path("/nonexistent/medsupply-m16-test/medsupply.db")
    assert not missing.exists()

    monkeypatch.setattr(settings, "DB_PATH", missing)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_review)
    at.run()

    assert not at.exception


# ---------------------------------------------------------------------------
# 조치 저장 로직 — 버튼 클릭이 아니라 함수 직접 호출로 검증(브리프 명시)
# ---------------------------------------------------------------------------


def test_save_action_history_increments_history_and_data_version(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DB_PATH", live_db)
    st.cache_data.clear()
    st.cache_resource.clear()

    conn = sqlite3.connect(live_db)
    try:
        item_id = conn.execute("SELECT item_id FROM items ORDER BY item_id LIMIT 1").fetchone()[0]
        before_count = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
    finally:
        conn.close()
    before_version = inventory.current_data_version()

    write_conn = workbench.open_write_conn()
    try:
        history_id = writer.save_action_history(
            write_conn, item_id, "발주량 조정", "김약사", "AppTest 직접 호출 검증",
            status="진행 중", risk_type=None,
        )
    finally:
        write_conn.close()
    st.cache_data.clear()

    conn = sqlite3.connect(live_db)
    try:
        after_count = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
    finally:
        conn.close()
    after_version = inventory.current_data_version()

    assert isinstance(history_id, int)
    assert after_count == before_count + 1
    assert after_version == before_version + 1


# ---------------------------------------------------------------------------
# trend_chart 시그니처 계약 — series_df(위치) + events/forecast(키워드 전용)
# ---------------------------------------------------------------------------


def _sample_series() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2026-07-30", "2026-07-31", "2026-08-01"],
            "usage_qty": [10, 12, 11],
            "incoming_qty": [0, 0, 0],
            "closing_stock": [100, 90, 79],
        }
    )


class TestTrendChartSignature:
    def test_base_traces_use_series_df_data(self) -> None:
        fig = charts.trend_chart(_sample_series())
        names = [trace.name for trace in fig.data]
        assert names == ["재고", "일 사용량"]
        assert list(fig.data[0].y) == [100, 90, 79]
        assert list(fig.data[1].y) == [10, 12, 11]

    def test_no_events_means_no_vlines(self) -> None:
        fig = charts.trend_chart(_sample_series())
        assert len(fig.layout.shapes) == 0

    def test_events_add_one_vline_per_entry(self) -> None:
        fig = charts.trend_chart(
            _sample_series(), events=[(dt.date(2026, 7, 31), "공급 공고", "#59506e")],
        )
        assert len(fig.layout.shapes) == 1

    def test_forecast_adds_dashed_trace_named_predicted_usage(self) -> None:
        fig = charts.trend_chart(_sample_series(), forecast=[13.0, 14.0])
        names = [trace.name for trace in fig.data]
        assert names == ["재고", "일 사용량", "예측 사용량"]
        forecast_trace = fig.data[2]
        assert forecast_trace.line.dash == "dash"
        assert forecast_trace.line.color == "#59506e"
        assert list(forecast_trace.y) == [13.0, 14.0]

    def test_forecast_none_means_no_third_trace(self) -> None:
        fig = charts.trend_chart(_sample_series(), forecast=None)
        assert len(fig.data) == 2

    def test_events_and_forecast_are_keyword_only(self) -> None:
        sig = inspect.signature(charts.trend_chart)
        params = list(sig.parameters.values())
        assert params[0].name == "series_df"
        assert params[0].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        for name in ("events", "forecast"):
            assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY
