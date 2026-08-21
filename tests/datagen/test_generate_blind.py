"""scripts/datagen/blind.py + scripts/generate_blind.py 블라인드 스냅샷 생성기 테스트(Task S-22).

data/scenarios/blind_ranges.yaml에서 결정적으로 뽑은 파라미터(유형당 시나리오 1개)로
표준 생성 경로(scripts.datagen.baseline·inject·labels·config)를 재사용해 블라인드 스냅샷을
만드는 scripts/datagen/blind.py를 검증한다. 검증 축(브리프 그대로):
- 동일 시드 2회 결정성(db content_hash·라벨 동일)
- 다른 시드 상이
- 범위 준수(뽑힌 값이 YAML 구간 안)
- 지연 도착 arm 실재(생성 DB에 expected<actual 행 존재)
- manifest 스키마·해시 일치(결정적 정렬·upsert)
- out DB에 라벨·시나리오 흔적 없음(validate_dataset 전체 통과)

scripts/datagen/은 medsupply 패키지를 참조하지 않는다(격리 원칙) — 이 테스트도
scripts.datagen.*·scripts.validate_dataset·scripts.generate_blind만 import한다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts import generate_blind as blind_cli
from scripts import validate_dataset as vd
from scripts.datagen import blind
from scripts.datagen.config import ALLOWED_TYPES, validate_scenario_config

REPO_ROOT = Path(__file__).resolve().parents[2]
RANGES_PATH = REPO_ROOT / "data" / "scenarios" / "blind_ranges.yaml"
ITEMS_CSV = REPO_ROOT / "data" / "reference" / "items_master.csv"
BASE_DATE = "2026-08-01"
BASE_DATE_OBJ = date.fromisoformat(BASE_DATE)
TIMELINE_START_OBJ = BASE_DATE_OBJ - timedelta(days=364)

SEED_A = 90001
SEED_B = 90002

_ANCHOR_KEY = {
    "demand_surge": "surge_start_date",
    "supply_halt": "halt_start_date",
    "delivery_delay": "expected_date",
}


@pytest.fixture(scope="module")
def ranges() -> dict:
    return blind.load_ranges(RANGES_PATH)


def _flatten_scenarios(cfg) -> list[tuple[str, dict]]:
    """(type, params) 평탄화 — composite는 sub_scenarios 각각을 별도 원소로 편다."""
    flat: list[tuple[str, dict]] = []
    for sc in cfg.scenarios:
        if sc.type == "composite":
            for sub in sc.params["sub_scenarios"]:
                flat.append((sub["type"], sub["params"]))
        else:
            flat.append((sc.type, sc.params))
    return flat


# --- 1. build_blind_config: 결정성 · 배분 · 범위 준수 -------------------------


class TestBuildBlindConfigStructure:
    def test_four_scenarios_one_per_type_distinct_items(self, ranges: dict) -> None:
        cfg = blind.build_blind_config(
            ranges, seed=SEED_A, base_date=BASE_DATE_OBJ, items_csv=ITEMS_CSV
        )
        assert len(cfg.scenarios) == 4
        assert {sc.type for sc in cfg.scenarios} == set(ALLOWED_TYPES)
        item_ids = [sc.item_id for sc in cfg.scenarios]
        assert len(item_ids) == len(set(item_ids)) == 4

    def test_passes_validate_scenario_config_with_min_per_type_one(self, ranges: dict) -> None:
        for seed in range(10):
            cfg = blind.build_blind_config(
                ranges, seed=seed, base_date=BASE_DATE_OBJ, items_csv=ITEMS_CSV
            )
            violations = validate_scenario_config(cfg, items_csv=ITEMS_CSV, min_per_type=1)
            assert violations == [], (seed, violations)

    def test_same_seed_is_deterministic(self, ranges: dict) -> None:
        cfg1 = blind.build_blind_config(
            ranges, seed=SEED_A, base_date=BASE_DATE_OBJ, items_csv=ITEMS_CSV
        )
        cfg2 = blind.build_blind_config(
            ranges, seed=SEED_A, base_date=BASE_DATE_OBJ, items_csv=ITEMS_CSV
        )
        assert cfg1 == cfg2

    def test_different_seed_differs(self, ranges: dict) -> None:
        cfg1 = blind.build_blind_config(
            ranges, seed=SEED_A, base_date=BASE_DATE_OBJ, items_csv=ITEMS_CSV
        )
        cfg2 = blind.build_blind_config(
            ranges, seed=SEED_B, base_date=BASE_DATE_OBJ, items_csv=ITEMS_CSV
        )
        assert cfg1 != cfg2

    def test_composite_sub_scenarios_use_an_allowed_pair(self, ranges: dict) -> None:
        allowed = [tuple(p) for p in ranges["compound"]["allowed_pairs"]]
        for seed in range(20):
            cfg = blind.build_blind_config(
                ranges, seed=seed, base_date=BASE_DATE_OBJ, items_csv=ITEMS_CSV
            )
            composite = next(sc for sc in cfg.scenarios if sc.type == "composite")
            sub_types = tuple(sub["type"] for sub in composite.params["sub_scenarios"])
            assert sub_types in allowed, (seed, sub_types)

    def test_composite_delivery_delay_sub_never_gets_arrives_late(self, ranges: dict) -> None:
        """구현 범위 제한(halt·delay 상호작용 회피) — arrives_late는 standalone
        delivery_delay에서만 뽑힌다."""
        for seed in range(20):
            cfg = blind.build_blind_config(
                ranges, seed=seed, base_date=BASE_DATE_OBJ, items_csv=ITEMS_CSV
            )
            composite = next(sc for sc in cfg.scenarios if sc.type == "composite")
            for sub in composite.params["sub_scenarios"]:
                if sub["type"] == "delivery_delay":
                    assert "arrives_late" not in sub["params"]


class TestBuildBlindConfigRangeCompliance:
    def test_drawn_values_within_ranges_across_many_seeds(self, ranges: dict) -> None:
        offset_r = ranges["scenario_start_offset_days_range"]
        ds_r = ranges["demand_surge"]
        sh_r = ranges["supply_halt"]
        dd_r = ranges["delivery_delay"]

        for seed in range(60):
            cfg = blind.build_blind_config(
                ranges, seed=seed, base_date=BASE_DATE_OBJ, items_csv=ITEMS_CSV
            )
            for type_, params in _flatten_scenarios(cfg):
                anchor = date.fromisoformat(str(params[_ANCHOR_KEY[type_]]))
                assert TIMELINE_START_OBJ <= anchor <= BASE_DATE_OBJ, (seed, type_, anchor)
                offset = (anchor - BASE_DATE_OBJ).days
                assert offset_r["min"] <= offset <= offset_r["max"], (seed, type_, offset)

                if type_ == "demand_surge":
                    assert (
                        ds_r["ramp_days_range"]["min"]
                        <= params["ramp_days"]
                        <= ds_r["ramp_days_range"]["max"]
                    )
                    assert (
                        ds_r["peak_multiplier_range"]["min"]
                        <= params["peak_multiplier"]
                        <= ds_r["peak_multiplier_range"]["max"]
                    )
                    assert params["sustain"] is True
                elif type_ == "supply_halt":
                    assert "expected_restart_date" in params
                    if params["expected_restart_date"] is not None:
                        restart = date.fromisoformat(str(params["expected_restart_date"]))
                        duration = (restart - anchor).days
                        assert (
                            sh_r["halt_duration_days_range"]["min"]
                            <= duration
                            <= sh_r["halt_duration_days_range"]["max"]
                        ), (seed, duration)
                    if "demand_shift_multiplier" in params:
                        assert (
                            sh_r["demand_shift_multiplier_range"]["min"]
                            <= params["demand_shift_multiplier"]
                            <= sh_r["demand_shift_multiplier_range"]["max"]
                        )
                elif type_ == "delivery_delay":
                    assert (
                        dd_r["delay_days_range"]["min"]
                        <= params["delay_days"]
                        <= dd_r["delay_days_range"]["max"]
                    )
                    assert (
                        dd_r["qty_ratio_range"]["min"]
                        <= params["qty_ratio"]
                        <= dd_r["qty_ratio_range"]["max"]
                    )
                    # F8(S-22 픽스 라운드 1, 컨트롤러 리뷰): 예전 단언
                    # `params.get("arrives_late", False) in (True, False)`는 .get()의
                    # 반환값이 사실상 언제나 bool이라 공허했다(범위 준수를 검증하지
                    # 못함) — 실제 구성 로직의 불변식(키가 있으면 항상 정확히 True,
                    # 결코 명시적 False가 아니다)을 검증하도록 바꿨다.
                    assert "arrives_late" not in params or params["arrives_late"] is True


# --- 2. generate_blind 오케스트레이션: 결정성 -------------------------------


@pytest.fixture()
def blind_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "out": tmp_path / "run" / "blind_x.db",
        "sealed_dir": tmp_path / "run" / "sealed",
        "manifest_path": tmp_path / "run" / "manifest.json",
    }


class TestGenerateBlindDeterminism:
    def test_same_seed_twice_same_content_hash_and_labels(self, tmp_path: Path) -> None:
        out1 = tmp_path / "r1" / "blind_x.db"
        out2 = tmp_path / "r2" / "blind_x.db"
        result1 = blind.generate_blind(
            RANGES_PATH, SEED_A, BASE_DATE, out1,
            sealed_dir=tmp_path / "r1" / "sealed", manifest_path=tmp_path / "r1" / "manifest.json",
        )
        result2 = blind.generate_blind(
            RANGES_PATH, SEED_A, BASE_DATE, out2,
            sealed_dir=tmp_path / "r2" / "sealed", manifest_path=tmp_path / "r2" / "manifest.json",
        )
        assert result1.summary.content_hash == result2.summary.content_hash
        assert result1.db_sha256 == result2.db_sha256

        labels1 = json.loads(result1.labels_path.read_text(encoding="utf-8"))
        labels2 = json.loads(result2.labels_path.read_text(encoding="utf-8"))
        assert labels1 == labels2
        assert result1.labels_sha256 == result2.labels_sha256

    def test_different_seed_produces_different_content_hash(self, tmp_path: Path) -> None:
        out1 = tmp_path / "a" / "blind_a.db"
        out2 = tmp_path / "b" / "blind_b.db"
        result1 = blind.generate_blind(
            RANGES_PATH, SEED_A, BASE_DATE, out1,
            sealed_dir=tmp_path / "a" / "sealed", manifest_path=tmp_path / "a" / "manifest.json",
        )
        result2 = blind.generate_blind(
            RANGES_PATH, SEED_B, BASE_DATE, out2,
            sealed_dir=tmp_path / "b" / "sealed", manifest_path=tmp_path / "b" / "manifest.json",
        )
        assert result1.summary.content_hash != result2.summary.content_hash


# --- 3. 지연 '도착' arm 실재 -------------------------------------------------


class TestDelayedArrivalArm:
    def _find_seed_with_arrives_late(self, ranges: dict) -> int:
        for seed in range(80):
            cfg = blind.build_blind_config(
                ranges, seed=seed, base_date=BASE_DATE_OBJ, items_csv=ITEMS_CSV
            )
            dd = next(sc for sc in cfg.scenarios if sc.type == "delivery_delay")
            if dd.params.get("arrives_late"):
                return seed
        pytest.fail("80개 시드 내에서 arrives_late=True인 delivery_delay를 찾지 못했다")

    def test_generated_db_has_expected_before_actual_row(
        self, ranges: dict, tmp_path: Path
    ) -> None:
        seed = self._find_seed_with_arrives_late(ranges)
        out = tmp_path / "blind_arrival.db"
        result = blind.generate_blind(
            RANGES_PATH, seed, BASE_DATE, out,
            sealed_dir=tmp_path / "sealed", manifest_path=tmp_path / "manifest.json",
        )
        assert result.has_delayed_arrival is True

        conn = sqlite3.connect(out)
        try:
            rows = conn.execute(
                "SELECT expected_date, actual_date FROM incoming_shipments"
                " WHERE actual_date IS NOT NULL AND expected_date IS NOT NULL"
                " AND actual_date > expected_date"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "expected_date < actual_date인 행이 생성 DB에 없다"

    def test_delayed_arrival_leaves_no_ghost_income(
        self, ranges: dict, tmp_path: Path
    ) -> None:
        """F2(S-22 픽스 라운드 1, 컨트롤러 리뷰 차단 항목): 지연 도착 arm이 실제로 뽑힌
        시드에서 validate_dataset 검사 11(도착 장부 정합)이 PASS해야 한다 — 강제 생성
        arm의 도착 수량이 stock_usage_daily에도 credit돼 있어야 한다는 뜻이다."""
        seed = self._find_seed_with_arrives_late(ranges)
        out = tmp_path / "blind_arrival_ledger.db"
        blind.generate_blind(
            RANGES_PATH, seed, BASE_DATE, out,
            sealed_dir=tmp_path / "sealed", manifest_path=tmp_path / "manifest.json",
        )
        conn = sqlite3.connect(out)
        try:
            result = vd.check_arrival_ledger_consistency(conn)
        finally:
            conn.close()
        assert result.status == "PASS", result.message


# --- 3c. 정상 품목 미끼(decoys) — Task S-22 픽스 라운드 1 F1(컨트롤러 리뷰, M-30 전 필수)


def _reverse_engineering_flagged_items(conn: sqlite3.Connection, base_date: str) -> set[str]:
    """컨트롤러 리뷰 F1이 지적한 역산 SQL 그대로: actual_date>expected_date이거나
    (actual_date IS NULL AND expected_date<base_date)인 발주를 가진 품목 집합."""
    rows = conn.execute(
        "SELECT DISTINCT item_id FROM incoming_shipments"
        " WHERE (actual_date IS NOT NULL AND actual_date > expected_date)"
        " OR (actual_date IS NULL AND expected_date < ?)",
        (base_date,),
    ).fetchall()
    return {r[0] for r in rows}


def _timeline_days() -> list[date]:
    return [TIMELINE_START_OBJ + timedelta(days=i) for i in range(365)]


class TestDecoyEligibilityGuard:
    """Task S-30c A: 미끼 적격성을 두 미끼 공통으로 통일하고, 측정 시점을 base_date 한
    점에서 **스윕 구간 최저치**로 옮겼다.

    S-30b 실증: 옛 가드(base_date 고점 측정)를 통과한 미끼 2 39건 중 37건(94.9%)이
    스윕 중 '주의'를 넘었고 36건이 '경고' 이상까지 갔다 — "가드가 있으니 등급은 안
    변한다"는 주장이 거짓이었다. 미끼 1은 가드 자체가 없었다."""

    def _generate(self, tmp_path: Path, seed: int = SEED_A) -> tuple[Path, dict]:
        out = tmp_path / f"blind_decoy_{seed}.db"
        result = blind.generate_blind(
            RANGES_PATH, seed, BASE_DATE, out,
            sealed_dir=tmp_path / "sealed", manifest_path=tmp_path / "manifest.json",
        )
        labels = json.loads(result.labels_path.read_text(encoding="utf-8"))
        official_item_ids = {lbl["item_id"] for lbl in labels}
        return out, {"result": result, "official_item_ids": official_item_ids}

    def test_sweep_min_coverage_uses_window_trough_not_base_date(
        self, tmp_path: Path
    ) -> None:
        """핵심 회귀 방지: base_date가 재발주 직후 고점이어도 스윕 저점이 낮으면 부적격."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            (REPO_ROOT / "medsupply" / "data" / "schema.sql").read_text(encoding="utf-8")
        )
        conn.execute("INSERT INTO items(item_id, item_name) VALUES ('ITM-T', '테스트')")
        rows = [
            ("ITM-T", "2026-07-10", 10, 0, 80),    # 스윕 저점(커버리지 8일)
            ("ITM-T", "2026-07-20", 10, 0, 400),
            ("ITM-T", "2026-08-01", 10, 0, 1000),  # base_date 고점(커버리지 100일)
        ]
        conn.executemany(
            "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty,"
            " closing_stock) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        try:
            coverage = blind.sweep_min_coverage_days(
                conn, "ITM-T", (date(2026, 7, 1), date(2026, 8, 1))
            )
        finally:
            conn.close()
        assert coverage == pytest.approx(8.0), "스윕 저점(80/10)이 아니라 다른 값을 봤다"

    def test_shipped_catalog_yields_no_eligible_items(self, tmp_path: Path) -> None:
        """정직 고지 테스트 — 현재 카탈로그·베이스라인 리듬에서는 스윕 저점 커버리지가
        대체로 8~10일이라 44일 기준을 넘는 정상 품목이 없다(S-30b §2.5). 따라서 이 회차
        설계에서는 미끼가 하나도 주입되지 않는다. 이 사실이 바뀌면(카탈로그·리듬 변경)
        이 테스트가 먼저 알려 준다."""
        _out, ctx = self._generate(tmp_path)
        counts = ctx["result"].manifest_entry["params_summary"]["decoy_counts"]
        assert counts["candidate_count"] == 120
        assert counts["eligible_count"] == 0
        assert counts["minor_delay_count"] == 0
        assert counts["safe_overdue_count"] == 0

    def _boost(self, conn: sqlite3.Connection, item_ids: list[str]) -> None:
        """적격 품목이 실재하는 상황을 만든다(전 구간 재고를 크게 올려 저점 커버리지 확보)."""
        conn.executemany(
            "UPDATE stock_usage_daily SET closing_stock = 1000000 WHERE item_id = ?",
            [(i,) for i in item_ids],
        )
        conn.commit()

    def test_decoys_land_only_on_eligible_items(self, tmp_path: Path) -> None:
        out, ctx = self._generate(tmp_path)
        ranges = blind.load_ranges(RANGES_PATH)
        _observable, sweep_window = blind.measurement_windows(ranges, BASE_DATE_OBJ)

        conn = sqlite3.connect(out)
        try:
            normal_ids = sorted(
                r[0] for r in conn.execute("SELECT item_id FROM items")
                if r[0] not in ctx["official_item_ids"]
            )
            boosted = normal_ids[:30]
            self._boost(conn, boosted)

            report = blind.inject_decoys(
                conn, ranges, SEED_A, 0, BASE_DATE_OBJ, _timeline_days(),
                ctx["official_item_ids"], sweep_window,
            )
            flagged = _reverse_engineering_flagged_items(conn, BASE_DATE) - ctx[
                "official_item_ids"
            ]
        finally:
            conn.close()

        assert report.eligible_count == len(boosted)
        assert report.minor_delay_count > 0 and report.safe_overdue_count > 0
        assert report.minor_delay_count + report.safe_overdue_count <= report.eligible_count
        assert flagged, "적격 품목이 있는데도 미끼가 하나도 안 들어갔다"
        assert flagged <= set(boosted), "부적격 품목에 미끼가 들어갔다"

    def test_minor_delay_decoy_stays_under_receipt_delay_threshold(
        self, tmp_path: Path
    ) -> None:
        """미끼 1은 receipt_delay_days(3, config/analytics_params.toml) 미만이어야
        anomaly.detect_receipt_delay가 반응하지 않는다(그 신호에 한해 불변 — 등급은
        무임계 overdue_cutoff 때문에 불변이 아니었다, S-30b §2.4)."""
        out, ctx = self._generate(tmp_path)
        ranges = blind.load_ranges(RANGES_PATH)
        _observable, sweep_window = blind.measurement_windows(ranges, BASE_DATE_OBJ)

        conn = sqlite3.connect(out)
        try:
            normal_ids = sorted(
                r[0] for r in conn.execute("SELECT item_id FROM items")
                if r[0] not in ctx["official_item_ids"]
            )
            self._boost(conn, normal_ids[:30])
            blind.inject_decoys(
                conn, ranges, SEED_A, 0, BASE_DATE_OBJ, _timeline_days(),
                ctx["official_item_ids"], sweep_window,
            )
            rows = conn.execute(
                "SELECT item_id, expected_date, actual_date FROM incoming_shipments"
                " WHERE actual_date IS NOT NULL AND actual_date > expected_date"
            ).fetchall()
            ledger = vd.check_arrival_ledger_consistency(conn)
        finally:
            conn.close()

        decoy_rows = [r for r in rows if r[0] not in ctx["official_item_ids"]]
        assert decoy_rows, "미끼 1(경미한 지연 도착) 행을 하나도 못 찾았다"
        for item_id, expected_date, actual_date in decoy_rows:
            delay = (date.fromisoformat(actual_date) - date.fromisoformat(expected_date)).days
            assert 1 <= delay <= 2, (item_id, delay)
        assert ledger.status == "PASS"

    def test_decoy_assignment_is_deterministic(self, tmp_path: Path) -> None:
        out1, ctx1 = self._generate(tmp_path / "r1", SEED_A)
        out2, ctx2 = self._generate(tmp_path / "r2", SEED_A)
        conn1 = sqlite3.connect(out1)
        conn2 = sqlite3.connect(out2)
        try:
            flagged1 = _reverse_engineering_flagged_items(conn1, BASE_DATE)
            flagged2 = _reverse_engineering_flagged_items(conn2, BASE_DATE)
        finally:
            conn1.close()
            conn2.close()
        assert flagged1 == flagged2
        assert ctx1["result"].summary.content_hash == ctx2["result"].summary.content_hash


# --- 3d. S-30c A: 배치 구간 ↔ 측정 창 결합 ------------------------------------


class TestMeasurementWindowCoupling:
    """S-30b 지배 원인(라벨 12/20이 스윕 시작 전 품절 — 채점 규칙상 감지 불가)의 생성
    단계 차단. 라벨 전건의 stockout_date가 [스윕 시작, 스윕 종료 + watch_days] 안에
    들어갈 때까지 재추첨한다."""

    _SEEDS = (SEED_A, SEED_B, 4)

    def test_measurement_windows_resolves_yaml_offsets(self, ranges: dict) -> None:
        observable, sweep = blind.measurement_windows(ranges, BASE_DATE_OBJ)
        assert sweep == (date(2026, 7, 1), date(2026, 8, 1))
        assert observable == (date(2026, 7, 1), date(2026, 8, 31))

    def test_shipped_yaml_satisfies_arrival_timeline_invariant(self, ranges: dict) -> None:
        """앵커 상한 + 최대 지연 일수 <= 0이어야 지연 도착일이 타임라인 안에 남는다."""
        anchor_max = ranges["scenario_start_offset_days_range"]["max"]
        delay_max = ranges["delivery_delay"]["delay_days_range"]["max"]
        assert anchor_max + delay_max <= 0
        blind._validate_offset_ranges(ranges)  # 예외 없이 통과

    def test_validate_offset_ranges_rejects_late_anchor(self, ranges: dict) -> None:
        broken = json.loads(json.dumps(ranges))
        broken["scenario_start_offset_days_range"]["max"] = -1
        with pytest.raises(ValueError, match="불변식 위반"):
            blind._validate_offset_ranges(broken)

    @pytest.mark.parametrize("seed", _SEEDS)
    def test_all_labels_stockout_inside_observable_window(
        self, tmp_path: Path, ranges: dict, seed: int
    ) -> None:
        result = blind.generate_blind(
            RANGES_PATH, seed, BASE_DATE, tmp_path / f"win_{seed}.db",
            sealed_dir=tmp_path / "sealed", manifest_path=tmp_path / "manifest.json",
        )
        labels = json.loads(result.labels_path.read_text(encoding="utf-8"))
        observable, _sweep = blind.measurement_windows(ranges, BASE_DATE_OBJ)
        assert len(labels) == 4
        for lbl in labels:
            stockout = date.fromisoformat(lbl["stockout_date"])
            assert observable[0] <= stockout <= observable[1], (seed, lbl)

    def test_manifest_records_the_window_used(self, tmp_path: Path) -> None:
        result = blind.generate_blind(
            RANGES_PATH, SEED_A, BASE_DATE, tmp_path / "win_meta.db",
            sealed_dir=tmp_path / "sealed", manifest_path=tmp_path / "manifest.json",
        )
        window = result.manifest_entry["params_summary"]["observable_window"]
        assert window == {"start": "2026-07-01", "end": "2026-08-31"}


# --- 3b. 물리적 무효과 재시도(max_generation_attempts) -----------------------


class TestGenerationRetry:
    """임의 품목·파라미터 조합은 우연히 물리적으로 무효과(예: halt 기간 중 재주문점 밑으로
    안 내려감)일 수 있다 — inject.py의 _assert_effects_effective가 명확한 ValueError로
    잡는다. generate_blind는 다른 서브시드 네임스페이스("attempt{N}")로 재시도해야 한다.
    seed=4는 attempt 0이 실제로 무효과라 재시도가 필요한 실사례로 사전 확인했다."""

    _RETRY_SEED = 4

    def test_retry_succeeds_and_result_still_passes_validation(self, tmp_path: Path) -> None:
        out = tmp_path / "blind_retry.db"
        result = blind.generate_blind(
            RANGES_PATH, self._RETRY_SEED, BASE_DATE, out,
            sealed_dir=tmp_path / "sealed", manifest_path=tmp_path / "manifest.json",
        )
        assert result.attempts_used == 2, "이 seed는 attempt 0 실패가 사전 확인된 사례다"

        conn = sqlite3.connect(out)
        try:
            results = vd.run_all_checks(conn)
        finally:
            conn.close()
        fails = [(name, r.message) for name, r in results if r.status == "FAIL"]
        assert fails == []

    def test_retry_is_deterministic_across_runs(self, tmp_path: Path) -> None:
        out1 = tmp_path / "r1.db"
        out2 = tmp_path / "r2.db"
        result1 = blind.generate_blind(
            RANGES_PATH, self._RETRY_SEED, BASE_DATE, out1,
            sealed_dir=tmp_path / "s1", manifest_path=tmp_path / "m1.json",
        )
        result2 = blind.generate_blind(
            RANGES_PATH, self._RETRY_SEED, BASE_DATE, out2,
            sealed_dir=tmp_path / "s2", manifest_path=tmp_path / "m2.json",
        )
        assert result1.attempts_used == result2.attempts_used == 2
        assert result1.summary.content_hash == result2.summary.content_hash

    def test_exhausted_attempts_raises_clear_value_error(self, tmp_path: Path) -> None:
        """max_generation_attempts를 1로 낮추면(attempt 0만 시도) 이 seed는 재시도 없이
        바로 소진돼 명확한 ValueError로 실패해야 한다(조용한 폴백 없음)."""
        limited_ranges = blind.load_ranges(RANGES_PATH)
        limited_ranges["max_generation_attempts"] = 1
        limited_path = tmp_path / "limited_ranges.yaml"
        import yaml as yaml_mod

        limited_path.write_text(yaml_mod.safe_dump(limited_ranges), encoding="utf-8")

        out = tmp_path / "blind_exhausted.db"
        with pytest.raises(ValueError, match="재시도"):
            blind.generate_blind(
                limited_path, self._RETRY_SEED, BASE_DATE, out,
                sealed_dir=tmp_path / "sealed", manifest_path=tmp_path / "manifest.json",
            )


# --- 4. manifest 스키마 · 해시 일치 · 결정적 정렬(append/upsert) -------------


class TestManifest:
    def test_manifest_schema_and_hashes_match(self, blind_paths: dict[str, Path]) -> None:
        result = blind.generate_blind(
            RANGES_PATH, SEED_A, BASE_DATE, blind_paths["out"],
            sealed_dir=blind_paths["sealed_dir"], manifest_path=blind_paths["manifest_path"],
        )

        manifest = json.loads(blind_paths["manifest_path"].read_text(encoding="utf-8"))
        assert manifest["runs"], manifest
        entry = next(r for r in manifest["runs"] if r["seed"] == SEED_A)

        assert entry["db_file"] == blind_paths["out"].name
        assert entry["labels_file"] == result.labels_path.name
        assert entry["db_sha256"] == result.db_sha256 == blind._sha256_file(blind_paths["out"])
        assert (
            entry["labels_sha256"]
            == result.labels_sha256
            == blind._sha256_file(result.labels_path)
        )
        assert set(entry["params_summary"]) >= {
            "scenario_type_counts", "has_delayed_arrival_arm", "item_count",
        }
        assert entry["params_summary"]["scenario_type_counts"] == {
            t: 1 for t in ALLOWED_TYPES
        }

    def test_manifest_leaks_no_item_ids_or_dates(self, blind_paths: dict[str, Path]) -> None:
        """봉인 무결성: manifest는 유형 커버리지 확인 목적의 요약만 담아야 하고, 라벨의
        item_id·onset_date·stockout_date 등 정답 단서를 노출하면 안 된다."""
        blind.generate_blind(
            RANGES_PATH, SEED_A, BASE_DATE, blind_paths["out"],
            sealed_dir=blind_paths["sealed_dir"], manifest_path=blind_paths["manifest_path"],
        )
        manifest_text = blind_paths["manifest_path"].read_text(encoding="utf-8")
        cfg = blind.build_blind_config(
            blind.load_ranges(RANGES_PATH), seed=SEED_A, base_date=BASE_DATE_OBJ,
            items_csv=ITEMS_CSV,
        )
        for sc in cfg.scenarios:
            assert sc.item_id not in manifest_text

    def test_manifest_append_is_sorted_by_seed_and_upserts(self, tmp_path: Path) -> None:
        sealed = tmp_path / "sealed"
        manifest_path = tmp_path / "manifest.json"
        out_b = tmp_path / "blind_b.db"
        out_a = tmp_path / "blind_a.db"

        blind.generate_blind(
            RANGES_PATH, SEED_B, BASE_DATE, out_b,
            sealed_dir=sealed, manifest_path=manifest_path,
        )
        blind.generate_blind(
            RANGES_PATH, SEED_A, BASE_DATE, out_a,
            sealed_dir=sealed, manifest_path=manifest_path,
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        seeds = [r["seed"] for r in manifest["runs"]]
        assert seeds == [SEED_A, SEED_B]

        # 같은 시드 재생성 → 추가(append)가 아니라 교체(upsert).
        blind.generate_blind(
            RANGES_PATH, SEED_A, BASE_DATE, out_a,
            sealed_dir=sealed, manifest_path=manifest_path,
        )
        manifest2 = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert [r["seed"] for r in manifest2["runs"]] == [SEED_A, SEED_B]

    def test_upsert_preserves_sibling_top_level_keys(self) -> None:
        """S-30c: manifest.json에는 생성기가 안 쓰는 봉인 증거(1차 블라인드 predictions
        절 — 커밋으로 고정된 예측 해시)가 함께 산다. 2차 회차를 생성한다고 그 기록이
        사라지면 안 된다."""
        manifest = {
            "version": 1,
            "runs": [{"seed": 1, "db_file": "a.db"}],
            "predictions": {"recorded_at": "2026-08-21T02:44:57+00:00", "files": []},
        }
        updated = blind.upsert_manifest_entry(manifest, {"seed": 2, "db_file": "b.db"})
        assert [r["seed"] for r in updated["runs"]] == [1, 2]
        assert updated["predictions"] == manifest["predictions"]


# --- 5. out DB에 라벨 · 시나리오 흔적 없음(validate_dataset 전량 통과) -------


class TestOutDbHasNoScenarioTrace:
    def test_out_db_passes_full_validate_dataset(self, blind_paths: dict[str, Path]) -> None:
        blind.generate_blind(
            RANGES_PATH, SEED_A, BASE_DATE, blind_paths["out"],
            sealed_dir=blind_paths["sealed_dir"], manifest_path=blind_paths["manifest_path"],
        )
        conn = sqlite3.connect(blind_paths["out"])
        try:
            results = vd.run_all_checks(conn)
        finally:
            conn.close()
        fails = [(name, r.message) for name, r in results if r.status == "FAIL"]
        assert fails == [], fails

    def test_no_scenario_columns_in_schema(self, blind_paths: dict[str, Path]) -> None:
        blind.generate_blind(
            RANGES_PATH, SEED_A, BASE_DATE, blind_paths["out"],
            sealed_dir=blind_paths["sealed_dir"], manifest_path=blind_paths["manifest_path"],
        )
        conn = sqlite3.connect(blind_paths["out"])
        try:
            result = vd.check_no_scenario_columns(conn)
        finally:
            conn.close()
        assert result.status == "PASS"


# --- 6. CLI 배선(scripts/generate_blind.py) ---------------------------------


class TestCli:
    def test_main_wires_arguments_through_to_generate_blind(self, tmp_path: Path) -> None:
        out = tmp_path / "cli" / "blind_cli.db"
        sealed = tmp_path / "cli" / "sealed"
        manifest_path = tmp_path / "cli" / "manifest.json"

        result = blind_cli.main(
            [
                "--ranges", str(RANGES_PATH),
                "--seed", str(SEED_A),
                "--base-date", BASE_DATE,
                "--out", str(out),
                "--sealed-dir", str(sealed),
                "--manifest", str(manifest_path),
            ]
        )
        assert out.exists()
        assert manifest_path.exists()
        assert result.summary.content_hash
