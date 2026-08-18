"""검토 대기함(약사 검토 워크벤치) 뷰."""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from medsupply.ui.charts import gauge, trend_chart
from medsupply.ui.components import header
from medsupply.views._demo import DRUGS, copilot_answer


def render() -> None:
    header("약사 검토 워크벤치", "위험 근거와 동일 조건 대체 후보를 확인한 뒤 조치안을 작성합니다.", "검토 대기함")
    selected = st.selectbox("품목 선택", DRUGS["품목"], index=0)
    row = DRUGS[DRUGS["품목"] == selected].iloc[0]
    st.markdown(f'''<div class="drug-label"><div class="label-top"><div><div class="label-name">{row['품목']}</div><div class="label-inn">{row['성분명'].upper()} · {row['분류']}</div><div class="label-meta"><span class="meta-chip">500 mg</span><span class="meta-chip">{row['제형']}</span><span class="meta-chip">{row['투여경로']}</span><span class="meta-chip">전문의약품</span><span class="meta-chip">필수의약품</span></div></div><div><span class="rx">Rx</span></div></div><div class="source-strip"><span>품목기준코드 20260817001</span><span>제조사 {row['공급사']}</span><span>포장단위 100정/병</span><span>최종 갱신 2026.08.01 09:30</span></div></div>''', unsafe_allow_html=True)
    st.markdown('<div class="workflow"><div class="workflow-step done"><b>1 · 위험 확인 ✓</b>현재 품절 · 92</div><div class="workflow-step done"><b>2 · 근거 검토 ✓</b>근거 4건 일치</div><div class="workflow-step current"><b>3 · 대체약 검토</b>동일 조건 2개</div><div class="workflow-step"><b>4 · 조치 확정</b>약사 확인</div><div class="workflow-step"><b>5 · 결과 추적</b>이력 관리</div></div>', unsafe_allow_html=True)
    summary_col, copilot_col = st.columns([1.8, 1])
    with summary_col:
        st.markdown('<div class="panel"><div class="panel-title">검토 요약</div><div class="panel-sub">현재 선택 품목의 핵심 위험 신호</div><div class="score"><span>품절 위험</span><strong style="color:#a8352a">92 · 위험</strong></div><div class="score"><span>예상 소진</span><strong>6일 후</strong></div><div class="score"><span>동일 조건 대체 후보</span><strong>2개</strong></div><div class="score"><span>담당자 확인</span><strong style="color:#966a15">검토 중</strong></div></div>', unsafe_allow_html=True)
    with copilot_col:
        with st.container(border=True):
            st.markdown("#### AI 문의")
            st.caption(f"현재 품목: {row['품목']} · 근거 확인용 코파일럿")
            if "copilot_messages" not in st.session_state:
                st.session_state.copilot_messages = []
            q1, q2 = st.columns(2)
            suggested = None
            if q1.button("왜 위험한가요?", use_container_width=True):
                suggested = "왜 위험한가요?"
            if q2.button("소진일 계산 근거", use_container_width=True):
                suggested = "예상 소진일 계산 근거를 알려줘"
            q3, q4 = st.columns(2)
            if q3.button("대체 후보 비교", use_container_width=True):
                suggested = "동일 조건 대체 후보를 비교해줘"
            if q4.button("발주량 계산 근거", use_container_width=True):
                suggested = "발주 요청량 계산 근거를 알려줘"
            if suggested:
                st.session_state.copilot_messages.append((suggested, copilot_answer(suggested)))
            for user_text, answer_text in st.session_state.copilot_messages[-2:]:
                with st.chat_message("user"):
                    st.markdown(user_text)
                with st.chat_message("assistant"):
                    st.markdown(answer_text)
            free_question = st.text_input("직접 질문", placeholder="공고 내용을 요약해줘", label_visibility="collapsed")
            if st.button("질문 보내기", type="primary", use_container_width=True):
                if free_question.strip():
                    st.session_state.copilot_messages.append((free_question, copilot_answer(free_question)))
                    st.rerun()
            st.caption("답변은 의사결정 참고용이며 발주·대체를 자동 실행하지 않습니다.")
    a, b, c, d = st.columns(4)
    a.metric("현재 재고", "152정", "−35% / 7일", delta_color="inverse")
    b.metric("일평균 사용량", "25.4정", "+41% / 4주")
    c.metric("예상 소진", f"{row['예상소진일']}일 후", "8월 23일")
    d.metric("다음 입고", "미정", "5일 지연")
    left, right = st.columns([1.7, 1])
    with left:
        st.markdown('<div class="panel"><div class="panel-title">Risk Timeline</div><div class="panel-sub">재고와 사용량 변화 · 최근 4주</div>', unsafe_allow_html=True)
        st.plotly_chart(trend_chart(), use_container_width=True, config={"displayModeBar":False})
        st.markdown('</div>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="panel"><div class="panel-title">품절 위험점수</div><div class="panel-sub">결정적 규칙 기반 산정 · LLM 판정 미관여</div>', unsafe_allow_html=True)
        st.plotly_chart(gauge(int(row["위험점수"])), use_container_width=True, config={"displayModeBar":False})
        st.markdown('<div class="factor"><b>재고 커버리지 · +38점</b><span>현재 추세 기준 6일 후 소진 예상</span></div><div class="factor"><b>수요 급증 · +24점</b><span>최근 4주 사용량 41% 증가</span></div><div class="factor"><b>공급중단 · +20점</b><span>8월 15일 제조사 공고 매핑</span></div><div class="factor"><b>입고 지연 · +10점</b><span>예정일 대비 5일 지연</span></div></div>', unsafe_allow_html=True)
    info_tab, alt_tab, source_tab = st.tabs(["AI 근거 설명", "대체 후보", "공고·근거"])
    with info_tab:
        st.markdown('<div class="panel"><div class="panel-title">왜 위험한가?</div><div class="panel-sub">NHS SPS 방식으로 핵심 판단과 권장 조치를 분리했습니다.</div><div class="notice">최근 4주간 일평균 사용량이 18정에서 25.4정으로 <b>41% 증가</b>한 반면, 현재 재고는 152정으로 감소했습니다. 제조사의 공급중단 공고와 입고 지연이 동시에 확인되어, 현재 추세가 유지되면 <b>6일 이내 소진</b>될 가능성이 높습니다.</div><br><div class="action"><b>01 · 대체 가능 품목 재고 확인</b><p>동일 성분·함량·제형 후보 2개를 확인하고 약사가 대체 가능 여부를 검토합니다.</p></div><div class="action"><b>02 · 유통사 입고 일정 재확인</b><p>미확정 발주 건의 공급 가능 수량과 최단 입고일을 확인합니다.</p></div><div class="action"><b>03 · 사용 부서에 위험 공유</b><p>예상 소진일과 대체 검토 필요성을 처방 부서에 사전 공유합니다.</p></div></div>', unsafe_allow_html=True)
    with alt_tab:
        st.markdown('''<div class="clinical-warning"><b>약사 확인 필수</b> · 동일 조건 후보와 조건이 다른 후보를 구분하며, 자동 대체 처방을 의미하지 않습니다.</div><div class="med-tree"><div class="tree-root"><span class="molecule">⌬</span><div>아세트아미노펜<small style="display:block;font-family:var(--mono);color:#7d786c;font-size:10px;font-weight:500;letter-spacing:.06em">ACETAMINOPHEN · 동일 성분 관계</small></div></div><div class="tree-group"><div class="tree-condition">500mg · 정제 · 경구</div><div class="tree-node current"><strong>현재 품목 · 한빛제약</strong><span class="stock-low">재고 152정</span></div><div class="tree-node"><strong>동일 조건 · 대한제약</strong><span class="stock-ok">재고 420정</span></div><div class="tree-node"><strong>동일 조건 · 유니메드</strong><span class="stock-low">재고 84정</span></div></div><div class="tree-section-label">조건이 다른 후보</div><div class="tree-group"><div class="tree-condition" style="color:#59506e">650mg · 서방정 · 경구</div><div class="tree-node mismatch"><strong>아세트아미노펜서방정 650mg</strong><span class="badge inactive">조건 불일치</span><small style="display:block;clear:both;color:#966a15;padding-top:6px">함량·방출 제형 상이 · 처방 변경 및 임상 검토 필요</small></div></div></div>''', unsafe_allow_html=True)
    with source_tab:
        st.markdown('<div class="panel"><div class="panel-title">판단 근거와 출처</div><div class="score"><span>기관 재고 스냅샷</span><strong>08.01 09:30</strong></div><div class="score"><span>최근 4주 사용량</span><strong>+41%</strong></div><div class="score"><span>제조사 공급중단 공고</span><strong>원문 확인</strong></div><div class="score"><span>입고예정 데이터</span><strong>5일 지연</strong></div><br><div class="tiny">AI는 위험등급 판정에 관여하지 않으며 입력 근거를 자연어로 요약합니다.</div></div>', unsafe_allow_html=True)
    with st.expander("약사 검토 및 대응 조치 기록", expanded=True):
        col1, col2 = st.columns(2)
        action_type = col1.selectbox("조치 유형", ["입고 일정 확인", "대체 품목 검토", "발주량 조정", "처방 부서 공유"])
        owner = col2.text_input("담당자", "김약사")
        note = st.text_area("조치 내용", placeholder="확인한 내용과 후속 계획을 입력하세요.")
        reviewed = st.checkbox("위험 근거와 대체 후보 조건을 확인했습니다.")
        if st.button("이력 저장", type="primary"):
            if reviewed:
                st.success(f"{datetime.now():%Y-%m-%d %H:%M} · {owner} · {action_type} 이력이 저장되었습니다.")
            else:
                st.warning("약사 검토 확인 후 저장할 수 있습니다.")
