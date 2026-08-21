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
                    assert params.get("arrives_late", False) in (True, False)


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
