"""발주 요청안 서비스 — views/orders.py가 소비하는 실데이터 계산 계층.

queries.py(순수 SQL 조회)와 workbench.load_item_detail(current_stock·forecast·
avg_daily_usage·substitutes)을 조합해 결정적 발주 요청안을 산출한다(task-M25-brief.md
계약). 이 모듈은 새 SQL을 직접 작성하지 않는다 — 새 조회가 필요하면 queries.py에
추가한다(계층 규칙, task-M15-brief.md). get_conn()·current_data_version()은
medsupply.services.inventory를, 쓰기 전용 커넥션은
medsupply.services.workbench.open_write_conn()을 그대로 재사용한다(중복 구현 금지) —
저장 자체는 뷰(medsupply/views/orders.py)가 medsupply.data.writer.save_order_request·
save_action_history를 직접 호출한다(단일 쓰기 경로 원칙, 이 모듈은 쓰기를 하지 않는다).

캐시 규칙:
- compute_order_proposal(): st.cache_data로 결과를 캐시하며, data_version 인자를 캐시 키에
  포함해 무효화 신호로 쓴다(호출부가 inventory.current_data_version()의 값을 넘긴다). 같은
  data_version을 workbench.load_item_detail 호출에도 그대로 전달한다(내부에서 그 함수를
  재사용하므로 두 캐시 레이어가 같은 무효화 신호를 공유한다).
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date

import pandas as pd
import streamlit as st

from medsupply.analytics import asof
from medsupply.data import queries
from medsupply.services import inventory, workbench

#: expected_demand 산정 지평(일) — 브리프 계약(14일 예상 수요).
_DEMAND_HORIZON_DAYS = 14
#: suggested_qty 올림 단위 — 브리프 계약(shortage를 50 단위로 올린다).
_ORDER_QTY_ROUND = 50


def _pending_incoming_qty(conn: sqlite3.Connection, item_id: str, as_of_date: date) -> int:
    """as_of_date 시점 기준 미래 예정(pending) 입고 수량 합(asof.is_pending_at 술어).

    workbench._next_shipment과 같은 as_of 재구성 규칙을 쓰되(연체 건은 미래 예정이
    아니므로 제외 — F2), 최근접 1건이 아니라 전체 합을 구한다(발주 요청안의 "미래 예정
    입고 수량 합" 계약, task-M25-brief.md).
    """
    shipments = queries.get_incoming_shipments(conn, item_id=item_id, pending_only=False)
    if shipments.empty:
        return 0

    expected = pd.to_datetime(shipments["expected_date"], errors="coerce").dt.date
    actual = pd.to_datetime(shipments["actual_date"], errors="coerce").dt.date

    total = 0
    for exp, act, qty in zip(expected, actual, shipments["expected_qty"]):
        if asof.is_pending_at(exp, act, as_of_date):
            total += 0 if pd.isna(qty) else int(qty)
    return total


def _suppliers(item: dict, substitutes: pd.DataFrame) -> list[str]:
    """현 품목 supplier + same_condition 대체 후보 supplier 중복 제거 목록(현 품목 먼저)."""
    suppliers: list[str] = []
    own = item.get("supplier")
    if own:
        suppliers.append(own)

    same_condition = substitutes[substitutes["same_condition"]]
    for supplier in same_condition["supplier"]:
        if supplier and supplier not in suppliers:
            suppliers.append(supplier)
    return suppliers


@st.cache_data
def compute_order_proposal(item_id: str, data_version: int = 0) -> dict:
    """발주 요청안(결정적 산식, LLM 미관여) — task-M25-brief.md 계약 그대로.

    반환 dict: item_id, item_name, current_stock, expected_demand, incoming_qty,
    shortage, suggested_qty, suppliers, risk_type, grade.

    expected_demand: forecasts(최신 run).total_forecast가 있으면 그 값(14일 지평),
    없으면 avg_daily_usage * 14일로 폴백, 둘 다 없으면 None. incoming_qty:
    meta.base_date 기준 미래 예정 입고 수량 합. shortage: expected_demand·current_stock이
    둘 다 있을 때만 max(0, ceil(expected_demand - current_stock - incoming_qty)), 아니면
    산출 불가로 None(이 경우 suggested_qty는 폼 기본값을 위해 0). suggested_qty: shortage를
    50 단위로 올린 값(0이면 0). risk_type·grade: 위험 평가 run이 없으면 둘 다 None.
    """
    conn = inventory.get_conn()
    item = queries.get_item(conn, item_id)
    detail = workbench.load_item_detail(item_id, data_version=data_version)

    meta = queries.get_meta(conn)
    base_date = date.fromisoformat(meta["base_date"])

    forecast = detail["forecast"]
    avg_daily_usage = detail["avg_daily_usage"]
    if forecast is not None:
        expected_demand = forecast["total_forecast"]
    elif avg_daily_usage is not None:
        expected_demand = avg_daily_usage * _DEMAND_HORIZON_DAYS
    else:
        expected_demand = None

    current_stock = detail["current_stock"]
    incoming_qty = _pending_incoming_qty(conn, item_id, base_date)

    if expected_demand is None or current_stock is None:
        shortage = None
        suggested_qty = 0
    else:
        shortage = max(0, math.ceil(expected_demand - current_stock - incoming_qty))
        suggested_qty = math.ceil(shortage / _ORDER_QTY_ROUND) * _ORDER_QTY_ROUND

    risk = detail["risk"]
    grade = risk["grade"] if risk else None
    risk_type = risk["risk_type"] if risk else None

    return {
        "item_id": item_id,
        "item_name": item["item_name"],
        "current_stock": current_stock,
        "expected_demand": expected_demand,
        "incoming_qty": incoming_qty,
        "shortage": shortage,
        "suggested_qty": suggested_qty,
        "suppliers": _suppliers(item, detail["substitutes"]),
        "risk_type": risk_type,
        "grade": grade,
    }
