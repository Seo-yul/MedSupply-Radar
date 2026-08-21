"""ground truth 라벨 도출 — docs/data-model.md §4 포맷 + stockout_basis 확장.

**격리 원칙**: 이 모듈은 `medsupply` 패키지를 일절 import하지 않는다. 시나리오 config는
`scripts/datagen/`과 측정 스크립트만 읽는다는 원칙(`config.py` 참조)을 그대로 따른다.

라벨 산출 규칙(브리프 §라벨 도출 + 1주차 리뷰 F3·F5 보정):
- onset_date = 시나리오 개시일(유형별 앵커 날짜). composite는 하위 시나리오 앵커 날짜 중
  최초(가장 이른) 날짜를 쓴다 — 복합 위험이 처음 시작된 시점을 대표하기 위함.
- stockout_date = 주입 후 stock_usage_daily에서 closing_stock이 처음 0에 도달한 날짜
  (실측치, "observed"). base_date까지 0에 도달하지 않으면 최근 28일 평균 사용량과
  "잔여 재고 + 아직 만기가 안 된 정상 입고 예정분"으로 선형 외삽한 예측일
  ("extrapolated"). 브리프의 "미이행 입고 무시"는 시나리오가 실제로 막은(halt·delay로
  영구 미이행이 된) 발주에만 적용된다 — halt·delay와 무관하게 그냥 아직 도착일이 안 된
  정상 발주까지 무시하면 demand_surge 단독 시나리오(halt·delay 없음)의 외삽이 부당하게
  비관적으로 나온다(1주차 리뷰 F3, SC-003·SC-005에서 확인). 그래서 derive_labels는
  inject.py가 실제로 "막았다"고 표시한 order_date 집합(blocked_orders)에 없는 미이행
  발주만 외삽에 가산한다.
- params_ref = scenario_id.
- delay_days는 관측 가능한 "지연 일수"가 아니라 inject.py 내부의 "재발주 게이트" 기간
  이다(1주차 리뷰 F5 판정) — 막힌 발주가 delay_days만큼 슬롯을 붙잡아 둔 뒤 풀려나
  새 발주를 다시 시도할 수 있게 하는 내부 파라미터일 뿐, 데이터에 그 값이 직접
  기록되지는 않는다. 실제로 관측 가능한(측정 스크립트가 볼 수 있는) 지연 정도는
  as_of(또는 base_date)와 미이행 발주의 expected_date 차이로 결정된다 — 이 라벨의
  onset_date~stockout_date 구간이 그 근거다.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta

from scripts.datagen.config import ANCHOR_DATE_KEYS, Scenario, ScenarioConfig

#: 외삽에 쓰는 "최근" 구간 길이(일) — 브리프 지정값.
EXTRAPOLATION_WINDOW_DAYS = 28

#: stockout_basis → 시나리오 귀속 표기(Task S-30c B, S-30b (c)③ 권고).
#:
#: `_stockout_date`의 "observed" 분기는 시나리오 주입 이후 실제로 closing_stock이 0에
#: 닿은 날을 쓴다 — 데이터에 관측된 사실이다. 반면 "extrapolated" 분기는 **향후 재입고를
#: 전량 동결**하는 가정 위의 선형 외삽이라, 시나리오 효과가 base_date 이전에 이미 소멸한
#: 품목에도 "품절 라벨"을 붙일 수 있다. S-30b §3 실증: 같은 공식을 어떤 시나리오도 없는
#: 정상 품목 120건에 그대로 적용하면 43건이 오라클 지평(스윕 종료+30일) 이내로 외삽되고,
#: 85건(70.8%)이 문제의 라벨값보다 이른 품절일을 받는다. 즉 그 라벨은 "시나리오의 결과"가
#: 아니라 "공식의 산물"일 수 있으며, 감지 실패로 집계하기 전에 라벨 타당성을 의심해야 한다.
STOCKOUT_ATTRIBUTION = {
    "observed": "observed_stockout",
    "extrapolated": "unverified_extrapolation",
}


def _onset_date(sc: Scenario) -> date:
    """시나리오 개시일 — 유형별 앵커 날짜(composite는 하위 시나리오 중 최초 개시일)."""
    if sc.type != "composite":
        key = ANCHOR_DATE_KEYS[sc.type]
        return date.fromisoformat(str(sc.params[key]))

    sub_scenarios = sc.params["sub_scenarios"]
    anchors = [
        date.fromisoformat(str(sub["params"][ANCHOR_DATE_KEYS[sub["type"]]]))
        for sub in sub_scenarios
    ]
    return min(anchors)


def _in_transit_qty(
    conn: sqlite3.Connection, item_id: str, blocked_order_dates: frozenset[str]
) -> int:
    """미이행(actual_date IS NULL) 발주 중 blocked_order_dates에 없는 것들의 expected_qty
    합계 — "시나리오가 실제로 막지 않은, 그냥 아직 만기가 안 된" 정상 입고 예정분이다
    (1주차 리뷰 F3). 정상 품목(시나리오 미대상)은 blocked_order_dates가 항상 빈 집합이라
    미이행 발주가 있으면 전부 가산된다 — 정상 품목은 애초에 stockout_date를 라벨화하지
    않으므로 이 함수가 호출될 일이 없다(derive_labels는 cfg.scenarios 20건만 순회).
    """
    rows = conn.execute(
        "SELECT order_date, expected_qty FROM incoming_shipments"
        " WHERE item_id = ? AND actual_date IS NULL",
        (item_id,),
    ).fetchall()
    return sum(qty for order_date, qty in rows if order_date not in blocked_order_dates)


def _stockout_date(
    conn: sqlite3.Connection,
    item_id: str,
    base_date: date,
    blocked_order_dates: frozenset[str] = frozenset(),
) -> tuple[date, str]:
    """closing_stock이 처음 0에 도달한 날짜("observed"). base_date까지 미도달이면 최근
    EXTRAPOLATION_WINDOW_DAYS일 평균 사용량과 "잔여 재고 + 비차단 미이행 입고"로 선형
    외삽한 날짜("extrapolated"). blocked_order_dates는 시나리오가 실제로 막은(halt·delay·
    강제 생성) order_date 집합이다 — 그 집합에 없는 미이행 발주는 시나리오와 무관하게
    그냥 아직 도착일이 안 된 정상 입고라서 외삽 시 remaining_stock에 가산한다(F3)."""
    rows = conn.execute(
        "SELECT date, usage_qty, closing_stock FROM stock_usage_daily"
        " WHERE item_id = ? AND date <= ? ORDER BY date",
        (item_id, base_date.isoformat()),
    ).fetchall()
    if not rows:
        raise ValueError(f"{item_id}: stock_usage_daily 데이터가 없다")

    for row_date, _usage, closing in rows:
        if closing == 0:
            return date.fromisoformat(row_date), "observed"

    recent = rows[-EXTRAPOLATION_WINDOW_DAYS:]
    avg_daily_usage = sum(r[1] for r in recent) / len(recent)
    remaining_stock = rows[-1][2] + _in_transit_qty(conn, item_id, blocked_order_dates)

    if avg_daily_usage <= 0:
        raise ValueError(
            f"{item_id}: 최근 {EXTRAPOLATION_WINDOW_DAYS}일 평균 사용량이 0 이하라 외삽 불가"
            f"(remaining_stock={remaining_stock})"
        )

    days_to_stockout = math.ceil(remaining_stock / avg_daily_usage)
    return base_date + timedelta(days=days_to_stockout), "extrapolated"


def derive_labels(
    conn: sqlite3.Connection,
    cfg: ScenarioConfig,
    *,
    onset_overrides: dict[str, date] | None = None,
    blocked_orders: dict[str, set[str]] | None = None,
    annotate_attribution: bool = False,
) -> list[dict[str, object]]:
    """cfg의 20건 시나리오 각각에 대해 라벨 1건씩 도출한다.

    conn은 주입이 끝난(scripts.datagen.inject.inject_scenarios가 채운) SQLite 커넥션이어야
    한다 — stock_usage_daily를 직접 읽어 stockout_date를 계산하기 때문이다. 반환 순서는
    cfg.scenarios 순서(scenario_id 오름차순, SC-001..SC-020)를 따른다.

    onset_overrides는 scenario_id → 실제 onset_date 매핑이다. delivery_delay(및 이를
    포함하는 composite)는 "지정 expected_date에 가장 가까운 주문(또는 강제 생성한 신규
    주문)"을 실제로 찾아야 정확한 onset을 알 수 있는데, 그 탐색은 주입 시뮬레이션
    (inject.py)의 책임이라 이 함수 혼자서는 재현할 수 없다 — inject_scenarios가 실제로
    주입에 사용한 값을 넘겨준다. 값이 없는 scenario_id는 config 파라미터의 앵커 날짜
    (_onset_date)로 폴백한다(demand_surge·supply_halt는 애초에 앵커 날짜 자체가 정답이라
    폴백으로도 정확하다).

    blocked_orders는 item_id → 그 품목에서 시나리오가 실제로 막은 order_date 집합이다
    (1주차 리뷰 F3). 없는 item_id는 빈 집합으로 취급한다 — 즉 그 품목의 미이행 발주는
    전부 "비차단"으로 보고 외삽에 가산한다.

    annotate_attribution(Task S-30c B): True면 라벨마다 `scenario_attribution` 필드를
    덧붙인다(STOCKOUT_ATTRIBUTION 매핑 — extrapolated는 "시나리오 귀속 미검증"). **기본값
    False에서는 라벨 스키마가 종전과 100% 동일**하다 — 동결된 표준 라벨 파일
    (data/scenarios/ground_truth/standard_v1.json)이 재생성해도 바이트 단위로 그대로여야
    하기 때문이다. 블라인드 생성 경로(scripts/datagen/blind.py)만 이 옵션을 켠다.
    """
    base_date = date.fromisoformat(cfg.base_date)
    onset_overrides = onset_overrides or {}
    blocked_orders = blocked_orders or {}
    labels: list[dict[str, object]] = []

    for sc in cfg.scenarios:
        onset = onset_overrides.get(sc.scenario_id) or _onset_date(sc)
        item_blocked = frozenset(blocked_orders.get(sc.item_id, set()))
        stockout, basis = _stockout_date(conn, sc.item_id, base_date, item_blocked)
        if onset >= stockout:
            raise ValueError(
                f"{sc.scenario_id}: onset_date({onset})가 stockout_date({stockout}) 이상이다"
            )

        label: dict[str, object] = {
            "item_id": sc.item_id,
            "scenario_type": sc.type,
            "onset_date": onset.isoformat(),
            "stockout_date": stockout.isoformat(),
            "params_ref": sc.scenario_id,
            "stockout_basis": basis,
        }
        if annotate_attribution:
            label["scenario_attribution"] = STOCKOUT_ATTRIBUTION[basis]
        labels.append(label)

    return labels


def labels_to_json(labels: list[dict[str, object]]) -> str:
    """라벨 리스트를 표준 JSON(UTF-8, 들여쓰기 2칸, 줄바꿈 종료)으로 직렬화한다."""
    return json.dumps(labels, ensure_ascii=False, indent=2) + "\n"
