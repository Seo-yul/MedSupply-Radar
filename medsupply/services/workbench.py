"""약사 검토 워크벤치 서비스 — review.py(품목 상세)가 소비하는 실데이터 조회 계층.

queries.py(순수 SQL 조회)의 결과를 조합·파생해 화면 친화적 dict를 만든다. 이 모듈은
새 SQL을 직접 작성하지 않는다 — 새 조회가 필요하면 queries.py에 추가한다(계층 규칙,
task-M16-brief.md). get_conn()·current_data_version()은 medsupply.services.inventory를
그대로 재사용한다(중복 구현 금지).

캐시 규칙:
- load_item_detail(): st.cache_data로 결과를 캐시하며, data_version 인자를 캐시 키에
  포함해 무효화 신호로 쓴다(호출부가 inventory.current_data_version()의 값을 넘긴다).

쓰기 연결:
- inventory.get_conn()은 st.cache_resource로 세션 간 공유되는 단일 커넥션이라, 조치
  이력 저장처럼 세션마다 발생하는 단발성 쓰기에 재사용하기에는 적합하지 않다(다른
  세션의 읽기·캐시된 커넥션과 뒤섞일 수 있다). open_write_conn()은 저장 시점에만 열고
  호출부가 즉시 닫는 짧은 수명의 별도 커넥션을 제공한다 — 이 함수 자체는 INSERT/UPDATE를
  하지 않으며, 실제 쓰기는 여전히 medsupply.data.writer의 공개 함수만 거친다(단일 쓰기
  경로 원칙 유지).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from medsupply import settings
from medsupply.analytics import asof
from medsupply.data import queries
from medsupply.services import inventory

#: 사용량 평균 산출 윈도우(일) — 최근 28일 vs 그 직전 28일(주 단위 델타 표기용).
_USAGE_WINDOW_DAYS = 28
#: series 조회 창(일) — 최근 28일 + 그 직전 28일 = 56일(브리프 계약).
_SERIES_WINDOW_DAYS = _USAGE_WINDOW_DAYS * 2


def open_write_conn() -> sqlite3.Connection:
    """조치 이력 저장 등 단발성 쓰기 전용 커넥션(호출부가 사용 직후 닫는다).

    inventory.get_conn()은 세션 간 공유되는 캐시 리소스 커넥션이라 쓰기에는 재사용하지
    않는다. 이 커넥션으로도 쓰기는 반드시 medsupply.data.writer의 공개 함수만 거친다.
    """
    conn = sqlite3.connect(str(settings.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _risk_row(conn: sqlite3.Connection, run_id: str, item_id: str) -> dict | None:
    """지정 run의 해당 품목 risk_results 1행(factors_json은 json.loads 해 factors 키로).

    run은 존재하지만 그 run에 해당 품목 행이 없으면(정상적으로는 발생하지 않지만
    방어적으로) None을 반환한다.
    """
    results = queries.get_risk_results(conn, run_id)
    match = results.loc[results["item_id"] == item_id]
    if match.empty:
        return None

    record = match.iloc[0].to_dict()
    factors_raw = record.pop("factors_json", None)
    record["factors"] = json.loads(factors_raw) if factors_raw else {}
    record["score"] = None if pd.isna(record.get("score")) else int(record["score"])
    record["days_to_stockout"] = (
        None if pd.isna(record.get("days_to_stockout")) else int(record["days_to_stockout"])
    )
    record["escalated_by_notice"] = bool(record.get("escalated_by_notice"))
    return record


def _usage_averages(series: pd.DataFrame) -> tuple[float | None, float | None]:
    """(avg_daily_usage, avg_prev) — 최근 28일 평균과 그 직전 28일 평균.

    series가 비어 있으면 둘 다 None. avg_daily_usage는 series에 있는 만큼(최대 28일)의
    usage_qty 평균(소수 1자리)이며, series가 1행이라도 있으면 항상 계산된다. avg_prev는
    "그 직전 28일"이 온전히 존재할 때만(len(series) >= 56) 계산하고, 그렇지 않으면
    데이터 부족으로 None이다.
    """
    if series.empty:
        return None, None

    recent = series.tail(_USAGE_WINDOW_DAYS)
    avg_daily_usage = round(float(recent["usage_qty"].mean()), 1)

    if len(series) < _SERIES_WINDOW_DAYS:
        return avg_daily_usage, None

    prev_window = series.iloc[-_SERIES_WINDOW_DAYS : -_USAGE_WINDOW_DAYS]
    avg_prev = round(float(prev_window["usage_qty"].mean()), 1)
    return avg_daily_usage, avg_prev


def _next_shipment(conn: sqlite3.Connection, item_id: str, as_of: date) -> dict | None:
    """as_of(meta.base_date) 시점 기준 가장 가까운 미래 예정(pending) 입고 1건. 없으면 None.

    get_incoming_shipments(pending_only=True)의 `actual_date IS NULL`만으로는 연체 건(예정일이
    이미 지났는데 미입고, 예: ITM-0039의 7월 18일 예정)까지 "다음 입고"로 잘못 선정한다 —
    동결 모델(analytics.depletion의 overdue_cutoff)은 그런 연체 품목의 미래 예정 입고를 소진
    추정에서 아예 배제하는데, 화면은 그 연체 건 자체를 다음 입고 예정으로 보여주는 모순이
    생긴다(2주차 브랜치 리뷰 F2). 그래서 pending_only=False로 전체(입고완료·연체 포함)를
    가져와 as_of 시점 상태를 asof.is_pending_at(모듈 asof.py가 규칙의 단일 소스)으로
    재구성한다 — is_pending_at은 expected_date > as_of AND as_of 시점 미도착만 pending으로
    본다. get_incoming_shipments가 이미 expected_date 오름차순으로 정렬해 반환하므로, pending
    필터를 통과한 첫 행이 곧 최근접 미래 예정 건이다.
    """
    shipments = queries.get_incoming_shipments(conn, item_id=item_id, pending_only=False)
    if shipments.empty:
        return None

    parsed = shipments.copy()
    for col in ("expected_date", "actual_date"):
        parsed[col] = pd.to_datetime(parsed[col], errors="coerce").dt.date

    pending_mask = pd.Series(
        [
            asof.is_pending_at(expected, actual, as_of)
            for expected, actual in zip(parsed["expected_date"], parsed["actual_date"])
        ],
        index=parsed.index,
    )
    pending = shipments[pending_mask]
    if pending.empty:
        return None

    row = pending.iloc[0]
    return {
        "expected_date": row["expected_date"],
        "qty": None if pd.isna(row["expected_qty"]) else int(row["expected_qty"]),
        "status": row["status"],
    }


@st.cache_data
def load_item_detail(item_id: str, data_version: int = 0) -> dict:
    """품목 상세(약사 검토 워크벤치)에 필요한 실데이터를 일괄 조회한다.

    data_version은 호출부(inventory.current_data_version())가 넘기는 캐시 무효화
    신호일 뿐 조회 조건으로는 쓰이지 않는다. 위험 평가 배치 run이 아직 없으면
    risk/prev_risk/forecast는 모두 None이고 나머지 키(재고·사용량·대체 후보 등)는
    평소대로 계산된다 — 예외를 던지지 않는다.
    """
    del data_version  # 캐시 키 무효화 전용 — 조회 조건에는 쓰지 않는다.

    conn = inventory.get_conn()
    item = queries.get_item(conn, item_id)

    latest_runs = queries.get_latest_runs(conn, 2)
    risk = _risk_row(conn, latest_runs[0], item_id) if latest_runs else None
    prev_risk = _risk_row(conn, latest_runs[1], item_id) if len(latest_runs) >= 2 else None
    forecast = queries.get_forecast(conn, latest_runs[0], item_id) if latest_runs else None

    meta = queries.get_meta(conn)
    base_date = date.fromisoformat(meta["base_date"])
    series = queries.get_daily_series(
        conn,
        item_id,
        start=base_date - timedelta(days=_SERIES_WINDOW_DAYS - 1),
        end=base_date,
    )

    current_stock = None
    if not series.empty:
        last_stock = series.iloc[-1]["closing_stock"]
        current_stock = None if pd.isna(last_stock) else int(last_stock)

    avg_daily_usage, avg_prev = _usage_averages(series)

    active_map = queries.get_active_notice_map(conn, as_of=base_date)
    has_active_notice = (not active_map.empty) and (item_id in set(active_map["item_id"]))

    substitutes = queries.get_substitutes(conn, item_id, same_condition_only=False)

    return {
        "item": item,
        "risk": risk,
        "prev_risk": prev_risk,
        "series": series,
        "forecast": forecast,
        "current_stock": current_stock,
        "avg_daily_usage": avg_daily_usage,
        "avg_prev": avg_prev,
        "next_shipment": _next_shipment(conn, item_id, base_date),
        "has_active_notice": has_active_notice,
        "substitutes": substitutes,
        "ingredient_name_kr": item.get("ingredient_name_kr") or "-",
        "ingredient_name_en": item.get("ingredient_name_en") or "-",
        "explanation": queries.get_explanation(conn, item_id),
    }
