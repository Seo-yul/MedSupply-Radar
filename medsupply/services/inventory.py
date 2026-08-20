"""통합조회 서비스 — 수급 상황실(및 이후 화면들)이 소비하는 실데이터 조회 계층.

queries.py(순수 SQL 조회)의 결과를 조합·파생해 화면 친화적 DataFrame을 만든다. 이
모듈은 새 SQL을 직접 작성하지 않는다 — 새 조회가 필요하면 queries.py에 추가한다
(계층 규칙, task-M15-brief.md).

캐시 규칙:
- get_conn(): st.cache_resource로 세션 간 공유되는 단일 커넥션(check_same_thread=False
  — Streamlit은 세션마다 다른 스레드에서 스크립트를 실행할 수 있어, 캐시로 공유되는
  커넥션도 여러 스레드에서 재사용될 수 있다).
- load_overview(): st.cache_data로 결과를 캐시하며, data_version 인자를 캐시 키에
  포함해 무효화 신호로 쓴다(호출부가 current_data_version()의 값을 넘긴다 — writer.py의
  모든 쓰기가 data_version을 증가시킨다).

settings.DB_PATH는 항상 모듈 속성으로 접근한다(``from medsupply import settings`` 후
``settings.DB_PATH``) — 테스트가 monkeypatch로 값을 바꿔치기할 수 있어야 하므로, 호출
시점에 매번 다시 읽는다.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import numpy as np
import pandas as pd
import streamlit as st

from medsupply import settings
from medsupply.data import queries

#: form(제형) → form_code 매핑(브리프 규칙). 매핑에 없는 제형(정제·서방정·산제·패치 등)은
#: 전부 TAB 하나로 묶는다(TAB/EXT 세분화 없음 — 단순 유지).
_FORM_CODE_MAP = {"바이알": "INJ", "주사": "INJ", "시럽": "SYR", "캡슐": "CAP"}
_DEFAULT_FORM_CODE = "TAB"

#: 공급상태 라벨(docs/data-model.md §2.2 — 표기 문자열은 불변 계약).
STATUS_STOCKOUT = "현재 품절"
STATUS_HALTED = "공급중단"
STATUS_EXPECTED = "품절 예상"
STATUS_NORMAL = "정상화"

#: supply_status 우선순위 ③에 해당하는 위험등급 집합.
_EXPECTED_STOCKOUT_GRADES = {"위험", "경고"}

#: get_active_notice_map이 반환하는 활성 공고 중 '공급중단' 라벨(②)에 해당하는 notice_type.
_HALT_NOTICE_TYPE = "공급중단"


@st.cache_resource
def get_conn() -> sqlite3.Connection:
    """settings.DB_PATH를 여는 읽기 전용 커넥션(캐시 리소스, 세션 간 공유).

    check_same_thread=False — 캐시로 공유되는 이 커넥션이 여러 세션의 스크립트 실행
    스레드에서 재사용될 수 있기 때문이다. 이 모듈은 쓰기를 하지 않는다(쓰기는
    medsupply/data/writer.py의 몫).
    """
    conn = sqlite3.connect(str(settings.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def current_data_version(conn: sqlite3.Connection | None = None) -> int:
    """meta.data_version(캐시 무효화 신호). 키가 없으면 0.

    conn을 생략하면 get_conn()의 공유 커넥션을 쓴다. load_overview() 호출 직전에
    이 함수의 반환값을 data_version 인자로 넘기는 것이 정상 사용법이다.
    """
    if conn is None:
        conn = get_conn()
    meta = queries.get_meta(conn)
    return int(meta.get("data_version", 0))


def _form_code(form: object) -> str:
    if not isinstance(form, str):
        return _DEFAULT_FORM_CODE
    return _FORM_CODE_MAP.get(form, _DEFAULT_FORM_CODE)


def _halted_item_ids(conn: sqlite3.Connection, meta: dict) -> set[str]:
    """활성 '공급중단' 공고가 매핑된 item_id 집합(브리프 규칙 ②).

    meta.base_date가 없으면(비정상 스냅샷) 판정 불가로 보고 빈 집합을 반환한다.
    """
    base_date = meta.get("base_date")
    if not base_date:
        return set()
    active_map = queries.get_active_notice_map(conn, as_of=date.fromisoformat(base_date))
    if active_map.empty:
        return set()
    halted = active_map.loc[active_map["notice_type"] == _HALT_NOTICE_TYPE, "item_id"]
    return set(halted)


def _derive_supply_status(df: pd.DataFrame, halted_item_ids: set[str]) -> pd.Series:
    """공급상태 4분기(확정 규칙, 우선순위순).

    ① current_stock <= 0 → 현재 품절
    ② 활성 '공급중단' 공고 매핑 존재 → 공급중단
    ③ grade ∈ {위험, 경고} → 품절 예상
    ④ 그 외 → 정상화
    """
    is_stockout = df["current_stock"] <= 0  # NaN 비교는 항상 False → 안전
    is_halted = df["item_id"].isin(halted_item_ids)
    is_expected = df["grade"].isin(_EXPECTED_STOCKOUT_GRADES)

    return pd.Series(
        np.select(
            [is_stockout, is_halted, is_expected],
            [STATUS_STOCKOUT, STATUS_HALTED, STATUS_EXPECTED],
            default=STATUS_NORMAL,
        ),
        index=df.index,
    )


@st.cache_data
def load_overview(
    search: str = "",
    ingredient: str | None = None,
    form: str | None = None,
    supplier: str | None = None,
    grade: str | None = None,
    status: str | None = None,
    data_version: int = 0,
) -> pd.DataFrame:
    """수급 상황실 개요 목록 — list_items + current_stock/supply_status/form_code 파생.

    data_version은 호출부(current_data_version())가 넘기는 캐시 무효화 신호일 뿐 조회
    조건으로는 쓰이지 않는다. status는 supply_status(파생 컬럼) 필터라 SQL이 아니라
    pandas에서 걸러진다. score 내림차순 정렬(NULL은 뒤로).

    risk_results가 비어 있으면(배치 미실행) grade/score/days_to_stockout이 NULL인
    목록을 그대로 반환한다 — 이 함수는 등급을 판단하지 않는다.
    """
    del data_version  # 캐시 키 무효화 전용 — 조회 조건에는 쓰지 않는다.

    conn = get_conn()
    items = queries.list_items(
        conn,
        ingredient_code=ingredient,
        form=form,
        supplier=supplier,
        grade=grade,
        search=(search or None),
    )

    stock_map = queries.get_current_stock_map(conn)
    items = items.merge(stock_map, on="item_id", how="left")

    meta = queries.get_meta(conn)
    halted_item_ids = _halted_item_ids(conn, meta)

    items["supply_status"] = _derive_supply_status(items, halted_item_ids)
    items["form_code"] = items["form"].map(_form_code)

    if status is not None:
        items = items[items["supply_status"] == status]

    return items.sort_values("score", ascending=False, na_position="last").reset_index(
        drop=True
    )
