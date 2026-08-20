"""Task M-25: medsupply/services/orders.py 계약 테스트 + orders.py 실데이터 렌더 테스트.

두 종류의 픽스처를 쓴다:
- compute_order_proposal 단위 계약(TestComputeOrderProposal): shortage 산식을 손검산
  가능한 값으로 고정해야 하므로, 실 스냅샷(generate_dataset.py)이 아니라 medsupply.data.db로
  직접 최소 행만 INSERT한 온디스크 소형 DB를 쓴다(픽스처 DB, 브리프 표현 그대로).
  compute_order_proposal은 conn을 받지 않고 스스로 settings.DB_PATH를 여는 계약이라
  (inventory.get_conn() 경유), :memory: 커넥션을 직접 넘길 수 없다 — tests/platform/
  test_workbench_service.py와 동일하게 monkeypatch로 settings.DB_PATH를 바꿔치기한다.
- AppTest 렌더·저장 로직(그 아래 나머지): tests/platform/test_review_live.py와 동일 관례 —
  온디스크 표준 스냅샷(subprocess로 --baseline-only 생성 + 위험 평가 배치 1 run)에 대해
  실행한다.

st.cache_data/st.cache_resource는 프로세스 전역이라 각 테스트에서 반드시 clear()해야
이전 테스트의 커넥션·결과가 재사용되지 않는다.
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
from medsupply.data import db, writer
from medsupply.services import inventory, workbench
from medsupply.services import orders as orders_service

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"
BATCH_SCRIPT = REPO_ROOT / "scripts" / "run_risk_batch.py"

SEED = 20260801
BASE_DATE = "2026-08-01"

#: compute_order_proposal 손검산 픽스처 전용 base_date(표준 스냅샷과 무관한 독립 소형 DB).
PROPOSAL_BASE_DATE = "2026-08-25"
PROPOSAL_ITEM_ID = "ITM-PROP"


def _run_orders() -> None:
    from medsupply import theme
    from medsupply.views import orders

    theme.inject_css()
    orders.render()


def _activate(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    """settings.DB_PATH를 db_path로 바꾸고 캐시를 초기화한다(모든 테스트의 필수 선행 단계)."""
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    st.cache_data.clear()
    st.cache_resource.clear()


# ---------------------------------------------------------------------------
# compute_order_proposal 손검산 픽스처 DB 빌더
# ---------------------------------------------------------------------------


def _build_proposal_db(
    db_path: Path,
    *,
    closing_stock: int,
    total_forecast: float | None,
    usage_rows: list[int] | None = None,
    pending_shipments: list[tuple[str, int]] = (),
    supplier: str = "대한제약",
    substitute: tuple[str, str] | None = None,
) -> str:
    """최소 필드만 채운 결정적 픽스처 DB — item_id를 반환한다.

    total_forecast가 주어지면 risk_results(1 run) + forecasts 행을 만들어
    workbench.load_item_detail의 forecast 경로를 태운다(grade='정상', risk_type='general'
    고정 — 이 테스트가 검증하는 대상이 아니라 값 유무만 문제 되므로 임의 유효값). None이면
    위험 평가 run 자체를 만들지 않아(forecast=None) avg_daily_usage 폴백 경로를 태운다.

    usage_rows가 주어지면 PROPOSAL_BASE_DATE로 끝나는 연속 일자에 그 usage_qty들을,
    마지막 행(=PROPOSAL_BASE_DATE)의 closing_stock을 closing_stock으로 채운다. 없으면
    PROPOSAL_BASE_DATE 1일치만 넣는다.

    substitute=(sibling_item_id, sibling_supplier)가 주어지면 같은 substitute_group을
    공유하는 대체 후보 품목 1개를 추가한다(suppliers 목록 검증용).
    """
    conn = db.get_connection(str(db_path))
    db.init_db(conn, drop=False)

    group_id = None
    if substitute is not None:
        group_id = "SG-PROP"
        conn.execute(
            "INSERT INTO ingredients(ingredient_code, ingredient_name_kr, ingredient_name_en,"
            " atc_code) VALUES ('ING-PROP', '테스트성분', 'Test Ingredient', NULL)"
        )
        conn.execute(
            "INSERT INTO substitute_groups(substitute_group_id, ingredient_code, strength,"
            " form, route, group_label) VALUES (?, 'ING-PROP', NULL, NULL, NULL, ?)",
            (group_id, group_id),
        )

    conn.execute(
        "INSERT INTO items(item_id, item_name, ingredient_code, supplier, is_essential,"
        " substitute_group_id) VALUES (?, '테스트품목', NULL, ?, 0, ?)",
        (PROPOSAL_ITEM_ID, supplier, group_id),
    )
    if substitute is not None:
        sibling_id, sibling_supplier = substitute
        conn.execute(
            "INSERT INTO items(item_id, item_name, ingredient_code, supplier, is_essential,"
            " substitute_group_id) VALUES (?, '테스트대체품목', NULL, ?, 0, ?)",
            (sibling_id, sibling_supplier, group_id),
        )

    base = date.fromisoformat(PROPOSAL_BASE_DATE)
    if usage_rows:
        offset = len(usage_rows) - 1
        for i, qty in enumerate(usage_rows):
            d = base - timedelta(days=offset - i)
            conn.execute(
                "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty,"
                " closing_stock) VALUES (?, ?, ?, 0, ?)",
                (PROPOSAL_ITEM_ID, d.isoformat(), qty, closing_stock),
            )
    else:
        conn.execute(
            "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty,"
            " closing_stock) VALUES (?, ?, 0, 0, ?)",
            (PROPOSAL_ITEM_ID, PROPOSAL_BASE_DATE, closing_stock),
        )

    for expected_date, qty in pending_shipments:
        conn.execute(
            "INSERT INTO incoming_shipments(item_id, order_date, expected_date, expected_qty,"
            " actual_date, actual_qty, status) VALUES (?, ?, ?, ?, NULL, NULL, '예정')",
            (PROPOSAL_ITEM_ID, PROPOSAL_BASE_DATE, expected_date, qty),
        )

    if total_forecast is not None:
        run_id = f"{PROPOSAL_BASE_DATE}#feed0001"
        conn.execute(
            "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade,"
            " escalated_by_notice, risk_type, score, days_to_stockout, depletion_date,"
            " factors_json) VALUES (?, ?, ?, '정상', '정상', 0, 'general', 10, NULL, NULL, '{}')",
            (run_id, PROPOSAL_ITEM_ID, PROPOSAL_BASE_DATE),
        )
        conn.execute(
            "INSERT INTO forecasts(run_id, item_id, as_of, horizon_days, avg_daily_forecast,"
            " total_forecast, daily_json) VALUES (?, ?, ?, 14, ?, ?, '[]')",
            (run_id, PROPOSAL_ITEM_ID, PROPOSAL_BASE_DATE, total_forecast / 14, total_forecast),
        )

    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        [("base_date", PROPOSAL_BASE_DATE), ("data_version", "1")],
    )
    conn.commit()
    conn.close()
    return PROPOSAL_ITEM_ID


# ---------------------------------------------------------------------------
# compute_order_proposal 계약 — shortage 산식 손검산
# ---------------------------------------------------------------------------


class TestComputeOrderProposal:
    def test_shortage_manual_calculation_rounds_up_to_50(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """수요 356·재고 152·입고 0 → shortage 204 → 50 올림 250(브리프 명시 손검산)."""
        db_path = tmp_path / "t.db"
        item_id = _build_proposal_db(db_path, closing_stock=152, total_forecast=356.0)
        _activate(monkeypatch, db_path)

        proposal = orders_service.compute_order_proposal(
            item_id, data_version=inventory.current_data_version()
        )

        assert proposal["current_stock"] == 152
        assert proposal["expected_demand"] == 356.0
        assert proposal["incoming_qty"] == 0
        assert proposal["shortage"] == 204
        assert proposal["suggested_qty"] == 250

    def test_shortage_deducts_pending_incoming_qty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """미래 예정 입고 수량만큼 shortage가 줄어든다(asof.is_pending_at 술어)."""
        db_path = tmp_path / "t.db"
        future = (date.fromisoformat(PROPOSAL_BASE_DATE) + timedelta(days=5)).isoformat()
        item_id = _build_proposal_db(
            db_path, closing_stock=152, total_forecast=356.0,
            pending_shipments=[(future, 60)],
        )
        _activate(monkeypatch, db_path)

        proposal = orders_service.compute_order_proposal(
            item_id, data_version=inventory.current_data_version()
        )

        assert proposal["incoming_qty"] == 60
        assert proposal["shortage"] == 144
        assert proposal["suggested_qty"] == 150

    def test_overdue_incoming_shipment_not_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """연체(예정일이 이미 지난) 입고는 미래 예정이 아니므로 합산에서 제외된다."""
        db_path = tmp_path / "t.db"
        overdue = (date.fromisoformat(PROPOSAL_BASE_DATE) - timedelta(days=3)).isoformat()
        item_id = _build_proposal_db(
            db_path, closing_stock=152, total_forecast=356.0,
            pending_shipments=[(overdue, 60)],
        )
        _activate(monkeypatch, db_path)

        proposal = orders_service.compute_order_proposal(
            item_id, data_version=inventory.current_data_version()
        )

        assert proposal["incoming_qty"] == 0
        assert proposal["shortage"] == 204

    def test_expected_demand_falls_back_to_avg_daily_usage_when_no_forecast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """예측 run이 없으면 avg_daily_usage * 14로 폴백하고, risk/grade는 None이다."""
        db_path = tmp_path / "t.db"
        item_id = _build_proposal_db(
            db_path, closing_stock=50, total_forecast=None, usage_rows=[10, 10, 10],
        )
        _activate(monkeypatch, db_path)

        proposal = orders_service.compute_order_proposal(
            item_id, data_version=inventory.current_data_version()
        )

        assert proposal["expected_demand"] == 140.0
        assert proposal["shortage"] == 90
        assert proposal["suggested_qty"] == 100
        assert proposal["grade"] is None
        assert proposal["risk_type"] is None

    def test_shortage_zero_when_stock_covers_demand(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """재고가 예상 수요를 넘으면 shortage·suggested_qty 모두 0(충분)."""
        db_path = tmp_path / "t.db"
        item_id = _build_proposal_db(db_path, closing_stock=300, total_forecast=100.0)
        _activate(monkeypatch, db_path)

        proposal = orders_service.compute_order_proposal(
            item_id, data_version=inventory.current_data_version()
        )

        assert proposal["shortage"] == 0
        assert proposal["suggested_qty"] == 0

    def test_suppliers_include_own_first_then_same_condition_dedup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """suppliers = 현 품목 supplier + same_condition 대체 후보 supplier(중복 제거)."""
        db_path = tmp_path / "t.db"
        item_id = _build_proposal_db(
            db_path, closing_stock=100, total_forecast=100.0,
            supplier="대한제약", substitute=("ITM-SIB", "유니메드"),
        )
        _activate(monkeypatch, db_path)

        proposal = orders_service.compute_order_proposal(
            item_id, data_version=inventory.current_data_version()
        )

        assert proposal["suppliers"] == ["대한제약", "유니메드"]
        assert proposal["grade"] == "정상"
        assert proposal["risk_type"] == "general"

    def test_returns_item_id_and_item_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "t.db"
        item_id = _build_proposal_db(db_path, closing_stock=100, total_forecast=100.0)
        _activate(monkeypatch, db_path)

        proposal = orders_service.compute_order_proposal(
            item_id, data_version=inventory.current_data_version()
        )

        assert proposal["item_id"] == item_id
        assert proposal["item_name"] == "테스트품목"


# ---------------------------------------------------------------------------
# 서브프로세스·DB 헬퍼(표준 스냅샷 — AppTest 렌더·저장 로직 검증용)
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
    """orders.py의 selectbox가 index 0으로 고르는 품목명(load_overview 재사용, M-16과 동일)."""
    _activate(monkeypatch, db_path)
    data_version = inventory.current_data_version()
    overview = inventory.load_overview(data_version=data_version)
    return overview.iloc[0]["item_name"]


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 스냅샷 + 위험 평가 배치 1 run — 모듈 1회만 생성(비용 절감)."""
    db_path = tmp_path_factory.mktemp("orders_live_base") / "base.db"
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


def test_orders_renders_real_snapshot_without_exception(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    top_name = _top_score_item_name(live_db, monkeypatch)

    at = AppTest.from_function(_run_orders)
    at.run()

    assert not at.exception
    rendered = "\n".join(md.value for md in at.markdown)
    assert top_name in rendered


# ---------------------------------------------------------------------------
# DB 부재 경로
# ---------------------------------------------------------------------------


def test_orders_missing_db_shows_warning_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()
    _activate(monkeypatch, missing)

    at = AppTest.from_function(_run_orders)
    at.run()

    assert not at.exception
    assert len(at.warning) >= 1
    assert any("표준 스냅샷이 없습니다" in w.value for w in at.warning)


def test_orders_smoke_still_passes_with_no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """기존 test_views_smoke.py의 orders 스모크(DB 부재 → 경고 경로)가 여전히
    통과하는지 이 파일에서도 같은 조건으로 재확인한다(회귀 방지)."""
    missing = Path("/nonexistent/medsupply-m25-test/medsupply.db")
    assert not missing.exists()
    _activate(monkeypatch, missing)

    at = AppTest.from_function(_run_orders)
    at.run()

    assert not at.exception


# ---------------------------------------------------------------------------
# 저장 로직 — 버튼 클릭이 아니라 함수 직접 호출로 검증(브리프 명시)
# ---------------------------------------------------------------------------


def test_save_order_request_and_linked_action_history_increment_and_bump_version(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate(monkeypatch, live_db)

    conn = sqlite3.connect(live_db)
    try:
        item_id = conn.execute(
            "SELECT item_id FROM items ORDER BY item_id LIMIT 1"
        ).fetchone()[0]
        before_orders = conn.execute("SELECT COUNT(*) FROM order_requests").fetchone()[0]
        before_actions = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
    finally:
        conn.close()
    before_version = inventory.current_data_version()

    proposal = orders_service.compute_order_proposal(item_id, data_version=before_version)

    write_conn = workbench.open_write_conn()
    try:
        order_id = writer.save_order_request(
            write_conn, item_id, "대한제약", 300, "2026-08-15", "김약사", "테스트 사유",
        )
        history_id = writer.save_action_history(
            write_conn, item_id, "발주 요청", "김약사",
            note="대한제약 300개 · 희망 2026-08-15",
            status="진행 중", order_id=order_id, risk_type=proposal["risk_type"],
        )
    finally:
        write_conn.close()
    st.cache_data.clear()

    conn = sqlite3.connect(live_db)
    try:
        after_orders = conn.execute("SELECT COUNT(*) FROM order_requests").fetchone()[0]
        after_actions = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
        linked_order_id = conn.execute(
            "SELECT order_id FROM action_history WHERE history_id = ?", (history_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    after_version = inventory.current_data_version()

    assert isinstance(order_id, int)
    assert after_orders == before_orders + 1
    assert after_actions == before_actions + 1
    assert linked_order_id == order_id
    # 저장 호출 2회(발주 요청 + 조치 이력) → data_version이 2 증가한다.
    assert after_version == before_version + 2
