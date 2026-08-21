"""품절 위험 시나리오 config 로더 · 검증기.

**격리 원칙**: 이 모듈과 `data/scenarios/scenario_config.yaml`은 데이터 생성 스크립트
(`scripts/datagen/`)와 측정 스크립트만 읽는다. `medsupply/` 패키지와 `app.py`는 이 모듈을
어떤 형태로도 import·참조하지 않는다 — 위험도 판정 로직이 정답(시나리오)을 볼 수 없어야
측정이 객관적이기 때문이다. 이 파일 자신도 `medsupply/`를 import하지 않는다.

로더는 PyYAML `safe_load`만 사용한다. 검증 실패는 예외가 아니라 위반 메시지 리스트로
반환한다(빈 리스트 = 통과) — 측정 스크립트가 리포트에 그대로 담을 수 있도록 하기 위함이다.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: scenario.type / sub_scenario.type에 허용되는 값. composite만 sub_scenarios로 나머지
#: 3종을 중첩할 수 있다(중첩 안의 sub_scenario.type에는 composite 자신은 올 수 없다).
ALLOWED_TYPES: tuple[str, ...] = ("demand_surge", "supply_halt", "delivery_delay", "composite")
ALLOWED_SUB_TYPES: tuple[str, ...] = ("demand_surge", "supply_halt", "delivery_delay")

#: 유형별 배분 최소치·전체 품목 대비 시나리오 품목 비율 상한(브리프 배분 요구).
MIN_SCENARIOS_PER_TYPE = 4
MAX_ITEM_RATIO = 0.30

#: 유형별 필수 params 키. composite는 최상위에 sub_scenarios만 요구하고, 각 원소는
#: 자신의 type에 해당하는 아래 키 집합을 재귀적으로 요구한다.
REQUIRED_PARAM_KEYS: dict[str, tuple[str, ...]] = {
    "demand_surge": ("surge_start_date", "ramp_days", "peak_multiplier", "sustain"),
    "supply_halt": ("halt_start_date", "expected_restart_date"),
    "delivery_delay": ("expected_date", "delay_days", "qty_ratio"),
    "composite": ("sub_scenarios",),
}

#: 범위(timeline_start~base_date) 검사 대상 "앵커 날짜" 키 — 패턴이 시작되는 날짜만
#: 검사한다. expected_restart_date는 미정(null) 또는 base_date 이후의 예고 시점을
#: 뜻하는 전망치라 범위 검사에서 제외한다(스키마 주석 "null = 미정" 참조).
ANCHOR_DATE_KEYS: dict[str, str] = {
    "demand_surge": "surge_start_date",
    "supply_halt": "halt_start_date",
    "delivery_delay": "expected_date",
}


@dataclass(frozen=True)
class Scenario:
    """시나리오 1건. params는 type별 스키마를 따르는 dict(구조는 REQUIRED_PARAM_KEYS 참조)."""

    scenario_id: str
    item_id: str
    type: str
    reference: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ScenarioConfig:
    """scenario_config.yaml 전체."""

    version: int
    base_date: str
    timeline_start: str
    scenarios: tuple[Scenario, ...]


def load_scenario_config(
    path: str | Path = Path("data/scenarios/scenario_config.yaml"),
) -> ScenarioConfig:
    """scenario_config.yaml을 PyYAML safe_load로 읽어 ScenarioConfig로 변환한다."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    scenarios = tuple(
        Scenario(
            scenario_id=item["scenario_id"],
            item_id=item["item_id"],
            type=item["type"],
            reference=item.get("reference", ""),
            params=item.get("params") or {},
        )
        for item in raw.get("scenarios", [])
    )

    return ScenarioConfig(
        version=raw["version"],
        base_date=raw["base_date"],
        timeline_start=raw["timeline_start"],
        scenarios=scenarios,
    )


def _load_item_ids(items_csv: str | Path) -> set[str]:
    with Path(items_csv).open("r", encoding="utf-8", newline="") as f:
        return {row["item_id"] for row in csv.DictReader(f)}


def _date_in_range(value: str, start: str, end: str) -> bool:
    """ISO(YYYY-MM-DD) 문자열은 사전식 비교가 시간순 비교와 일치한다."""
    return start <= value <= end


def _validate_params_for_type(
    scenario_id: str,
    type_: str,
    params: dict[str, Any],
    timeline_start: str,
    base_date: str,
    *,
    label_prefix: str = "",
) -> list[str]:
    """단일 (type, params) 쌍에 대해 필수 키·앵커 날짜 범위를 검사한다.

    composite의 sub_scenarios 원소에도 재사용할 수 있도록 라벨 접두어를 받는다.
    """
    violations: list[str] = []
    required_keys = REQUIRED_PARAM_KEYS.get(type_, ())
    missing = [k for k in required_keys if k not in params]
    if missing:
        violations.append(
            f"{scenario_id}: {label_prefix}필수 params 키 누락({type_}): {missing}"
        )

    anchor_key = ANCHOR_DATE_KEYS.get(type_)
    if anchor_key and anchor_key in params:
        value = params[anchor_key]
        if value is not None and not _date_in_range(str(value), timeline_start, base_date):
            violations.append(
                f"{scenario_id}: {label_prefix}{anchor_key}({value})가 "
                f"timeline_start~base_date 범위 밖"
            )

    return violations


def _validate_scenario_params(
    sc: Scenario, timeline_start: str, base_date: str
) -> list[str]:
    if sc.type not in ALLOWED_TYPES:
        return []  # 허용되지 않는 type은 별도 규칙(허용값 검사)에서 이미 보고한다.

    if sc.type != "composite":
        return _validate_params_for_type(
            sc.scenario_id, sc.type, sc.params, timeline_start, base_date
        )

    violations: list[str] = []
    sub_scenarios = sc.params.get("sub_scenarios")
    if not sub_scenarios:
        violations.append(f"{sc.scenario_id}: composite에 sub_scenarios 없음")
        return violations

    for i, sub in enumerate(sub_scenarios):
        sub_type = sub.get("type")
        sub_params = sub.get("params") or {}
        if sub_type not in ALLOWED_SUB_TYPES:
            violations.append(
                f"{sc.scenario_id}: sub_scenarios[{i}]의 type '{sub_type}'이 허용값 아님"
            )
            continue
        violations.extend(
            _validate_params_for_type(
                sc.scenario_id,
                sub_type,
                sub_params,
                timeline_start,
                base_date,
                label_prefix=f"sub_scenarios[{i}] ",
            )
        )

    return violations


def validate_scenario_config(
    cfg: ScenarioConfig,
    items_csv: str | Path = Path("data/reference/items_master.csv"),
    *,
    min_per_type: int = MIN_SCENARIOS_PER_TYPE,
) -> list[str]:
    """cfg를 검사해 위반 메시지 리스트를 반환한다(빈 리스트 = 통과).

    검사 항목: 유형 4종 각 ≥min_per_type, 시나리오 품목 비율 ≤30%, item_id 실존, item_id
    중복 없음, scenario_id 유일, type 허용값, 앵커 날짜가 timeline_start~base_date 범위,
    reference 비어있지 않음, 유형별 필수 params 키 존재.

    min_per_type 기본값은 MIN_SCENARIOS_PER_TYPE(4)로 표준 config 검증과 동일하다(Task S-22
    브리프 배경: 블라인드 생성기는 유형당 시나리오 1개만 만들므로, 이 하한을 완화해 호출할
    수 있게 매개변수화했다 — 개수 검사 로직 자체를 복제하지 않기 위함).
    """
    violations: list[str] = []
    all_item_ids = _load_item_ids(items_csv)

    type_counts: dict[str, int] = {t: 0 for t in ALLOWED_TYPES}
    seen_scenario_ids: set[str] = set()
    seen_item_ids: set[str] = set()

    for sc in cfg.scenarios:
        if sc.scenario_id in seen_scenario_ids:
            violations.append(f"scenario_id 중복: {sc.scenario_id}")
        seen_scenario_ids.add(sc.scenario_id)

        if sc.type in type_counts:
            type_counts[sc.type] += 1
        else:
            violations.append(f"{sc.scenario_id}: 허용되지 않는 type '{sc.type}'")

        if sc.item_id not in all_item_ids:
            violations.append(
                f"{sc.scenario_id}: item_id '{sc.item_id}'가 items_master.csv에 실존하지 않음"
            )

        if sc.item_id in seen_item_ids:
            violations.append(f"{sc.scenario_id}: item_id 중복 사용 '{sc.item_id}'")
        seen_item_ids.add(sc.item_id)

        if not sc.reference or not sc.reference.strip():
            violations.append(f"{sc.scenario_id}: reference가 비어 있음")

        violations.extend(_validate_scenario_params(sc, cfg.timeline_start, cfg.base_date))

    for t in ALLOWED_TYPES:
        if type_counts[t] < min_per_type:
            violations.append(
                f"유형 '{t}' 시나리오 수가 {min_per_type}개 미만: {type_counts[t]}"
            )

    if all_item_ids:
        ratio = len(seen_item_ids) / len(all_item_ids)
        if ratio > MAX_ITEM_RATIO:
            violations.append(
                f"시나리오 품목 비율 {ratio:.1%}가 상한 {MAX_ITEM_RATIO:.0%} 초과"
            )

    return violations
