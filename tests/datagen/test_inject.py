"""scripts/datagen/inject.py + labels.py 결정적 시나리오 주입기 테스트.

실제 scenario_config.yaml(20건)·마스터 CSV·스키마를 사용해 tmp_path에 주입된 SQLite
스냅샷을 생성하고, 결정성·유형별 주입 규칙·재고 항등식·정상 품목 불변·라벨 포맷·
schema/writer의 action_history.risk_type 계약을 검증한다.

scripts/datagen/은 medsupply 패키지를 참조하지 않는다(격리 원칙) — 이 테스트도
scripts.datagen.* 만 import하고 medsupply는 import하지 않는다(단, schema.sql CHECK
제약을 raw SQL로 검증하는 부분은 파일 경로만 읽으므로 격리 위반이 아니다).
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from scripts.datagen import config as config_mod
from scripts.datagen import inject
from scripts.datagen import labels as labels_mod
from scripts.datagen.baseline import _simulate_item, compute_content_hash, generate_baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_CONFIG_PATH = REPO_ROOT / "data" / "scenarios" / "scenario_config.yaml"
SCHEMA_PATH = REPO_ROOT / "medsupply" / "data" / "schema.sql"
ACTION_HISTORY_SEED_CSV = REPO_ROOT / "data" / "reference" / "action_history_seed.csv"
ITEMS_CSV = REPO_ROOT / "data" / "reference" / "items_master.csv"

SEED_A = 20260801
BASE_DATE = "2026-08-01"

# scenario_config.yaml의 20개 시나리오 item_id와 겹치지 않는 표본(정상 품목 불변 검증용).
NON_SCENARIO_ITEMS = ["ITM-0002", "ITM-0005", "ITM-0006"]
# 시나리오 품목 표본(재고 항등식 검증용) — demand_surge·supply_halt·composite 각 1건.
SAMPLE_SCENARIO_ITEMS = ["ITM-0044", "ITM-0103", "ITM-0001"]

InjectResult = tuple[Path, object, list[dict]]


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def _dump_table(path: Path, table: str, item_id: str | None = None) -> list[tuple]:
    conn = _connect(path)
    try:
        if item_id is not None:
            return conn.execute(
                f"SELECT * FROM {table} WHERE item_id = ? ORDER BY rowid", (item_id,)
            ).fetchall()
        return conn.execute(f"SELECT * FROM {table} ORDER BY item_id, rowid").fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def injected_a(tmp_path_factory: pytest.TempPathFactory) -> InjectResult:
    out = tmp_path_factory.mktemp("inject_a") / "medsupply.db"
    summary, labels = inject.inject_scenarios(out, seed=SEED_A, base_date=BASE_DATE)
    return out, summary, labels


@pytest.fixture(scope="module")
def injected_a_repeat(tmp_path_factory: pytest.TempPathFactory) -> InjectResult:
    out = tmp_path_factory.mktemp("inject_a_repeat") / "medsupply.db"
    summary, labels = inject.inject_scenarios(out, seed=SEED_A, base_date=BASE_DATE)
    return out, summary, labels


@pytest.fixture(scope="module")
def plain_baseline_a(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("plain_baseline_a") / "medsupply.db"
    generate_baseline(out, seed=SEED_A, base_date=BASE_DATE)
    return out


# --- 1. 결정성 ---------------------------------------------------------------


class TestDeterminism:
    def test_same_seed_produces_same_content_hash(
        self, injected_a: InjectResult, injected_a_repeat: InjectResult
    ) -> None:
        _, summary_a, _ = injected_a
        _, summary_b, _ = injected_a_repeat
        assert summary_a.content_hash == summary_b.content_hash

    def test_same_seed_produces_identical_stock_usage_daily(
        self, injected_a: InjectResult, injected_a_repeat: InjectResult
    ) -> None:
        path_a, _, _ = injected_a
        path_b, _, _ = injected_a_repeat
        assert _dump_table(path_a, "stock_usage_daily") == _dump_table(path_b, "stock_usage_daily")

    def test_same_seed_produces_identical_incoming_shipments(
        self, injected_a: InjectResult, injected_a_repeat: InjectResult
    ) -> None:
        path_a, _, _ = injected_a
        path_b, _, _ = injected_a_repeat
        assert _dump_table(path_a, "incoming_shipments") == _dump_table(path_b, "incoming_shipments")

    def test_same_seed_produces_identical_labels(
        self, injected_a: InjectResult, injected_a_repeat: InjectResult
    ) -> None:
        _, _, labels_a = injected_a
        _, _, labels_b = injected_a_repeat
        assert labels_a == labels_b


# --- 2. 유형별 대표 1건씩 -----------------------------------------------------


class TestTypeSpecificInjection:
    """SC-003(demand_surge)·SC-008(supply_halt)·SC-011(delivery_delay)·SC-016(composite)."""

    def test_demand_surge_usage_elevated_in_sustain_window(self, injected_a: InjectResult) -> None:
        """SC-003 ITM-0044: surge_start 2026-07-10, ramp 7일, peak 2.2배, sustain."""
        path, _, _ = injected_a
        conn = _connect(path)
        try:
            early = conn.execute(
                "SELECT AVG(usage_qty) FROM stock_usage_daily"
                " WHERE item_id = 'ITM-0044' AND date < '2026-07-10'"
            ).fetchone()[0]
            late = conn.execute(
                "SELECT AVG(usage_qty) FROM stock_usage_daily WHERE item_id = 'ITM-0044'"
                " AND date BETWEEN '2026-07-20' AND '2026-08-01'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert early is not None and late is not None
        assert late > early * 1.4, f"surge 반영 안됨: early={early}, late={late}"

    def test_supply_halt_leaves_unfulfilled_orders_after_halt(self, injected_a: InjectResult) -> None:
        """SC-008 ITM-0103: halt_start 2026-07-03, expected_restart_date null."""
        path, _, _ = injected_a
        conn = _connect(path)
        try:
            rows = conn.execute(
                "SELECT actual_date FROM incoming_shipments"
                " WHERE item_id = 'ITM-0103' AND expected_date >= '2026-07-03'"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "halt 이후 기대되는 발주가 전혀 생성되지 않았다"
        assert all(r[0] is None for r in rows), "halt 이후 발주는 전부 미이행(actual NULL)이어야 한다"

    def test_delivery_delay_target_order_actual_is_null(self, injected_a: InjectResult) -> None:
        """SC-011 ITM-0026: expected_date 2026-07-27에 실제로 선정된 대상 주문이 미이행.

        "가장 가까운 주문"의 실제 선정 결과는 라벨의 onset_date(inject._resolve_effects가
        확정한 값)로 정확히 조회한다 — 주입 후에는 이후 재고 압박으로 새 발주가 추가로
        생성되어(브리프 의도대로) 원 target보다 지정일에 더 가까운 정상 주문이 나중에
        생길 수 있으므로, "완제품 DB에서 날짜만으로 최근접 재추정"은 신뢰할 수 없다.
        """
        path, _, labels = injected_a
        label = next(lbl for lbl in labels if lbl["params_ref"] == "SC-011")
        assert label["item_id"] == "ITM-0026"

        conn = _connect(path)
        try:
            rows = conn.execute(
                "SELECT actual_date FROM incoming_shipments"
                " WHERE item_id = 'ITM-0026' AND expected_date = ?",
                (label["onset_date"],),
            ).fetchall()
        finally:
            conn.close()
        assert rows, f"onset_date({label['onset_date']})와 expected_date가 일치하는 발주가 없다"
        assert all(r[0] is None for r in rows), "delivery_delay 대상 주문이 이행되었다"

    def test_composite_item_shows_both_demand_and_halt_traces(self, injected_a: InjectResult) -> None:
        """SC-016 ITM-0001: demand_surge(start 06-18, peak 2.4) + supply_halt(start 07-10)."""
        path, _, _ = injected_a
        conn = _connect(path)
        try:
            early = conn.execute(
                "SELECT AVG(usage_qty) FROM stock_usage_daily"
                " WHERE item_id = 'ITM-0001' AND date < '2026-06-18'"
            ).fetchone()[0]
            # ramp 완료(06-18+14일=07-02) 이후·halt 시작(07-10) 이전 구간 — 절삭 영향 없이
            # 순수 demand_surge 신호만 관측.
            late = conn.execute(
                "SELECT AVG(usage_qty) FROM stock_usage_daily WHERE item_id = 'ITM-0001'"
                " AND date BETWEEN '2026-07-05' AND '2026-07-09'"
            ).fetchone()[0]
            halt_rows = conn.execute(
                "SELECT actual_date FROM incoming_shipments"
                " WHERE item_id = 'ITM-0001' AND expected_date >= '2026-07-10'"
            ).fetchall()
        finally:
            conn.close()
        assert early is not None and late is not None
        assert late > early * 1.3, f"composite의 demand_surge 요소 미반영: early={early}, late={late}"
        assert halt_rows, "composite의 supply_halt 요소 미반영(halt 이후 발주 없음)"
        assert all(r[0] is None for r in halt_rows)


# --- 3. 재고 항등식 -----------------------------------------------------------


class TestStockIdentity:
    def test_stock_identity_holds_for_sample_scenario_items(self, injected_a: InjectResult) -> None:
        path, _, _ = injected_a
        conn = _connect(path)
        try:
            for item_id in SAMPLE_SCENARIO_ITEMS:
                rows = conn.execute(
                    "SELECT date, usage_qty, incoming_qty, closing_stock"
                    " FROM stock_usage_daily WHERE item_id = ? ORDER BY date",
                    (item_id,),
                ).fetchall()
                assert len(rows) >= 360, item_id

                for row in rows:
                    assert row[3] >= 0, f"{item_id} {row[0]}: 재고 음수"

                for i in range(1, len(rows)):
                    prev_closing = rows[i - 1][3]
                    _, usage, incoming, closing = rows[i]
                    assert prev_closing - usage + incoming == closing, f"{item_id} {rows[i][0]}"
        finally:
            conn.close()


# --- 4. 정상 품목 불변 --------------------------------------------------------


class TestNonScenarioItemsUnchanged:
    def test_non_scenario_items_identical_to_plain_baseline(
        self, injected_a: InjectResult, plain_baseline_a: Path
    ) -> None:
        injected_path, _, _ = injected_a
        for item_id in NON_SCENARIO_ITEMS:
            assert _dump_table(injected_path, "stock_usage_daily", item_id) == _dump_table(
                plain_baseline_a, "stock_usage_daily", item_id
            ), item_id
            assert _dump_table(injected_path, "incoming_shipments", item_id) == _dump_table(
                plain_baseline_a, "incoming_shipments", item_id
            ), item_id


# --- 5. 라벨 -------------------------------------------------------------------


class TestLabels:
    _REQUIRED_FIELDS = {
        "item_id", "scenario_type", "onset_date", "stockout_date", "params_ref", "stockout_basis",
    }

    def test_twenty_labels_present_with_complete_fields(self, injected_a: InjectResult) -> None:
        _, _, labels = injected_a
        assert len(labels) == 20
        for label in labels:
            assert self._REQUIRED_FIELDS <= label.keys(), label
            assert label["stockout_basis"] in {"observed", "extrapolated"}, label
            assert label["onset_date"] < label["stockout_date"], label

    def test_label_item_ids_match_scenario_config(self, injected_a: InjectResult) -> None:
        _, _, labels = injected_a
        cfg = config_mod.load_scenario_config(SCENARIO_CONFIG_PATH)
        expected_item_ids = {sc.item_id for sc in cfg.scenarios}
        assert {lbl["item_id"] for lbl in labels} == expected_item_ids

    def test_label_params_ref_matches_scenario_id(self, injected_a: InjectResult) -> None:
        _, _, labels = injected_a
        cfg = config_mod.load_scenario_config(SCENARIO_CONFIG_PATH)
        by_item = {sc.item_id: sc.scenario_id for sc in cfg.scenarios}
        for label in labels:
            assert label["params_ref"] == by_item[label["item_id"]]


# --- 6. 핸드오프 방어 회귀(expected_qty NULL 금지) ----------------------------


class TestExpectedQtyNeverNull:
    def test_expected_qty_always_filled_for_scenario_items(self, injected_a: InjectResult) -> None:
        path, _, labels = injected_a
        conn = _connect(path)
        try:
            for item_id in {lbl["item_id"] for lbl in labels}:
                rows = conn.execute(
                    "SELECT expected_qty FROM incoming_shipments WHERE item_id = ?", (item_id,)
                ).fetchall()
                assert rows, item_id
                assert all(r[0] is not None and r[0] > 0 for r in rows), item_id
        finally:
            conn.close()


# --- 7. expected_restart_date 검증(halt_start_date 이후여야 함) --------------


class TestHaltRestartValidation:
    def test_rejects_restart_before_halt(self) -> None:
        with pytest.raises(ValueError):
            inject._validate_halt_restart("SC-TEST", date(2026, 7, 10), date(2026, 7, 1))

    def test_rejects_restart_equal_to_halt(self) -> None:
        with pytest.raises(ValueError):
            inject._validate_halt_restart("SC-TEST", date(2026, 7, 10), date(2026, 7, 10))

    def test_accepts_restart_after_halt(self) -> None:
        inject._validate_halt_restart("SC-TEST", date(2026, 7, 10), date(2026, 8, 1))

    def test_accepts_none_restart(self) -> None:
        inject._validate_halt_restart("SC-TEST", date(2026, 7, 10), None)


# --- 8. schema 변경: action_history.risk_type CHECK 제약 ---------------------


class TestActionHistoryRiskTypeSchema:
    def _conn_with_schema(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO items(item_id, item_name) VALUES ('ITEM-TEST', '테스트품목')")
        conn.commit()
        return conn

    def test_accepts_contract_risk_type(self) -> None:
        conn = self._conn_with_schema()
        conn.execute(
            "INSERT INTO action_history(item_id, action_type, risk_type) VALUES (?, ?, ?)",
            ("ITEM-TEST", "대체 검토", "supply_halt"),
        )
        conn.commit()
        assert conn.execute("SELECT risk_type FROM action_history").fetchone()[0] == "supply_halt"

    def test_accepts_null_risk_type(self) -> None:
        conn = self._conn_with_schema()
        conn.execute(
            "INSERT INTO action_history(item_id, action_type) VALUES (?, ?)",
            ("ITEM-TEST", "대체 검토"),
        )
        conn.commit()
        assert conn.execute("SELECT risk_type FROM action_history").fetchone()[0] is None

    def test_rejects_unknown_risk_type(self) -> None:
        conn = self._conn_with_schema()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO action_history(item_id, action_type, risk_type) VALUES (?, ?, ?)",
                ("ITEM-TEST", "대체 검토", "not_a_type"),
            )


# --- 9. load_action_history_seed ----------------------------------------------


@pytest.fixture()
def action_history_conn(tmp_path: Path) -> sqlite3.Connection:
    out = tmp_path / "medsupply.db"
    generate_baseline(out, seed=SEED_A, base_date=BASE_DATE)
    conn = sqlite3.connect(out)
    yield conn
    conn.close()


class TestLoadActionHistorySeed:
    def test_loads_eight_rows(self, action_history_conn: sqlite3.Connection) -> None:
        count = inject.load_action_history_seed(action_history_conn, ACTION_HISTORY_SEED_CSV)
        assert count == 8
        assert (
            action_history_conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0] == 8
        )

    def test_fields_match_csv(self, action_history_conn: sqlite3.Connection) -> None:
        inject.load_action_history_seed(action_history_conn, ACTION_HISTORY_SEED_CSV)

        with ACTION_HISTORY_SEED_CSV.open("r", encoding="utf-8", newline="") as f:
            csv_rows = list(csv.DictReader(f))

        db_rows = action_history_conn.execute(
            "SELECT item_id, action_type, owner, note, status, risk_grade_before,"
            " risk_grade_after, result_note, created_at, risk_type"
            " FROM action_history ORDER BY history_id"
        ).fetchall()

        assert len(db_rows) == len(csv_rows) == 8
        for csv_row, db_row in zip(csv_rows, db_rows):
            assert db_row == (
                csv_row["item_id"],
                csv_row["action_type"],
                csv_row["owner"],
                csv_row["note"],
                csv_row["status"],
                csv_row["risk_grade_before"],
                csv_row["risk_grade_after"],
                csv_row["result_note"],
                csv_row["created_at"],
                csv_row["risk_type"],
            )


# --- 10. 픽스 라운드 1(1주차 리뷰 F1~F4·F7·F8) 회귀 --------------------------


class TestContentHashConsistency:
    """F2: config_hash를 content_hash보다 먼저 확정해야 완성 DB에서 재계산한 값과 일치한다."""

    def test_stored_content_hash_matches_recomputed(self, injected_a: InjectResult) -> None:
        path, summary, _ = injected_a
        conn = _connect(path)
        try:
            recomputed = compute_content_hash(conn)
            stored = conn.execute("SELECT value FROM meta WHERE key = 'content_hash'").fetchone()[0]
        finally:
            conn.close()
        assert recomputed == stored
        assert recomputed == summary.content_hash


class TestDeliveryDelayForcedOrder:
    """F4: 지정 expected_date ±7일 내 자연 발주가 없으면 그 날짜에 신규 발주를 강제
    생성해 미이행 대상으로 삼는다."""

    def test_forced_order_matches_specified_date_exactly(self, injected_a: InjectResult) -> None:
        """SC-012 ITM-0068: 자연 발주 리듬상 가장 가까운 후보가 16일 떨어져 있었다
        (2주차 픽스 이전) — 이제는 지정일(2026-07-24)에 정확히 강제 생성된다."""
        path, _, labels = injected_a
        label = next(lbl for lbl in labels if lbl["params_ref"] == "SC-012")
        assert label["item_id"] == "ITM-0068"
        assert label["onset_date"] == "2026-07-24"

        conn = _connect(path)
        try:
            rows = conn.execute(
                "SELECT expected_qty, actual_date FROM incoming_shipments"
                " WHERE item_id = 'ITM-0068' AND expected_date = '2026-07-24'"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "강제 생성된 발주가 DB에 없다"
        assert all(qty is not None and qty > 0 and actual is None for qty, actual in rows)

    def test_all_delivery_delay_targets_within_tolerance_or_forced_exact(
        self, injected_a: InjectResult
    ) -> None:
        """delivery_delay를 포함하는 7건(SC-011~015·018·020) 각각에서, 실제 대상이 된
        발주(자연 매칭 또는 강제 생성)의 expected_date가 지정값과 7일 이내이거나(자연
        매칭) 강제 생성으로 정확히 일치해야 한다.

        composite(SC-018·SC-020)의 라벨 onset_date는 하위 요소 전체의 최솟값이라(halt가
        delivery_delay보다 이를 수 있음) delivery_delay 자체의 이탈 여부를 가리지 못한다
        — 그래서 라벨이 아니라 _resolve_effects를 다시 호출해 delivery_delay 하위 요소의
        해석 결과를 직접 확인한다.
        """
        path, _, _ = injected_a
        cfg = config_mod.load_scenario_config(SCENARIO_CONFIG_PATH)
        base_date_obj = date.fromisoformat(BASE_DATE)
        timeline_start = base_date_obj - timedelta(days=364)
        days = [timeline_start + timedelta(days=i) for i in range(365)]

        conn = _connect(path)
        try:
            checked = 0
            for sc in cfg.scenarios:
                subs = sc.params["sub_scenarios"] if sc.type == "composite" else [
                    {"type": sc.type, "params": sc.params}
                ]
                delay_subs = [s for s in subs if s["type"] == "delivery_delay"]
                if not delay_subs:
                    continue
                checked += 1

                pack_size, supplier, atc_code, form = conn.execute(
                    "SELECT pack_size, supplier, atc_code, form FROM items WHERE item_id = ?",
                    (sc.item_id,),
                ).fetchone()
                item_row = {
                    "item_id": sc.item_id, "pack_size": str(pack_size),
                    "supplier": supplier, "atc_code": atc_code, "form": form,
                }
                _de, _he, delay_effects, forced_rows, _onset = inject._resolve_effects(
                    sc, item_row, SEED_A, days
                )
                target = date.fromisoformat(delay_subs[0]["params"]["expected_date"])

                if delay_effects:
                    offset = abs((delay_effects[-1].expected_date - target).days)
                    assert offset <= inject.DELIVERY_DELAY_MAX_OFFSET_DAYS, sc.scenario_id
                else:
                    assert forced_rows, sc.scenario_id
                    forced_expected = date.fromisoformat(str(forced_rows[-1]["expected_date"]))
                    assert forced_expected == target, sc.scenario_id

            assert checked == 7
        finally:
            conn.close()


class TestEffectEffectivenessAssertion:
    """F1: 각 효과(halt/delay/demand_surge)가 최소 1건의 행 변화를 만들지 못하면
    _assert_effects_effective가 명확한 에러로 실패해야 한다."""

    _ITEM = {
        "item_id": "ITEM-X", "pack_size": "10", "supplier": "테스트공급사",
        "atc_code": "A00AA00", "form": "정제",
    }

    def test_raises_when_halt_never_blocks_anything(self) -> None:
        trace = inject.ScenarioTrace(
            halt_blocked_order_dates=frozenset(), delay_blocked_order_dates=frozenset()
        )
        halt_effects = (inject.HaltEffect(start=date(2026, 7, 1)),)
        with pytest.raises(ValueError, match="무효과"):
            inject._assert_effects_effective(
                "SC-TEST", self._ITEM, SEED_A, [date(2026, 7, 1)],
                (), halt_effects, (), [], [], trace,
            )

    def test_passes_when_halt_has_at_least_one_blocked_order(self) -> None:
        trace = inject.ScenarioTrace(
            halt_blocked_order_dates=frozenset({"2026-07-05"}), delay_blocked_order_dates=frozenset()
        )
        halt_effects = (inject.HaltEffect(start=date(2026, 7, 1)),)
        inject._assert_effects_effective(
            "SC-TEST", self._ITEM, SEED_A, [date(2026, 7, 1)],
            (), halt_effects, (), [], [], trace,
        )  # 예외 없이 통과해야 한다

    def test_all_twenty_scenarios_pass_effectiveness_check(self, injected_a: InjectResult) -> None:
        """injected_a 픽스처가 예외 없이 생성됐다는 사실 자체가 20건 전부 통과했다는
        증거이지만, 의도를 명시적으로 남겨 회귀를 방지한다."""
        _, summary, labels = injected_a
        assert len(labels) == 20
        assert summary.truncation_count >= 0  # 생성 자체가 성공했다는 것이 핵심 단언


class TestExtrapolationCreditsNonBlockedIncoming:
    """F3: 외삽은 시나리오가 실제로 막지 않은 미이행 발주를 remaining_stock에 가산해야
    한다(그래야 demand_surge 단독 시나리오의 외삽이 부당하게 비관적으로 나오지 않는다)."""

    def _seed_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE stock_usage_daily (item_id TEXT, date TEXT, usage_qty INTEGER,"
            " incoming_qty INTEGER, closing_stock INTEGER)"
        )
        conn.execute(
            "CREATE TABLE incoming_shipments (item_id TEXT, order_date TEXT, expected_date TEXT,"
            " expected_qty INTEGER, actual_date TEXT, actual_qty INTEGER, status TEXT)"
        )
        stock = 300
        base = date(2026, 7, 1)
        rows = []
        for i in range(28):
            d = base + timedelta(days=i)
            stock -= 10
            rows.append(("ITEM-X", d.isoformat(), 10, 0, stock))
        conn.executemany("INSERT INTO stock_usage_daily VALUES (?, ?, ?, ?, ?)", rows)
        # 미이행 발주 1건(아직 도착 전, actual NULL) — 시나리오가 막은 것이 아니라
        # 그냥 만기가 안 된 정상 발주라고 가정한다.
        conn.execute(
            "INSERT INTO incoming_shipments VALUES"
            " ('ITEM-X', '2026-07-25', '2026-08-05', 200, NULL, NULL, '입고 예정')"
        )
        conn.commit()
        return conn

    def test_non_blocked_pending_shipment_extends_runway(self) -> None:
        conn = self._seed_conn()
        base_date = date(2026, 7, 28)

        without_credit, basis1 = labels_mod._stockout_date(
            conn, "ITEM-X", base_date, frozenset({"2026-07-25"})
        )
        with_credit, basis2 = labels_mod._stockout_date(conn, "ITEM-X", base_date, frozenset())

        assert basis1 == "extrapolated"
        assert basis2 == "extrapolated"
        assert with_credit > without_credit

    def test_default_blocked_set_is_empty_credits_everything(self) -> None:
        conn = self._seed_conn()
        base_date = date(2026, 7, 28)

        default_call, _ = labels_mod._stockout_date(conn, "ITEM-X", base_date)
        explicit_empty, _ = labels_mod._stockout_date(conn, "ITEM-X", base_date, frozenset())

        assert default_call == explicit_empty


class TestNoEffectEquivalence:
    """F8: 효과가 전혀 없으면 simulate_item_with_scenario의 앞 3개 반환값(stock_rows·
    shipment_rows·truncation_count)은 baseline._simulate_item과 바이트 동일해야 한다."""

    def test_matches_baseline_simulate_item_for_sample_items(self) -> None:
        base_date_obj = date.fromisoformat(BASE_DATE)
        timeline_start = base_date_obj - timedelta(days=364)
        days = [timeline_start + timedelta(days=i) for i in range(365)]

        with ITEMS_CSV.open("r", encoding="utf-8", newline="") as f:
            items_by_id = {row["item_id"]: row for row in csv.DictReader(f)}

        for item_id in NON_SCENARIO_ITEMS:
            item = items_by_id[item_id]
            base_stock, base_ship, base_trunc = _simulate_item(item, SEED_A, days)
            scen_stock, scen_ship, scen_trunc, trace = inject.simulate_item_with_scenario(
                item, SEED_A, days
            )
            assert scen_stock == base_stock, item_id
            assert scen_ship == base_ship, item_id
            assert scen_trunc == base_trunc, item_id
            assert trace.halt_blocked_order_dates == frozenset()
            assert trace.delay_blocked_order_dates == frozenset()


# --- 11. S-22: delivery_delay 지연 '도착' arm(arrives_late) ------------------


def _synthetic_days() -> list[date]:
    base = date.fromisoformat(BASE_DATE)
    start = base - timedelta(days=364)
    return [start + timedelta(days=i) for i in range(365)]


class TestDelayEffectArrivesLate:
    """2주차 리뷰 F6 이월: 표준 스냅샷은 delivery_delay가 전부 영구 미이행(actual_date가
    끝까지 NULL)으로만 남아, receipt_delay as_of 수정(S-17d)을 '늦게라도 도착한' 실데이터로
    검증한 적이 없다. arrives_late=True는 release_day(=expected_date+delay_days)에 실제로
    입고되어 expected_date < actual_date인 행을 만든다 — 기본값 False는 기존 동작(영구
    미이행) 그대로다."""

    _ITEM = {
        "item_id": "ITEM-X", "pack_size": "10", "supplier": "테스트공급사",
        "atc_code": "A00AA00", "form": "정제",
    }

    def _natural_first_order(self) -> tuple[date, date]:
        _stock, shipments, _trunc, _trace = inject.simulate_item_with_scenario(
            self._ITEM, SEED_A, _synthetic_days()
        )
        assert shipments, "테스트 품목에 자연 발주가 없다(픽스처 재검토 필요)"
        first = shipments[0]
        return date.fromisoformat(str(first["order_date"])), date.fromisoformat(
            str(first["expected_date"])
        )

    def test_arrives_late_default_false_stays_permanently_unfulfilled(self) -> None:
        """회귀: arrives_late 생략(기본 False)은 기존과 동일하게 영구 미이행이어야 한다."""
        order_date, expected_date = self._natural_first_order()
        delay_eff = inject.DelayEffect(
            order_date=order_date, expected_date=expected_date, delay_days=5, qty_ratio=None,
        )
        assert delay_eff.arrives_late is False

        _stock, shipments, _trunc, trace = inject.simulate_item_with_scenario(
            self._ITEM, SEED_A, _synthetic_days(), delay_effects=(delay_eff,)
        )
        row = next(r for r in shipments if r["order_date"] == order_date.isoformat())
        assert row["actual_date"] is None
        assert row["actual_qty"] is None
        assert order_date.isoformat() in trace.delay_blocked_order_dates

    def test_arrives_late_true_records_actual_date_after_expected(self) -> None:
        order_date, expected_date = self._natural_first_order()
        delay_days = 5
        delay_eff = inject.DelayEffect(
            order_date=order_date, expected_date=expected_date, delay_days=delay_days,
            qty_ratio=None, arrives_late=True,
        )

        _stock, shipments, _trunc, trace = inject.simulate_item_with_scenario(
            self._ITEM, SEED_A, _synthetic_days(), delay_effects=(delay_eff,)
        )
        row = next(r for r in shipments if r["order_date"] == order_date.isoformat())
        assert row["actual_date"] is not None
        actual = date.fromisoformat(str(row["actual_date"]))
        assert actual == expected_date + timedelta(days=delay_days)
        assert actual > expected_date
        assert row["actual_qty"] == row["expected_qty"]
        assert row["status"] == "입고 완료"
        # 자연 도착 판정에도 쓰이는 delay_blocked_order_dates는 여전히 채워진다(도착 여부와
        # 무관하게 "시나리오가 겨냥한 발주"라는 사실 자체는 변하지 않는다).
        assert order_date.isoformat() in trace.delay_blocked_order_dates

    def test_arrives_late_true_stock_reflects_the_late_incoming(self) -> None:
        """자연 도착 arm은 day-loop를 그대로 타므로 재고 궤적에도 반영돼야 한다.

        (a) 원래 예정일(expected_date)에는 아직 도착하지 않아 재고가 무효과(without) 대비
        낮게 유지되고(막힌 구간의 직접 증거), (b) 실제 도착일(arrival)에는 지연분 전량이
        incoming_qty에 반영된다. closing_stock을 arrival 시점에서 바로 비교하지 않는 이유:
        절삭(truncation)이 전혀 없는 구간이면 "언제 credit되든 구간 끝 재고는 산술적으로
        동일"해질 수 있어(합산 순서 무관) 신뢰할 수 없는 비교다 — 막힌 구간 '내부'에서
        실제로 재고가 낮아짐을 직접 확인하는 쪽이 항상 성립하는 불변식이다.
        """
        order_date, expected_date = self._natural_first_order()
        delay_days = 5
        arrival = expected_date + timedelta(days=delay_days)
        delay_eff = inject.DelayEffect(
            order_date=order_date, expected_date=expected_date, delay_days=delay_days,
            qty_ratio=None, arrives_late=True,
        )
        stock_with, shipments, _trunc, _trace = inject.simulate_item_with_scenario(
            self._ITEM, SEED_A, _synthetic_days(), delay_effects=(delay_eff,)
        )
        stock_without, _s, _t, _tr = inject.simulate_item_with_scenario(
            self._ITEM, SEED_A, _synthetic_days()
        )
        by_date_with = {r["date"]: r for r in stock_with}
        by_date_without = {r["date"]: r for r in stock_without}
        delayed_row = next(r for r in shipments if r["order_date"] == order_date.isoformat())

        # (a) 원래 예정일: without은 이미 입고돼 재고가 뛰지만, with는 아직 막혀 있다.
        assert by_date_without[expected_date.isoformat()]["incoming_qty"] > 0
        assert by_date_with[expected_date.isoformat()]["incoming_qty"] == 0
        assert (
            by_date_with[expected_date.isoformat()]["closing_stock"]
            < by_date_without[expected_date.isoformat()]["closing_stock"]
        )

        # (b) 실제 도착일: 지연분 전량이 incoming_qty로 credit된다.
        arrival_row = by_date_with[arrival.isoformat()]
        assert arrival_row["incoming_qty"] == delayed_row["actual_qty"]


class TestDeliveryDelayForcedArrivesLate:
    """강제 생성 arm(자연 발주 후보가 ±7일 밖 — F4)도 arrives_late를 지원해야, 어느 arm이
    뽑히든 지연 도착 arm이 안정적으로 재현 가능하다."""

    _ITEM_ROW = {
        "item_id": "ITEM-Y", "pack_size": "10", "supplier": "테스트공급사2",
        "atc_code": "A00AA00", "form": "정제",
    }

    def test_forced_order_arrives_late_when_flagged(self) -> None:
        days = _synthetic_days()
        timeline_start = days[0]
        target_expected = timeline_start + timedelta(days=5)
        delay_days = 6

        sc = config_mod.Scenario(
            scenario_id="SC-TEST-FORCED",
            item_id=self._ITEM_ROW["item_id"],
            type="delivery_delay",
            reference="테스트",
            params={
                "expected_date": target_expected.isoformat(),
                "delay_days": delay_days,
                "qty_ratio": 1.0,
                "arrives_late": True,
            },
        )
        demand_effects, halt_effects, delay_effects, forced_rows, onset = inject._resolve_effects(
            sc, self._ITEM_ROW, SEED_A, days
        )
        assert not demand_effects and not halt_effects
        assert not delay_effects, "이 케이스는 강제 생성 arm이어야 한다(자연 발주가 없어야 함)"
        assert len(forced_rows) == 1
        row = forced_rows[0]
        assert row["actual_date"] == (target_expected + timedelta(days=delay_days)).isoformat()
        assert row["actual_qty"] == row["expected_qty"]
        assert row["status"] == "입고 완료"
        assert onset == target_expected

    def test_forced_order_without_flag_stays_unfulfilled(self) -> None:
        """회귀: arrives_late를 안 주면 강제 생성 arm은 기존처럼 영구 미이행이다."""
        days = _synthetic_days()
        timeline_start = days[0]
        target_expected = timeline_start + timedelta(days=5)

        sc = config_mod.Scenario(
            scenario_id="SC-TEST-FORCED-2",
            item_id=self._ITEM_ROW["item_id"],
            type="delivery_delay",
            reference="테스트",
            params={"expected_date": target_expected.isoformat(), "delay_days": 6, "qty_ratio": 1.0},
        )
        _de, _he, delay_effects, forced_rows, _onset = inject._resolve_effects(
            sc, self._ITEM_ROW, SEED_A, days
        )
        assert not delay_effects
        assert len(forced_rows) == 1
        assert forced_rows[0]["actual_date"] is None
        assert forced_rows[0]["status"] == "입고 예정"


# --- 12. S-22: min_scenarios_per_type 매개변수화(플러밍) ---------------------


class TestMinScenariosPerTypeOverride:
    """블라인드 생성기는 유형당 시나리오 1개(표준 최소 4개 미만)로 inject_scenarios
    전체 오케스트레이션(베이스라인+주입+해시+라벨)을 그대로 재사용해야 한다. 재사용하는
    (품목, 파라미터) 4쌍은 표준 config의 SC-003·SC-008·SC-011·SC-016과 동일하다 — 이미
    '실제로 효과가 있다'고 검증된 조합이며, 품목별 시뮬레이션은 서로 독립적이라 20건 중
    4건만 추려도 동일하게 유효하다."""

    def _small_config_yaml(self, tmp_path: Path) -> Path:
        content = {
            "version": 1,
            "base_date": BASE_DATE,
            "timeline_start": "2025-08-02",
            "scenarios": [
                {
                    "scenario_id": "TEST-DS", "item_id": "ITM-0044", "type": "demand_surge",
                    "reference": "테스트(SC-003 재사용)",
                    "params": {
                        "surge_start_date": "2026-07-10", "ramp_days": 7,
                        "peak_multiplier": 2.2, "sustain": True,
                    },
                },
                {
                    "scenario_id": "TEST-SH", "item_id": "ITM-0103", "type": "supply_halt",
                    "reference": "테스트(SC-008 재사용)",
                    "params": {"halt_start_date": "2026-07-03", "expected_restart_date": None},
                },
                {
                    "scenario_id": "TEST-DD", "item_id": "ITM-0026", "type": "delivery_delay",
                    "reference": "테스트(SC-011 재사용)",
                    "params": {"expected_date": "2026-07-27", "delay_days": 5, "qty_ratio": 1.0},
                },
                {
                    "scenario_id": "TEST-CP", "item_id": "ITM-0001", "type": "composite",
                    "reference": "테스트(SC-016 재사용)",
                    "params": {
                        "sub_scenarios": [
                            {
                                "type": "demand_surge",
                                "params": {
                                    "surge_start_date": "2026-06-18", "ramp_days": 14,
                                    "peak_multiplier": 2.4, "sustain": True,
                                },
                            },
                            {
                                "type": "supply_halt",
                                "params": {
                                    "halt_start_date": "2026-07-10",
                                    "expected_restart_date": "2026-09-15",
                                },
                            },
                        ],
                    },
                },
            ],
        }
        path = tmp_path / "small_scenario_config.yaml"
        path.write_text(
            yaml.safe_dump(content, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return path

    def test_default_rejects_one_per_type_config(self, tmp_path: Path) -> None:
        config_path = self._small_config_yaml(tmp_path)
        out = tmp_path / "rejected.db"
        with pytest.raises(ValueError, match="4개 미만"):
            inject.inject_scenarios(
                out, seed=SEED_A, base_date=BASE_DATE, scenario_config_path=config_path
            )

    def test_min_scenarios_per_type_one_accepts_and_produces_four_labels(
        self, tmp_path: Path
    ) -> None:
        config_path = self._small_config_yaml(tmp_path)
        out = tmp_path / "accepted.db"
        summary, labels = inject.inject_scenarios(
            out, seed=SEED_A, base_date=BASE_DATE,
            scenario_config_path=config_path, min_scenarios_per_type=1,
        )
        assert len(labels) == 4
        assert {lbl["scenario_type"] for lbl in labels} == {
            "demand_surge", "supply_halt", "delivery_delay", "composite",
        }
        assert out.exists()
        assert summary.content_hash
