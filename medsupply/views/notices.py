"""공급 공고 매핑 뷰 — 표준 스냅샷 실데이터 렌더(medsupply.services.notices 경유).

마크업·CSS 클래스는 하드코딩 데모 버전(task-M17-brief.md 이전)과 동일하게 유지한다 —
이 파일이 바뀌는 것은 f-string에 들어가는 값뿐이다(재디자인 금지).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from medsupply import settings
from medsupply.services import inventory
from medsupply.services import notices as notices_service
from medsupply.ui.components import header

#: 확인 상태 필터 라디오 옵션(브리프 §1) — "전체"는 상태 무관 전체 목록(status=None).
_STATUS_FILTER_OPTIONS = ["전체", "자동확정", "확인 필요", "확인 완료"]


#: notice_item_map.match_basis → 한글 표기(medsupply/llm/mapping.py가 실제로 쓰는 값
#: 집합 'ingredient'|'ingredient_partial'|'product'만 안다 — 그 밖의 값(예: 레거시
#: 'standard_code')은 원문을 그대로 폴백해 보여준다).
_MATCH_BASIS_LABELS = {
    "ingredient": "성분 일치",
    "ingredient_partial": "성분 부분 일치",
    "product": "제품명 일치",
}


def _needs_review_label(value: int) -> str:
    return "검토 필요" if value == 1 else "-"


def _match_basis_label(value: str) -> str:
    return _MATCH_BASIS_LABELS.get(value, value)


def render() -> None:
    if not settings.DB_PATH.exists():
        st.warning("표준 스냅샷이 없습니다 — README의 생성 명령을 실행하세요")
        return

    data_version = inventory.current_data_version()

    header("공급 공고 매핑", "외부 공고를 구조화하고 기관 보유 품목과 자동으로 연결합니다.", "공급 공고")

    status_filter = st.radio(
        "확인 상태", _STATUS_FILTER_OPTIONS, horizontal=True, label_visibility="collapsed",
    )
    status_arg = None if status_filter == "전체" else status_filter

    # 전체 건수는 현재 필터와 무관한 총 공고 수(패널 제목·DB 부재 판정 공용).
    total_count = len(notices_service.load_notice_list(status=None, data_version=data_version))

    st.markdown(
        f'<div class="panel"><div class="panel-title">공고 {total_count}건</div>'
        '<div class="panel-sub">원문과 AI 추출 결과를 함께 확인할 수 있습니다.</div>',
        unsafe_allow_html=True,
    )

    if total_count == 0:
        st.info("적재된 공고가 없습니다 — scripts/load_notices.py 실행")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    notice_list = notices_service.load_notice_list(status=status_arg, data_version=data_version)

    list_df = pd.DataFrame(
        {
            "공고일": notice_list["published_date"],
            "제목": notice_list["title"],
            "유형": notice_list["notice_type"],
            "출처": notice_list["source"],
            "매핑 품목": notice_list["mapped_count"].astype(int),
            "확인 상태": notice_list["status"].fillna("미추출"),
            "신뢰도": notice_list["confidence"].apply(
                lambda v: "-" if pd.isna(v) else f"{v:.2f}"
            ),
        }
    )
    st.dataframe(list_df, hide_index=True, use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

    if notice_list.empty:
        return  # 현재 필터에 해당하는 공고가 없다 — 선택/상세로 이어갈 대상이 없다.

    notice_ids = notice_list["notice_id"].tolist()
    labels = {
        row.notice_id: f"{row.published_date} · {row.title}"
        for row in notice_list.itertuples()
    }
    selected_notice_id = st.selectbox(
        "공고 선택", notice_ids, index=0, format_func=lambda nid: labels[nid],
    )

    detail = notices_service.load_notice_detail(selected_notice_id, data_version=data_version)

    with st.expander(f'{detail["title"]} 추출 결과', expanded=True):
        x1, x2 = st.columns(2)
        with x1:
            st.text_area("공고 원문", detail["raw_text"] or "", height=260, disabled=True)
        with x2:
            payload = detail["payload"]
            if payload is None:
                st.info("추출 미실행 — python scripts/process_notices.py --all")
            else:
                halt_start = payload.get("halt_start_date") or "미상"
                restart = payload.get("expected_restart_date") or "미정"
                st.json(
                    {
                        "제품명": payload.get("product_names"),
                        "성분명": payload.get("ingredient_names"),
                        "사유": payload.get("reason"),
                        "기간": f"{halt_start} ~ {restart}",
                        "유형": detail["notice_type"],
                        "기관 매핑": f'{len(detail["mapped"])}개 품목',
                        "확인 상태": detail["status"],
                        "신뢰도": detail["confidence"],
                        "근거 발췌": payload.get("evidence_quotes"),
                    }
                )

    mapped = detail["mapped"]
    if mapped:
        mapped_df = pd.DataFrame(mapped)[["item_id", "item_name", "match_basis", "needs_review"]]
        mapped_df["match_basis"] = mapped_df["match_basis"].map(_match_basis_label)
        mapped_df["needs_review"] = mapped_df["needs_review"].map(_needs_review_label)
        mapped_df = mapped_df.rename(
            columns={
                "item_id": "품목코드",
                "item_name": "품목명",
                "match_basis": "매칭 근거",
                "needs_review": "검토 필요",
            }
        )
        st.dataframe(mapped_df, hide_index=True, use_container_width=True)
    else:
        st.caption("기관 보유 품목과 매핑되지 않았습니다(정상 — 미보유 품목 공고).")

    if detail["status"] == "확인 필요":
        if st.button("확인 완료로 저장", type="primary"):
            notices_service.confirm_notice(selected_notice_id)
            st.cache_data.clear()
            st.rerun()
    elif detail["status"] == "자동확정":
        st.caption("자동확정된 공고입니다")
    elif detail["status"] == "확인 완료":
        st.caption("확인 완료된 공고입니다")
