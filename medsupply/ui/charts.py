"""품절 위험 게이지와 재고·사용량 추이 차트."""

from __future__ import annotations

from datetime import timedelta

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


def trend_chart(
    series_df: pd.DataFrame,
    *,
    events: list[tuple] | None = None,
    forecast: list[float] | None = None,
) -> go.Figure:
    """재고·사용량 추이 차트(품목 상세). series_df 컬럼: date·closing_stock·usage_qty.

    events: [(date, 라벨, 색)] — 있는 항목만 세로 기준선(vline)으로 표시한다(호출부가
    "조회 불가"인 이벤트는 아예 리스트에 넣지 않는다 — 이 함수는 있는 것만 그린다).
    forecast: get_forecast(run)의 daily 리스트 — 있으면 마지막 실측일 다음 날부터 이어지는
    예측 구간 점선 trace(예측 사용량)로 추가한다.
    """
    has_data = not series_df.empty
    dates = pd.to_datetime(series_df["date"]) if has_data else pd.Series([], dtype="datetime64[ns]")
    stock = series_df["closing_stock"] if has_data else []
    usage = series_df["usage_qty"] if has_data else []
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=stock, name="재고", line=dict(color="#212b33", width=2.4), fill="tozeroy", fillcolor="rgba(33,43,51,.05)"))
    fig.add_trace(go.Scatter(x=dates, y=usage, name="일 사용량", yaxis="y2", line=dict(color="#a8352a", width=2, dash="dot")))
    if forecast and has_data:
        last_date = dates.iloc[-1]
        forecast_dates = [last_date + timedelta(days=i + 1) for i in range(len(forecast))]
        fig.add_trace(go.Scatter(x=forecast_dates, y=forecast, name="예측 사용량", yaxis="y2", line=dict(color="#59506e", width=2, dash="dash")))
    for event_date, label, color in events or []:
        fig.add_vline(x=pd.Timestamp(event_date), line_dash="dash", line_color=color, annotation_text=label, annotation_position="top left")
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
