from __future__ import annotations

from datetime import date

import streamlit as st

from medsupply import settings, theme
from medsupply.data import queries
from medsupply.services import inventory
from medsupply.views import PAGE_REGISTRY, alerts, evaluation, history, notices, orders, review, situation


st.set_page_config(
    page_title="MedSupply Radar",
    page_icon="💊",
    layout="wide",
    initial_sidebar_state="expanded",
)


theme.inject_css()


PG_SITUATION = st.Page(situation.render, title="수급 상황실", url_path="situation", default=True)
PG_REVIEW = st.Page(review.render, title="검토 대기함", url_path="review")
PG_ORDERS = st.Page(orders.render, title="발주·조치안", url_path="orders")
PG_HISTORY = st.Page(history.render, title="대응 이력", url_path="history")
PG_NOTICES = st.Page(notices.render, title="공급 공고", url_path="notices")
PG_ALERTS = st.Page(alerts.render, title="알림센터", url_path="alerts")
PG_EVAL = st.Page(evaluation.render, title="AI 평가", url_path="evaluation")

PAGES = [PG_SITUATION, PG_REVIEW, PG_ORDERS, PG_HISTORY, PG_NOTICES, PG_ALERTS, PG_EVAL]
PAGE_REGISTRY.update(
    situation=PG_SITUATION,
    review=PG_REVIEW,
    orders=PG_ORDERS,
    history=PG_HISTORY,
    notices=PG_NOTICES,
    alerts=PG_ALERTS,
    evaluation=PG_EVAL,
)
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
    if settings.DB_PATH.exists():
        meta = queries.get_meta(inventory.get_conn())
        base_date = date.fromisoformat(meta["base_date"])
        date_display = f"{base_date.year}. {base_date.month:02d}. {base_date.day:02d} 09:30"
        item_count_display = f"{meta.get('item_count', '0')}개 품목 · 위험 시나리오 유형 4종"
    else:
        date_display = "2026. 08. 01 09:30"
        item_count_display = "100개 품목 · 4개 위험 시나리오"
    st.caption("데이터 기준")
    st.markdown(f"**{date_display}**")
    st.caption(item_count_display)
    st.markdown("---")
    st.caption("병원 약제부 수급관제 · 데모 환경")

current_page.run()
