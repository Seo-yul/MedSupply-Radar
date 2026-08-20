"""발주·조치안 뷰 — 표준 스냅샷 실데이터 렌더(medsupply.services.orders 경유).

마크업·CSS 클래스는 하드코딩 데모 버전(task-M25-brief.md 이전)과 동일하게 유지한다 —
이 파일이 바뀌는 것은 f-string에 들어가는 값뿐이다(재디자인 금지). 자동 발주가 아닌
약사 검토용 요청안 작성이라는 원칙 문구도 그대로 둔다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from medsupply import settings
from medsupply.data import queries, writer
from medsupply.services import inventory, workbench
from medsupply.services import orders as orders_service
from medsupply.ui.components import header

#: 폼 기본값(브리프 §4 — 담당 약사 기본값은 하드코딩 데모부터 유지되어 온 관례값).
_OWNER_DEFAULT = "김약사"
#: 희망 입고일 기본값 = base_date + 이 일수(브리프 §4).
_DESIRED_DATE_LEAD_DAYS = 7


def _select_label(row: Any) -> str:
    """M-16(review.py)과 동일한 selectbox 라벨 규칙(브리프 §1 — "M-16과 동일")."""
    grade = row.grade if pd.notna(row.grade) else "미평가"
    score = int(row.score) if pd.notna(row.score) else "-"
    return f"{row.item_name} — {grade} · {score}"


def render() -> None:
    if not settings.DB_PATH.exists():
        st.warning("표준 스냅샷이 없습니다 — README의 생성 명령을 실행하세요")
        return

    conn = inventory.get_conn()
    data_version = inventory.current_data_version(conn)
    meta = queries.get_meta(conn)
    base_date = date.fromisoformat(meta["base_date"])
    overview = inventory.load_overview(data_version=data_version)

    header(
        "발주·대응 조치안",
        "자동 발주가 아닌 약사 검토용 요청안을 작성하고 후속 업무를 예약합니다.",
        "발주·조치안",
    )

    has_risk_run = bool(queries.get_latest_runs(conn, 1))
    if not has_risk_run:
        st.info(
            "위험 평가 배치를 실행하세요: python scripts/run_risk_batch.py"
            " --db data/medsupply.db --as-of <YYYY-MM-DD>"
        )

    item_ids = overview["item_id"].tolist()
    labels = {row.item_id: _select_label(row) for row in overview.itertuples()}
    selected_item_id = st.selectbox(
        "품목 선택", item_ids, index=0, format_func=lambda iid: labels[iid],
    )

    proposal = orders_service.compute_order_proposal(selected_item_id, data_version=data_version)
    detail = workbench.load_item_detail(selected_item_id, data_version=data_version)
    risk = detail["risk"]

    overview_row = overview.loc[overview["item_id"] == selected_item_id].iloc[0]
    same_condition_count = int(detail["substitutes"]["same_condition"].sum())
    score_text = str(int(overview_row.score)) if pd.notna(overview_row.score) else "-"

    st.markdown(
        '<div class="workflow">'
        f'<div class="workflow-step done"><b>1 · 위험 확인 ✓</b>{overview_row.supply_status} · {score_text}</div>'
        '<div class="workflow-step done"><b>2 · 근거 검토 ✓</b>완료</div>'
        f'<div class="workflow-step done"><b>3 · 대체약 확인 ✓</b>{same_condition_count}개 후보</div>'
        '<div class="workflow-step current"><b>4 · 발주·공유</b>요청안 작성</div>'
        '<div class="workflow-step"><b>5 · 결과 기록</b>대기</div></div>',
        unsafe_allow_html=True,
    )

    grade_text = proposal["grade"] or "미평가"
    depletion_text = risk["depletion_date"] if risk and risk.get("depletion_date") else "-"

    stock_text = "-" if proposal["current_stock"] is None else f"{proposal['current_stock']:,.0f}"
    demand_text = (
        "-" if proposal["expected_demand"] is None else f"{proposal['expected_demand']:,.0f}"
    )
    if proposal["shortage"] is None:
        shortage_text = "-"
    elif proposal["shortage"] == 0:
        shortage_text = "0 (충분)"
    else:
        shortage_text = f"{proposal['shortage']:,.0f}"

    left, right = st.columns([1.55, 1])
    with left:
        st.markdown(
            '<div class="panel">'
            f'<div class="panel-title">{proposal["item_name"]} · 발주 요청안</div>'
            '<div class="panel-sub">현재 재고와 14일 예상 수요를 기준으로 계산한 참고안입니다.</div>'
            '<div class="order-grid">'
            f'<div class="order-stat">현재 재고<b>{stock_text}</b></div>'
            f'<div class="order-stat">14일 예상 수요<b>{demand_text}</b></div>'
            f'<div class="order-stat short">부족 예상량<b>{shortage_text}</b></div>'
            "</div></div>",
            unsafe_allow_html=True,
        )
        supplier_options = proposal["suppliers"] or ["-"]
        c1, c2 = st.columns(2)
        supplier = c1.selectbox("대상 공급사", supplier_options)
        quantity = c2.number_input(
            "요청 수량(정)", min_value=0, value=proposal["suggested_qty"], step=50,
        )
        c3, c4 = st.columns(2)
        desired_date = c3.date_input(
            "희망 입고일", value=base_date + timedelta(days=_DESIRED_DATE_LEAD_DAYS),
        )
        owner = c4.text_input("담당 약사", _OWNER_DEFAULT)
        reason = st.text_area(
            "요청 사유",
            f"{grade_text} 등급 · {overview_row.supply_status} · 예상 소진 {depletion_text}",
        )
        st.warning("요청 수량은 참고값입니다. 실제 발주는 기관 규정과 공급사 확인 후 별도 시스템에서 수행합니다.")
        confirmed = st.checkbox("위험 근거와 요청 수량을 확인했습니다.")
        if st.button("조치안 검토 완료 및 이력 저장", type="primary", use_container_width=True):
            if confirmed:
                desired_date_str = desired_date.isoformat()
                quantity_int = int(quantity)
                write_conn = workbench.open_write_conn()
                try:
                    order_id = writer.save_order_request(
                        write_conn, selected_item_id, supplier, quantity_int,
                        desired_date_str, owner, reason,
                    )
                    writer.save_action_history(
                        write_conn, selected_item_id, "발주 요청", owner,
                        note=f"{supplier} {quantity_int}개 · 희망 {desired_date_str}",
                        status="진행 중", order_id=order_id, risk_type=proposal["risk_type"],
                    )
                finally:
                    write_conn.close()
                st.cache_data.clear()
                st.success(f"{order_id}번 요청안·이력이 저장되었습니다")
            else:
                st.warning("약사 확인 후 저장할 수 있습니다.")
    with right:
        first_supplier = proposal["suppliers"][0] if proposal["suppliers"] else "-"
        next_shipment = detail["next_shipment"]
        if next_shipment is not None:
            next_exp_date = date.fromisoformat(next_shipment["expected_date"])
            shipment_text = f"{first_supplier} · {next_exp_date:%Y.%m.%d}"
        else:
            shipment_text = f"{first_supplier} · 미정"
        st.markdown(
            '<div class="panel"><div class="panel-title">후속 조치</div>'
            f'<div class="task"><b>공급사 입고일 확인</b><span>{shipment_text}</span></div>'
            '<div class="task"><b>처방 부서 사전 공유</b><span>내과·소아청소년과 · 오늘 15:00</span></div>'
            '<div class="task"><b>위험도 재확인</b><span>입고 회신 후 자동 재계산</span></div></div>',
            unsafe_allow_html=True,
        )
        if risk and risk.get("depletion_date"):
            dep = date.fromisoformat(risk["depletion_date"])
            dep_phrase = f"{dep.month}월 {dep.day}일"
        else:
            dep_phrase = "-"
        st.markdown(
            '<div class="panel"><div class="panel-title">공유 메시지 미리보기</div>'
            f'<div class="message-preview">{proposal["item_name"]}의 수급 위험이 {grade_text} 단계입니다.'
            f' 동일 조건 대체품 확보 중이며 예상 소진일은 {dep_phrase}입니다.</div></div>',
            unsafe_allow_html=True,
        )
