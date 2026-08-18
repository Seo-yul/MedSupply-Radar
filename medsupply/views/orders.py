"""발주·조치안 뷰."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from medsupply.ui.components import header


def render() -> None:
    header("발주·대응 조치안", "자동 발주가 아닌 약사 검토용 요청안을 작성하고 후속 업무를 예약합니다.", "발주·조치안")
    st.markdown('<div class="workflow"><div class="workflow-step done"><b>1 · 위험 확인 ✓</b>완료</div><div class="workflow-step done"><b>2 · 근거 검토 ✓</b>완료</div><div class="workflow-step done"><b>3 · 대체약 확인 ✓</b>2개 후보</div><div class="workflow-step current"><b>4 · 발주·공유</b>요청안 작성</div><div class="workflow-step"><b>5 · 결과 기록</b>대기</div></div>', unsafe_allow_html=True)
    left, right = st.columns([1.55, 1])
    with left:
        st.markdown('<div class="panel"><div class="panel-title">아세트아미노펜정 500mg · 발주 요청안</div><div class="panel-sub">현재 재고와 14일 예상 수요를 기준으로 계산한 참고안입니다.</div><div class="order-grid"><div class="order-stat">현재 재고<b>152정</b></div><div class="order-stat">14일 예상 수요<b>356정</b></div><div class="order-stat short">부족 예상량<b>204정</b></div></div></div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        supplier = c1.selectbox("대상 공급사", ["대한제약", "유니메드", "한빛제약"])
        quantity = c2.number_input("요청 수량(정)", min_value=0, value=300, step=50)
        c3, c4 = st.columns(2)
        desired_date = c3.date_input("희망 입고일", value=pd.Timestamp("2026-08-04"))
        owner = c4.text_input("담당 약사", "김약사")
        reason = st.text_area("요청 사유", "필수의약품 현재 품절 · 예상 소진 D-6 · 동일 조건 대체품 확보")
        st.warning("요청 수량은 참고값입니다. 실제 발주는 기관 규정과 공급사 확인 후 별도 시스템에서 수행합니다.")
        confirmed = st.checkbox("위험 근거와 요청 수량을 확인했습니다.")
        if st.button("조치안 검토 완료 및 이력 저장", type="primary", use_container_width=True):
            if confirmed:
                st.success(f"{owner} · {supplier} · {quantity}정 · {desired_date:%Y-%m-%d} 요청안이 저장되었습니다.")
            else:
                st.warning("약사 확인 후 저장할 수 있습니다.")
    with right:
        st.markdown('<div class="panel"><div class="panel-title">후속 조치</div><div class="task"><b>공급사 입고일 확인</b><span>대한제약 · 2026.08.01 11:00</span></div><div class="task"><b>처방 부서 사전 공유</b><span>내과·소아청소년과 · 오늘 15:00</span></div><div class="task"><b>위험도 재확인</b><span>입고 회신 후 자동 재계산</span></div></div>', unsafe_allow_html=True)
        st.markdown('<div class="panel"><div class="panel-title">공유 메시지 미리보기</div><div class="message-preview">아세트아미노펜정 500mg의 수급 위험이 매우 높습니다. 동일 조건 대체품 확보 중이며 예상 소진일은 8월 7일입니다.</div></div>', unsafe_allow_html=True)
