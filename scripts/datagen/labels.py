"""ground truth 라벨 도출 — docs/data-model.md §4 포맷 + stockout_basis 확장.

**격리 원칙**: 이 모듈은 `medsupply` 패키지를 일절 import하지 않는다. 시나리오 config는
`scripts/datagen/`과 측정 스크립트만 읽는다는 원칙(`config.py` 참조)을 그대로 따른다.

라벨 산출 규칙(브리프 §라벨 도출):
- onset_date = 시나리오 개시일(유형별 앵커 날짜). composite는 하위 시나리오 앵커 날짜 중
  최초(가장 이른) 날짜를 쓴다 — 복합 위험이 처음 시작된 시점을 대표하기 위함.
- stockout_date = 주입 후 stock_usage_daily에서 closing_stock이 처음 0에 도달한 날짜
  (관측치, "observed"). base_date까지 0에 도달하지 않으면 최근 28일 평균 사용량과 잔여
  재고로 선형 외삽한 예상일("extrapolated") — 미이행 입고는 정의상 incoming_qty=0이라
  이미 반영되어 있으므로 별도로 무시 처리할 값이 없다(브리프의 "미이행 입고 무시"는 이
  자연스러운 결과를 명시한 것).
- params_ref = scenario_id.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta

from scripts.datagen.config import ANCHOR_DATE_KEYS, Scenario, ScenarioConfig

#: 외삽에 쓰는 "최근" 구간 길이(일) — 브리프 지정값.
EXTRAPOLATION_WINDOW_DAYS = 28


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


def _stockout_date(conn: sqlite3.Connection, item_id: str, base_date: date) -> tuple[date, str]:
    """closing_stock이 처음 0에 도달한 날짜("observed"). base_date까지 미도달이면 최근
    EXTRAPOLATION_WINDOW_DAYS일 평균 사용량·잔여 재고로 선형 외삽한 날짜("extrapolated")."""
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
    remaining_stock = rows[-1][2]

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
) -> list[dict[str, object]]:
    """cfg의 20건 시나리오 각각에 대해 라벨 1건씩 도출한다.

    conn은 주입이 끝난(scripts.datagen.inject.inject_scenarios가 채운) SQLite 커넥션이어야
    한다 — stock_usage_daily를 직접 읽어 stockout_date를 계산하기 때문이다. 반환 순서는
    cfg.scenarios 순서(scenario_id 오름차순, SC-001..SC-020)를 따른다.

    onset_overrides는 scenario_id → 실제 onset_date 매핑이다. delivery_delay(및 이를
    포함하는 composite)는 "지정 expected_date에 가장 가까운 주문"을 실제로 찾아야 정확한
    onset을 알 수 있는데, 그 탐색은 주입 시뮬레이션(inject.py)의 책임이라 이 함수 혼자서는
    재현할 수 없다 — inject_scenarios가 실제로 주입에 사용한 값을 넘겨준다. 값이 없는
    scenario_id는 config 파라미터의 앵커 날짜(_onset_date)로 폴백한다(demand_surge·
    supply_halt는 애초에 앵커 날짜 자체가 정답이라 폴백으로도 정확하다).
    """
    base_date = date.fromisoformat(cfg.base_date)
    onset_overrides = onset_overrides or {}
    labels: list[dict[str, object]] = []

    for sc in cfg.scenarios:
        onset = onset_overrides.get(sc.scenario_id) or _onset_date(sc)
        stockout, basis = _stockout_date(conn, sc.item_id, base_date)
        if onset >= stockout:
            raise ValueError(
                f"{sc.scenario_id}: onset_date({onset})가 stockout_date({stockout}) 이상이다"
            )

        labels.append(
            {
                "item_id": sc.item_id,
                "scenario_type": sc.type,
                "onset_date": onset.isoformat(),
                "stockout_date": stockout.isoformat(),
                "params_ref": sc.scenario_id,
                "stockout_basis": basis,
            }
        )

    return labels


def labels_to_json(labels: list[dict[str, object]]) -> str:
    """라벨 리스트를 표준 JSON(UTF-8, 들여쓰기 2칸, 줄바꿈 종료)으로 직렬화한다."""
    return json.dumps(labels, ensure_ascii=False, indent=2) + "\n"
