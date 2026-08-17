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
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Noto+Sans+KR:wght@400;500;600;700&display=swap');
:root { --ink:#10243e; --muted:#68758a; --line:#e2e8ef; --navy:#102a43; --teal:#087f8c; --amber:#d97706; --red:#c2413b; --purple:#7656a3; --inactive:#7b8794; --bg:#f3f7f8; }
html, body, [class*="css"] { font-family:'Manrope','Noto Sans KR',sans-serif; }
.stApp { background:var(--bg); color:var(--ink); }
[data-testid="stSidebar"] { background:linear-gradient(180deg,#15233c 0%,#1b3151 100%); border:0; }
[data-testid="stSidebar"] * { color:#eef4ff; }
[data-testid="stSidebar"] .stRadio label { padding:.48rem .65rem; border-radius:9px; }
[data-testid="stSidebar"] .stRadio label:hover { background:rgba(255,255,255,.08); }
[data-testid="stMetric"] { background:#fff; border:1px solid var(--line); padding:17px 19px; border-radius:14px; box-shadow:0 2px 10px rgba(20,33,61,.035); }
[data-testid="stMetricLabel"] { color:var(--muted); font-weight:600; }
[data-testid="stMetricValue"] { color:var(--ink); font-weight:800; }
div[data-testid="stHorizontalBlock"] { gap:1rem; }
.block-container { padding-top:1.6rem; padding-bottom:3rem; max-width:1500px; }
.brand { display:flex; align-items:center; gap:10px; font-size:19px; font-weight:800; padding:3px 0 22px; }
.brand-mark { width:38px; height:38px; display:grid; place-items:center; border-radius:11px; background:linear-gradient(135deg,#0f8b8d,#5bbfba); font-size:20px; }
.eyebrow { color:#087f8c; font-size:12px; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }
.page-title { font-size:29px; line-height:1.2; font-weight:800; margin:4px 0 5px; letter-spacing:-.03em; }
.page-sub { color:var(--muted); font-size:14px; margin-bottom:20px; }
.panel { background:#fff; border:1px solid var(--line); border-radius:15px; padding:20px; box-shadow:0 2px 10px rgba(20,33,61,.035); margin-bottom:14px; }
.panel-title { font-weight:800; font-size:16px; margin-bottom:4px; }
.panel-sub { color:var(--muted); font-size:12px; margin-bottom:14px; }
.risk-row { display:grid; grid-template-columns:1.8fr .7fr .8fr .8fr; gap:10px; align-items:center; padding:13px 4px; border-bottom:1px solid #edf0f5; font-size:13px; }
.risk-row:last-child { border-bottom:0; }
.drug { font-weight:700; } .drug small { display:block; color:var(--muted); font-weight:500; margin-top:3px; }
.badge { display:inline-block; width:max-content; padding:5px 9px; border-radius:999px; font-size:11px; font-weight:800; }
.critical { color:#a62f2a; background:#fde8e7; } .high { color:#a95508; background:#fff0d5; }
.watch { color:#66408d; background:#eee8f6; } .safe { color:#087568; background:#dff3ef; }
.inactive { color:#5f6b78; background:#e9edf1; }
.notice { border-left:4px solid #7656a3; background:#f8f5fb; padding:14px 16px; border-radius:0 10px 10px 0; color:#344054; font-size:13px; }
.factor { padding:12px 13px; background:#f8f9fc; border:1px solid #eceff4; border-radius:11px; margin:8px 0; }
.factor b { display:block; font-size:13px; } .factor span { color:var(--muted); font-size:12px; }
.action { padding:14px 15px; border:1px solid #dce4f5; border-radius:12px; margin:9px 0; background:#fbfcff; }
.action b { color:#176b73; font-size:13px; } .action p { color:#4a5568; font-size:12px; margin:5px 0 0; }
.drug-label { background:#fff; border:1px solid #d8e1e8; border-left:7px solid #c2413b; border-radius:14px; padding:18px 20px; margin:4px 0 18px; box-shadow:0 3px 14px rgba(16,42,67,.05); }
.label-top { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }
.label-name { font-size:24px; font-weight:800; letter-spacing:-.02em; color:#10243e; }
.label-inn { font-size:12px; color:#68758a; margin:4px 0 11px; letter-spacing:.04em; }
.label-meta { display:flex; flex-wrap:wrap; gap:6px; }
.meta-chip { border:1px solid #d8e1e8; background:#f7fafb; border-radius:6px; padding:4px 8px; font-size:11px; font-weight:700; color:#43546a; }
.rx { display:inline-grid; place-items:center; width:38px; height:38px; border:2px solid #102a43; border-radius:8px; font-family:serif; font-weight:800; font-size:18px; }
.source-strip { display:flex; gap:18px; flex-wrap:wrap; padding-top:13px; margin-top:13px; border-top:1px dashed #d8e1e8; font-size:11px; color:#68758a; }
.event-pill { display:inline-block; padding:4px 8px; border-radius:6px; font-size:10px; font-weight:800; margin-right:5px; }
.event-red { color:#a62f2a; background:#fde8e7; } .event-amber { color:#a95508; background:#fff0d5; } .event-purple { color:#66408d; background:#eee8f6; }
.alt-card { border:1px solid #dbe4e9; border-radius:11px; padding:12px 14px; background:#fff; margin:8px 0; }
.alt-card b { font-size:13px; } .alt-card small { display:block; color:#68758a; margin:4px 0 7px; }
.stock-ok { color:#087568; font-weight:800; } .stock-low { color:#a95508; font-weight:800; }
.clinical-warning { border:1px solid #eed7a9; background:#fffaf0; color:#754b0b; border-radius:10px; padding:12px 14px; font-size:12px; margin:10px 0; }
.clinical-hero { position:relative; overflow:hidden; display:flex; justify-content:space-between; align-items:center; gap:24px; color:#fff; background:linear-gradient(120deg,#102a43 0%,#154b59 62%,#087f8c 100%); border-radius:18px; padding:22px 26px; margin:3px 0 19px; box-shadow:0 8px 25px rgba(16,42,67,.14); }
.clinical-hero:after { content:'Rx'; position:absolute; right:145px; top:-28px; font-family:Georgia,serif; font-size:125px; font-weight:700; color:rgba(255,255,255,.055); transform:rotate(-8deg); }
.hero-kicker { font-size:11px; font-weight:800; letter-spacing:.15em; color:#9be4dc; margin-bottom:6px; }
.hero-title { font-size:25px; font-weight:800; letter-spacing:-.025em; }
.hero-copy { font-size:12px; color:#d6e5ea; margin-top:5px; }
.hero-mark { position:relative; z-index:1; min-width:75px; height:75px; border:1px solid rgba(255,255,255,.28); background:rgba(255,255,255,.1); border-radius:18px; display:grid; place-items:center; font-size:36px; backdrop-filter:blur(5px); }
.incident-strip { display:grid; grid-template-columns:repeat(4,1fr); gap:9px; margin:0 0 18px; }
.incident { background:#fff; border:1px solid #e0e7ec; border-radius:11px; padding:11px 13px; font-size:11px; color:#667588; }
.incident b { display:block; font-size:13px; margin-top:4px; color:#10243e; }
.incident.red { border-top:3px solid #c2413b; } .incident.amber { border-top:3px solid #d97706; } .incident.purple { border-top:3px solid #7656a3; } .incident.teal { border-top:3px solid #087f8c; }
.unit { display:inline-flex; align-items:center; gap:5px; color:#516173; font-size:11px; font-weight:700; }
.unit-icon { width:22px; height:22px; display:inline-grid; place-items:center; border:1px solid #d8e1e8; border-radius:6px; background:#f7fafb; }
.score { display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid #edf0f5; font-size:13px; }
.score strong { color:#244fb7; }
.tiny { font-size:11px; color:var(--muted); }
div.stButton > button { border-radius:10px; font-weight:700; border-color:#d9dfeb; }
div.stButton > button[kind="primary"] { background:#087f8c; border-color:#087f8c; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


DRUGS = pd.DataFrame(
    [
        ["아세트아미노펜정 500mg", "Acetaminophen", "해열·진통", "한빛제약", "정제", "경구", "공급중단", "매우 높음", 92, 6, -35, "공급중단 + 수요급증"],
        ["세프트리악손주 1g", "Ceftriaxone", "항생제", "메디팜", "바이알", "정맥주사", "입고지연", "높음", 81, 9, -18, "입고 5일 지연"],
        ["아목시실린캡슐 500mg", "Amoxicillin", "항생제", "그린바이오", "캡슐", "경구", "수요급증", "높음", 76, 11, -12, "사용량 41% 증가"],
        ["덱시부프로펜시럽", "Dexibuprofen", "해열·진통", "한빛제약", "시럽", "경구", "관찰", "관찰", 58, 19, 8, "계절 수요 증가"],
        ["메트포르민정 500mg", "Metformin", "당뇨", "유니메드", "정제", "경구", "정상공급", "안정", 24, 43, 22, "정상 범위"],
        ["아토르바스타틴정 10mg", "Atorvastatin", "고지혈증", "메디팜", "정제", "경구", "정상공급", "안정", 16, 55, 31, "정상 범위"],
    ],
    columns=["품목", "성분명", "분류", "공급사", "제형", "투여경로", "공급상태", "위험등급", "위험점수", "예상소진일", "재고변동", "주요원인"],
)


def header(title: str, subtitle: str) -> None:
    st.markdown(f'<div class="eyebrow">MEDSUPPLY RADAR</div><div class="page-title">{title}</div><div class="page-sub">{subtitle}</div>', unsafe_allow_html=True)


def gauge(score: int) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=score,
        number={"font": {"size": 34, "color": "#14213d"}},
        gauge={
            "axis": {"range": [0, 100], "tickwidth": 0, "tickcolor": "white"},
            "bar": {"color": "#ef5b5b", "thickness": .24},
            "bgcolor": "#eef1f6", "borderwidth": 0,
            "steps": [{"range": [0, 100], "color": "#eef1f6"}],
            "threshold": {"line": {"color": "#b42318", "width": 3}, "thickness": .75, "value": 85},
        },
    ))
    fig.update_layout(height=190, margin=dict(l=20, r=20, t=25, b=0), paper_bgcolor="rgba(0,0,0,0)")
    return fig


def trend_chart() -> go.Figure:
    dates = pd.date_range("2026-07-20", periods=29, freq="D")
    usage = [18,19,17,20,21,19,22,23,24,21,25,27,26,29,32,30,35,38,41,39,44,48,46,51,54,58,56,61,64]
    stock = [890,871,854,834,813,794,772,749,725,704,679,652,626,597,565,535,500,462,421,382,338,290,244,193,139,81,69,38,22]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=stock, name="재고", line=dict(color="#087f8c", width=3), fill="tozeroy", fillcolor="rgba(8,127,140,.08)"))
    fig.add_trace(go.Scatter(x=dates, y=usage, name="일 사용량", yaxis="y2", line=dict(color="#ef8d32", width=2, dash="dot")))
    fig.add_vline(x=dates[18], line_dash="dash", line_color="#7656a3", annotation_text="공급중단 공고", annotation_position="top left")
    fig.add_vline(x=dates[23], line_dash="dot", line_color="#d97706", annotation_text="입고 지연", annotation_position="top right")
    fig.update_layout(
        height=305, margin=dict(l=15,r=15,t=20,b=10), hovermode="x unified",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=1.12, x=.72),
        xaxis=dict(showgrid=False), yaxis=dict(gridcolor="#edf0f5", title="재고 수량"),
        yaxis2=dict(overlaying="y", side="right", showgrid=False, title="사용량"),
    )
    return fig


with st.sidebar:
    st.markdown('<div class="brand"><span class="brand-mark">⚕</span> MedSupply Radar</div>', unsafe_allow_html=True)
    page = st.radio("메뉴", ["관제 대시보드", "품목 상세", "공급 공고", "알림센터", "대응 이력", "AI 평가"], label_visibility="collapsed")
    st.markdown("---")
    st.caption("데이터 기준")
    st.markdown("**2026. 08. 17 09:30**")
    st.caption("100개 품목 · 4개 위험 시나리오")
    st.markdown("---")
    st.caption("병원 약제부 수급관제 · 데모 환경")


if page == "관제 대시보드":
    st.markdown('<div class="clinical-hero"><div><div class="hero-kicker">HOSPITAL PHARMACY · SUPPLY COMMAND CENTER</div><div class="hero-title">병원 약제부 의약품 수급관제</div><div class="hero-copy">재고·사용량·입고·공급중단 신호를 통합해 품절 위험과 약사 조치 우선순위를 제공합니다.</div></div><div class="hero-mark">⚕</div></div>', unsafe_allow_html=True)
    st.markdown('<div class="incident-strip"><div class="incident red">품절 임박<b>3 품목 · 7일 이내</b></div><div class="incident amber">입고 지연<b>4건 · 평균 4.2일</b></div><div class="incident purple">외부 공급 공고<b>신규 3건 매핑</b></div><div class="incident teal">정상 공급<b>72 품목 · 안정</b></div></div>', unsafe_allow_html=True)
    header("오늘의 수급위험", "위험 변화를 먼저 확인하고, 근거와 대응까지 한 흐름으로 관리하세요.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("관제 품목", "100", "+4 신규")
    c2.metric("최고 위험", "3", "+1 오늘")
    c3.metric("7일 내 소진", "5", "+2 전일 대비", delta_color="inverse")
    c4.metric("조치 대기", "4", "−2 처리 완료")
    st.write("")
    left, right = st.columns([1.65, 1])
    with left:
        st.markdown('<div class="panel"><div class="panel-title">현재 공급 부족 품목</div><div class="panel-sub">ASHP 방식의 사건 유형과 예상 소진일을 함께 표시합니다.</div>', unsafe_allow_html=True)
        for row in DRUGS.head(5).itertuples():
            css = {"매우 높음":"critical", "높음":"high", "관찰":"watch", "안정":"safe"}[row.위험등급]
            event_css = "event-purple" if row.공급상태 == "공급중단" else "event-amber" if row.공급상태 in ["입고지연", "수요급증"] else "safe"
            form_icon = "💉" if row.제형 == "바이알" else "🥄" if row.제형 == "시럽" else "💊"
            st.markdown(f'<div class="risk-row"><div class="drug">{row.품목}<small>{row.성분명} · <span class="unit"><span class="unit-icon">{form_icon}</span>{row.제형} · {row.투여경로}</span></small></div><span class="event-pill {event_css}">{row.공급상태}</span><b>{row.위험점수}점</b><span>D-{row.예상소진일}</span></div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        if st.button("위험 1위 품목 상세 보기 →", type="primary", use_container_width=True):
            st.session_state["selected_drug"] = DRUGS.iloc[0]["품목"]
            st.info("왼쪽 메뉴에서 ‘품목 상세’를 선택하면 연결된 화면을 볼 수 있습니다.")
    with right:
        st.markdown('<div class="panel"><div class="panel-title">위험등급 분포</div><div class="panel-sub">전체 100개 품목 기준</div>', unsafe_allow_html=True)
        dist = pd.DataFrame({"등급":["매우 높음","높음","관찰","안정"], "품목 수":[3,7,18,72]})
        fig = px.bar(dist, x="품목 수", y="등급", orientation="h", color="등급", color_discrete_map={"매우 높음":"#e45757","높음":"#ee9b39","관찰":"#5b8def","안정":"#42aa7a"})
        fig.update_layout(height=260, showlegend=False, margin=dict(l=5,r=15,t=5,b=5), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", xaxis=dict(showgrid=False), yaxis_title=None)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False})
        st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('<div class="notice"><b>오늘의 핵심 신호</b><br>공급중단 공고 1건이 기관 보유 품목 3개와 새로 매핑되었습니다.</div>', unsafe_allow_html=True)

elif page == "품목 상세":
    header("품목 상세", "위험점수의 변화와 판단 근거를 확인하고 대응을 기록합니다.")
    selected = st.selectbox("품목 선택", DRUGS["품목"], index=0)
    row = DRUGS[DRUGS["품목"] == selected].iloc[0]
    st.markdown(f'''<div class="drug-label"><div class="label-top"><div><div class="label-name">{row['품목']}</div><div class="label-inn">{row['성분명'].upper()} · {row['분류']}</div><div class="label-meta"><span class="meta-chip">500 mg</span><span class="meta-chip">{row['제형']}</span><span class="meta-chip">{row['투여경로']}</span><span class="meta-chip">전문의약품</span><span class="meta-chip">필수의약품</span></div></div><div><span class="rx">Rx</span></div></div><div class="source-strip"><span>품목기준코드 20260817001</span><span>제조사 {row['공급사']}</span><span>포장단위 100정/병</span><span>최종 갱신 2026.08.17 09:30</span></div></div>''', unsafe_allow_html=True)
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
        st.markdown('<div class="clinical-warning"><b>약사 확인 필수</b> · 아래 항목은 동일 성분·함량·제형 기반 후보이며 자동 대체 처방을 의미하지 않습니다.</div><div class="alt-card"><b>대한아세트아미노펜정 500mg</b><small>Acetaminophen · 500mg · 정제 · 경구 · 대한제약</small><span class="stock-ok">재고 420정 · 16일분</span></div><div class="alt-card"><b>유니타세트정 500mg</b><small>Acetaminophen · 500mg · 정제 · 경구 · 유니메드</small><span class="stock-low">재고 84정 · 3일분</span></div><div class="alt-card"><b>아세트아미노펜서방정 650mg</b><small>함량·방출 제형 상이 · 처방 변경 및 임상 검토 필요</small><span class="badge inactive">조건 불일치</span></div>', unsafe_allow_html=True)
    with source_tab:
        st.markdown('<div class="panel"><div class="panel-title">판단 근거와 출처</div><div class="score"><span>기관 재고 스냅샷</span><strong>08.17 09:30</strong></div><div class="score"><span>최근 4주 사용량</span><strong>+41%</strong></div><div class="score"><span>제조사 공급중단 공고</span><strong>원문 확인</strong></div><div class="score"><span>입고예정 데이터</span><strong>5일 지연</strong></div><br><div class="tiny">AI는 위험등급 판정에 관여하지 않으며 입력 근거를 자연어로 요약합니다.</div></div>', unsafe_allow_html=True)
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

elif page == "공급 공고":
    header("공급 공고 매핑", "외부 공고를 구조화하고 기관 보유 품목과 자동으로 연결합니다.")
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

elif page == "알림센터":
    header("알림센터", "위험 변화와 신규 공고 매핑을 중요도순으로 확인합니다.")
    for title, desc, badge, css in [
        ("아세트아미노펜정 위험등급 상승", "높음 → 매우 높음 · 사용량 급증 및 공급중단 공고 매핑", "긴급", "critical"),
        ("세프트리악손주 입고 지연", "예정 입고일보다 5일 지연 · 예상 소진 D-9", "높음", "high"),
        ("신규 공급중단 공고 매핑", "대한제약 공고가 기관 보유 품목 3개와 연결됨", "확인", "watch"),
    ]:
        st.markdown(f'<div class="panel"><span class="badge {css}">{badge}</span><div class="panel-title" style="margin-top:10px">{title}</div><div class="panel-sub" style="margin:0">{desc}</div></div>', unsafe_allow_html=True)

elif page == "대응 이력":
    header("대응 이력", "위험 품목에 대한 조치와 결과를 조직 지식으로 축적합니다.")
    history = pd.DataFrame([
        ["2026-08-17 09:12", "아세트아미노펜정 500mg", "입고 일정 확인", "김약사", "진행 중"],
        ["2026-08-16 15:40", "세프트리악손주 1g", "대체 품목 검토", "이약사", "완료"],
        ["2026-08-15 11:20", "덱시부프로펜시럽", "발주량 조정", "김약사", "완료"],
    ], columns=["일시", "품목", "조치", "담당자", "상태"])
    st.dataframe(history, use_container_width=True, hide_index=True)

else:
    header("AI 평가", "Langfuse LLM-as-a-Judge 지표로 생성 품질과 회귀를 관리합니다.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("근거충실성", "0.91", "+0.03")
    c2.metric("원인관련성", "0.88", "+0.01")
    c3.metric("대응실행가능성", "0.84", "−0.02")
    c4.metric("환각 없음", "96%", "+1%p")
    left, right = st.columns([1.5, 1])
    with left:
        scores = pd.DataFrame({"Experiment":["prompt-v1","prompt-v2","prompt-v3"], "근거충실성":[.82,.88,.91], "원인관련성":[.80,.85,.88], "대응실행가능성":[.76,.86,.84]})
        long = scores.melt("Experiment", var_name="지표", value_name="점수")
        fig = px.line(long, x="Experiment", y="점수", color="지표", markers=True, color_discrete_sequence=["#356df3","#47b3a3","#ef8d32"])
        fig.add_hline(y=.8, line_dash="dash", line_color="#aab2c0", annotation_text="통과 기준 0.8")
        fig.update_layout(height=340, yaxis_range=[.6,1], paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", yaxis=dict(gridcolor="#edf0f5"), legend_title=None)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False})
    with right:
        st.markdown('<div class="panel"><div class="panel-title">최신 Experiment</div><div class="panel-sub">medsupply-prompt-v3 · 20 cases</div><div class="score"><span>평가 통과</span><strong>18 / 20</strong></div><div class="score"><span>사람 교차검토</span><strong>4 / 4</strong></div><div class="score"><span>이전 버전 대비</span><strong>+2.1%</strong></div><div class="score"><span>회귀 기준</span><strong>PASS</strong></div><br><div class="tiny">Judge 모델과 루브릭 버전을 고정해 비교합니다.</div></div>', unsafe_allow_html=True)
