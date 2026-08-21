"""data/scenarios/scenario_config.yaml 로더·검증기 테스트.

이 파일은 scripts/datagen/config.py(격리된 시나리오 설정 로더·검증기)를 검증한다.
scripts/datagen/은 medsupply/ 패키지를 참조하지 않는다 — 검증 객관성을 위해
품절 시나리오 정의는 분석 로직에서 격리되어야 한다(브리프 참조).

검증 축
- 로드 계약: version/base_date 등 최상위 필드
- validate_scenario_config가 실데이터에 대해 빈 리스트(통과)를 반환
- 배분 규칙: 유형별 ≥4, 시나리오 품목 비율 ≤30%, 데모 4품목 포함
- 검증기 자체 검증: 손상된 config에 대해 각 규칙이 실제로 위반을 검출
- 격리 선제 정적 검사: medsupply/*.py·app.py에 scenario/ground_truth 문자열이 없음
  (본격 가드는 후속 test_isolation.py 소관 — 이 테스트는 예방적 스모크 검사)
"""

from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

import pytest

from scripts.datagen.config import (
    Scenario,
    ScenarioConfig,
    load_scenario_config,
    validate_scenario_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_CONFIG_PATH = REPO_ROOT / "data" / "scenarios" / "scenario_config.yaml"
ITEMS_CSV = REPO_ROOT / "data" / "reference" / "items_master.csv"

ALLOWED_TYPES = ("demand_surge", "supply_halt", "delivery_delay", "composite")

# 브리프가 지정한 기존 데모 4품목 → 필수 시나리오 유형 매핑(시연 서사 연속성).
DEMO_REQUIRED = {
    "아세트아미노펜정 500mg": "composite",
    "세프트리악손주 1g": "delivery_delay",
    "아목시실린캡슐 500mg": "demand_surge",
    "덱시부프로펜시럽": "supply_halt",
}


def _item_names_by_id() -> dict[str, str]:
    with ITEMS_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return {row["item_id"]: row["item_name"] for row in reader}


def _total_item_count() -> int:
    with ITEMS_CSV.open("r", encoding="utf-8", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


@pytest.fixture(scope="module")
def cfg() -> ScenarioConfig:
    return load_scenario_config(SCENARIO_CONFIG_PATH)


class TestLoad:
    def test_load_succeeds(self, cfg: ScenarioConfig) -> None:
        assert isinstance(cfg, ScenarioConfig)
        assert len(cfg.scenarios) > 0

    def test_version(self, cfg: ScenarioConfig) -> None:
        assert cfg.version == 1

    def test_base_date(self, cfg: ScenarioConfig) -> None:
        assert cfg.base_date == "2026-08-01"

    def test_timeline_start_present(self, cfg: ScenarioConfig) -> None:
        assert cfg.timeline_start
        assert cfg.timeline_start < cfg.base_date

    def test_scenarios_are_scenario_instances(self, cfg: ScenarioConfig) -> None:
        for sc in cfg.scenarios:
            assert isinstance(sc, Scenario)
            assert sc.scenario_id
            assert sc.item_id
            assert sc.type in ALLOWED_TYPES
            assert isinstance(sc.params, dict)


class TestValidateRealData:
    def test_validate_returns_no_violations(self, cfg: ScenarioConfig) -> None:
        violations = validate_scenario_config(cfg, items_csv=ITEMS_CSV)
        assert violations == []


class TestAllocation:
    def test_each_type_has_at_least_four(self, cfg: ScenarioConfig) -> None:
        counts: dict[str, int] = {t: 0 for t in ALLOWED_TYPES}
        for sc in cfg.scenarios:
            counts[sc.type] += 1
        for t in ALLOWED_TYPES:
            assert counts[t] >= 4, f"{t} 시나리오가 4개 미만: {counts[t]}"
        assert sum(counts.values()) >= 16

    def test_item_ratio_at_most_30_percent(self, cfg: ScenarioConfig) -> None:
        item_ids = [sc.item_id for sc in cfg.scenarios]
        total = _total_item_count()
        assert len(item_ids) / total <= 0.30

    def test_no_item_used_twice(self, cfg: ScenarioConfig) -> None:
        item_ids = [sc.item_id for sc in cfg.scenarios]
        assert len(item_ids) == len(set(item_ids))

    def test_demo_four_items_included_with_correct_type(self, cfg: ScenarioConfig) -> None:
        names_by_id = _item_names_by_id()
        type_by_name = {}
        for sc in cfg.scenarios:
            name = names_by_id.get(sc.item_id)
            if name is not None:
                type_by_name[name] = sc.type

        for demo_name, expected_type in DEMO_REQUIRED.items():
            assert demo_name in type_by_name, f"데모 품목 누락: {demo_name}"
            assert type_by_name[demo_name] == expected_type, (
                f"{demo_name}는 {expected_type}이어야 하나 {type_by_name[demo_name]}"
            )


class TestValidatorSelfCheck:
    """검증기 자체가 각 위반 규칙을 실제로 검출하는지 확인(손상 config 주입)."""

    def test_detects_type_shortage(self, cfg: ScenarioConfig) -> None:
        # composite을 2개만 남기고 모두 제거 → 유형별 4개 미만 위반 유도.
        kept_composite = 0
        filtered = []
        for sc in cfg.scenarios:
            if sc.type == "composite":
                kept_composite += 1
                if kept_composite > 2:
                    continue
            filtered.append(sc)
        broken = dataclasses.replace(cfg, scenarios=tuple(filtered))

        violations = validate_scenario_config(broken, items_csv=ITEMS_CSV)
        assert any("composite" in v and ("4" in v or "부족" in v) for v in violations), violations

    def test_detects_nonexistent_item(self, cfg: ScenarioConfig) -> None:
        scenarios = list(cfg.scenarios)
        scenarios[0] = dataclasses.replace(scenarios[0], item_id="ITM-9999")
        broken = dataclasses.replace(cfg, scenarios=tuple(scenarios))

        violations = validate_scenario_config(broken, items_csv=ITEMS_CSV)
        assert any("ITM-9999" in v for v in violations), violations

    def test_detects_duplicate_item(self, cfg: ScenarioConfig) -> None:
        scenarios = list(cfg.scenarios)
        scenarios[1] = dataclasses.replace(scenarios[1], item_id=scenarios[0].item_id)
        broken = dataclasses.replace(cfg, scenarios=tuple(scenarios))

        violations = validate_scenario_config(broken, items_csv=ITEMS_CSV)
        assert any("중복" in v for v in violations), violations

    def test_detects_date_out_of_range(self, cfg: ScenarioConfig) -> None:
        scenarios = list(cfg.scenarios)
        target_idx = next(
            i for i, sc in enumerate(scenarios) if sc.type == "demand_surge"
        )
        target = scenarios[target_idx]
        bad_params = dict(target.params)
        bad_params["surge_start_date"] = "2020-01-01"
        scenarios[target_idx] = dataclasses.replace(target, params=bad_params)
        broken = dataclasses.replace(cfg, scenarios=tuple(scenarios))

        violations = validate_scenario_config(broken, items_csv=ITEMS_CSV)
        assert any("범위" in v for v in violations), violations

    def test_detects_missing_required_param_key(self, cfg: ScenarioConfig) -> None:
        scenarios = list(cfg.scenarios)
        target_idx = next(
            i for i, sc in enumerate(scenarios) if sc.type == "delivery_delay"
        )
        target = scenarios[target_idx]
        bad_params = dict(target.params)
        del bad_params["delay_days"]
        scenarios[target_idx] = dataclasses.replace(target, params=bad_params)
        broken = dataclasses.replace(cfg, scenarios=tuple(scenarios))

        violations = validate_scenario_config(broken, items_csv=ITEMS_CSV)
        assert any("delay_days" in v for v in violations), violations

    def test_detects_empty_reference(self, cfg: ScenarioConfig) -> None:
        scenarios = list(cfg.scenarios)
        scenarios[0] = dataclasses.replace(scenarios[0], reference="")
        broken = dataclasses.replace(cfg, scenarios=tuple(scenarios))

        violations = validate_scenario_config(broken, items_csv=ITEMS_CSV)
        assert any("reference" in v for v in violations), violations

    def test_detects_duplicate_scenario_id(self, cfg: ScenarioConfig) -> None:
        scenarios = list(cfg.scenarios)
        scenarios[1] = dataclasses.replace(scenarios[1], scenario_id=scenarios[0].scenario_id)
        broken = dataclasses.replace(cfg, scenarios=tuple(scenarios))

        violations = validate_scenario_config(broken, items_csv=ITEMS_CSV)
        assert any("scenario_id" in v and "중복" in v for v in violations), violations

    def test_detects_invalid_type(self, cfg: ScenarioConfig) -> None:
        scenarios = list(cfg.scenarios)
        scenarios[0] = dataclasses.replace(scenarios[0], type="unknown_type")
        broken = dataclasses.replace(cfg, scenarios=tuple(scenarios))

        violations = validate_scenario_config(broken, items_csv=ITEMS_CSV)
        assert any("unknown_type" in v for v in violations), violations


class TestIsolationPreflight:
    """medsupply/·app.py가 시나리오/정답 데이터를 참조하지 않는지에 대한 선제 정적 검사.

    본격적인 가드(AST 분석 등)는 후속 태스크의 test_isolation.py 소관이다. 여기서는
    단순 문자열 검사만 한다. medsupply/settings.py는 격리 원칙 주석에서 경로
    'data/scenarios'를 합법적으로 언급하므로, 그 부분 문자열만 제거한 뒤 남은
    텍스트에서 scenario/ground_truth를 검사한다(해당 파일·라인을 통째로 예외 처리하지
    않기 위함).
    """

    def test_no_scenario_or_ground_truth_leak(self) -> None:
        targets = [REPO_ROOT / "app.py", *sorted((REPO_ROOT / "medsupply").rglob("*.py"))]
        assert targets, "검사 대상 파일을 찾지 못함"

        violations = []
        for path in targets:
            text = path.read_text(encoding="utf-8").lower()
            masked = text.replace("data/scenarios", "")
            if "scenario" in masked or "ground_truth" in masked:
                violations.append(str(path.relative_to(REPO_ROOT)))

        assert violations == [], f"격리 위반 후보(문자열 검사): {violations}"


class TestMinPerTypeOverride:
    """min_per_type 매개변수화(Task S-22 브리프 배경) — 블라인드 생성기는 유형당 시나리오
    1개(표준의 최소 4개보다 적음)만 만들므로, validate_scenario_config가 이 하한을 인자로
    받을 수 있어야 표준 검증 로직을 복제하지 않고 재사용할 수 있다. 기본값은 기존
    MIN_SCENARIOS_PER_TYPE(4)과 동일해 표준 config·기존 호출부의 검증 결과는 그대로다.
    """

    _MINIMAL_PARAMS = {
        "demand_surge": {
            "surge_start_date": "2026-07-01", "ramp_days": 7, "peak_multiplier": 1.5,
            "sustain": True,
        },
        "supply_halt": {"halt_start_date": "2026-07-01", "expected_restart_date": None},
        "delivery_delay": {"expected_date": "2026-07-01", "delay_days": 5, "qty_ratio": 1.0},
    }

    def _one_per_type_config(self) -> ScenarioConfig:
        scenarios = tuple(
            Scenario(
                scenario_id=f"B-{t.upper()}",
                item_id=item_id,
                type=t,
                reference="블라인드 최소 구성 테스트",
                params=params,
            )
            for t, item_id, params in [
                ("demand_surge", "ITM-0002", self._MINIMAL_PARAMS["demand_surge"]),
                ("supply_halt", "ITM-0005", self._MINIMAL_PARAMS["supply_halt"]),
                ("delivery_delay", "ITM-0006", self._MINIMAL_PARAMS["delivery_delay"]),
                (
                    "composite", "ITM-0007",
                    {"sub_scenarios": [
                        {"type": "demand_surge", "params": self._MINIMAL_PARAMS["demand_surge"]},
                    ]},
                ),
            ]
        )
        return ScenarioConfig(
            version=1, base_date="2026-08-01", timeline_start="2025-08-02", scenarios=scenarios
        )

    def test_default_still_rejects_one_per_type(self) -> None:
        """매개변수를 안 주면 종전과 동일하게(4개 미만) 위반으로 잡는다."""
        violations = validate_scenario_config(self._one_per_type_config(), items_csv=ITEMS_CSV)
        assert any("4개 미만" in v for v in violations), violations

    def test_min_per_type_one_accepts_one_per_type(self) -> None:
        """min_per_type=1이면 유형당 1개짜리 구성도 통과한다(개수 위반만 사라짐 — 다른
        위반이 없는 최소 구성이라 전체 리스트가 빈 값이어야 한다)."""
        violations = validate_scenario_config(
            self._one_per_type_config(), items_csv=ITEMS_CSV, min_per_type=1
        )
        assert violations == []

    def test_real_config_unaffected_by_new_keyword_default(self, cfg: ScenarioConfig) -> None:
        """표준 scenario_config.yaml은 새 키워드를 안 줬을 때와 명시적으로 기본값을 줬을
        때 결과가 동일해야 한다(하위 호환 회귀 가드)."""
        assert validate_scenario_config(cfg, items_csv=ITEMS_CSV) == validate_scenario_config(
            cfg, items_csv=ITEMS_CSV, min_per_type=4
        )
