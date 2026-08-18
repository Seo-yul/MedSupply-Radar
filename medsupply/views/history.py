"""대응 이력과 결과 추적 뷰."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from medsupply.ui.components import header


def render() -> None:
    header("대응 이력과 결과 추적", "조치가 실제 위험을 낮췄는지 확인하고 다음 대응의 기준으로 축적합니다.", "대응 이력")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("완료 조치", "18", "+4 이번 주")
    c2.metric("진행 중", "2", "−1 전일 대비")
    c3.metric("평균 처리시간", "3.4시간", "−0.8시간")
    c4.metric("위험도 하락", "14건", "완료 조치의 78%")
    st.write("")
    history = pd.DataFrame([
        ["2026-08-01 09:12", "아세트아미노펜정 500mg", "대체품 300정 요청안", "공급사 회신 대기", "김약사", "진행 중"],
        ["2026-07-31 15:40", "세프트리악손주 1g", "입고 일정 재확인", "8월 5일 100 vial 확정", "김약사", "완료"],
        ["2026-07-29 11:20", "덱시부프로펜시럽", "대체 재고 확보", "위험 76 → 42 하락", "김약사", "완료"],
    ], columns=["일시", "품목", "조치", "결과", "담당자", "상태"])
    st.dataframe(history, use_container_width=True, hide_index=True)
    st.success("완료된 조치 18건 중 14건에서 품목 위험도가 실제로 하락했습니다.")
