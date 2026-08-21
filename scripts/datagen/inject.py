"""결정적 시나리오 주입기 — 베이스라인 위에 20건 품절 시나리오를 결정적으로 얹는다.

**격리 원칙**: baseline.py와 동일하게 이 모듈은 `medsupply` 패키지를 일절 import하지
않는다(schema.sql 파일을 직접 읽어 적용하는 방식 유지). 시나리오 config(`config.py`,
`data/scenarios/scenario_config.yaml`)는 이 모듈과 라벨 도출(`labels.py`)에서만 읽는다.

**서브시드**: 브리프 계약상 `f"{seed}:inject:{scenario_id}"`가 주입기 서브시드 네임스페이스다
(SUBSEED_TEMPLATE). v1 규칙에서 주입 자체는 결정적 계수·날짜 적용이라 이 서브시드로 추가
난수를 뽑지는 않지만, 이름공간을 고정해두면 향후 확률적 요소가 추가되어도 베이스라인
노이즈(`item_subseed`)와 우연히 겹치지 않는다.

**재시뮬레이션 방식**: 베이스라인 생성(`baseline.generate_baseline`) 이후, 시나리오 품목
20개에 한해 stock_usage_daily·incoming_shipments 행을 처음부터 다시 시뮬레이션해
교체한다. `simulate_item_with_scenario`는 baseline.py의 `_simulate_item`과 동일한 서브시드
공식·재고 항등식·1-pack 하한 로직을 baseline의 헬퍼(item_subseed·supplier_lead_time·
_weekly_factor·_seasonal_factor·_round_to_pack·_ceil_to_pack 등)를 그대로 재사용해
구현한 파라미터화 버전이다(baseline.py 자체는 수정하지 않는다 — 계약 동결 대상 밖).
효과가 전혀 없으면 baseline._simulate_item과 바이트 동일한 결과를 낸다. 시나리오 품목이
아닌 나머지는 재시뮬레이션 대상이 아니므로 베이스라인 결과와 완전히 동일하게 남는다.
"""

from __future__ import annotations

import csv
import hashlib
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from scripts.datagen import baseline, config
from scripts.datagen import labels as labels_mod
from scripts.datagen.baseline import GenerationSummary
from scripts.datagen.config import Scenario, ScenarioConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIO_CONFIG_PATH = REPO_ROOT / "data" / "scenarios" / "scenario_config.yaml"


class IneffectiveInjectionError(ValueError):
    """뽑힌 (품목, 파라미터) 조합이 물리적으로 무효과일 때만 발생한다(예: halt 기간이 너무
    짧아 재고가 재주문점 밑으로 안 내려가 미이행 발주가 0건, demand_surge가 반사실과 사용량
    차이를 못 만듦). ValueError의 서브클래스라 기존 `pytest.raises(ValueError, ...)` 호출부와
    하위 호환된다.

    Task S-22 픽스 라운드 1 F7(컨트롤러 리뷰): generate_blind의 재시도 루프는 이 예외만
    좁혀서 잡는다 — config 구조 위반(config.validate_scenario_config)·효과 해석 구현
    오류("구현 오류" 문구가 붙은 assert들)·halt 복원일 역전 등은 계속 일반 ValueError로
    남겨 즉시 실패시킨다. 재시도로 감싸도 되는 것은 "이 임의 품목·파라미터 조합이 우연히
    안 먹혔다"뿐이며, 코드 결함까지 재시도가 조용히 삼켜 회귀를 숨기면 안 되기 때문이다.
    """

#: 주입기 서브시드 네임스페이스(브리프 계약). 현재는 결정적 계수 적용뿐이라 값을 직접
#: 소비하지는 않지만, 향후 확률적 요소 도입 시 이 공식으로 뽑아야 한다.
SUBSEED_TEMPLATE = "{seed}:inject:{scenario_id}"


def injection_subseed(seed: int, scenario_id: str) -> int:
    """시나리오별 주입 서브시드 = sha256(f"{seed}:inject:{scenario_id}") 앞 8자리 hex."""
    digest = hashlib.sha256(
        SUBSEED_TEMPLATE.format(seed=seed, scenario_id=scenario_id).encode("utf-8")
    ).hexdigest()
    return int(digest[:8], 16)


# ---------------------------------------------------------------------------
# 효과 모델 — 유형별 주입 규칙(브리프 §주입 규칙)의 내부 표현
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemandEffect:
    """사용량 배수 효과. ramp_days<=0이면 start부터 즉시 peak_multiplier(계단 함수) —
    supply_halt의 demand_shift_multiplier가 이 형태로 표현된다."""

    start: date
    ramp_days: int
    peak_multiplier: float
    sustain: bool


@dataclass(frozen=True)
class HaltEffect:
    """start(halt_start_date) 이후 expected_date가 도래하는 모든 발주가 미이행된다."""

    start: date


@dataclass(frozen=True)
class DelayEffect:
    """order_date에 생성되는 특정 발주 1건만 미이행 상태로 만든다.

    qty_ratio가 있으면 expected_qty를 reorder_qty*qty_ratio(pack 단위 올림)로 줄인다.
    delay_days만큼 대기한 뒤(release_day = expected_date + delay_days) 재시도 슬롯을
    풀어준다 — arrives_late=False(기본값)면 그 특정 발주 자체는 관측 구간 내내
    미이행(actual_date=NULL)으로 남지만, 이후 재고가 다시 ROP 밑으로 떨어지면 새 발주를
    시도할 수 있다(halt와 달리 영구 봉쇄가 아니다). expected_date는 실제로 막힌 그 발주의
    예정일(대상 선정 결과)이다 — scenario_config.yaml의 expected_date 파라미터는 S-03에서
    베이스라인 존재 이전에 정해진 "지정" 값이라 실제 발주 리듬과 정확히 일치하지 않을 수
    있고(브리프: "가장 가까운 주문"), 라벨의 onset_date는 이 실제 값을 써야 onset<stockout이
    항상 성립한다.

    arrives_late(Task S-22): True면 release_day에 그 발주가 결국 도착한다(actual_date=
    release_day, actual_qty=expected_qty, status="입고 완료") — expected_date < actual_date인
    행이 생긴다. 2주차 리뷰 F6 이월: 표준 스냅샷은 이 arm이 0건이라
    medsupply.analytics.asof의 as_of 기준 재구성(S-17d, "미래 도착 스탬프로 과거 시점을
    소급 왜곡하지 않는다")을 실데이터로 검증한 적이 없었다 — 블라인드 생성기가 이 공백을
    메운다. 기본값 False는 종전 동작(영구 미이행) 그대로다.
    """

    order_date: date
    expected_date: date
    delay_days: int
    qty_ratio: float | None
    arrives_late: bool = False


def _combined_demand_multiplier(d: date, effects: tuple[DemandEffect, ...]) -> float:
    multiplier = 1.0
    for eff in effects:
        multiplier *= _single_demand_multiplier(d, eff)
    return multiplier


def _single_demand_multiplier(d: date, eff: DemandEffect) -> float:
    if d < eff.start:
        return 1.0
    if eff.ramp_days > 0:
        ramp_end = eff.start + timedelta(days=eff.ramp_days)
        if d < ramp_end:
            t = (d - eff.start).days / eff.ramp_days
            return 1.0 + t * (eff.peak_multiplier - 1.0)
    return eff.peak_multiplier if eff.sustain else 1.0


def _validate_halt_restart(scenario_id: str, halt_start: date, expected_restart: date | None) -> None:
    """expected_restart_date가 있으면 halt_start_date 이후인지 검증한다(1주차 인계 사항).

    위반 시 ValueError로 명확히 실패한다(브리프: "위반 시 명확한 에러").
    """
    if expected_restart is not None and expected_restart <= halt_start:
        raise ValueError(
            f"{scenario_id}: expected_restart_date({expected_restart.isoformat()})가"
            f" halt_start_date({halt_start.isoformat()}) 이후가 아니다"
        )


# ---------------------------------------------------------------------------
# 품목 시뮬레이션(baseline._simulate_item의 파라미터화 버전)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioTrace:
    """simulate_item_with_scenario의 4번째 반환값 — 무엇이 왜 막혔는지의 부기 정보.

    stock_rows·shipment_rows(1~3번째 반환값)에는 절대 섞지 않는다 — baseline._simulate_item과
    반환 형식을 바이트 단위로 동일하게 유지해야 무효과 동등성(1주차 리뷰 F8)이 성립한다.
    halt_blocked_order_dates·delay_blocked_order_dates는 각각 halt·delay 때문에
    미이행으로 남은 발주의 order_date(ISO 문자열) 집합이다(교집합 가능 — 동시에 해당될 수
    있음). 효과별 실효성 검증(F1)과 라벨 외삽에서 "실제로 막힌 발주" 판별(F3)에 쓰인다.
    """

    halt_blocked_order_dates: frozenset[str]
    delay_blocked_order_dates: frozenset[str]


@dataclass(frozen=True)
class ForcedArrival:
    """day-loop 밖(자연 발주 상태기계 밖)에서 합성된 발주(delivery_delay 강제 생성 arm)가
    그래도 실제로 도착할 때, 그 수량을 재고 궤적(stock_usage_daily)에 credit하기 위한 스펙
    (Task S-22 픽스 라운드 1 F2 — 컨트롤러 리뷰).

    강제 생성 발주는 자연 발주 상태기계(pending_row)를 거치지 않고 _resolve_effects가
    바로 shipment 행을 합성한다(기존 설계 — 재고 궤적에 원래 영향이 없다). arrives_late=True
    강제 발주는 incoming_shipments에 actual_date·actual_qty를 채우면서도 stock_usage_daily가
    그 수량을 몰랐다 — "장부에는 도착했다고 적혀 있는데 재고 궤적에는 반영되지 않은" 유령
    입고 모순이었다(validate_dataset.py 검사 11이 잡는다). ForcedArrival은 이 수량을 day-loop
    에 직접 주입해 두 장부가 항상 일치하게 만든다(사후 SQL 패치 금지 — day-loop 계층 처리).
    """

    date: date
    qty: int


def simulate_item_with_scenario(
    item: dict[str, str],
    seed: int,
    days: list[date],
    *,
    demand_effects: tuple[DemandEffect, ...] = (),
    halt_effects: tuple[HaltEffect, ...] = (),
    delay_effects: tuple[DelayEffect, ...] = (),
    forced_arrivals: tuple[ForcedArrival, ...] = (),
) -> tuple[list[dict[str, object]], list[dict[str, object]], int, ScenarioTrace]:
    """단일 품목의 시나리오 반영 일별 재고·사용량·발주 시뮬레이션(날짜순 진행).

    baseline._simulate_item과 동일한 서브시드 공식·노이즈 시퀀스·재고 항등식·1-pack
    하한 로직을 쓰되, 사용량 배수(demand_effects)와 미이행 발주 규칙(halt_effects·
    delay_effects)을 얹는다. 네 효과(forced_arrivals 포함)가 모두 비어 있으면 앞 3개
    반환값(stock_rows·shipment_rows·truncation_count)은 baseline._simulate_item과 바이트
    동일하다(1주차 리뷰 F8 — 무효과 동등성 회귀 테스트로 고정). 4번째 반환값(ScenarioTrace)은
    baseline._simulate_item에는 없는 이 함수만의 부기 정보다.

    forced_arrivals(Task S-22 F2): 지정된 date에 qty만큼을 그날의 incoming에 가산한다 —
    자연 발주 상태기계(pending_row)와 완전히 독립적으로 더해진다(동시에 자연 입고가 있어도
    합산). usage는 그날의 incoming을 더하기 전 stock으로 계산하므로(기존 규칙 그대로)
    forced_arrivals는 같은 날짜의 절삭(truncation)에는 영향을 주지 않고, closing_stock과
    그 이후 날짜들의 진행에만 반영된다.
    """
    item_id = item["item_id"]
    pack_size = int(item["pack_size"])
    supplier = item["supplier"]
    atc_code = item["atc_code"]

    rng = random.Random(baseline.item_subseed(seed, item_id))

    lo, hi = baseline._BASE_USAGE_RANGES[baseline._form_bucket(item["form"])]
    base_usage = round(rng.uniform(lo, hi), 1)

    lead_time = baseline.supplier_lead_time(supplier)
    rop = base_usage * (lead_time + 7)
    reorder_qty = baseline._ceil_to_pack(base_usage * 30, pack_size)

    initial_days = rng.randint(20, 35)
    stock = baseline._round_to_pack(base_usage * initial_days, pack_size)

    stock_rows: list[dict[str, object]] = []
    shipment_rows: list[dict[str, object]] = []

    pending_row: dict[str, object] | None = None
    pending_expected: date | None = None
    pending_blocked = False
    pending_release_day: date | None = None
    pending_arrives_late = False

    truncation_count = 0
    halt_blocked_order_dates: set[str] = set()
    delay_blocked_order_dates: set[str] = set()

    forced_arrival_by_date: dict[date, int] = {}
    for fa in forced_arrivals:
        forced_arrival_by_date[fa.date] = forced_arrival_by_date.get(fa.date, 0) + fa.qty

    for d in days:
        noise = max(0.5, rng.gauss(1.0, 0.12))
        multiplier = _combined_demand_multiplier(d, demand_effects)
        raw_usage = round(
            base_usage
            * baseline._weekly_factor(d)
            * baseline._seasonal_factor(d, atc_code)
            * noise
            * multiplier
        )
        raw_usage = max(0, raw_usage)

        if raw_usage > stock:
            usage = stock
            truncation_count += 1
        else:
            usage = raw_usage

        incoming = 0
        if pending_row is not None and pending_expected == d and not pending_blocked:
            incoming = int(pending_row["expected_qty"])  # type: ignore[arg-type]
            pending_row["actual_date"] = d.isoformat()
            pending_row["actual_qty"] = incoming
            pending_row["status"] = "입고 완료"
            pending_row = None
            pending_expected = None
            pending_arrives_late = False
        elif (
            pending_row is not None
            and pending_blocked
            and pending_arrives_late
            and pending_release_day is not None
            and d == pending_release_day
        ):
            # S-22 지연 '도착' arm: 막혔던 발주가 release_day에 결국 도착한다(expected_date
            # < actual_date인 행). 아래 "포기" 분기와 달리 incoming을 실제로 credit한다.
            incoming = int(pending_row["expected_qty"])  # type: ignore[arg-type]
            pending_row["actual_date"] = d.isoformat()
            pending_row["actual_qty"] = incoming
            pending_row["status"] = "입고 완료"
            pending_row = None
            pending_expected = None
            pending_blocked = False
            pending_release_day = None
            pending_arrives_late = False

        if (
            pending_row is not None
            and pending_blocked
            and pending_release_day is not None
            and d >= pending_release_day
        ):
            pending_row = None
            pending_expected = None
            pending_blocked = False
            pending_release_day = None
            pending_arrives_late = False

        forced_qty = forced_arrival_by_date.get(d)
        if forced_qty is not None:
            # F2(S-22 픽스 라운드 1): 자연 발주 상태기계와 독립적으로 그날의 incoming에
            # 가산한다 — 강제 생성 발주의 도착 수량이 재고 궤적에도 반영되게 한다.
            incoming += forced_qty

        stock = stock - usage + incoming

        stock_rows.append(
            {
                "item_id": item_id,
                "date": d.isoformat(),
                "usage_qty": usage,
                "incoming_qty": incoming,
                "closing_stock": stock,
            }
        )

        if stock < rop and pending_row is None:
            expected_date = d + timedelta(days=lead_time)
            expected_qty = reorder_qty
            release_day: date | None = None
            order_date_str = d.isoformat()

            blocked_by_halt = any(expected_date >= h.start for h in halt_effects)
            if blocked_by_halt:
                release_day = expected_date
                halt_blocked_order_dates.add(order_date_str)

            delay_eff = next((e for e in delay_effects if e.order_date == d), None)
            if delay_eff is not None:
                delay_blocked_order_dates.add(order_date_str)
                if delay_eff.qty_ratio is not None:
                    # F7(1주차 리뷰): _ceil_to_pack은 감축분을 다음 pack 배수로 올림
                    # 처리해 감축이 흡수될 수 있다(예: 300개 1팩 품목에서 0.7배가 다시
                    # 300으로 올림). _round_to_pack(하한 1팩)을 써야 실제로 줄어든다.
                    expected_qty = baseline._round_to_pack(
                        reorder_qty * delay_eff.qty_ratio, pack_size
                    )
                release_day = expected_date + timedelta(days=delay_eff.delay_days)

            new_row: dict[str, object] = {
                "item_id": item_id,
                "order_date": order_date_str,
                "expected_date": expected_date.isoformat(),
                "expected_qty": int(expected_qty),
                "actual_date": None,
                "actual_qty": None,
                "status": "입고 예정",
            }
            shipment_rows.append(new_row)
            pending_row = new_row
            pending_expected = expected_date
            pending_blocked = blocked_by_halt or (delay_eff is not None)
            pending_release_day = release_day
            pending_arrives_late = bool(delay_eff is not None and delay_eff.arrives_late)

    trace = ScenarioTrace(
        halt_blocked_order_dates=frozenset(halt_blocked_order_dates),
        delay_blocked_order_dates=frozenset(delay_blocked_order_dates),
    )
    return stock_rows, shipment_rows, truncation_count, trace


# ---------------------------------------------------------------------------
# 시나리오 → 효과 변환(composite는 sub_scenarios를 순서대로 중첩 적용)
# ---------------------------------------------------------------------------


def _sub_specs(sc: Scenario) -> list[dict]:
    if sc.type == "composite":
        return list(sc.params["sub_scenarios"])
    return [{"type": sc.type, "params": sc.params}]


def _closest_order(
    shipment_rows: list[dict[str, object]], target_expected: date
) -> dict[str, object] | None:
    """expected_date가 target_expected에 가장 가까운 발주 행을 반환한다(동률은 이른 order_date).

    발주가 하나도 없으면 None을 반환한다(호출부가 강제 생성 경로로 분기하도록).
    """
    if not shipment_rows:
        return None

    def _key(row: dict[str, object]) -> tuple[int, str]:
        expected = date.fromisoformat(str(row["expected_date"]))
        return abs((expected - target_expected).days), str(row["order_date"])

    return min(shipment_rows, key=_key)


#: delivery_delay "가장 가까운 주문"의 최대 허용 이탈(일). 이보다 멀면 자연 발주 리듬에
#: 대상이 없다고 보고 두 번째 arm(강제 생성)으로 분기한다(1주차 리뷰 F4).
DELIVERY_DELAY_MAX_OFFSET_DAYS = 7


def _item_reorder_profile(item: dict[str, str], seed: int) -> tuple[int, int]:
    """(reorder_qty, lead_time) — baseline._simulate_item·simulate_item_with_scenario와
    동일한 결정적 공식(순수 조회, 시뮬레이션 없이 재사용). delivery_delay 강제 생성
    (F4)이 발주 시퀀스를 처음부터 돌리지 않고도 reorder_qty·lead_time만 알아야 할 때 쓴다.
    """
    pack_size = int(item["pack_size"])
    rng = random.Random(baseline.item_subseed(seed, item["item_id"]))
    lo, hi = baseline._BASE_USAGE_RANGES[baseline._form_bucket(item["form"])]
    base_usage = round(rng.uniform(lo, hi), 1)
    lead_time = baseline.supplier_lead_time(item["supplier"])
    reorder_qty = baseline._ceil_to_pack(base_usage * 30, pack_size)
    return reorder_qty, lead_time


def _resolve_effects(
    sc: Scenario, item_row: dict[str, str], seed: int, days: list[date]
) -> tuple[
    tuple[DemandEffect, ...],
    tuple[HaltEffect, ...],
    tuple[DelayEffect, ...],
    list[dict[str, object]],
    tuple[ForcedArrival, ...],
    date,
]:
    """시나리오 1건(composite는 하위 요소 전체)을 효과 리스트 + 강제 발주 + 실제
    onset_date로 변환한다. 반환: (demand_effects, halt_effects, delay_effects,
    forced_shipment_rows, forced_arrivals, onset_date).

    forced_arrivals(Task S-22 F2): 강제 생성 발주 중 arrives_late=True인 것의 도착 스펙.
    호출부가 이걸 simulate_item_with_scenario(forced_arrivals=...)에 그대로 넘겨야
    incoming_shipments의 도착 기록과 stock_usage_daily의 재고 궤적이 일치한다(유령 입고
    금지).

    delivery_delay 요소는 "지정 expected_date에 가장 가까운 주문"을 찾아야 하므로, 그
    시점까지 이미 확정된 효과(이전 sub_scenario들의 demand/halt)를 반영한 드라이런
    시뮬레이션을 1회 수행해 대상 발주를 특정한다. 가장 가까운 주문도
    DELIVERY_DELAY_MAX_OFFSET_DAYS(7일)보다 멀면(또는 발주가 아예 없으면) 브리프의
    두 번째 arm("또는 그 시점의 신규 주문")을 적용한다 — 지정 expected_date에 신규
    발주를 강제로 만들어 그것을 대상으로 삼는다(1주차 리뷰 F4). 강제 발주는 day-loop의
    "동시 대기 발주 1건" 상태기계를 건드리지 않는다 — 어차피 영원히 미이행이라 재고
    궤적(stock_usage_daily)에 아무 영향이 없고, 자연 발주 흐름과 독립적으로 존재한다.

    반환하는 onset_date는 유형별 앵커 날짜(demand_surge=surge_start_date,
    supply_halt=halt_start_date)이되, delivery_delay만 "지정 expected_date"가 아니라
    실제로 대상이 된 발주(자연 매칭이든 강제 생성이든)의 expected_date를 쓴다 — S-03
    시점에는 베이스라인이 없어 지정값이 실제 발주 리듬과 어긋날 수 있고, 라벨의
    onset<stockout 불변식은 실제로 주입된 사건 기준으로만 성립하기 때문이다. composite는
    하위 요소들의 (보정된) 앵커 날짜 중 최솟값을 쓴다.
    """
    demand_effects: list[DemandEffect] = []
    halt_effects: list[HaltEffect] = []
    delay_effects: list[DelayEffect] = []
    forced_rows: list[dict[str, object]] = []
    forced_arrivals: list[ForcedArrival] = []
    resolved_onsets: list[date] = []

    for sub in _sub_specs(sc):
        sub_type = sub["type"]
        params = sub["params"]

        if sub_type == "demand_surge":
            start = date.fromisoformat(str(params["surge_start_date"]))
            demand_effects.append(
                DemandEffect(
                    start=start,
                    ramp_days=int(params["ramp_days"]),
                    peak_multiplier=float(params["peak_multiplier"]),
                    sustain=bool(params["sustain"]),
                )
            )
            resolved_onsets.append(start)

        elif sub_type == "supply_halt":
            halt_start = date.fromisoformat(str(params["halt_start_date"]))
            restart_raw = params.get("expected_restart_date")
            restart = date.fromisoformat(str(restart_raw)) if restart_raw else None
            _validate_halt_restart(sc.scenario_id, halt_start, restart)
            halt_effects.append(HaltEffect(start=halt_start))
            resolved_onsets.append(halt_start)

            shift = params.get("demand_shift_multiplier")
            if shift is not None:
                demand_effects.append(
                    DemandEffect(start=halt_start, ramp_days=0, peak_multiplier=float(shift), sustain=True)
                )

        elif sub_type == "delivery_delay":
            target_expected = date.fromisoformat(str(params["expected_date"]))
            _dry_stock, dry_shipments, _dry_trunc, _dry_trace = simulate_item_with_scenario(
                item_row,
                seed,
                days,
                demand_effects=tuple(demand_effects),
                halt_effects=tuple(halt_effects),
                delay_effects=tuple(delay_effects),
                forced_arrivals=tuple(forced_arrivals),
            )
            qty_ratio = params.get("qty_ratio")
            qty_ratio_f = float(qty_ratio) if qty_ratio is not None else None
            delay_days = int(params["delay_days"])
            # S-22 지연 '도착' arm: True면 이 발주는 release_day(expected_date+delay_days)에
            # 결국 도착한다(expected_date < actual_date). 기본 False는 종전처럼 영구 미이행.
            arrives_late = bool(params.get("arrives_late", False))

            matched = _closest_order(dry_shipments, target_expected)
            offset = (
                abs((date.fromisoformat(str(matched["expected_date"])) - target_expected).days)
                if matched is not None
                else None
            )

            if matched is not None and offset is not None and offset <= DELIVERY_DELAY_MAX_OFFSET_DAYS:
                matched_order_date = date.fromisoformat(str(matched["order_date"]))
                matched_expected_date = date.fromisoformat(str(matched["expected_date"]))
                delay_effects.append(
                    DelayEffect(
                        order_date=matched_order_date,
                        expected_date=matched_expected_date,
                        delay_days=delay_days,
                        qty_ratio=qty_ratio_f,
                        arrives_late=arrives_late,
                    )
                )
                resolved_onsets.append(matched_expected_date)
            else:
                # 두 번째 arm(1주차 리뷰 F4): ±7일 내 자연 발주가 없다 — 지정
                # expected_date에 신규 발주를 강제 생성해 그것을 미이행 대상으로 삼는다.
                reorder_qty, item_lead_time = _item_reorder_profile(item_row, seed)
                forced_qty = reorder_qty
                if qty_ratio_f is not None:
                    forced_qty = baseline._round_to_pack(
                        reorder_qty * qty_ratio_f, int(item_row["pack_size"])
                    )
                forced_order_date = target_expected - timedelta(days=item_lead_time)
                if arrives_late:
                    # 강제 생성 발주는 day-loop 상태기계 밖에서 만들어지는 합성 행이라(재고
                    # 궤적에 영향 없음, 기존 설계) 자연 arm처럼 시뮬레이션으로 도착을 만들
                    # 수 없다 — 대신 도착 필드를 직접 채운다. release_day는 자연 arm과 동일한
                    # 공식(expected_date+delay_days)을 쓴다.
                    arrival_date = target_expected + timedelta(days=delay_days)
                    forced_rows.append(
                        {
                            "item_id": item_row["item_id"],
                            "order_date": forced_order_date.isoformat(),
                            "expected_date": target_expected.isoformat(),
                            "expected_qty": int(forced_qty),
                            "actual_date": arrival_date.isoformat(),
                            "actual_qty": int(forced_qty),
                            "status": "입고 완료",
                        }
                    )
                    # F2(S-22 픽스 라운드 1): incoming_shipments의 도착 기록만으로는
                    # stock_usage_daily가 이 수량을 모른다 — day-loop에 직접 credit해야
                    # 두 장부가 일치한다(유령 입고 금지, 사후 SQL 패치 금지).
                    forced_arrivals.append(ForcedArrival(date=arrival_date, qty=int(forced_qty)))
                else:
                    forced_rows.append(
                        {
                            "item_id": item_row["item_id"],
                            "order_date": forced_order_date.isoformat(),
                            "expected_date": target_expected.isoformat(),
                            "expected_qty": int(forced_qty),
                            "actual_date": None,
                            "actual_qty": None,
                            "status": "입고 예정",
                        }
                    )
                resolved_onsets.append(target_expected)

        else:
            raise ValueError(f"{sc.scenario_id}: 알 수 없는 시나리오 유형 {sub_type!r}")

    return (
        tuple(demand_effects),
        tuple(halt_effects),
        tuple(delay_effects),
        forced_rows,
        tuple(forced_arrivals),
        min(resolved_onsets),
    )


def _assert_effects_effective(
    scenario_id: str,
    item_row: dict[str, str],
    seed: int,
    days: list[date],
    demand_effects: tuple[DemandEffect, ...],
    halt_effects: tuple[HaltEffect, ...],
    delay_effects: tuple[DelayEffect, ...],
    forced_rows: list[dict[str, object]],
    forced_arrivals: tuple[ForcedArrival, ...],
    stock_rows: list[dict[str, object]],
    trace: ScenarioTrace,
    *,
    observable_window: tuple[date, date] | None = None,
) -> None:
    """이 시나리오의 각 효과(halt·delay·demand_surge)가 실제로 최소 1건의 행 변화를
    만들었는지 검증한다(1주차 리뷰 F1). scenario_config.yaml의 강도가 베이스라인 리듬과
    맞지 않으면 주입이 물리적으로 무효과가 될 수 있다(예: halt 기간이 너무 짧아 재고가
    ROP 밑으로 내려갈 시간이 없어 미이행 발주가 하나도 생기지 않음 — SC-006·SC-010·
    SC-018에서 실제로 발생했던 사례). 이런 경우를 조용히 지나치지 않고 명확한 에러로
    실패시켜, config 조정 없이는 "정답 라벨은 있는데 데이터에는 신호가 없는" 상태로
    감지율 측정에 들어가는 일이 없게 한다.

    예외 종류 구분(Task S-22 F7): "무효과"(임의 품목·파라미터 조합이 우연히 안 먹힘)는
    IneffectiveInjectionError — 재시도로 해소될 수 있는 것들이다. "구현 오류"(코드
    결함이면 재시도해도 계속 재현되거나, 최악의 경우 재시도가 우연히 그 버그를 안 밟는
    조합으로 넘어가 회귀를 숨긴다)는 일반 ValueError로 남긴다 — 재시도 루프가 이 둘을
    구분해서 잡아야 한다.

    observable_window(Task S-30c A): (창 시작, 창 끝) 튜플을 주면 "효과가 물리적으로
    발생했는가"에 더해 **"그 효과가 측정 구간 안에서 관측 가능한가"**까지 단언한다 —
    S-30b가 실증한 평가 설계 결함(1차 블라인드 라벨 20건 중 12건이 스윕 시작 이전에
    품절되어 채점 규칙 `first_alert <= stockout_date`를 어떤 탐지기도 만족할 수 없었다)의
    생성 단계 차단이다. 여기서는 **관측 품절**(closing_stock이 실제로 0에 닿은 날, 라벨의
    "observed" 근거와 동일 정의)만 본다 — 0에 닿지 않는 품목의 라벨은 외삽이라 base_date
    이후로만 나오며, 그 상한 검사는 라벨 도출 후 assert_labels_observable이 맡는다.
    기본값 None이면 이 검사를 하지 않는다(표준 20건 경로는 종전과 완전히 동일하다).
    """
    if observable_window is not None:
        window_start, window_end = observable_window
        observed_stockout = next(
            (
                date.fromisoformat(str(row["date"]))
                for row in stock_rows
                if row["closing_stock"] == 0
            ),
            None,
        )
        if observed_stockout is not None and not (
            window_start <= observed_stockout <= window_end
        ):
            raise IneffectiveInjectionError(
                f"{scenario_id}: 관측 품절일({observed_stockout.isoformat()})이 측정 구간"
                f"[{window_start.isoformat()}, {window_end.isoformat()}] 밖이라 채점 규칙상"
                " 감지 성공이 불가능하다(무효과와 동일 취급 — 재추첨 대상)"
            )

    if halt_effects and not trace.halt_blocked_order_dates:
        raise IneffectiveInjectionError(
            f"{scenario_id}: supply_halt가 미이행 발주를 1건도 만들지 못했다(무효과) —"
            " halt_start_date를 앞당기거나 demand_shift_multiplier를 조정해야 한다"
        )

    for d_eff in delay_effects:
        if d_eff.order_date.isoformat() not in trace.delay_blocked_order_dates:
            raise ValueError(
                f"{scenario_id}: delivery_delay 대상 발주(order_date="
                f"{d_eff.order_date.isoformat()})가 미이행 상태로 기록되지 않았다(구현 오류)"
            )

    for row in forced_rows:
        if row["actual_date"] is not None:
            # S-22: arrives_late=True인 강제 생성 발주는 예정일보다 "늦게" 도착하는 것이
            # 정당한 의도된 결과다(expected_date < actual_date) — 예정일 이전/당일에
            # 이행된 것처럼 보이는 경우만 구현 오류로 본다(그 경우 "지연"이라는 전제 자체가
            # 깨진다). arrives_late 미사용 표준 config는 forced_rows의 actual_date가 항상
            # None이라 이 분기 자체에 진입하지 않는다(회귀 영향 없음).
            actual = date.fromisoformat(str(row["actual_date"]))
            expected = date.fromisoformat(str(row["expected_date"]))
            if actual <= expected:
                raise ValueError(
                    f"{scenario_id}: 강제 생성 발주가 예정일 이전/당일에 이행 상태로"
                    " 기록됐다(구현 오류)"
                )

    if forced_arrivals:
        # F2(S-22 픽스 라운드 1) 자가 점검: 강제 도착분이 실제로 재고 궤적에 credit됐는지
        # 생성 시점에 바로 잡는다(유령 입고 자가 점검 — validate_dataset 검사 11과 같은
        # 불변식을 여기서도 확인해 실패를 최대한 앞에서 잡는다). 이건 파라미터 운이 아니라
        # 배선 결함이면 나는 문제라 일반 ValueError로 둔다(재시도 비대상).
        incoming_by_date = {r["date"]: r["incoming_qty"] for r in stock_rows}
        for fa in forced_arrivals:
            credited = incoming_by_date.get(fa.date.isoformat(), 0)
            if credited < fa.qty:
                raise ValueError(
                    f"{scenario_id}: 강제 도착(date={fa.date.isoformat()}, qty={fa.qty})이"
                    f" stock_usage_daily.incoming_qty에 반영되지 않았다(유령 입고, 구현 오류)"
                    f" — 실제 credited={credited}"
                )

    if demand_effects:
        # demand_effects만 뺀 반사실(counterfactual)과 비교해 사용량이 실제로 달라졌는지
        # 확인한다(halt·delay는 그대로 둬 demand 기여만 격리). 감시 구간은 가장 이른
        # demand 효과의 시작일부터다 — 그 이전은 애초에 비교 대상이 아니다.
        window_start = min(e.start for e in demand_effects).isoformat()
        _no_stock, _no_ship, _no_trunc, _no_trace = simulate_item_with_scenario(
            item_row, seed, days, halt_effects=halt_effects, delay_effects=delay_effects
        )
        real_usage = {r["date"]: r["usage_qty"] for r in stock_rows if r["date"] >= window_start}
        counterfactual_usage = {r["date"]: r["usage_qty"] for r in _no_stock if r["date"] >= window_start}
        if all(real_usage[d] == counterfactual_usage[d] for d in real_usage):
            raise IneffectiveInjectionError(
                f"{scenario_id}: demand_surge가 사용량에 어떤 변화도 만들지 못했다(무효과)"
            )


def assert_labels_observable(
    labels: list[dict[str, object]], observable_window: tuple[date, date]
) -> None:
    """도출된 라벨 전건의 stockout_date가 측정 구간 안인지 단언한다(Task S-30c A).

    `_assert_effects_effective`의 창 검사는 stock_rows의 관측 품절만 볼 수 있어 외삽 라벨
    (base_date까지 재고가 0에 닿지 않은 품목 — `labels._stockout_date`의 "extrapolated"
    분기)의 상한을 판단하지 못한다. 이 함수는 라벨 도출이 끝난 뒤 두 근거(observed·
    extrapolated)를 구분하지 않고 **채점기가 실제로 읽는 값**인 stockout_date로 최종
    확인한다. 창 밖이면 재시도 대상 예외(IneffectiveInjectionError)를 던진다 — 임의 품목
    추첨의 운 문제이지 코드 결함이 아니기 때문이다.

    창의 의미(S-30b §1 분류 정의):
    - 하한(스윕 시작): `first_alert`는 아무리 일러도 스윕 첫날이므로, 그 이전 품절은
      채점 규칙 `first_alert <= stockout_date`를 구조적으로 만족할 수 없다(H1a).
    - 상한(스윕 종료 + watch_days): 그 밖의 품절은 '주의' 판정 근거 자체가 없다(H1b).
    """
    window_start, window_end = observable_window
    outside = [
        (str(lbl["params_ref"]), str(lbl["stockout_date"]))
        for lbl in labels
        if not (
            window_start <= date.fromisoformat(str(lbl["stockout_date"])) <= window_end
        )
    ]
    if outside:
        detail = ", ".join(f"{ref}={stockout}" for ref, stockout in outside)
        raise IneffectiveInjectionError(
            f"라벨 품절일이 측정 구간[{window_start.isoformat()},"
            f" {window_end.isoformat()}] 밖이다({detail}) — 채점 규칙상 감지 성공이"
            " 불가능하거나('주의' 근거 없음) 관측 불가라 재추첨 대상이다"
        )


# ---------------------------------------------------------------------------
# 대응 이력 시드 적재(브리프: inject.py 제공, 실제 표준 스냅샷 적재는 S-13)
# ---------------------------------------------------------------------------


def load_action_history_seed(conn: sqlite3.Connection, csv_path: str | Path) -> int:
    """data/reference/action_history_seed.csv를 action_history에 적재한다.

    medsupply.data.writer를 경유하지 않는 raw SQL 적재다(datagen은 medsupply 미참조
    원칙 — baseline.py가 마스터 CSV를 적재하는 것과 동일한 방식). CSV의 ingredient_code
    컬럼은 action_history 테이블에 없는 컬럼이라(시나리오 품목·성분 매칭 근거를 사람이
    확인하기 위한 참고용) 저장하지 않는다. 반환값은 적재한 행 수.
    """
    csv_path = Path(csv_path)
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    with conn:
        for row in rows:
            conn.execute(
                "INSERT INTO action_history(item_id, action_type, owner, note, status,"
                " risk_grade_before, risk_grade_after, result_note, created_at, risk_type)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["item_id"],
                    row["action_type"],
                    row["owner"],
                    row["note"],
                    row["status"],
                    row["risk_grade_before"],
                    row["risk_grade_after"],
                    row["result_note"],
                    row["created_at"],
                    row["risk_type"],
                ),
            )

    return len(rows)


# ---------------------------------------------------------------------------
# 오케스트레이션
# ---------------------------------------------------------------------------


def replace_item_rows(
    conn: sqlite3.Connection,
    item_id: str,
    stock_rows: list[dict[str, object]],
    shipment_rows: list[dict[str, object]],
) -> None:
    """item_id의 stock_usage_daily·incoming_shipments 행을 통째로 지우고 새로 넣는다.

    inject_scenarios가 시나리오 품목을 재시뮬레이션 결과로 교체할 때 쓰던 DELETE+INSERT
    패턴을 재사용 가능한 함수로 뽑았다(Task S-22 픽스 라운드 1 — 컨트롤러 리뷰 F1). 블라인드
    생성기의 정상 품목 미끼 주입도 이 함수를 그대로 재사용한다 — 로직을 복제하지 않기
    위함이다. 다른 item_id의 행에는 영향이 없다.
    """
    with conn:
        conn.execute("DELETE FROM stock_usage_daily WHERE item_id = ?", (item_id,))
        conn.execute("DELETE FROM incoming_shipments WHERE item_id = ?", (item_id,))
        conn.executemany(
            "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty,"
            " closing_stock) VALUES (:item_id, :date, :usage_qty, :incoming_qty,"
            " :closing_stock)",
            stock_rows,
        )
        conn.executemany(
            "INSERT INTO incoming_shipments(item_id, order_date, expected_date,"
            " expected_qty, actual_date, actual_qty, status) VALUES (:item_id,"
            " :order_date, :expected_date, :expected_qty, :actual_date, :actual_qty,"
            " :status)",
            shipment_rows,
        )


def _load_item_row(conn: sqlite3.Connection, item_id: str) -> dict[str, str]:
    row = conn.execute(
        "SELECT item_id, pack_size, supplier, atc_code, form FROM items WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"item_id {item_id!r}가 items 테이블에 없다")
    item_id_, pack_size, supplier, atc_code, form = row
    return {
        "item_id": item_id_,
        "pack_size": str(pack_size),
        "supplier": supplier,
        "atc_code": atc_code,
        "form": form,
    }


def inject_scenarios(
    out_path: str | Path,
    seed: int,
    base_date: str | date,
    *,
    scenario_config_path: str | Path = DEFAULT_SCENARIO_CONFIG_PATH,
    items_csv: str | Path | None = None,
    reference_dir: str | Path = baseline.DEFAULT_REFERENCE_DIR,
    schema_path: str | Path = baseline.DEFAULT_SCHEMA_PATH,
    min_scenarios_per_type: int = config.MIN_SCENARIOS_PER_TYPE,
    observable_window: tuple[date, date] | None = None,
    annotate_attribution: bool = False,
) -> tuple[GenerationSummary, list[dict[str, object]]]:
    """베이스라인 생성 후 scenario_config.yaml의 시나리오를 결정적으로 재시뮬레이션 주입한다.

    반환: (요약, 라벨 리스트 — 표준 config는 20건). out_path에 최종 SQLite DB를 쓴다(기존
    파일은 baseline.generate_baseline이 삭제 후 재생성). 시나리오 품목이 아닌 나머지 품목은
    베이스라인 결과와 완전히 동일하게 남는다(재시뮬레이션 대상이 아니므로).

    action_history_seed.csv 적재는 이 함수의 책임이 아니다(브리프: 적재는 S-13 소관,
    이 함수는 순수 시나리오 주입 + 라벨 도출까지만 담당). 필요하면
    load_action_history_seed를 별도로 호출한다.

    min_scenarios_per_type(Task S-22): config.validate_scenario_config로 그대로 전달되는
    유형별 최소 개수 하한. 기본값은 config.MIN_SCENARIOS_PER_TYPE(4)로 표준 config 검증과
    동일하다 — 블라인드 생성기(유형당 시나리오 1개)가 이 오케스트레이션 전체(베이스라인
    생성+주입+해시+라벨 도출)를 복제하지 않고 재사용할 수 있도록 매개변수화했다.

    observable_window(Task S-30c A): (창 시작, 창 끝). 주면 시나리오별
    `_assert_effects_effective`(관측 품절)와 라벨 전건 `assert_labels_observable`
    (observed·extrapolated 공통)에서 "품절일이 측정 구간 안"을 단언한다 — 창 밖이면
    IneffectiveInjectionError(재시도 대상)다. 기본값 None이면 두 검사 모두 비활성이라
    표준 20건 경로는 종전과 완전히 동일하게 동작한다(동결 무침해).

    annotate_attribution(Task S-30c B): labels.derive_labels로 그대로 전달한다 — True면
    라벨에 `scenario_attribution`("외삽 라벨은 시나리오 귀속 미검증")이 붙는다. 기본값
    False에서 표준 라벨 파일은 바이트 단위로 종전과 동일하다.
    """
    start = time.monotonic()

    out_path = Path(out_path)
    scenario_config_path = Path(scenario_config_path)
    reference_dir = Path(reference_dir)
    schema_path = Path(schema_path)
    items_csv_path = Path(items_csv) if items_csv is not None else reference_dir / "items_master.csv"

    base_date_obj = date.fromisoformat(base_date) if isinstance(base_date, str) else base_date
    timeline_start = base_date_obj - timedelta(days=364)
    days = [timeline_start + timedelta(days=i) for i in range(365)]

    cfg = config.load_scenario_config(scenario_config_path)
    violations = config.validate_scenario_config(
        cfg, items_csv=items_csv_path, min_per_type=min_scenarios_per_type
    )
    if violations:
        raise ValueError("시나리오 config 검증 실패:\n" + "\n".join(violations))

    baseline.generate_baseline(
        out_path,
        seed=seed,
        base_date=base_date_obj,
        reference_dir=reference_dir,
        schema_path=schema_path,
    )

    conn = sqlite3.connect(out_path)
    try:
        total_truncations = 0
        onset_overrides: dict[str, date] = {}
        blocked_orders: dict[str, set[str]] = {}
        for sc in cfg.scenarios:
            item_row = _load_item_row(conn, sc.item_id)
            (
                demand_effects,
                halt_effects,
                delay_effects,
                forced_rows,
                forced_arrivals,
                onset_date,
            ) = _resolve_effects(sc, item_row, seed, days)
            onset_overrides[sc.scenario_id] = onset_date
            stock_rows, shipment_rows, truncations, trace = simulate_item_with_scenario(
                item_row,
                seed,
                days,
                demand_effects=demand_effects,
                halt_effects=halt_effects,
                delay_effects=delay_effects,
                forced_arrivals=forced_arrivals,
            )
            shipment_rows = shipment_rows + forced_rows
            total_truncations += truncations

            _assert_effects_effective(
                sc.scenario_id,
                item_row,
                seed,
                days,
                demand_effects,
                halt_effects,
                delay_effects,
                forced_rows,
                forced_arrivals,
                stock_rows,
                trace,
                observable_window=observable_window,
            )

            # F3(1주차 리뷰): 라벨 외삽이 "실제로 막힌" 발주와 "아직 만기가 안 된 정상
            # 재고 중" 발주를 구분할 수 있도록, 이 품목에서 막힌 order_date 전체(halt·
            # delay·강제 생성)를 모아 derive_labels에 넘긴다.
            blocked_orders[sc.item_id] = (
                set(trace.halt_blocked_order_dates)
                | set(trace.delay_blocked_order_dates)
                | {row["order_date"] for row in forced_rows}
            )

            replace_item_rows(conn, sc.item_id, stock_rows, shipment_rows)

        # F2(1주차 리뷰): config_hash를 content_hash보다 먼저 확정해야 한다.
        # compute_content_hash는 meta 테이블 전체(자기 자신인 content_hash 키만 제외)를
        # 직렬화하므로, config_hash를 나중에 갱신하면 방금 계산한 content_hash가 "갱신 전"
        # config_hash 값을 반영한 채로 저장되어 완성 DB에서 재계산한 content_hash와
        # meta.content_hash가 어긋난다.
        config_hash = hashlib.sha256(scenario_config_path.read_bytes()).hexdigest()
        with conn:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'config_hash'", (config_hash,))

        content_hash = baseline.compute_content_hash(conn)
        with conn:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'content_hash'", (content_hash,))

        item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        ts_count = conn.execute("SELECT COUNT(*) FROM stock_usage_daily").fetchone()[0]
        shipment_count = conn.execute("SELECT COUNT(*) FROM incoming_shipments").fetchone()[0]

        labels = labels_mod.derive_labels(
            conn,
            cfg,
            onset_overrides=onset_overrides,
            blocked_orders=blocked_orders,
            annotate_attribution=annotate_attribution,
        )
        if observable_window is not None:
            assert_labels_observable(labels, observable_window)
    finally:
        conn.close()

    elapsed = time.monotonic() - start
    summary = GenerationSummary(
        item_count=item_count,
        timeseries_row_count=ts_count,
        shipment_count=shipment_count,
        truncation_count=total_truncations,
        content_hash=content_hash,
        elapsed_seconds=elapsed,
    )
    return summary, labels
