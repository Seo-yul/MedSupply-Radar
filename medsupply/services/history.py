"""대응 이력 서비스 — views/history.py가 소비하는 실데이터 조회 계층.

queries.py(순수 SQL 조회)의 결과를 화면 친화적 DataFrame으로 감싼다. 이 모듈은 새 SQL을
직접 작성하지 않는다 — 새 조회가 필요하면 queries.py에 추가한다(계층 규칙,
task-M15-brief.md). search(품목명·내용 부분 일치)는 새 SQL 대신 pandas 필터로 구현한다
(task-M18-brief.md에서 명시적으로 허용). get_conn()·current_data_version()은
medsupply.services.inventory를 그대로 재사용한다(중복 구현 금지).

캐시 규칙:
- load_history(): st.cache_data로 결과를 캐시하며, data_version 인자를 캐시 키에
  포함해 무효화 신호로 쓴다(호출부가 inventory.current_data_version()의 값을 넘긴다).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from medsupply.data import queries
from medsupply.services import inventory


@st.cache_data
def load_history(
    risk_type: str | None = None, search: str = "", data_version: int = 0
) -> pd.DataFrame:
    """조치 이력 목록 — list_action_history(risk_type) 위임 + item_name/note 부분 일치 검색.

    data_version은 호출부(inventory.current_data_version())가 넘기는 캐시 무효화 신호일
    뿐 조회 조건으로는 쓰이지 않는다. search는 item_name·note 부분 일치(대소문자 무시)를
    pandas 필터로 적용한다(새 SQL 없음) — 빈 문자열이면 필터를 건너뛴다.
    """
    del data_version  # 캐시 키 무효화 전용 — 조회 조건에는 쓰지 않는다.
    conn = inventory.get_conn()
    history = queries.list_action_history(conn, risk_type=risk_type)

    if search:
        needle = search.lower()
        name_match = history["item_name"].str.lower().str.contains(needle, na=False)
        note_match = history["note"].str.lower().str.contains(needle, na=False)
        history = history[name_match | note_match]

    return history.reset_index(drop=True)
