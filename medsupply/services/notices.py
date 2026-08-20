"""공급 공고 서비스 — views/notices.py가 소비하는 실데이터 조회 계층.

queries.py(순수 SQL 조회)의 결과를 화면 친화적 DataFrame/dict로 그대로 감싼다. 이 모듈은
새 SQL을 직접 작성하지 않는다 — 새 조회가 필요하면 queries.py에 추가한다(계층 규칙,
task-M15-brief.md). get_conn()은 medsupply.services.inventory를, 쓰기 전용 커넥션은
medsupply.services.workbench.open_write_conn()을 그대로 재사용한다(중복 구현 금지).

캐시 규칙:
- load_notice_list()/load_notice_detail(): st.cache_data로 결과를 캐시하며, data_version
  인자를 캐시 키에 포함해 무효화 신호로 쓴다(호출부가 inventory.current_data_version()의
  값을 넘긴다).

쓰기 연결:
- confirm_notice()는 workbench.open_write_conn()으로 연 단발성 커넥션에서
  writer.set_notice_status 하나만 거쳐 쓴다(단일 쓰기 경로 원칙 유지). 캐시 무효화
  (st.cache_data.clear())는 review.py의 조치 저장 관례와 동일하게 호출부(뷰)의 책임이다.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from medsupply.data import queries, writer
from medsupply.services import inventory, workbench

#: writer.set_notice_status에 넘길 확인 완료 상태 문자열(계약값, task-M17-brief.md §6).
_STATUS_CONFIRMED = "확인 완료"


@st.cache_data
def load_notice_list(status: str | None = None, data_version: int = 0) -> pd.DataFrame:
    """공고 목록 — queries.get_notices 위임.

    data_version은 호출부(inventory.current_data_version())가 넘기는 캐시 무효화 신호일
    뿐 조회 조건으로는 쓰이지 않는다.
    """
    del data_version  # 캐시 키 무효화 전용 — 조회 조건에는 쓰지 않는다.
    conn = inventory.get_conn()
    return queries.get_notices(conn, status=status)


@st.cache_data
def load_notice_detail(notice_id: str, data_version: int = 0) -> dict | None:
    """공고 1건 상세 — queries.get_notice_detail 위임. 공고 미존재 시 None."""
    del data_version  # 캐시 키 무효화 전용 — 조회 조건에는 쓰지 않는다.
    conn = inventory.get_conn()
    return queries.get_notice_detail(conn, notice_id)


def confirm_notice(notice_id: str) -> None:
    """'확인 필요' 공고를 '확인 완료'로 저장한다(쓰기 단일 경로: writer.set_notice_status).

    inventory.get_conn()의 공유 캐시 커넥션이 아니라 workbench.open_write_conn()으로 연
    단발성 커넥션을 즉시 닫는다. 캐시 무효화(st.cache_data.clear())는 호출부(뷰)의 책임이다.
    """
    conn = workbench.open_write_conn()
    try:
        writer.set_notice_status(conn, notice_id, _STATUS_CONFIRMED)
    finally:
        conn.close()
