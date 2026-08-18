"""CSS 원문 문자열과 전역 스타일 주입.

app.py 최상단에 있던 CSS 상수와 st.markdown 주입 호출을 그대로 옮긴 모듈이다.
CSS 문자열 내용은 한 글자도 수정하지 않는다(불변성 검증 대상).
"""

from __future__ import annotations

import streamlit as st


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


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
