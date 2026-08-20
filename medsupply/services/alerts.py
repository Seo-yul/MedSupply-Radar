"""알림 서비스 — sync_alerts(결정적·멱등 파생)와 views/alerts.py가 소비하는 조회 계층.

sync_alerts는 위험 변화(등급 상승·입고 지연 신호)와 공고 매핑에서 알림을 결정적으로
파생해 medsupply.data.writer.create_alert 하나로만 저장한다(단일 쓰기 경로 원칙). LLM
미관여, `datetime.now()` 미사용(created_at은 writer.create_alert의 몫) — 같은 입력이면
항상 같은 알림 집합을 만든다. 멱등은 create_alert의 dedupe_key UNIQUE 제약이 보장한다
(중복 시 create_alert가 예외를 삼키고 None을 반환 — 이 모듈은 그 결과를 created/skipped로
집계할 뿐이다).

sync_alerts(conn)은 다른 서비스 함수(compute_order_proposal 등)와 달리 conn을 인자로
직접 받는다 — 쓰기 함수라 `@st.cache_data`로 캐시하지 않으며, 호출부(views/alerts.py)가
medsupply.services.workbench.open_write_conn()으로 연 단발성 커넥션을 넘긴다.

load_alerts()는 다른 서비스와 동일한 조회 캐시 계층 관례를 따른다 — queries.py(순수 SQL
조회)의 결과를 그대로 감싸며 새 SQL을 직접 작성하지 않는다(계층 규칙, task-M15-brief.md).
get_conn()·current_data_version()은 medsupply.services.inventory를 그대로 재사용한다
(중복 구현 금지).

캐시 규칙:
- load_alerts(): st.cache_data로 결과를 캐시하며, data_version 인자를 캐시 키에 포함해
  무효화 신호로 쓴다(호출부가 inventory.current_data_version()의 값을 넘긴다).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pandas as pd
import streamlit as st

from medsupply.analytics.types import GRADE_ORDER
from medsupply.data import queries, writer
from medsupply.services import inventory

#: 위험등급 심각도 순위(0=가장 심각) — GRADE_ORDER(medsupply.analytics.types)를 그대로
#: 재사용해 등급 서열의 단일 소스를 유지한다.
_GRADE_RANK = {grade.value: rank for rank, grade in enumerate(GRADE_ORDER)}

#: 최신 등급별 등급 상승 알림 severity(브리프 §1) — '주의'로 상승은 매핑에 없어 생성 안 함.
_ESCALATION_SEVERITY = {"위험": "긴급", "경고": "높음"}

#: 입고 지연 규칙(§2) 대상 등급.
_RECEIPT_DELAY_GRADES = {"위험", "경고"}

#: risk_type → 한글 표기(situation.py의 동일 매핑과 같은 값 — docs/data-model.md §2.5).
_RISK_TYPE_LABELS = {
    "demand_surge": "수요 급증",
    "supply_halt": "공급 중단",
    "delivery_delay": "입고 지연",
    "composite": "복합",
    "general": "일반",
}


def _is_escalation(prev_grade: str, new_grade: str) -> bool:
    """new_grade가 prev_grade보다 심각한 등급인가(정상<주의<경고<위험 서열 상 상승)."""
    return _GRADE_RANK[new_grade] < _GRADE_RANK[prev_grade]


def _record(alert_id: int | None, counts: dict[str, int]) -> None:
    """create_alert 반환값을 created/skipped 집계에 반영한다(None=dedupe로 skip)."""
    if alert_id is None:
        counts["skipped"] += 1
    else:
        counts["created"] += 1


def _sync_grade_escalations(
    conn: sqlite3.Connection,
    latest_run_id: str,
    latest_results: pd.DataFrame,
    prev_results: pd.DataFrame,
    item_names: dict[str, str],
    counts: dict[str, int],
) -> None:
    """규칙 1 — 등급 상승(item_id asc, get_risk_results가 이미 그 순서로 반환)."""
    prev_grades = dict(zip(prev_results["item_id"], prev_results["grade"]))

    for row in latest_results.itertuples():
        prev_grade = prev_grades.get(row.item_id)
        if prev_grade is None or not _is_escalation(prev_grade, row.grade):
            continue
        severity = _ESCALATION_SEVERITY.get(row.grade)
        if severity is None:
            continue  # '주의'로 상승은 생성 안 함(브리프 §1).

        item_name = item_names.get(row.item_id, row.item_id)
        risk_type_kr = _RISK_TYPE_LABELS.get(row.risk_type, row.risk_type)
        alert_id = writer.create_alert(
            conn,
            alert_type="grade_up",
            item_id=row.item_id,
            title=f"{item_name} 위험등급 상승",
            body=f"{prev_grade} → {row.grade} · {risk_type_kr}",
            severity=severity,
            dedupe_key=f"grade_up:{row.item_id}:{latest_run_id}",
        )
        _record(alert_id, counts)


def _sync_receipt_delays(
    conn: sqlite3.Connection,
    latest_run_id: str,
    latest_results: pd.DataFrame,
    item_names: dict[str, str],
    counts: dict[str, int],
) -> None:
    """규칙 2 — 입고 지연(직전 run 유무와 무관하게 최신 run만으로 동작)."""
    for row in latest_results.itertuples():
        if row.grade not in _RECEIPT_DELAY_GRADES:
            continue
        factors = json.loads(row.factors_json) if row.factors_json else {}
        delays = [a for a in factors.get("anomalies", []) if a.get("kind") == "receipt_delay"]
        if not delays:
            continue

        item_name = item_names.get(row.item_id, row.item_id)
        alert_id = writer.create_alert(
            conn,
            alert_type="receipt_delay",
            item_id=row.item_id,
            title=f"{item_name} 입고 지연",
            body=delays[0].get("detail", ""),
            severity="높음",
            dedupe_key=f"receipt_delay:{row.item_id}:{latest_run_id}",
        )
        _record(alert_id, counts)


def _sync_notice_map(
    conn: sqlite3.Connection,
    base_date: date,
    item_names: dict[str, str],
    counts: dict[str, int],
) -> None:
    """규칙 3 — 신규 공고 매핑(위험 평가 run과 무관, meta.base_date 기준 활성 매핑)."""
    active_map = queries.get_active_notice_map(conn, as_of=base_date)
    if active_map.empty:
        return

    notices_df = queries.get_notices(conn)
    notice_titles = dict(zip(notices_df["notice_id"], notices_df["title"]))

    ordered = active_map.sort_values(["item_id", "notice_id"]).reset_index(drop=True)
    for row in ordered.itertuples():
        item_name = item_names.get(row.item_id, row.item_id)
        notice_title = notice_titles.get(row.notice_id, row.notice_id)
        alert_id = writer.create_alert(
            conn,
            alert_type="notice_map",
            item_id=row.item_id,
            title=f"{item_name} 공급 공고 매핑",
            body=f"{notice_title} 공고가 매핑되었습니다.",
            severity="확인",
            dedupe_key=f"notice_map:{row.notice_id}:{row.item_id}",
        )
        _record(alert_id, counts)


def sync_alerts(conn: sqlite3.Connection) -> dict:
    """위험 변화·공고 매핑에서 알림을 결정적으로 파생한다(task-M26-brief.md 계약).

    쓰기 연결(conn)로 호출한다 — 호출부가 medsupply.services.workbench.open_write_conn()
    으로 연 커넥션을 넘기고 사용 후 닫는다. 반환 {created: n, skipped: n}(skipped는
    dedupe_key 충돌로 create_alert가 None을 반환한 건수). 규칙 3개 모두 item_id
    오름차순으로 처리한다(결정적 처리 순서):

    1. 등급 상승 — get_latest_runs(conn, 2)의 [최신, 직전]을 비교한다. 직전 run이 없으면
       (전체 run이 1개 이하) 이 규칙 전체를 스킵한다. 품목별 직전→최신 등급이 상승
       (정상<주의<경고<위험)이고 최신이 '위험'이면 긴급, '경고'면 높음, '주의'면 생성
       안 함.
    2. 입고 지연 — 최신 run(직전 run 유무와 무관)에서 grade가 위험/경고이고
       factors_json.anomalies에 receipt_delay가 있으면 높음.
    3. 신규 공고 매핑 — get_active_notice_map(as_of=meta.base_date)의 각
       (notice_id, item_id)마다 확인 등급 알림.
    """
    counts = {"created": 0, "skipped": 0}

    items_df = queries.list_items(conn)
    item_names = dict(zip(items_df["item_id"], items_df["item_name"]))

    latest_runs = queries.get_latest_runs(conn, 2)
    if latest_runs:
        latest_run_id = latest_runs[0]
        latest_results = queries.get_risk_results(conn, latest_run_id)

        if len(latest_runs) >= 2:
            prev_results = queries.get_risk_results(conn, latest_runs[1])
            _sync_grade_escalations(
                conn, latest_run_id, latest_results, prev_results, item_names, counts
            )

        _sync_receipt_delays(conn, latest_run_id, latest_results, item_names, counts)

    meta = queries.get_meta(conn)
    base_date_str = meta.get("base_date")
    if base_date_str:
        _sync_notice_map(conn, date.fromisoformat(base_date_str), item_names, counts)

    return counts


@st.cache_data
def load_alerts(unread_only: bool = False, data_version: int = 0) -> pd.DataFrame:
    """알림 목록 — queries.fetch_alerts 위임(최신순, 최대 50건).

    data_version은 호출부(inventory.current_data_version())가 넘기는 캐시 무효화 신호일
    뿐 조회 조건으로는 쓰이지 않는다.
    """
    del data_version  # 캐시 키 무효화 전용 — 조회 조건에는 쓰지 않는다.
    conn = inventory.get_conn()
    return queries.fetch_alerts(conn, unread_only=unread_only)
