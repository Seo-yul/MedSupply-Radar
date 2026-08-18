"""알림센터 뷰."""

from __future__ import annotations

import streamlit as st

from medsupply.ui.components import header


def render() -> None:
    header("알림센터", "위험 변화와 신규 공고 매핑을 중요도순으로 확인합니다.", "알림센터")
    for title, desc, badge, css in [
        ("아세트아미노펜정 위험등급 상승", "경고 → 위험 · 사용량 급증 및 공급중단 공고 매핑", "긴급", "critical"),
        ("세프트리악손주 입고 지연", "예정 입고일보다 5일 지연 · 예상 소진 D-9", "높음", "high"),
        ("신규 공급중단 공고 매핑", "대한제약 공고가 기관 보유 품목 3개와 연결됨", "확인", "watch"),
    ]:
        st.markdown(f'<div class="panel"><span class="badge {css}">{badge}</span><div class="panel-title" style="margin-top:10px">{title}</div><div class="panel-sub" style="margin:0">{desc}</div></div>', unsafe_allow_html=True)
