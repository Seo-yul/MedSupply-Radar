"""AI 평가 뷰."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from medsupply.ui.components import header


def render() -> None:
    header("AI 평가", "Langfuse LLM-as-a-Judge 지표로 생성 품질과 회귀를 관리합니다.", "AI 평가")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("근거충실성", "0.91", "+0.03")
    c2.metric("원인관련성", "0.88", "+0.01")
    c3.metric("대응실행가능성", "0.84", "−0.02")
    c4.metric("환각 없음", "96%", "+1%p")
    left, right = st.columns([1.5, 1])
    with left:
        scores = pd.DataFrame({"Experiment":["prompt-v1","prompt-v2","prompt-v3"], "근거충실성":[.82,.88,.91], "원인관련성":[.80,.85,.88], "대응실행가능성":[.76,.86,.84]})
        long = scores.melt("Experiment", var_name="지표", value_name="점수")
        fig = px.line(long, x="Experiment", y="점수", color="지표", markers=True, color_discrete_sequence=["#212b33","#2f6e5c","#a8352a"])
        fig.add_hline(y=.8, line_dash="dash", line_color="#b9af9b", annotation_text="통과 기준 0.8")
        fig.update_layout(height=340, yaxis_range=[.6,1], paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(family="IBM Plex Sans KR", color="#49525a"), yaxis=dict(gridcolor="#eae3d2", tickfont=dict(family="IBM Plex Mono", size=11)), xaxis=dict(tickfont=dict(family="IBM Plex Mono", size=11)), legend_title=None)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False})
    with right:
        st.markdown('<div class="panel"><div class="panel-title">최신 Experiment</div><div class="panel-sub">medsupply-prompt-v3 · 20 cases</div><div class="score"><span>평가 통과</span><strong>18 / 20</strong></div><div class="score"><span>사람 교차검토</span><strong>4 / 4</strong></div><div class="score"><span>이전 버전 대비</span><strong>+2.1%</strong></div><div class="score"><span>회귀 기준</span><strong>PASS</strong></div><br><div class="tiny">Judge 모델과 루브릭 버전을 고정해 비교합니다.</div></div>', unsafe_allow_html=True)
