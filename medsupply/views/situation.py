"""수급 상황실 뷰."""

from __future__ import annotations

import streamlit as st

from medsupply.ui.components import header
from medsupply.views import PAGE_REGISTRY
from medsupply.views._demo import DRUGS


def render() -> None:
    st.markdown('<div class="masthead"><div class="mast-row"><span>선경병원 약제부 · 의약품 수급 일보</span><span><b>제 213호</b> · 2026년 8월 1일 09:30 기준</span></div><div class="mast-title">MedSupply Radar</div><div class="mast-sub">재고·사용량·입고·공급중단 신호를 통합해 품절 위험과 약사 조치 우선순위를 알립니다.</div></div>', unsafe_allow_html=True)
    st.markdown('<div class="incident-strip"><div class="incident red">품절 임박<b>3 품목 · 7일 이내</b></div><div class="incident amber">입고 지연<b>4건 · 평균 4.2일</b></div><div class="incident purple">외부 공급 공고<b>신규 3건 매핑</b></div><div class="incident teal">정상 공급<b>72 품목 · 안정</b></div></div>', unsafe_allow_html=True)
    header("오늘의 의약품 수급 상황", "확인이 필요한 품목부터 약사 업무 우선순위로 정렬했습니다.", "수급 상황실")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("관제 품목", "100", "+4 신규")
    c2.metric("최고 위험", "3", "+1 오늘")
    c3.metric("7일 내 소진", "5", "+2 전일 대비", delta_color="inverse")
    c4.metric("조치 대기", "4", "−2 처리 완료")
    st.write("")
    left, right = st.columns([1.65, 1])
    with left:
        st.markdown('<div class="panel"><div class="panel-title">의약품 공급 상태</div><div class="panel-sub">2026.01.01~08.01 · 상태별 사건과 대응 진행상황을 확인합니다.</div>', unsafe_allow_html=True)
        status_filter = st.radio("공급 상태", ["전체", "현재 품절", "품절 예상", "공급중단", "정상화"], horizontal=True, label_visibility="collapsed")
        filtered = DRUGS if status_filter == "전체" else DRUGS[DRUGS["공급상태"] == status_filter]
        for row in filtered.itertuples():
            css = {"매우 높음":"critical", "높음":"high", "관찰":"watch", "안정":"safe"}[row.위험등급]
            event_css = "event-red" if row.공급상태 == "현재 품절" else "event-purple" if row.공급상태 == "공급중단" else "event-amber" if row.공급상태 == "품절 예상" else "safe"
            form_code = {"바이알": "INJ", "시럽": "SYR", "캡슐": "CAP"}.get(row.제형, "TAB")
            st.markdown(f'<div class="risk-row"><div class="drug">{row.품목}<small>{row.성분명} · <span class="unit"><span class="unit-icon">{form_code}</span>{row.제형} · {row.투여경로}</span></small></div><span class="event-pill {event_css}">{row.공급상태}</span><b>{row.위험점수}점</b><span>D-{row.예상소진일}</span></div>', unsafe_allow_html=True)
            essential = "필수의약품" if row.필수의약품 == "Y" else "일반 관리품목"
            normal_date = row.예상정상화일 if row.공급상태 != "정상화" else f"{row.예상정상화일} 정상화"
            st.markdown(f'<div class="risk-meta"><span>최초 발생 <b>{row.최초발생일}</b></span><span>최근 갱신 <b>{row.최근갱신일}</b></span><span>예상 정상화 <b>{normal_date}</b></span><span><b>{essential}</b></span><span>대응 <b>{row.대응상태}</b></span></div>', unsafe_allow_html=True)
        if filtered.empty:
            st.info("해당 상태의 품목이 없습니다.")
        st.markdown('</div>', unsafe_allow_html=True)
        if st.button("검토 대기 4건 확인 →", type="primary", use_container_width=True):
            st.session_state["selected_drug"] = DRUGS.iloc[0]["품목"]
            target = PAGE_REGISTRY.get("review")
            if target is not None:
                st.switch_page(target)
    with right:
        st.markdown('<div class="panel"><div class="panel-title">나의 오늘 할 일</div><div class="panel-sub">위험도와 예상 소진일 기준</div><div class="task"><b>09:00 · 긴급</b><span>아세트아미노펜 동일 조건 대체품 확인</span></div><div class="task"><b>11:00 · 발주 확인</b><span>세프트리악손 공급사 입고일 재확인</span></div><div class="task"><b>15:00 · 부서 공유</b><span>내과·소아청소년과 수급위험 전달</span></div></div>', unsafe_allow_html=True)
        st.markdown('<div class="notice"><b>오늘의 핵심 신호</b><br>필수의약품 2종이 10일 이내 소진될 수 있습니다. 신규 공급 공고 1건이 기관 품목 3개와 매핑되었습니다.</div>', unsafe_allow_html=True)
    st.markdown('<div class="workflow"><div class="workflow-step done"><b>1 · 위험 확인</b>상황실</div><div class="workflow-step current"><b>2 · 근거 검토</b>대기 4건</div><div class="workflow-step"><b>3 · 대체약 확인</b>동일 조건</div><div class="workflow-step"><b>4 · 발주·공유</b>요청안 작성</div><div class="workflow-step"><b>5 · 결과 기록</b>효과 추적</div></div>', unsafe_allow_html=True)
