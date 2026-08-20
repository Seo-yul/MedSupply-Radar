"""알림센터 뷰 — 표준 스냅샷 실데이터 렌더(medsupply.services.alerts 경유).

마크업·CSS 클래스는 하드코딩 데모 버전(task-M26-brief.md 이전)과 동일하게 유지한다 —
badge css {critical, high, watch} 구조 그대로, 바뀌는 것은 f-string에 들어가는 값뿐이다
(재디자인 금지).
"""

from __future__ import annotations

import streamlit as st

from medsupply import settings
from medsupply.data import writer
from medsupply.services import alerts as alerts_service
from medsupply.services import inventory, workbench
from medsupply.ui.components import header

#: alerts.severity → badge CSS 클래스(브리프 §2, 데모의 {critical, high, watch} 구조 유지).
_SEVERITY_BADGE = {"긴급": "critical", "높음": "high", "확인": "watch"}

#: alerts.alert_type → 한글화(브리프 §2 "panel-sub에 ... alert_type 한글화 병기").
_ALERT_TYPE_LABELS = {
    "grade_up": "위험등급 상승",
    "receipt_delay": "입고 지연",
    "notice_map": "공고 매핑",
}


def render() -> None:
    if not settings.DB_PATH.exists():
        st.warning("표준 스냅샷이 없습니다 — README의 생성 명령을 실행하세요")
        return

    write_conn = workbench.open_write_conn()
    try:
        alerts_service.sync_alerts(write_conn)
    except Exception as exc:
        st.warning(f"알림 동기화에 실패했습니다: {exc}")
    finally:
        write_conn.close()

    data_version = inventory.current_data_version()

    header("알림센터", "위험 변화와 신규 공고 매핑을 중요도순으로 확인합니다.", "알림센터")

    total_count = len(alerts_service.load_alerts(unread_only=False, data_version=data_version))
    unread_count = len(alerts_service.load_alerts(unread_only=True, data_version=data_version))
    st.caption(f"미확인 {unread_count}건 · 전체 {total_count}건")

    unread_only = st.toggle("미확인만", value=False)
    alerts_df = alerts_service.load_alerts(unread_only=unread_only, data_version=data_version)

    if alerts_df.empty:
        st.info("알림이 없습니다 — 위험 변화가 감지되면 여기에 쌓입니다.")
        return

    for row in alerts_df.itertuples():
        badge_css = _SEVERITY_BADGE.get(row.severity, "watch")
        type_label = _ALERT_TYPE_LABELS.get(row.alert_type, row.alert_type)
        st.markdown(
            f'<div class="panel"><span class="badge {badge_css}">{row.severity}</span>'
            f'<div class="panel-title" style="margin-top:10px">{row.title}</div>'
            f'<div class="panel-sub" style="margin:0">{row.body or ""}</div>'
            f'<div class="panel-sub" style="margin:0">{row.created_at} · {type_label}</div>'
            "</div>",
            unsafe_allow_html=True,
        )
        if row.is_read == 0:
            if st.button("읽음 처리", key=f"read_{row.alert_id}"):
                read_conn = workbench.open_write_conn()
                try:
                    writer.mark_alert_read(read_conn, row.alert_id)
                finally:
                    read_conn.close()
                st.cache_data.clear()
                st.rerun()
        else:
            st.caption("확인됨")
