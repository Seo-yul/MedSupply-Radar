"""뷰 페이지 레지스트리.

app.py가 st.Page 객체를 생성한 직후 PAGE_REGISTRY를 채운다. situation 뷰처럼
다른 페이지로 전환해야 하는 뷰가 app.py를 import하지 않고도(순환 참조 회피)
st.switch_page 대상을 얻을 수 있게 하기 위함이다. AppTest처럼 레지스트리가
채워지지 않은 채로 뷰 함수만 단독 실행되는 경우를 대비해, 소비하는 쪽에서는
항상 PAGE_REGISTRY.get(...)으로 조회하고 None이면 전환을 건너뛴다.
"""

from __future__ import annotations

import streamlit as st

PAGE_REGISTRY: dict[str, "st.Page"] = {}
