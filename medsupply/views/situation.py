"""수급 상황실 뷰 — 표준 스냅샷 실데이터 렌더(medsupply.services.inventory 경유).

마크업·CSS 클래스는 하드코딩 데모 버전(task-M15-brief.md 이전)과 동일하게 유지한다 —
이 파일이 바뀌는 것은 f-string에 들어가는 값뿐이다(재디자인 금지).
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import streamlit as st

from medsupply import settings
from medsupply.data import queries
from medsupply.services import inventory
from medsupply.ui.components import header
from medsupply.views import PAGE_REGISTRY

#: risk_results.risk_type → 한글 표기(docs/data-model.md §2.5).
_RISK_TYPE_LABELS = {
    "demand_surge": "수요 급증",
    "supply_halt": "공급 중단",
    "delivery_delay": "입고 지연",
    "composite": "복합",
    "general": "일반",
}


def _delta_text(delta: int | None) -> str | None:
    """st.metric delta 표기(부호 있는 정수 문자열). delta가 None이면 델타 미표시."""
    if delta is None:
        return None
    return f"{delta:+d}"


def _receipt_delay_stats(latest_results: pd.DataFrame) -> tuple[int, float]:
    """최신 run의 factors_json에서 receipt_delay 신호 보유 품목 수·평균 지연일을 구한다."""
    item_count = 0
    metrics: list[float] = []
    for factors_raw in latest_results["factors_json"]:
        factors = json.loads(factors_raw) if factors_raw else {}
        item_delays = [
            a["metric"] for a in factors.get("anomalies", []) if a.get("kind") == "receipt_delay"
        ]
        if item_delays:
            item_count += 1
            metrics.extend(item_delays)
    avg_delay = (sum(metrics) / len(metrics)) if metrics else 0.0
    return item_count, avg_delay


def render() -> None:
    if not settings.DB_PATH.exists():
        st.warning("표준 스냅샷이 없습니다 — README의 생성 명령을 실행하세요")
        return

    conn = inventory.get_conn()
    data_version = inventory.current_data_version(conn)
    meta = queries.get_meta(conn)

    base_date = date.fromisoformat(meta["base_date"])
    issue_no = base_date.timetuple().tm_yday
    mast_date_str = f"{base_date.year}년 {base_date.month}월 {base_date.day}일 09:30 기준"

    overview_all = inventory.load_overview(data_version=data_version)
    latest_runs = queries.get_latest_runs(conn, 2)
    has_batch = bool(latest_runs)

    today_danger = int((overview_all["grade"] == "위험").sum())
    today_soon = int((overview_all["days_to_stockout"] <= 7).sum())
    normal_count = int((overview_all["grade"] == "정상").sum())
    essential_risk_count = int(
        (
            (overview_all["is_essential"] == 1)
            & overview_all["grade"].isin(["위험", "경고"])
        ).sum()
    )

    danger_delta: int | None = None
    soon_delta: int | None = None
    if len(latest_runs) >= 2:
        prev_results = queries.get_risk_results(conn, latest_runs[1])
        prev_danger = int((prev_results["grade"] == "위험").sum())
        prev_soon = int((prev_results["days_to_stockout"] <= 7).sum())
        danger_delta = today_danger - prev_danger
        soon_delta = today_soon - prev_soon

    delay_items, avg_delay = (0, 0.0)
    depletion_map: dict[str, str | None] = {}
    if has_batch:
        latest_results = queries.get_risk_results(conn, latest_runs[0])
        delay_items, avg_delay = _receipt_delay_stats(latest_results)
        # list_items(→ load_overview)는 depletion_date를 싣지 않으므로(계약 컬럼 밖),
        # risk-meta의 "예상 소진" 표기는 이미 조회한 latest_results에서 따로 조인한다.
        depletion_map = dict(zip(latest_results["item_id"], latest_results["depletion_date"]))

    active_map = queries.get_active_notice_map(conn, as_of=base_date)
    notice_count = int(active_map["notice_id"].nunique()) if not active_map.empty else 0
    mapped_item_count = int(active_map["item_id"].nunique()) if not active_map.empty else 0

    pending_notices = len(queries.get_notices(conn, status="확인 필요"))

    st.markdown(
        '<div class="masthead"><div class="mast-row"><span>선경병원 약제부 · 의약품 수급 일보</span>'
        f'<span><b>제 {issue_no}호</b> · {mast_date_str}</span></div>'
        '<div class="mast-title">MedSupply Radar</div>'
        '<div class="mast-sub">재고·사용량·입고·공급중단 신호를 통합해 품절 위험과 약사 조치 우선순위를'
        ' 알립니다.</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="incident-strip">'
        f'<div class="incident red">품절 임박<b>{today_soon} 품목 · 7일 이내</b></div>'
        f'<div class="incident amber">입고 지연<b>{delay_items}건 · 평균 {avg_delay:.1f}일</b></div>'
        f'<div class="incident purple">외부 공급 공고<b>신규 {notice_count}건 매핑</b></div>'
        f'<div class="incident teal">정상 공급<b>{normal_count} 품목 · 안정</b></div>'
        "</div>",
        unsafe_allow_html=True,
    )
    header("오늘의 의약품 수급 상황", "확인이 필요한 품목부터 약사 업무 우선순위로 정렬했습니다.", "수급 상황실")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("관제 품목", meta.get("item_count", "0"), None)
    c2.metric("최고 위험", str(today_danger), _delta_text(danger_delta))
    c3.metric("7일 내 소진", str(today_soon), _delta_text(soon_delta), delta_color="inverse")
    c4.metric("조치 대기", str(pending_notices), None)
    st.write("")
    left, right = st.columns([1.65, 1])
    with left:
        st.markdown(
            '<div class="panel"><div class="panel-title">의약품 공급 상태</div>'
            '<div class="panel-sub">2026.01.01~08.01 · 상태별 사건과 대응 진행상황을 확인합니다.</div>',
            unsafe_allow_html=True,
        )
        status_filter = st.radio(
            "공급 상태",
            ["전체", "현재 품절", "품절 예상", "공급중단", "정상화"],
            horizontal=True,
            label_visibility="collapsed",
        )
        status_arg = None if status_filter == "전체" else status_filter
        filtered = inventory.load_overview(status=status_arg, data_version=data_version).head(12)

        if not has_batch:
            st.info(
                "위험 평가 배치를 실행하세요: python scripts/run_risk_batch.py"
                " --db data/medsupply.db --as-of <YYYY-MM-DD>"
            )

        for row in filtered.itertuples():
            event_css = (
                "event-red"
                if row.supply_status == "현재 품절"
                else "event-purple"
                if row.supply_status == "공급중단"
                else "event-amber"
                if row.supply_status == "품절 예상"
                else "safe"
            )
            score_text = f"{int(row.score)}점" if pd.notna(row.score) else "-"
            days_text = f"D-{int(row.days_to_stockout)}" if pd.notna(row.days_to_stockout) else "D-"
            st.markdown(
                f'<div class="risk-row"><div class="drug">{row.item_name}<small>{row.ingredient_name_kr} · '
                f'<span class="unit"><span class="unit-icon">{row.form_code}</span>{row.form} · {row.route}'
                f'</span></small></div><span class="event-pill {event_css}">{row.supply_status}</span>'
                f"<b>{score_text}</b><span>{days_text}</span></div>",
                unsafe_allow_html=True,
            )
            essential = "필수의약품" if row.is_essential == 1 else "일반 관리품목"
            depletion_value = depletion_map.get(row.item_id)
            depletion_text = "-" if pd.isna(depletion_value) else depletion_value
            risk_type_text = (
                _RISK_TYPE_LABELS.get(row.risk_type, "-") if pd.notna(row.risk_type) else "-"
            )
            grade_text = row.grade if pd.notna(row.grade) else "-"
            st.markdown(
                f'<div class="risk-meta"><span>예상 소진 <b>{depletion_text}</b></span>'
                f'<span><b>{essential}</b></span>'
                f'<span>위험 유형 <b>{risk_type_text}</b></span>'
                f'<span>등급 <b>{grade_text}</b></span></div>',
                unsafe_allow_html=True,
            )
        if filtered.empty:
            st.info("해당 상태의 품목이 없습니다.")
        st.markdown('</div>', unsafe_allow_html=True)
        if st.button(f"검토 대기 {pending_notices}건 확인 →", type="primary", use_container_width=True):
            if not overview_all.empty:
                st.session_state["selected_drug"] = overview_all.iloc[0]["item_name"]
            target = PAGE_REGISTRY.get("review")
            if target is not None:
                st.switch_page(target)
    with right:
        top3 = overview_all.head(3)
        task_html = "".join(
            f'<div class="task"><b>{i + 9}:00 · {row.grade if pd.notna(row.grade) else "-"}</b>'
            f'<span>{row.item_name} 대체 가능성·발주 확인</span></div>'
            for i, row in enumerate(top3.itertuples())
        )
        st.markdown(
            '<div class="panel"><div class="panel-title">나의 오늘 할 일</div>'
            '<div class="panel-sub">위험도와 예상 소진일 기준</div>' + task_html + "</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="notice"><b>오늘의 핵심 신호</b><br>필수의약품 {essential_risk_count}종이'
            f' 10일 이내 소진될 수 있습니다. 신규 공급 공고 {notice_count}건이 기관 품목'
            f' {mapped_item_count}개와 매핑되었습니다.</div>',
            unsafe_allow_html=True,
        )
    st.markdown(
        '<div class="workflow"><div class="workflow-step done"><b>1 · 위험 확인</b>상황실</div>'
        f'<div class="workflow-step current"><b>2 · 근거 검토</b>대기 {pending_notices}건</div>'
        '<div class="workflow-step"><b>3 · 대체약 확인</b>동일 조건</div>'
        '<div class="workflow-step"><b>4 · 발주·공유</b>요청안 작성</div>'
        '<div class="workflow-step"><b>5 · 결과 기록</b>효과 추적</div></div>',
        unsafe_allow_html=True,
    )
