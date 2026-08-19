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
    풀어준다 — 그 특정 발주 자체는 관측 구간 내내 미이행(actual_date=NULL)으로 남지만,
    이후 재고가 다시 ROP 밑으로 떨어지면 새 발주를 시도할 수 있다(halt와 달리 영구
    봉쇄가 아니다). expected_date는 실제로 막힌 그 발주의 예정일(대상 선정 결과)이다 —
    scenario_config.yaml의 expected_date 파라미터는 S-03에서 베이스라인 존재 이전에
    정해진 "지정" 값이라 실제 발주 리듬과 정확히 일치하지 않을 수 있고(브리프: "가장
    가까운 주문"), 라벨의 onset_date는 이 실제 값을 써야 onset<stockout이 항상 성립한다.
    """

    order_date: date
    expected_date: date
    delay_days: int
    qty_ratio: float | None


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


def simulate_item_with_scenario(
    item: dict[str, str],
    seed: int,
    days: list[date],
    *,
    demand_effects: tuple[DemandEffect, ...] = (),
    halt_effects: tuple[HaltEffect, ...] = (),
    delay_effects: tuple[DelayEffect, ...] = (),
) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
    """단일 품목의 시나리오 반영 일별 재고·사용량·발주 시뮬레이션(날짜순 진행).

    baseline._simulate_item과 동일한 서브시드 공식·노이즈 시퀀스·재고 항등식·1-pack
    하한 로직을 쓰되, 사용량 배수(demand_effects)와 미이행 발주 규칙(halt_effects·
    delay_effects)을 얹는다. 세 효과가 모두 비어 있으면 baseline._simulate_item과
    바이트 동일한 결과를 낸다. 반환 형식은 baseline._simulate_item과 동일하다.
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

    truncation_count = 0

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
            blocked = False
            release_day: date | None = None

            if any(expected_date >= h.start for h in halt_effects):
                blocked = True
                release_day = expected_date

            delay_eff = next((e for e in delay_effects if e.order_date == d), None)
            if delay_eff is not None:
                blocked = True
                if delay_eff.qty_ratio is not None:
                    expected_qty = baseline._ceil_to_pack(
                        reorder_qty * delay_eff.qty_ratio, pack_size
                    )
                release_day = expected_date + timedelta(days=delay_eff.delay_days)

            new_row: dict[str, object] = {
                "item_id": item_id,
                "order_date": d.isoformat(),
                "expected_date": expected_date.isoformat(),
                "expected_qty": int(expected_qty),
                "actual_date": None,
                "actual_qty": None,
                "status": "입고 예정",
            }
            shipment_rows.append(new_row)
            pending_row = new_row
            pending_expected = expected_date
            pending_blocked = blocked
            pending_release_day = release_day

    return stock_rows, shipment_rows, truncation_count


# ---------------------------------------------------------------------------
# 시나리오 → 효과 변환(composite는 sub_scenarios를 순서대로 중첩 적용)
# ---------------------------------------------------------------------------


def _sub_specs(sc: Scenario) -> list[dict]:
    if sc.type == "composite":
        return list(sc.params["sub_scenarios"])
    return [{"type": sc.type, "params": sc.params}]


def _closest_order(
    shipment_rows: list[dict[str, object]], target_expected: date
) -> dict[str, object]:
    """expected_date가 target_expected에 가장 가까운 발주 행을 반환한다(동률은 이른 order_date)."""
    if not shipment_rows:
        raise ValueError("주입 대상 발주를 찾을 수 없다(품목 시뮬레이션에 발주가 전혀 없음)")

    def _key(row: dict[str, object]) -> tuple[int, str]:
        expected = date.fromisoformat(str(row["expected_date"]))
        return abs((expected - target_expected).days), str(row["order_date"])

    return min(shipment_rows, key=_key)


def _resolve_effects(
    sc: Scenario, item_row: dict[str, str], seed: int, days: list[date]
) -> tuple[tuple[DemandEffect, ...], tuple[HaltEffect, ...], tuple[DelayEffect, ...], date]:
    """시나리오 1건(composite는 하위 요소 전체)을 효과 리스트 + 실제 onset_date로 변환한다.

    delivery_delay 요소는 "지정 expected_date에 가장 가까운 주문"을 찾아야 하므로, 그
    시점까지 이미 확정된 효과(이전 sub_scenario들의 demand/halt)를 반영한 드라이런
    시뮬레이션을 1회 수행해 대상 발주를 특정한다. 반환하는 onset_date는 유형별 앵커
    날짜(demand_surge=surge_start_date, supply_halt=halt_start_date)이되, delivery_delay만
    "지정 expected_date"가 아니라 실제로 대상이 된 발주의 expected_date를 쓴다 — S-03
    시점에는 베이스라인이 없어 지정값이 실제 발주 리듬과 어긋날 수 있고(브리프의 "가장
    가까운 주문" 표현 자체가 이를 전제), 라벨의 onset<stockout 불변식은 실제로 주입된
    사건 기준으로만 성립하기 때문이다. composite는 하위 요소들의 (보정된) 앵커 날짜 중
    최솟값을 쓴다.
    """
    demand_effects: list[DemandEffect] = []
    halt_effects: list[HaltEffect] = []
    delay_effects: list[DelayEffect] = []
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
            _dry_stock, dry_shipments, _dry_trunc = simulate_item_with_scenario(
                item_row,
                seed,
                days,
                demand_effects=tuple(demand_effects),
                halt_effects=tuple(halt_effects),
                delay_effects=tuple(delay_effects),
            )
            matched = _closest_order(dry_shipments, target_expected)
            matched_order_date = date.fromisoformat(str(matched["order_date"]))
            matched_expected_date = date.fromisoformat(str(matched["expected_date"]))
            qty_ratio = params.get("qty_ratio")
            delay_effects.append(
                DelayEffect(
                    order_date=matched_order_date,
                    expected_date=matched_expected_date,
                    delay_days=int(params["delay_days"]),
                    qty_ratio=float(qty_ratio) if qty_ratio is not None else None,
                )
            )
            resolved_onsets.append(matched_expected_date)

        else:
            raise ValueError(f"{sc.scenario_id}: 알 수 없는 시나리오 유형 {sub_type!r}")

    return tuple(demand_effects), tuple(halt_effects), tuple(delay_effects), min(resolved_onsets)


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
) -> tuple[GenerationSummary, list[dict[str, object]]]:
    """베이스라인 생성 후 scenario_config.yaml의 20건을 결정적으로 재시뮬레이션 주입한다.

    반환: (요약, 라벨 20건 리스트). out_path에 최종 SQLite DB를 쓴다(기존 파일은
    baseline.generate_baseline이 삭제 후 재생성). 시나리오 품목이 아닌 나머지 품목은
    베이스라인 결과와 완전히 동일하게 남는다(재시뮬레이션 대상이 아니므로).

    action_history_seed.csv 적재는 이 함수의 책임이 아니다(브리프: 적재는 S-13 소관,
    이 함수는 순수 시나리오 주입 + 라벨 도출까지만 담당). 필요하면
    load_action_history_seed를 별도로 호출한다.
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
    violations = config.validate_scenario_config(cfg, items_csv=items_csv_path)
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
        for sc in cfg.scenarios:
            item_row = _load_item_row(conn, sc.item_id)
            demand_effects, halt_effects, delay_effects, onset_date = _resolve_effects(
                sc, item_row, seed, days
            )
            onset_overrides[sc.scenario_id] = onset_date
            stock_rows, shipment_rows, truncations = simulate_item_with_scenario(
                item_row,
                seed,
                days,
                demand_effects=demand_effects,
                halt_effects=halt_effects,
                delay_effects=delay_effects,
            )
            total_truncations += truncations

            with conn:
                conn.execute("DELETE FROM stock_usage_daily WHERE item_id = ?", (sc.item_id,))
                conn.execute("DELETE FROM incoming_shipments WHERE item_id = ?", (sc.item_id,))
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

        content_hash = baseline.compute_content_hash(conn)
        config_hash = hashlib.sha256(scenario_config_path.read_bytes()).hexdigest()
        with conn:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'content_hash'", (content_hash,))
            conn.execute("UPDATE meta SET value = ? WHERE key = 'config_hash'", (config_hash,))

        item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        ts_count = conn.execute("SELECT COUNT(*) FROM stock_usage_daily").fetchone()[0]
        shipment_count = conn.execute("SELECT COUNT(*) FROM incoming_shipments").fetchone()[0]

        labels = labels_mod.derive_labels(conn, cfg, onset_overrides=onset_overrides)
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
