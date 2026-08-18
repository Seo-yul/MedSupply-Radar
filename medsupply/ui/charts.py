"""품절 위험 게이지와 재고·사용량 추이 차트."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go


def gauge(score: int) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=score,
        number={"font": {"size": 34, "color": "#212b33", "family": "IBM Plex Mono"}},
        gauge={
            "axis": {"range": [0, 100], "tickwidth": 0, "tickcolor": "rgba(0,0,0,0)"},
            "bar": {"color": "#a8352a", "thickness": .24},
            "bgcolor": "#efe8d9", "borderwidth": 0,
            "steps": [{"range": [0, 100], "color": "#efe8d9"}],
            "threshold": {"line": {"color": "#6e1f17", "width": 3}, "thickness": .75, "value": 85},
        },
    ))
    fig.update_layout(height=190, margin=dict(l=20, r=20, t=25, b=0), paper_bgcolor="rgba(0,0,0,0)", font=dict(family="IBM Plex Sans KR", color="#212b33"))
    return fig


def trend_chart() -> go.Figure:
    dates = pd.date_range("2026-07-20", periods=29, freq="D")
    usage = [18,19,17,20,21,19,22,23,24,21,25,27,26,29,32,30,35,38,41,39,44,48,46,51,54,58,56,61,64]
    stock = [890,871,854,834,813,794,772,749,725,704,679,652,626,597,565,535,500,462,421,382,338,290,244,193,139,81,69,38,22]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=stock, name="재고", line=dict(color="#212b33", width=2.4), fill="tozeroy", fillcolor="rgba(33,43,51,.05)"))
    fig.add_trace(go.Scatter(x=dates, y=usage, name="일 사용량", yaxis="y2", line=dict(color="#a8352a", width=2, dash="dot")))
    fig.add_vline(x=dates[18], line_dash="dash", line_color="#59506e", annotation_text="공급중단 공고", annotation_position="top left")
    fig.add_vline(x=dates[23], line_dash="dot", line_color="#966a15", annotation_text="입고 지연", annotation_position="top right")
    fig.update_layout(
        height=305, margin=dict(l=15,r=15,t=20,b=10), hovermode="x unified",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="IBM Plex Sans KR", color="#49525a", size=12),
        legend=dict(orientation="h", y=1.12, x=.72),
        xaxis=dict(showgrid=False, tickfont=dict(family="IBM Plex Mono", size=11)),
        yaxis=dict(gridcolor="#eae3d2", title="재고 수량", tickfont=dict(family="IBM Plex Mono", size=11)),
        yaxis2=dict(overlaying="y", side="right", showgrid=False, title="사용량", tickfont=dict(family="IBM Plex Mono", size=11)),
    )
    return fig
