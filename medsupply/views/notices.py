"""공급 공고 매핑 뷰."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from medsupply.ui.components import header


def render() -> None:
    header("공급 공고 매핑", "외부 공고를 구조화하고 기관 보유 품목과 자동으로 연결합니다.", "공급 공고")
    st.markdown('<div class="panel"><div class="panel-title">신규 공고 3건</div><div class="panel-sub">원문과 AI 추출 결과를 함께 확인할 수 있습니다.</div>', unsafe_allow_html=True)
    notices = pd.DataFrame([
        ["2026-08-15", "아세트아미노펜 500mg 공급중단 안내", "대한제약", "3개", "검토 필요"],
        ["2026-08-14", "세프트리악손주 출하 지연", "메디팜", "1개", "높은 신뢰도"],
        ["2026-08-12", "덱시부프로펜시럽 공급 정상화", "한빛제약", "2개", "높은 신뢰도"],
    ], columns=["공고일", "제목", "공급사", "매핑 품목", "AI 신뢰도"])
    st.dataframe(notices, use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)
    with st.expander("아세트아미노펜 공고 추출 결과", expanded=True):
        x1, x2 = st.columns(2)
        with x1:
            st.text_area("공고 원문", "원료 수급 차질로 인해 아세트아미노펜정 500mg 제품의 공급을 2026년 8월 15일부터 잠정 중단합니다...", height=180, disabled=True)
        with x2:
            st.json({"성분":"아세트아미노펜", "함량":"500mg", "사유":"원료 수급 차질", "기간":"2026-08-15 ~ 미정", "기관 매핑":"3개 품목", "확인 상태":"담당자 검토 필요"})
