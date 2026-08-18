"""공용 헤더 컴포넌트."""

from __future__ import annotations

import streamlit as st


def header(title: str, subtitle: str, section: str = "MEDSUPPLY RADAR") -> None:
    st.markdown(f'<div class="eyebrow">{section}</div><div class="page-title">{title}</div><div class="page-sub">{subtitle}</div>', unsafe_allow_html=True)
