from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


st.set_page_config(
    page_title="MedSupply Radar",
    page_icon="💊",
    layout="wide",
    initial_sidebar_state="expanded",
)


CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Hahmlet:wght@500;600;700;800&family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap');
:root { --paper:#faf7f0; --paper-2:#f3ede0; --card:#fffcf6; --ink:#212b33; --ink-soft:#49525a; --muted:#7d786c; --faint:#a9a296; --seal:#a8352a; --amber:#966a15; --green:#2f6e5c; --plum:#59506e; --rule:#dcd4c2; --dotted:#c9c0ac; --serif:'Hahmlet','Noto Serif KR',serif; --sans:'IBM Plex Sans KR','Apple SD Gothic Neo',sans-serif; --mono:'IBM Plex Mono','IBM Plex Sans KR',monospace; }
html, body, .stApp { font-family:var(--sans); }
.stApp { background:var(--paper); color:var(--ink); }
h1, h2, h3, h4, h5, h6 { font-family:var(--serif); letter-spacing:-.01em; }
[data-testid="stSidebar"] { background:var(--paper-2); border-right:1px solid var(--rule); }
[data-testid="stSidebar"] [data-testid="stPageLink"] a { padding:.28rem .55rem; border-radius:3px; }
[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover { background:rgba(33,43,51,.05); }
[data-testid="stSidebar"] [data-testid="stPageLink"] a p { font-size:14px; }
.toc-label { display:flex; align-items:center; gap:6px; font-family:var(--mono); font-size:10px; font-weight:600; letter-spacing:.18em; color:var(--muted); text-transform:uppercase; padding:2px 0 7px; border-bottom:1px solid var(--rule); margin-bottom:7px; }
.toc-icon { font-size:12px; letter-spacing:0; }
[data-testid="stSidebar"] hr { border-color:var(--rule); }
[data-testid="stSidebar"] p strong { font-family:var(--mono); font-weight:600; font-size:13px; }
[data-testid="stMetric"] { background:transparent; border:0; border-top:1px solid var(--ink); border-radius:0; padding:9px 2px 0; box-shadow:none; }
[data-testid="stMetricLabel"] { color:var(--muted); font-weight:600; letter-spacing:.02em; }
[data-testid="stMetricValue"] { color:var(--ink); font-family:var(--mono); font-weight:600; }
[data-testid="stMetricDelta"] { font-family:var(--mono); font-size:12px; background:transparent; padding-left:0; filter:saturate(.5) brightness(.82); }
div[data-testid="stHorizontalBlock"] { gap:1.1rem; }
.block-container { padding-top:3rem; padding-bottom:3rem; max-width:1420px; }
.brand { display:flex; align-items:center; gap:9px; font-family:var(--serif); font-size:17px; font-weight:700; padding:3px 0 18px; }
.brand-mark { width:30px; height:30px; display:grid; place-items:center; border-radius:3px; border:1.5px solid var(--seal); color:var(--seal); font-size:16px; }
.masthead { padding:2px 0 0; margin-bottom:16px; border-bottom:4px double var(--ink); }
.mast-row { display:flex; justify-content:space-between; align-items:baseline; gap:12px; padding-bottom:9px; border-bottom:1px solid var(--rule); font-family:var(--mono); font-size:10.5px; letter-spacing:.1em; color:var(--muted); }
.mast-row b { color:var(--seal); font-weight:600; }
.mast-title { font-family:var(--serif); font-size:42px; font-weight:800; letter-spacing:-.015em; line-height:1.12; padding:16px 0 2px; }
.mast-sub { color:var(--muted); font-size:13px; padding-bottom:14px; }
.eyebrow { font-family:var(--mono); color:var(--seal); font-size:10.5px; font-weight:600; letter-spacing:.18em; text-transform:uppercase; }
.page-title { font-family:var(--serif); font-size:30px; line-height:1.22; font-weight:700; margin:5px 0 5px; letter-spacing:-.02em; }
.page-sub { color:var(--muted); font-size:13.5px; margin-bottom:20px; padding-bottom:15px; border-bottom:1px solid var(--rule); }
.panel { background:transparent; border:0; border-top:2px solid var(--ink); border-radius:0; padding:11px 2px 2px; margin-bottom:12px; }
.panel-title { font-family:var(--serif); font-weight:700; font-size:16.5px; margin-bottom:3px; }
.panel-sub { color:var(--muted); font-size:12px; margin-bottom:12px; }
.risk-row { display:grid; grid-template-columns:1.65fr .65fr .55fr .55fr; gap:10px; align-items:start; padding:14px 2px 6px; font-size:13px; }
.risk-row b { font-family:var(--mono); font-weight:600; font-size:14px; }
.risk-row > span:last-child { font-family:var(--mono); color:var(--ink-soft); }
.drug { font-family:var(--serif); font-weight:600; font-size:14.5px; }
.drug small { display:block; font-family:var(--sans); color:var(--muted); font-weight:400; margin-top:4px; }
.risk-meta { display:flex; flex-wrap:wrap; gap:5px 16px; margin:0 2px 11px; padding:0 0 12px; border-bottom:1px dotted var(--dotted); color:var(--faint); font-family:var(--mono); font-size:10px; }
.risk-meta b { color:var(--ink-soft); font-weight:600; }
.badge { display:inline-block; width:max-content; padding:3px 8px; border-radius:2px; border:1.5px solid currentColor; background:transparent; font-family:var(--mono); font-size:10.5px; font-weight:600; letter-spacing:.08em; }
.badge.critical { transform:rotate(-2deg); }
.critical { color:var(--seal); } .high { color:var(--amber); }
.watch { color:var(--plum); } .safe { color:var(--green); }
.inactive { color:var(--muted); }
.notice { border-left:4px double var(--ink); background:var(--paper-2); padding:13px 16px; color:var(--ink-soft); font-size:13px; }
.factor { padding:9px 13px; border-left:2px solid var(--rule); margin:10px 0; }
.factor b { display:block; font-size:13px; } .factor span { color:var(--muted); font-size:12px; }
.action { padding:13px 15px; border:1px solid var(--rule); border-radius:3px; margin:9px 0; background:var(--card); }
.action b { color:var(--seal); font-size:13px; } .action p { color:var(--ink-soft); font-size:12px; margin:5px 0 0; }
.drug-label { background:var(--card); border:1px solid var(--ink); border-left:6px solid var(--seal); border-radius:3px; padding:18px 20px; margin:4px 0 18px; }
.label-top { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }
.label-name { font-family:var(--serif); font-size:24px; font-weight:700; letter-spacing:-.015em; }
.label-inn { font-family:var(--mono); font-size:11px; color:var(--muted); margin:5px 0 12px; letter-spacing:.07em; }
.label-meta { display:flex; flex-wrap:wrap; gap:6px; }
.meta-chip { border:1px solid var(--rule); border-radius:2px; padding:3px 7px; font-family:var(--mono); font-size:10.5px; font-weight:600; color:var(--ink-soft); }
.rx { display:inline-grid; place-items:center; width:38px; height:38px; border:2px solid var(--seal); color:var(--seal); border-radius:3px; font-family:var(--serif); font-weight:800; font-size:18px; }
.source-strip { display:flex; gap:18px; flex-wrap:wrap; padding-top:12px; margin-top:13px; border-top:1px dashed var(--dotted); font-family:var(--mono); font-size:10.5px; color:var(--muted); }
.event-pill { display:inline-block; padding:3px 8px; border-radius:2px; border:1.5px solid currentColor; font-family:var(--mono); font-size:10px; font-weight:600; letter-spacing:.07em; margin-right:5px; }
.event-red { color:var(--seal); transform:rotate(-2deg); } .event-amber { color:var(--amber); } .event-purple { color:var(--plum); }
.alt-card { border:1px solid var(--rule); border-radius:3px; padding:12px 14px; background:var(--card); margin:8px 0; }
.alt-card b { font-size:13px; } .alt-card small { display:block; color:var(--muted); margin:4px 0 7px; }
.stock-ok { color:var(--green); font-family:var(--mono); font-weight:600; } .stock-low { color:var(--amber); font-family:var(--mono); font-weight:600; }
.clinical-warning { border:1px solid var(--seal); background:transparent; color:#7c2a21; border-radius:3px; padding:12px 14px; font-size:12px; margin:10px 0; }
.med-tree { border:1px solid var(--rule); border-radius:4px; background:var(--card); padding:18px 20px; margin-top:10px; }
.tree-root { display:flex; align-items:center; gap:9px; font-family:var(--serif); font-size:17px; font-weight:700; }
.tree-root .molecule { width:31px; height:31px; display:grid; place-items:center; border-radius:3px; border:1px solid var(--rule); background:var(--paper-2); color:var(--ink); }
.tree-group { position:relative; margin:12px 0 6px 15px; padding-left:26px; border-left:1px solid var(--dotted); }
.tree-group:before { content:''; position:absolute; left:0; top:14px; width:20px; border-top:1px solid var(--dotted); }
.tree-condition { display:inline-block; color:var(--green); border:1px solid currentColor; border-radius:2px; padding:4px 8px; font-family:var(--mono); font-size:10.5px; font-weight:600; margin:2px 0 9px; }
.tree-node { position:relative; margin:0 0 8px 16px; padding:10px 12px; border:1px solid var(--rule); border-radius:3px; background:var(--paper); font-size:12px; }
.tree-node:before { content:''; position:absolute; left:-17px; top:17px; width:16px; border-top:1px solid var(--dotted); }
.tree-node.current { border-color:var(--green); background:#f0f2e9; }
.tree-node.mismatch { border-color:#d8cba8; background:#f7f1e1; }
.tree-node strong { color:var(--ink); } .tree-node span { float:right; }
.tree-section-label { color:var(--plum); font-size:12px; font-weight:700; margin:18px 0 4px; }
.incident-strip { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin:0 0 22px; padding:13px 0; border-bottom:1px solid var(--rule); }
.incident { border-left:3px solid var(--rule); padding:2px 0 2px 12px; font-size:11px; color:var(--muted); }
.incident b { display:block; font-size:13.5px; margin-top:4px; color:var(--ink); }
.incident.red { border-left-color:var(--seal); } .incident.amber { border-left-color:var(--amber); } .incident.purple { border-left-color:var(--plum); } .incident.teal { border-left-color:var(--green); }
.unit { display:inline-flex; align-items:center; gap:6px; color:var(--ink-soft); font-size:11px; font-weight:500; }
.unit-icon { display:inline-grid; place-items:center; padding:1px 5px; border:1px solid var(--rule); border-radius:2px; font-family:var(--mono); font-size:9px; font-weight:600; letter-spacing:.06em; color:var(--muted); }
.workflow { display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin:2px 0 20px; }
.workflow-step { padding:9px 2px 2px; border-top:2px solid var(--rule); color:var(--muted); font-size:11px; }
.workflow-step b { display:block; color:var(--ink); font-size:12px; margin-bottom:3px; }
.workflow-step.done { border-top-color:var(--green); }
.workflow-step.current { border-top-color:var(--seal); }
.workflow-step.current b { color:var(--seal); }
.task { border-left:2px solid var(--rule); padding:9px 12px; margin:8px 0; }
.task b { display:block; color:var(--ink); font-size:12px; } .task span { color:var(--muted); font-family:var(--mono); font-size:10px; }
.order-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:9px; margin:10px 0 15px; }
.order-stat { padding:12px 13px; background:var(--paper-2); border-radius:3px; color:var(--muted); font-size:11px; }
.order-stat b { display:block; margin-top:4px; color:var(--ink); font-family:var(--mono); font-size:19px; font-weight:600; }
.order-stat.short b { color:var(--seal); }
.message-preview { padding:13px 15px; background:var(--paper-2); border-left:3px solid var(--plum); color:var(--ink-soft); font-size:12px; line-height:1.65; }
.sidebar-user { padding:11px 12px; border:1px solid var(--rule); border-radius:3px; background:var(--card); font-size:11px; color:var(--muted); margin-bottom:14px; }
.sidebar-user b { display:block; font-size:12px; color:var(--ink); margin-bottom:3px; }
.score { display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px dotted var(--dotted); font-size:13px; }
.score strong { color:var(--ink); font-family:var(--mono); font-weight:600; font-size:13px; }
.tiny { font-size:11px; color:var(--muted); }
div[data-testid="stAlertContainer"] { background:var(--paper-2); border-radius:0; border-left:3px solid var(--faint); color:var(--ink-soft); }
div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) { border-left-color:var(--green); }
div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) { border-left-color:var(--plum); }
div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) { border-left-color:var(--amber); }
div[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) { border-left-color:var(--seal); }
.stTabs [data-baseweb="tab-list"] { gap:2px; border-bottom:1px solid var(--rule); }
.stTabs [data-baseweb="tab"] { font-weight:600; font-size:13.5px; }
.stTabs [data-baseweb="tab-highlight"] { background-color:var(--seal); }
[data-testid="stChatMessage"] { background:transparent; }
[data-testid="stExpander"] { background:var(--card); }
div.stButton > button { border-radius:3px; font-weight:600; }
div.stButton > button[kind="primary"] { background:var(--seal); border-color:var(--seal); }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


DRUGS = pd.DataFrame(
    [
        ["아세트아미노펜정 500mg", "Acetaminophen", "해열·진통", "한빛제약", "정제", "경구", "현재 품절", "매우 높음", 92, 6, -35, "공급중단 + 수요급증", "2026-06-18", "2026-08-01", "2026-09-15", "Y", "대체품 검토 중"],
        ["세프트리악손주 1g", "Ceftriaxone", "항생제", "메디팜", "바이알", "정맥주사", "품절 예상", "높음", 81, 9, -18, "입고 5일 지연", "2026-07-21", "2026-08-01", "2026-08-19", "Y", "공급사 확인 중"],
        ["아목시실린캡슐 500mg", "Amoxicillin", "항생제", "그린바이오", "캡슐", "경구", "품절 예상", "높음", 76, 11, -12, "사용량 41% 증가", "2026-07-26", "2026-07-31", "2026-08-25", "Y", "발주량 조정"],
        ["덱시부프로펜시럽", "Dexibuprofen", "해열·진통", "한빛제약", "시럽", "경구", "공급중단", "관찰", 58, 19, 8, "계절 수요 증가", "2026-05-09", "2026-07-29", "2026-08-30", "N", "대체재고 확보"],
        ["메트포르민정 500mg", "Metformin", "당뇨", "유니메드", "정제", "경구", "정상화", "안정", 24, 43, 22, "정상 범위", "2026-01-14", "2026-07-28", "2026-07-20", "Y", "조치 완료"],
        ["아토르바스타틴정 10mg", "Atorvastatin", "고지혈증", "메디팜", "정제", "경구", "정상화", "안정", 16, 55, 31, "정상 범위", "2026-03-04", "2026-07-24", "2026-07-11", "N", "조치 완료"],
    ],
    columns=["품목", "성분명", "분류", "공급사", "제형", "투여경로", "공급상태", "위험등급", "위험점수", "예상소진일", "재고변동", "주요원인", "최초발생일", "최근갱신일", "예상정상화일", "필수의약품", "대응상태"],
)


def header(title: str, subtitle: str, section: str = "MEDSUPPLY RADAR") -> None:
    st.markdown(f'<div class="eyebrow">{section}</div><div class="page-title">{title}</div><div class="page-sub">{subtitle}</div>', unsafe_allow_html=True)


def copilot_answer(question: str) -> str:
    """Return grounded demo answers for the five MVP pharmacist question types."""
    normalized = question.replace(" ", "")
    if "소진" in normalized or "계산" in normalized:
        return """**예상 소진일은 약 6일 후입니다.**

`현재 재고 152정 ÷ 일평균 사용량 25.4정 = 5.98일`

**근거**
- 재고 스냅샷: 2026.08.01 09:30 · 152정
- 최근 4주 일평균 사용량: 25.4정
- 입고예정 데이터: 예정일 대비 5일 지연

입고 전 사용량이 변하면 예상 소진일도 다시 계산해야 합니다."""
    if "대체" in normalized or "후보" in normalized:
        return """**동일 성분·함량·제형·투여경로 후보는 2개입니다.**

- 대한아세트아미노펜정 500mg · 재고 420정
- 유니타세트정 500mg · 재고 84정

650mg 서방정은 함량과 방출 제형이 달라 동일 조건 후보가 아닙니다.

**근거:** 기관 성분·대체군 매핑 2026.08.01 · 최종 선택은 약사 검토가 필요합니다."""
    if "공고" in normalized or "공급중단" in normalized:
        return """**제조사는 원료 수급 차질을 이유로 공급중단을 안내했습니다.**

- 대상: 아세트아미노펜정 500mg
- 공급중단 시작: 2026.07.28
- 정상화 예상: 2026.09.15
- 기관 매핑 품목: 3개

**근거:** 제조사 공급중단 공고 원문 · AI 구조화 결과는 담당자 확인 전 상태입니다."""
    if "발주" in normalized or "수량" in normalized:
        return """**14일 기준 부족 예상량은 204정이며, 검토용 요청 수량은 300정입니다.**

`14일 예상 수요 356정 − 현재 재고 152정 = 부족 예상 204정`

300정은 포장단위와 안전재고를 고려한 참고값입니다. 실제 발주는 공급 가능 수량과 기관 규정을 확인한 뒤 **발주·조치안**에서 확정해야 합니다."""
    return """**현재 위험점수는 92점으로 ‘매우 높음’입니다.**

- 재고 커버리지: +38점 · 소진 예상 D-6
- 수요 급증: +24점 · 최근 4주 +41%
- 공급중단: +20점 · 제조사 공고 매핑
- 입고 지연: +10점 · 예정일 대비 5일

**근거:** 기관 재고·사용량·입고 데이터 및 공급중단 공고. AI는 위험등급 판정에 관여하지 않고 결과를 설명합니다."""


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


def page_situation() -> None:
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
            st.switch_page(PG_REVIEW)
    with right:
        st.markdown('<div class="panel"><div class="panel-title">나의 오늘 할 일</div><div class="panel-sub">위험도와 예상 소진일 기준</div><div class="task"><b>09:00 · 긴급</b><span>아세트아미노펜 동일 조건 대체품 확인</span></div><div class="task"><b>11:00 · 발주 확인</b><span>세프트리악손 공급사 입고일 재확인</span></div><div class="task"><b>15:00 · 부서 공유</b><span>내과·소아청소년과 수급위험 전달</span></div></div>', unsafe_allow_html=True)
        st.markdown('<div class="notice"><b>오늘의 핵심 신호</b><br>필수의약품 2종이 10일 이내 소진될 수 있습니다. 신규 공급 공고 1건이 기관 품목 3개와 매핑되었습니다.</div>', unsafe_allow_html=True)
    st.markdown('<div class="workflow"><div class="workflow-step done"><b>1 · 위험 확인</b>상황실</div><div class="workflow-step current"><b>2 · 근거 검토</b>대기 4건</div><div class="workflow-step"><b>3 · 대체약 확인</b>동일 조건</div><div class="workflow-step"><b>4 · 발주·공유</b>요청안 작성</div><div class="workflow-step"><b>5 · 결과 기록</b>효과 추적</div></div>', unsafe_allow_html=True)

def page_review() -> None:
    header("약사 검토 워크벤치", "위험 근거와 동일 조건 대체 후보를 확인한 뒤 조치안을 작성합니다.", "검토 대기함")
    selected = st.selectbox("품목 선택", DRUGS["품목"], index=0)
    row = DRUGS[DRUGS["품목"] == selected].iloc[0]
    st.markdown(f'''<div class="drug-label"><div class="label-top"><div><div class="label-name">{row['품목']}</div><div class="label-inn">{row['성분명'].upper()} · {row['분류']}</div><div class="label-meta"><span class="meta-chip">500 mg</span><span class="meta-chip">{row['제형']}</span><span class="meta-chip">{row['투여경로']}</span><span class="meta-chip">전문의약품</span><span class="meta-chip">필수의약품</span></div></div><div><span class="rx">Rx</span></div></div><div class="source-strip"><span>품목기준코드 20260817001</span><span>제조사 {row['공급사']}</span><span>포장단위 100정/병</span><span>최종 갱신 2026.08.01 09:30</span></div></div>''', unsafe_allow_html=True)
    st.markdown('<div class="workflow"><div class="workflow-step done"><b>1 · 위험 확인 ✓</b>현재 품절 · 92</div><div class="workflow-step done"><b>2 · 근거 검토 ✓</b>근거 4건 일치</div><div class="workflow-step current"><b>3 · 대체약 검토</b>동일 조건 2개</div><div class="workflow-step"><b>4 · 조치 확정</b>약사 확인</div><div class="workflow-step"><b>5 · 결과 추적</b>이력 관리</div></div>', unsafe_allow_html=True)
    summary_col, copilot_col = st.columns([1.8, 1])
    with summary_col:
        st.markdown('<div class="panel"><div class="panel-title">검토 요약</div><div class="panel-sub">현재 선택 품목의 핵심 위험 신호</div><div class="score"><span>품절 위험</span><strong style="color:#a8352a">92 · 매우 높음</strong></div><div class="score"><span>예상 소진</span><strong>6일 후</strong></div><div class="score"><span>동일 조건 대체 후보</span><strong>2개</strong></div><div class="score"><span>담당자 확인</span><strong style="color:#966a15">검토 중</strong></div></div>', unsafe_allow_html=True)
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

def page_orders() -> None:
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

def page_notices() -> None:
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

def page_alerts() -> None:
    header("알림센터", "위험 변화와 신규 공고 매핑을 중요도순으로 확인합니다.", "알림센터")
    for title, desc, badge, css in [
        ("아세트아미노펜정 위험등급 상승", "높음 → 매우 높음 · 사용량 급증 및 공급중단 공고 매핑", "긴급", "critical"),
        ("세프트리악손주 입고 지연", "예정 입고일보다 5일 지연 · 예상 소진 D-9", "높음", "high"),
        ("신규 공급중단 공고 매핑", "대한제약 공고가 기관 보유 품목 3개와 연결됨", "확인", "watch"),
    ]:
        st.markdown(f'<div class="panel"><span class="badge {css}">{badge}</span><div class="panel-title" style="margin-top:10px">{title}</div><div class="panel-sub" style="margin:0">{desc}</div></div>', unsafe_allow_html=True)

def page_history() -> None:
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

def page_eval() -> None:
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


PG_SITUATION = st.Page(page_situation, title="수급 상황실", url_path="situation", default=True)
PG_REVIEW = st.Page(page_review, title="검토 대기함", url_path="review")
PG_ORDERS = st.Page(page_orders, title="발주·조치안", url_path="orders")
PG_HISTORY = st.Page(page_history, title="대응 이력", url_path="history")
PG_NOTICES = st.Page(page_notices, title="공급 공고", url_path="notices")
PG_ALERTS = st.Page(page_alerts, title="알림센터", url_path="alerts")
PG_EVAL = st.Page(page_eval, title="AI 평가", url_path="evaluation")

PAGES = [PG_SITUATION, PG_REVIEW, PG_ORDERS, PG_HISTORY, PG_NOTICES, PG_ALERTS, PG_EVAL]
current_page = st.navigation(PAGES, position="hidden")

with st.sidebar:
    st.markdown('<div class="brand"><span class="brand-mark">⚕</span> MedSupply Radar</div>', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-user"><b>김약사 · 재고·발주 담당</b>선경병원 약제부</div>', unsafe_allow_html=True)
    st.markdown('<div class="toc-label"><span class="toc-icon">☰</span> 메뉴</div>', unsafe_allow_html=True)
    for pg in PAGES:
        st.page_link(pg)
    active_href = "" if current_page is PG_SITUATION else current_page.url_path
    st.markdown(f'<style>[data-testid="stPageLink"] a[href="{active_href}"] {{ background:rgba(168,53,42,.08) !important; }} [data-testid="stPageLink"] a[href="{active_href}"] p {{ color:var(--seal); font-weight:700; }}</style>', unsafe_allow_html=True)
    st.markdown("---")
    st.caption("데이터 기준")
    st.markdown("**2026. 08. 01 09:30**")
    st.caption("100개 품목 · 4개 위험 시나리오")
    st.markdown("---")
    st.caption("병원 약제부 수급관제 · 데모 환경")

current_page.run()
