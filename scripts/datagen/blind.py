"""블라인드 평가 스냅샷 생성기 — data/scenarios/blind_ranges.yaml의 구간에서 seed로
결정적 파라미터를 뽑아 기존 datagen 표준 생성 경로(baseline·inject·labels·config)를
그대로 재사용해 스냅샷을 만들고, 라벨을 봉인 규약에 따라 격리한다(Task S-22).

**격리 원칙**: baseline.py·inject.py·config.py·labels.py와 동일하게 이 모듈도 `medsupply`
패키지를 일절 import하지 않는다. `data/blind` 경로 리터럴은 이 모듈과
scripts/generate_blind.py 안에서만 쓴다(tests/test_isolation.py의 순방향 검사 대상은
scripts/datagen/를 애초에 스캔하지 않으므로 이 경로 리터럴이 걸릴 일이 없다).

**표준 경로 재사용, 로직 미복제**: 실제 베이스라인 생성·시나리오 재시뮬레이션·해시·라벨
도출은 전부 baseline.generate_baseline·inject.inject_scenarios·labels.derive_labels가
그대로 수행한다. 이 모듈이 새로 만드는 것은 (1) 범위 YAML에서 결정적으로 뽑은
ScenarioConfig 조립, (2) inject_scenarios 결과를 봉인 규약(sealed 라벨 + manifest)으로
감싸는 오케스트레이션뿐이다. inject_scenarios·validate_scenario_config에는 유형당
시나리오 1개(표준 최소 4개 미만)를 허용하기 위한 매개변수(min_scenarios_per_type/
min_per_type)만 추가했다 — 기본값은 종전과 동일해 표준 20건 config의 검증 결과는
바뀌지 않는다.

**out DB 무흔적**: 시나리오 파라미터·scenario_id는 out DB에 전혀 기록되지 않는다(라벨은
sealed/로만 나간다) — schema.sql 자체가 'scenario' 컬럼을 갖지 않으므로(기존 원칙)
validate_dataset.check_no_scenario_columns가 그대로 통과한다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from scripts.datagen import baseline, config, inject
from scripts.datagen import labels as labels_mod
from scripts.datagen.config import ALLOWED_TYPES, Scenario, ScenarioConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RANGES_PATH = REPO_ROOT / "data" / "scenarios" / "blind_ranges.yaml"
DEFAULT_BLIND_DIR = REPO_ROOT / "data" / "blind"
DEFAULT_SEALED_DIR = DEFAULT_BLIND_DIR / "sealed"
DEFAULT_MANIFEST_PATH = DEFAULT_BLIND_DIR / "manifest.json"
DEFAULT_ACTION_HISTORY_SEED_PATH = baseline.DEFAULT_REFERENCE_DIR / "action_history_seed.csv"

#: composite가 아닌, 단독으로 뽑을 수 있는 유형 3종(순서 고정 — 품목 배정 순서와 일치).
_STANDALONE_TYPES: tuple[str, ...] = ("demand_surge", "supply_halt", "delivery_delay")


# ---------------------------------------------------------------------------
# 범위 YAML 로딩 · 서브시드
# ---------------------------------------------------------------------------


def load_ranges(path: str | Path = DEFAULT_RANGES_PATH) -> dict[str, Any]:
    """blind_ranges.yaml을 PyYAML safe_load로 읽는다(scenario_config.yaml 로더와 동일 방식)."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def blind_subseed(seed: int, purpose: str) -> int:
    """블라인드 파라미터 뽑기 서브시드 = sha256(f"{seed}:blind:{purpose}") 앞 8자리 hex.

    baseline.item_subseed·inject.injection_subseed와 동일한 서브시드 관례(브리프: "서브시드
    관례 재사용")를 새 네임스페이스("blind")로 적용한 것 — 파라미터 뽑기는 베이스라인
    노이즈·주입 효과와 다른 관심사라 별도 네임스페이스를 쓴다.
    """
    digest = hashlib.sha256(f"{seed}:blind:{purpose}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


# ---------------------------------------------------------------------------
# 파라미터 뽑기(유형별) — 순수 함수, I/O 없음
# ---------------------------------------------------------------------------


def _sample_anchor_date(
    rng: random.Random, offset_range: dict[str, int], base_date: date
) -> tuple[date, int]:
    offset = rng.randint(int(offset_range["min"]), int(offset_range["max"]))
    return base_date + timedelta(days=offset), offset


def _build_type_params(
    type_: str,
    ranges: dict[str, Any],
    rng: random.Random,
    base_date: date,
    *,
    allow_arrives_late: bool,
) -> dict[str, Any]:
    """단일 유형 params dict를 뽑는다(composite의 sub_scenario에도 재사용).

    rng는 호출부가 유형별로 독립된 서브시드 스트림을 넘긴다 — 같은 유형이 standalone과
    composite sub_scenario 양쪽에 쓰이면 같은 스트림을 이어서 소비한다(둘 다 결정적이며
    서로 다른 값이 나오는 것이 의도다 — 서로 다른 품목의 서로 다른 시나리오 인스턴스).
    """
    offset_range = ranges["scenario_start_offset_days_range"]
    anchor, _offset = _sample_anchor_date(rng, offset_range, base_date)

    if type_ == "demand_surge":
        r = ranges["demand_surge"]
        ramp_days = rng.randint(int(r["ramp_days_range"]["min"]), int(r["ramp_days_range"]["max"]))
        peak = round(
            rng.uniform(float(r["peak_multiplier_range"]["min"]), float(r["peak_multiplier_range"]["max"])),
            2,
        )
        return {
            "surge_start_date": anchor.isoformat(),
            "ramp_days": ramp_days,
            "peak_multiplier": peak,
            "sustain": bool(r["sustain"]),
        }

    if type_ == "supply_halt":
        r = ranges["supply_halt"]
        duration = rng.randint(
            int(r["halt_duration_days_range"]["min"]), int(r["halt_duration_days_range"]["max"])
        )
        indefinite = rng.random() < float(r["indefinite_restart_probability"])
        restart = None if indefinite else (anchor + timedelta(days=duration)).isoformat()
        params: dict[str, Any] = {
            "halt_start_date": anchor.isoformat(),
            "expected_restart_date": restart,
        }
        if rng.random() < float(r["demand_shift_probability"]):
            shift = round(
                rng.uniform(
                    float(r["demand_shift_multiplier_range"]["min"]),
                    float(r["demand_shift_multiplier_range"]["max"]),
                ),
                2,
            )
            params["demand_shift_multiplier"] = shift
        return params

    if type_ == "delivery_delay":
        r = ranges["delivery_delay"]
        delay_days = rng.randint(int(r["delay_days_range"]["min"]), int(r["delay_days_range"]["max"]))
        qty_ratio = round(
            rng.uniform(float(r["qty_ratio_range"]["min"]), float(r["qty_ratio_range"]["max"])), 2
        )
        params = {
            "expected_date": anchor.isoformat(),
            "delay_days": delay_days,
            "qty_ratio": qty_ratio,
        }
        if allow_arrives_late and rng.random() < float(r["arrives_late_probability"]):
            params["arrives_late"] = True
        return params

    raise ValueError(f"알 수 없는 시나리오 유형: {type_!r}")


def measurement_windows(
    ranges: dict[str, Any], base_date: date
) -> tuple[tuple[date, date], tuple[date, date]]:
    """blind_ranges.yaml의 measurement_window 절을 실제 날짜 창 2개로 푼다(Task S-30c A).

    Returns:
        (observable_window, sweep_window)
        - observable_window = [스윕 시작, 스윕 종료 + watch_days] — 라벨 품절일이 여기
          들어야 채점 규칙상 감지 성공이 가능하다(하한) 그리고 '주의' 판정 근거가
          있다(상한). inject_scenarios(observable_window=...)로 그대로 넘어간다.
        - sweep_window = [스윕 시작, 스윕 종료] — 오탐 판정이 매일 이뤄지는 구간이라
          미끼 적격성(최저 커버리지)을 재는 창이다.

    격리 원칙상 이 모듈은 medsupply/config를 읽지 못하므로 watch_days는 YAML의 복제
    상수를 쓴다(measurement_window 절 주석 참조).
    """
    window = ranges["measurement_window"]
    sweep_start = base_date + timedelta(days=int(window["sweep_start_offset_days"]))
    sweep_end = base_date + timedelta(days=int(window["sweep_end_offset_days"]))
    horizon_end = sweep_end + timedelta(days=int(window["watch_days"]))
    return (sweep_start, horizon_end), (sweep_start, sweep_end)


def _validate_offset_ranges(ranges: dict[str, Any]) -> None:
    """앵커 오프셋 상한과 delivery_delay 지연 일수의 구조적 불변식을 검사한다(S-30c A).

    arrives_late arm은 expected_date + delay_days에 실제 도착 행을 만든다. 그 날짜가
    base_date를 넘으면 365일 타임라인 밖이라 재고에 credit되지 않고, inject.py가
    "유령 입고(구현 오류)"로 **재시도 불가** 에러를 던진다. 즉
    `scenario_start_offset_days_range.max + delivery_delay.delay_days_range.max <= 0`은
    범위 YAML이 지켜야 하는 불변식이다 — 나중에 상한을 넓힐 때 난해한 런타임 에러 대신
    여기서 바로 원인을 말하고 실패한다.
    """
    anchor_max = int(ranges["scenario_start_offset_days_range"]["max"])
    delay_max = int(ranges["delivery_delay"]["delay_days_range"]["max"])
    if anchor_max + delay_max > 0:
        raise ValueError(
            "blind_ranges.yaml 불변식 위반: scenario_start_offset_days_range.max"
            f"({anchor_max}) + delivery_delay.delay_days_range.max({delay_max}) ="
            f" {anchor_max + delay_max} > 0 — 지연 도착일이 base_date를 넘어 타임라인 밖으로"
            " 나가면 재고에 credit되지 않아 '유령 입고(구현 오류)'로 생성이 중단된다"
        )

    decoy_ranges = ranges.get("decoys")
    if decoy_ranges:
        decoy_anchor_max = int(decoy_ranges["target_offset_days_range"]["max"])
        decoy_delay_max = max(int(d) for d in decoy_ranges["minor_delay_days_choices"])
        if decoy_anchor_max + decoy_delay_max > 0:
            raise ValueError(
                "blind_ranges.yaml 불변식 위반: decoys.target_offset_days_range.max"
                f"({decoy_anchor_max}) + max(minor_delay_days_choices)({decoy_delay_max})"
                " > 0 — 미끼 1의 도착일이 타임라인 밖으로 나간다"
            )


def _load_item_ids(items_csv: str | Path) -> list[str]:
    # config._load_item_ids는 set을 반환한다(순서 비결정) — 여기서는 정렬해 재사용,
    # random.sample에 결정적 순서의 시퀀스를 넘기기 위함이다.
    return sorted(config._load_item_ids(items_csv))


def build_blind_config(
    ranges: dict[str, Any],
    seed: int,
    base_date: date,
    *,
    items_csv: str | Path,
    attempt: int = 0,
) -> ScenarioConfig:
    """블라인드 시나리오 config를 결정적으로 조립한다(순수 함수, I/O는 items_csv 읽기뿐).

    유형 4종 각 1개(demand_surge·supply_halt·delivery_delay·composite), 서로 다른 품목
    4개. composite는 ranges["compound"]["allowed_pairs"] 중 하나를 뽑아 그 두 하위 유형의
    params를 각 유형의 독립 서브시드 스트림에서 이어 뽑는다. attempt는 물리적 무효과로
    재시도할 때만 바뀌는 서브시드 네임스페이스 접두어다(같은 seed라도 attempt가 다르면
    다른 조합이 나온다 — generate_blind의 재시도 루프가 사용).
    """
    # F4(S-22 픽스 라운드 1, 컨트롤러 리뷰): 아래 로직 전체(zip으로 유형당 정확히 1개씩
    # 배정)는 scenario_items_per_type == 1을 하드코딩된 전제로 삼는다 — YAML 값을 읽어만
    # 두고 실제로 그 값에 맞춰 일반화하지는 않으므로, 값이 어긋나면 조용히 틀린 배분을
    # 만드는 대신 여기서 바로 명확하게 실패한다.
    per_type = int(ranges["item_allocation"]["scenario_items_per_type"])
    if per_type != 1:
        raise ValueError(
            f"item_allocation.scenario_items_per_type={per_type} 은 지원하지 않는다"
            " — build_blind_config는 유형당 정확히 1개 배정만 구현했다(고정값 1 필요)"
        )

    timeline_start = base_date - timedelta(days=364)
    all_item_ids = _load_item_ids(items_csv)
    if len(all_item_ids) < 4:
        raise ValueError(f"품목이 4개 미만이라 유형당 1개씩 배정할 수 없다: {len(all_item_ids)}")

    ns = f"a{attempt}"
    rng_items = random.Random(blind_subseed(seed, f"{ns}:item_selection"))
    rng_by_type = {
        t: random.Random(blind_subseed(seed, f"{ns}:{t}")) for t in _STANDALONE_TYPES
    }
    rng_compound = random.Random(blind_subseed(seed, f"{ns}:compound_pair"))

    chosen_items = rng_items.sample(all_item_ids, 4)
    item_by_type = dict(zip(_STANDALONE_TYPES, chosen_items[:3]))
    composite_item = chosen_items[3]

    scenarios: list[Scenario] = []
    for type_ in _STANDALONE_TYPES:
        params = _build_type_params(
            type_, ranges, rng_by_type[type_], base_date, allow_arrives_late=True
        )
        scenarios.append(
            Scenario(
                scenario_id=f"BLIND-{type_.upper()}",
                item_id=item_by_type[type_],
                type=type_,
                reference=f"블라인드 평가 스냅샷(seed={seed}, attempt={attempt}) — {type_}",
                params=params,
            )
        )

    pair = tuple(rng_compound.choice(ranges["compound"]["allowed_pairs"]))
    sub_scenarios = [
        {
            "type": sub_type,
            "params": _build_type_params(
                sub_type,
                ranges,
                rng_by_type[sub_type],
                base_date,
                allow_arrives_late=False,
            ),
        }
        for sub_type in pair
    ]
    scenarios.append(
        Scenario(
            scenario_id="BLIND-COMPOSITE",
            item_id=composite_item,
            type="composite",
            reference=f"블라인드 평가 스냅샷(seed={seed}, attempt={attempt}) — composite{pair}",
            params={"sub_scenarios": sub_scenarios},
        )
    )

    return ScenarioConfig(
        version=1,
        base_date=base_date.isoformat(),
        timeline_start=timeline_start.isoformat(),
        scenarios=tuple(scenarios),
    )


def _validate_normal_item_count(ranges: dict[str, Any], total_items: int) -> None:
    """카탈로그 크기 가드레일(item_allocation.normal_item_count_range) — 서브셋을 하지
    않으므로(전체 카탈로그 재사용) 매 attempt 동일하다. 재시도로 해결될 문제가 아니라
    range 밖이면 즉시 실패한다."""
    per_type = int(ranges["item_allocation"]["scenario_items_per_type"])
    scenario_items = per_type * len(ALLOWED_TYPES)  # 4유형 × 유형당 품목 수
    normal_count = total_items - scenario_items
    band = ranges["item_allocation"]["normal_item_count_range"]
    if not (int(band["min"]) <= normal_count <= int(band["max"])):
        raise ValueError(
            f"정상 품목 수({normal_count})가 blind_ranges.yaml의 normal_item_count_range"
            f"({band['min']}~{band['max']}) 밖이다 — 카탈로그 크기가 바뀌었는지 확인해야 한다"
        )


# ---------------------------------------------------------------------------
# 정상 품목 미끼(decoys) — Task S-22 픽스 라운드 1 F1(컨트롤러 리뷰, M-30 전 필수)
#
# 결함: "actual_date>expected_date OR (actual_date IS NULL AND expected_date<base_date)"
# 하나로 시나리오 품목 4개를 오탐 0으로 역산할 수 있었다(정상 품목은 baseline 궤적상 이
# 조건을 원천적으로 만족할 수 없다). 미끼는 이 시그니처를 정상 품목 일부에도 심어 흐린다.
# out DB에는 여전히 시나리오·라벨 흔적이 없다(미끼도 라벨화하지 않는다 — cfg.scenarios에
# 추가하지 않고 별도 후처리로만 주입).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecoyReport:
    """미끼 주입 결과 요약. item_id는 담지 않는다(정상 품목 중 어떤 것이 미끼를 받았는지도
    비노출 원칙 — manifest_entry의 params_summary에 그대로 실릴 수 있는 값이므로)."""

    candidate_count: int
    eligible_count: int
    minor_delay_count: int
    safe_overdue_count: int


def sweep_min_coverage_days(
    conn: sqlite3.Connection, item_id: str, sweep_window: tuple[date, date]
) -> float | None:
    """스윕 구간 **최저** 커버리지 = 창 내 min(closing_stock) / 평균 사용량(전체 구간).

    Task S-30c A에서 옛 `_coverage_days`(base_date 한 점 측정)를 대체했다. 옛 정의는
    "측정 시점 불일치" 때문에 거짓 보장을 만들었다 — 가드는 base_date 한 점을 봤지만
    채점은 스윕 매일을 보고 최저치가 등급을 결정하는데, 이 시뮬레이터에서 base_date는
    대체로 재발주 직후 고점이다(S-30b §2.3: 39건 중 37건이 보장을 위반).

    창 안에 행이 없거나 평균 사용량이 0 이하면 None(적격성 계산 불가 — 안전하게 부적격
    취급).
    """
    window_start, window_end = sweep_window
    trough_row = conn.execute(
        "SELECT MIN(closing_stock) FROM stock_usage_daily"
        " WHERE item_id = ? AND date BETWEEN ? AND ?",
        (item_id, window_start.isoformat(), window_end.isoformat()),
    ).fetchone()
    trough = trough_row[0] if trough_row is not None else None
    if trough is None:
        return None
    avg_row = conn.execute(
        "SELECT AVG(usage_qty) FROM stock_usage_daily WHERE item_id = ?", (item_id,)
    ).fetchone()
    avg_usage = avg_row[0] if avg_row is not None else None
    if not avg_usage:
        return None
    return trough / avg_usage


def _apply_minor_delay_decoy(
    conn: sqlite3.Connection,
    item_id: str,
    seed: int,
    base_date: date,
    days: list[date],
    decoy_ranges: dict[str, Any],
    rng: random.Random,
) -> None:
    """미끼 1: 1~2일 지연 도착(receipt_delay_days=3 미만이라 이상신호·등급 불변). 표준
    delivery_delay 해석 경로(inject._resolve_effects)를 그대로 재사용한다 — 자연/강제 arm
    분기·도착 credit(F2 ForcedArrival) 전부 동일 로직이다. 라벨화하지 않는다(가짜
    Scenario는 라벨 도출용 cfg.scenarios에 들어가지 않는다 — 오직 이 함수 안에서만 쓰고
    버려진다)."""
    item_row = inject._load_item_row(conn, item_id)
    offset_range = decoy_ranges["target_offset_days_range"]
    offset = rng.randint(int(offset_range["min"]), int(offset_range["max"]))
    target_expected = base_date + timedelta(days=offset)
    delay_days = int(rng.choice(decoy_ranges["minor_delay_days_choices"]))

    fake_sc = config.Scenario(
        scenario_id=f"DECOY-MINOR-{item_id}",
        item_id=item_id,
        type="delivery_delay",
        reference="블라인드 미끼(정상 품목 역산 방지, Task S-22 픽스 라운드 1 F1)",
        params={
            "expected_date": target_expected.isoformat(),
            "delay_days": delay_days,
            "qty_ratio": None,
            "arrives_late": True,
        },
    )
    _de, _he, delay_effects, forced_rows, forced_arrivals, _onset = inject._resolve_effects(
        fake_sc, item_row, seed, days
    )
    stock_rows, shipment_rows, _trunc, _trace = inject.simulate_item_with_scenario(
        item_row, seed, days, delay_effects=delay_effects, forced_arrivals=forced_arrivals
    )
    shipment_rows = shipment_rows + forced_rows
    inject.replace_item_rows(conn, item_id, stock_rows, shipment_rows)


def _apply_safe_overdue_decoy(
    conn: sqlite3.Connection, item_id: str, seed: int, target_expected: date
) -> None:
    """미끼 2: 영구 미이행 발주 1건을 day-loop 밖에서 직접 합성해 INSERT한다(적격성은
    호출부가 이미 확인했다). day-loop을 거치지 않으므로 재고 궤적(stock_usage_daily)에는
    전혀 손대지 않는다 — actual_date가 NULL이라 F2의 도착 장부 정합(검사 11) 대상도 아니다
    (그 검사는 actual_date IS NOT NULL 건만 본다). inject._item_reorder_profile로
    reorder_qty·lead_time만 순수 조회한다(시뮬레이션 재실행 없음)."""
    item_row = inject._load_item_row(conn, item_id)
    reorder_qty, lead_time = inject._item_reorder_profile(item_row, seed)
    order_date = target_expected - timedelta(days=lead_time)
    conn.execute(
        "INSERT INTO incoming_shipments(item_id, order_date, expected_date, expected_qty,"
        " actual_date, actual_qty, status) VALUES (?, ?, ?, ?, NULL, NULL, '입고 예정')",
        (item_id, order_date.isoformat(), target_expected.isoformat(), int(reorder_qty)),
    )


def inject_decoys(
    conn: sqlite3.Connection,
    ranges: dict[str, Any],
    seed: int,
    attempt: int,
    base_date: date,
    days: list[date],
    exclude_item_ids: set[str],
    sweep_window: tuple[date, date],
) -> DecoyReport:
    """정상 품목(exclude_item_ids 제외) 일부에 미끼 2종을 결정적으로 주입한다.

    Task S-30c A에서 적격성 규칙을 **두 미끼 공통**으로 통일했다: 스윕 구간 최저 커버리지
    (sweep_min_coverage_days) > decoys.min_sweep_coverage_days인 품목만 미끼를 받는다.
    옛 규칙(미끼 2에만, base_date 한 점 측정)은 S-30b 실측에서 39건 중 37건이 '주의'를
    넘겨 보장이 거짓임이 확인됐고, 미끼 1은 애초에 가드가 없었는데도 무임계
    overdue_cutoff 때문에 등급을 밀어 올렸다.

    미끼 1(경미한 지연 도착)과 미끼 2(안전 연체)는 겹치지 않는 별도 품목 집합에 적용한다.
    목표 개수보다 적격 품목이 적으면 있는 만큼만 준다(강제 금지 — 브리프 그대로).
    적격 품목이 0건이면 미끼는 하나도 주입되지 않는다.
    """
    decoy_ranges = ranges.get("decoys")
    if not decoy_ranges:
        return DecoyReport(0, 0, 0, 0)

    all_item_ids = sorted(row[0] for row in conn.execute("SELECT item_id FROM items"))
    candidates = [item_id for item_id in all_item_ids if item_id not in exclude_item_ids]

    min_coverage = float(decoy_ranges["min_sweep_coverage_days"])
    eligible = [
        item_id
        for item_id in candidates
        if (coverage := sweep_min_coverage_days(conn, item_id, sweep_window)) is not None
        and coverage > min_coverage
    ]

    ns = f"a{attempt}"
    rng_select = random.Random(blind_subseed(seed, f"{ns}:decoy_select"))
    rng_minor = random.Random(blind_subseed(seed, f"{ns}:decoy_minor_params"))
    rng_overdue = random.Random(blind_subseed(seed, f"{ns}:decoy_overdue_params"))

    minor_range = decoy_ranges["minor_delay_ratio_range"]
    minor_ratio = rng_select.uniform(float(minor_range["min"]), float(minor_range["max"]))
    minor_count = min(round(len(candidates) * minor_ratio), len(eligible))
    minor_items = rng_select.sample(eligible, minor_count) if minor_count > 0 else []

    remaining = [item_id for item_id in eligible if item_id not in minor_items]

    target_ratio = float(decoy_ranges["safe_overdue_target_ratio"])
    target_count = round(len(candidates) * target_ratio)
    overdue_count = min(target_count, len(remaining))
    overdue_items = rng_select.sample(remaining, overdue_count) if overdue_count > 0 else []

    for item_id in minor_items:
        _apply_minor_delay_decoy(conn, item_id, seed, base_date, days, decoy_ranges, rng_minor)

    offset_range = decoy_ranges["target_offset_days_range"]
    for item_id in overdue_items:
        offset = rng_overdue.randint(int(offset_range["min"]), int(offset_range["max"]))
        target_expected = base_date + timedelta(days=offset)
        _apply_safe_overdue_decoy(conn, item_id, seed, target_expected)

    return DecoyReport(
        candidate_count=len(candidates),
        eligible_count=len(eligible),
        minor_delay_count=len(minor_items),
        safe_overdue_count=len(overdue_items),
    )


# ---------------------------------------------------------------------------
# ScenarioConfig → YAML 직렬화(inject_scenarios는 경로만 받으므로 임시 파일에 쓴다)
# ---------------------------------------------------------------------------


def _scenario_to_dict(sc: Scenario) -> dict[str, Any]:
    return {
        "scenario_id": sc.scenario_id,
        "item_id": sc.item_id,
        "type": sc.type,
        "reference": sc.reference,
        "params": sc.params,
    }


def scenario_config_to_dict(cfg: ScenarioConfig) -> dict[str, Any]:
    return {
        "version": cfg.version,
        "base_date": cfg.base_date,
        "timeline_start": cfg.timeline_start,
        "scenarios": [_scenario_to_dict(sc) for sc in cfg.scenarios],
    }


def write_scenario_config_yaml(cfg: ScenarioConfig, dest: str | Path) -> None:
    dest = Path(dest)
    dest.write_text(
        yaml.safe_dump(scenario_config_to_dict(cfg), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 대응 이력 시드 적재(표준 generate_dataset.py의 apply_history_seed와 동일 책임).
#
# scripts/generate_dataset.py를 import하지 않는다 — scripts/datagen/은 scripts/ 상위
# 계층에 의존하지 않는다(의존 방향 유지, 브리프 "scripts/datagen/ 계층" 원칙). 대신 같은
# 재사용 가능 원시 함수(inject.load_action_history_seed·baseline.compute_content_hash)로
# 동일 동작을 이 계층 안에서 재구성한다 — generate_dataset.apply_history_seed와 동작은
# 같지만 코드를 공유하지 않는 것은 로직 복제가 아니라 계층 경계 유지다(둘 다 3~4줄짜리
# 얇은 오케스트레이션이고, 실제 로직은 두 원시 함수에 있다).
# ---------------------------------------------------------------------------


def _apply_action_history_seed(out_path: Path, csv_path: Path) -> str:
    conn = sqlite3.connect(out_path)
    try:
        inject.load_action_history_seed(conn, csv_path)
        new_hash = baseline.compute_content_hash(conn)
        with conn:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'content_hash'", (new_hash,))
    finally:
        conn.close()
    return new_hash


# ---------------------------------------------------------------------------
# sha256 · manifest 읽기/쓰기(append, 결정적 정렬 — seed 오름차순, upsert)
# ---------------------------------------------------------------------------


def _sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_manifest(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {"version": 1, "runs": []}
    return json.loads(path.read_text(encoding="utf-8"))


def upsert_manifest_entry(manifest: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """entry를 seed 기준으로 upsert(동일 seed면 교체)하고 seed 오름차순으로 정렬해
    반환한다(순수 함수 — 호출부가 파일에 쓴다).

    **runs 밖의 최상위 키는 그대로 보존한다**(Task S-30c). manifest.json에는 생성기가
    쓰지 않는 봉인 증거가 함께 산다 — S-30 1차 블라인드의 `predictions` 절(예측 파일
    sha256, 커밋으로 봉인된 감사 증거)이 그렇다. 옛 구현은 반환 dict를 version·runs로만
    재구성해 그런 키를 조용히 날렸다. 2차 회차를 생성하면 1차 봉인 기록이 사라지는
    셈이라(측정 결과 자체는 못 바꾸지만 증거는 잃는다) 보존으로 고친다.
    """
    runs = [r for r in manifest.get("runs", []) if r["seed"] != entry["seed"]]
    runs.append(entry)
    runs.sort(key=lambda r: r["seed"])
    preserved = {k: v for k, v in manifest.items() if k not in ("version", "runs")}
    return {"version": manifest.get("version", 1), "runs": runs, **preserved}


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _has_delayed_arrival(out_path: Path) -> bool:
    conn = sqlite3.connect(out_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM incoming_shipments"
            " WHERE actual_date IS NOT NULL AND expected_date IS NOT NULL"
            " AND actual_date > expected_date"
        ).fetchone()
        return bool(row[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 오케스트레이션
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlindResult:
    """generate_blind의 반환값 — CLI 출력·테스트 검증용. 라벨 본문(item_id·날짜 등)은
    담지 않는다(봉인 규약: 라벨은 sealed_dir의 파일로만 존재)."""

    seed: int
    summary: baseline.GenerationSummary
    db_path: Path
    db_sha256: str
    labels_path: Path
    labels_sha256: str
    has_delayed_arrival: bool
    attempts_used: int
    manifest_entry: dict[str, Any]


def generate_blind(
    ranges_path: str | Path,
    seed: int,
    base_date: str | date,
    out_path: str | Path,
    *,
    reference_dir: str | Path = baseline.DEFAULT_REFERENCE_DIR,
    schema_path: str | Path = baseline.DEFAULT_SCHEMA_PATH,
    sealed_dir: str | Path = DEFAULT_SEALED_DIR,
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    action_history_csv: str | Path = DEFAULT_ACTION_HISTORY_SEED_PATH,
) -> BlindResult:
    """블라인드 스냅샷 1건을 결정적으로 생성·봉인한다.

    범위 YAML에서 seed로 파라미터를 뽑아(random.Random 서브시드) 기존
    inject.inject_scenarios(min_scenarios_per_type=1)로 베이스라인+주입+해시+라벨 도출까지
    전부 위임한 뒤, 대응 이력 시드를 적재하고(표준 전체 빌드와 동일한 책임), 라벨을
    sealed_dir에 분리 저장하고 manifest_path를 갱신한다.

    out_path에는 라벨·시나리오 흔적이 전혀 남지 않는다(표준 원칙 그대로 — schema.sql에
    'scenario' 컬럼이 없다). 카탈로그는 서브셋하지 않는다(표준과 동일하게 전체 품목을
    쓴다 — action_history_seed.csv가 참조하는 8개 품목이 항상 존재해야 하기 때문이다).

    물리적으로 무효과인 조합(임의 품목이라 표준처럼 사람이 미세조정할 수 없다)은
    ranges["max_generation_attempts"]까지 다른 서브시드 네임스페이스로 재시도한다(같은
    seed에 대해 재시도 시퀀스 자체가 결정적이라 최종 결과도 seed에 대해 결정적이다).
    """
    ranges = load_ranges(ranges_path)
    base_date_obj = date.fromisoformat(base_date) if isinstance(base_date, str) else base_date
    out_path = Path(out_path)
    reference_dir = Path(reference_dir)
    items_csv_path = reference_dir / "items_master.csv"

    # baseline.generate_baseline·inject.inject_scenarios와 동일한 공식(re-derive) — 미끼
    # 주입(inject_decoys)이 같은 365일 타임라인으로 재시뮬레이션해야 한다.
    timeline_start = base_date_obj - timedelta(days=364)
    days = [timeline_start + timedelta(days=i) for i in range(365)]

    total_items = len(_load_item_ids(items_csv_path))
    _validate_normal_item_count(ranges, total_items)
    _validate_offset_ranges(ranges)

    # S-30c A: 배치 구간을 측정 창과 결합한다 — 라벨 전건의 품절일이 관측 가능 창 안에
    # 들 때까지 재추첨하고, 미끼 적격성은 스윕 구간 최저 커버리지로 잰다.
    observable_window, sweep_window = measurement_windows(ranges, base_date_obj)

    max_attempts = int(ranges.get("max_generation_attempts", 1))
    summary: baseline.GenerationSummary | None = None
    labels: list[dict[str, object]] | None = None
    cfg: ScenarioConfig | None = None
    last_error: inject.IneffectiveInjectionError | None = None
    attempt = 0

    for attempt in range(max_attempts):
        cfg = build_blind_config(
            ranges, seed, base_date_obj, items_csv=items_csv_path, attempt=attempt
        )
        with tempfile.TemporaryDirectory(prefix="blind-config-") as tmp_dir:
            config_yaml_path = Path(tmp_dir) / "blind_scenario_config.yaml"
            write_scenario_config_yaml(cfg, config_yaml_path)
            try:
                summary, labels = inject.inject_scenarios(
                    out_path,
                    seed=seed,
                    base_date=base_date_obj,
                    scenario_config_path=config_yaml_path,
                    reference_dir=reference_dir,
                    schema_path=schema_path,
                    min_scenarios_per_type=1,
                    observable_window=observable_window,
                )
                last_error = None
                break
            except inject.IneffectiveInjectionError as exc:
                # F7(S-22 픽스 라운드 1, 컨트롤러 리뷰): 재시도 대상은 "이 임의 조합이
                # 우연히 무효과였다"뿐이다 — config 구조 위반·효과 해석 구현 오류·halt
                # 복원일 역전 등 나머지 ValueError는 여기서 잡지 않고 즉시 전파된다(재시도가
                # 코드 결함을 조용히 숨기지 않게 하기 위함).
                last_error = exc
                continue

    if last_error is not None or summary is None or labels is None or cfg is None:
        # F9(S-22 픽스 라운드 1, 컨트롤러 리뷰): 마지막 시도의 baseline.generate_baseline이
        # out_path에 파일을 남긴 채로 실패했을 수 있다 — 호출부가 "예외가 났으니 결과 없음"
        # 이라고 믿을 수 있도록, 소진 실패 시 그 잔여 파일을 정리한다.
        if out_path.exists():
            out_path.unlink()
        raise ValueError(
            f"블라인드 시나리오 생성이 {max_attempts}회 재시도 후에도 실패했다"
            f"(seed={seed}): {last_error}"
        ) from last_error

    new_hash = _apply_action_history_seed(out_path, Path(action_history_csv))
    summary = dataclasses.replace(summary, content_hash=new_hash)

    # F1(S-22 픽스 라운드 1, 컨트롤러 리뷰 — M-30 전 필수): 공식 시나리오 4개를 제외한
    # 정상 품목 일부에 미끼를 심는다. 이후 content_hash를 다시 재계산해야 한다(미끼가
    # stock_usage_daily·incoming_shipments를 바꿨으므로 — F2와 동일한 이유로 "해시는 항상
    # 마지막에" 원칙을 지킨다).
    official_item_ids = {sc.item_id for sc in cfg.scenarios}
    decoy_conn = sqlite3.connect(out_path)
    try:
        decoy_report = inject_decoys(
            decoy_conn, ranges, seed, attempt, base_date_obj, days, official_item_ids,
            sweep_window,
        )
        final_hash = baseline.compute_content_hash(decoy_conn)
        with decoy_conn:
            decoy_conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'content_hash'", (final_hash,)
            )
    finally:
        decoy_conn.close()
    summary = dataclasses.replace(summary, content_hash=final_hash)

    sealed_dir = Path(sealed_dir)
    sealed_dir.mkdir(parents=True, exist_ok=True)
    labels_path = sealed_dir / f"{out_path.stem}.labels.json"
    labels_path.write_text(labels_mod.labels_to_json(labels), encoding="utf-8")

    db_sha256 = _sha256_file(out_path)
    labels_sha256 = _sha256_file(labels_path)
    has_delayed_arrival = _has_delayed_arrival(out_path)

    type_counts = {t: 0 for t in ALLOWED_TYPES}
    for lbl in labels:
        type_counts[lbl["scenario_type"]] += 1

    params_summary = {
        "seed": seed,
        "base_date": base_date_obj.isoformat(),
        "item_count": summary.item_count,
        "scenario_item_count": len(labels),
        "scenario_type_counts": type_counts,
        "has_delayed_arrival_arm": has_delayed_arrival,
        "attempts_used": attempt + 1,
        # F1(픽스 라운드 1): 개수만 담는다 — 어떤 품목이 미끼를 받았는지는 비노출.
        # S-30c A: 적격성이 두 미끼 공통이라 eligible_count 하나로 합쳤다.
        "decoy_counts": {
            "candidate_count": decoy_report.candidate_count,
            "eligible_count": decoy_report.eligible_count,
            "minor_delay_count": decoy_report.minor_delay_count,
            "safe_overdue_count": decoy_report.safe_overdue_count,
        },
        # S-30c A: 이 회차가 어떤 측정 창에 결합돼 생성됐는지 감사용으로 남긴다. 라벨
        # 누출이 아니다 — blind_ranges.yaml(커밋 대상)에 이미 공개된 설계 파라미터다.
        "observable_window": {
            "start": observable_window[0].isoformat(),
            "end": observable_window[1].isoformat(),
        },
    }
    entry = {
        "seed": seed,
        "db_file": out_path.name,
        "db_sha256": db_sha256,
        "labels_file": labels_path.name,
        "labels_sha256": labels_sha256,
        # F3(픽스 라운드 1, 컨트롤러 리뷰): 부트스트랩 원천 7테이블만 해싱하는 앵커라
        # 위험 평가 배치가 risk_results·forecasts·alerts를 추가해도 안 변한다 — db_sha256은
        # 파일 전체 바이트라 배치 실행 이후에는 더 이상 봉인 검증에 못 쓴다(생성 직후
        # 결속 확인용으로만 유지).
        "content_hash": summary.content_hash,
        "params_summary": params_summary,
    }

    manifest_path = Path(manifest_path)
    manifest = load_manifest(manifest_path)
    manifest = upsert_manifest_entry(manifest, entry)
    write_manifest(manifest_path, manifest)

    return BlindResult(
        seed=seed,
        summary=summary,
        db_path=out_path,
        db_sha256=db_sha256,
        labels_path=labels_path,
        labels_sha256=labels_sha256,
        has_delayed_arrival=has_delayed_arrival,
        attempts_used=attempt + 1,
        manifest_entry=entry,
    )
