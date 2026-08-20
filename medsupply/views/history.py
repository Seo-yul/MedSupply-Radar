"""대응 이력과 결과 추적 뷰 — 표준 스냅샷 실데이터 렌더(medsupply.services.history 경유).

마크업은 하드코딩 데모 버전(task-M18-brief.md 이전)과 동일하게 유지한다 — 이 파일이
바뀌는 것은 f-string에 들어가는 값과, 브리프가 새로 요구하는 필터 위젯뿐이다(재디자인
금지). 데모의 "평균 처리시간·위험도 하락" 지표는 완료 시각·전후 위험도 데이터가 없어
산출 불가라 제거하고(브리프 §1), 계산 가능한 지표로 대체했다.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from medsupply import settings
from medsupply.services import history as history_service
from medsupply.services import inventory
from medsupply.ui.components import header

#: action_history.risk_type → 한글 표기(situation.py의 동일 매핑과 같은 값 집합).
_RISK_TYPE_LABELS = {
    "demand_surge": "수요 급증",
    "supply_halt": "공급 중단",
    "delivery_delay": "입고 지연",
    "composite": "복합",
    "general": "일반",
}
#: 위험 유형 필터 selectbox 옵션(브리프 §2) — "전체"는 필터 미적용(risk_type=None).
_RISK_TYPE_FILTER_OPTIONS = ["전체", "demand_surge", "supply_halt", "delivery_delay", "composite", "general"]


def _risk_type_label(value: str) -> str:
    return "전체" if value == "전체" else _RISK_TYPE_LABELS[value]


def render() -> None:
    if not settings.DB_PATH.exists():
        st.warning("표준 스냅샷이 없습니다 — README의 생성 명령을 실행하세요")
        return

    data_version = inventory.current_data_version()

    header("대응 이력과 결과 추적", "조치가 실제 위험을 낮췄는지 확인하고 다음 대응의 기준으로 축적합니다.", "대응 이력")

    # 메트릭 4개는 필터와 무관한 전체 이력 기준(situation.py 상단 메트릭과 동일 관례).
    all_history = history_service.load_history(data_version=data_version)

    if all_history.empty:
        st.info("저장된 조치 이력이 없습니다 — 워크벤치에서 첫 조치를 기록하세요.")
        return

    total_count = len(all_history)
    completed_count = int((all_history["status"] == "완료").sum())
    in_progress_count = int((all_history["status"] == "진행 중").sum())
    order_linked_count = int(all_history["order_id"].notna().sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("전체 이력", str(total_count), None)
    c2.metric("완료", str(completed_count), None)
    c3.metric("진행 중", str(in_progress_count), None)
    c4.metric("발주 연계", str(order_linked_count), None)
    st.write("")

    f1, f2 = st.columns([2, 1])
    search = f1.text_input("품목·내용 검색")
    risk_type_filter = f2.selectbox(
        "위험 유형", _RISK_TYPE_FILTER_OPTIONS, format_func=_risk_type_label,
    )
    risk_type_arg = None if risk_type_filter == "전체" else risk_type_filter

    filtered = history_service.load_history(
        risk_type=risk_type_arg, search=search, data_version=data_version,
    )

    display_df = pd.DataFrame(
        {
            "일시": filtered["created_at"],
            "품목": filtered["item_name"],
            "조치": filtered["action_type"],
            "내용": filtered["note"],
            "담당자": filtered["owner"],
            "상태": filtered["status"],
            "위험 유형": filtered["risk_type"].apply(
                lambda v: "-" if pd.isna(v) else _RISK_TYPE_LABELS.get(v, "-")
            ),
        }
    )
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    filtered_completed = int((filtered["status"] == "완료").sum())
    filtered_in_progress = int((filtered["status"] == "진행 중").sum())
    st.caption(
        f"완료 {filtered_completed}건 · 진행 중 {filtered_in_progress}건 · "
        "워크벤치에서 저장한 조치가 이 목록에 축적됩니다."
    )
