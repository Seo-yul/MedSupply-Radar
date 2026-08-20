"""검토 대기함(약사 검토 워크벤치) 뷰 — 표준 스냅샷 실데이터 렌더
(medsupply.services.workbench 경유).

마크업·CSS 클래스는 하드코딩 데모 버전(task-M16-brief.md 이전)과 동일하게 유지한다 —
이 파일이 바뀌는 것은 f-string에 들어가는 값뿐이다(재디자인 금지). "AI 근거 설명" 탭은
llm_explanations 저장분으로 치환됐다(task-M23-brief.md). "AI 문의" 코파일럿 패널은
여전히 이번 범위 밖이다 — copilot_answer 로직과 정적 안내문은 손대지 않는다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

from medsupply import settings
from medsupply.data import queries, writer
from medsupply.llm.config import load_llm_config
from medsupply.llm.explanation import explain_item
from medsupply.services import inventory, workbench
from medsupply.ui.charts import gauge, trend_chart
from medsupply.ui.components import header
from medsupply.views._demo import copilot_answer

#: risk_results.factors_json의 anomalies[].kind → 한글 표기(브리프 규칙 §7).
_ANOMALY_LABELS = {
    "usage_surge": "수요 급증",
    "usage_drop": "사용량 급감",
    "receipt_delay": "입고 지연",
    "stock_rebuild": "재고 회복",
}

#: risk_results.grade → 검토 요약 색(브리프 규칙 §4의 인라인 스타일 방식 유지).
_GRADE_COLOR = {"위험": "#a8352a", "경고": "#966a15", "주의": "#59506e", "정상": "#2e6e4e"}
_GRADE_COLOR_UNKNOWN = "#49525a"  # risk 없음(미평가) 시 muted 톤(--ink-soft)

_ACTION_TYPES = ["입고 일정 확인", "대체 품목 검토", "발주량 조정", "처방 부서 공유"]


def _select_label(row: Any) -> str:
    grade = row.grade if pd.notna(row.grade) else "미평가"
    score = int(row.score) if pd.notna(row.score) else "-"
    return f"{row.item_name} — {grade} · {score}"


def _pct_change(current: float | None, prev: float | None) -> float | None:
    """current의 prev 대비 변화율(%). prev가 None이거나 0이면 계산 불가로 None."""
    if current is None or not prev:
        return None
    return (current - prev) / prev * 100


def _factor_rows_html(factors: dict, days_to_stockout: int | None) -> str:
    """요인 리스트(.factor) — anomalies·공고 상향·재고 커버리지에서 결정적으로 생성."""
    rows = [
        f'<div class="factor"><b>{_ANOMALY_LABELS.get(a.get("kind"), a.get("kind", ""))}</b>'
        f'<span>{a.get("detail", "")}</span></div>'
        for a in factors.get("anomalies", [])
    ]
    if factors.get("escalated_by_notice"):
        rows.append(
            '<div class="factor"><b>공급 공고 상향</b>'
            "<span>활성 공급중단 공고 매핑 — 1등급 상향</span></div>"
        )
    if days_to_stockout is not None and days_to_stockout <= 30:
        rows.append(
            '<div class="factor"><b>재고 커버리지</b>'
            f"<span>{days_to_stockout}일 내 소진 예상</span></div>"
        )
    if not rows:
        rows.append('<div class="factor"><b>특이 요인 없음</b><span>정상 소진 추세</span></div>')
    return "".join(rows)


def _stock_class(current_stock: float | None, avg_daily_usage: float | None) -> str:
    """결정 규칙(브리프 §8): 일평균 사용량의 7일분 미만이면 stock-low, 아니면 stock-ok."""
    stock = current_stock if current_stock is not None else 0
    avg = avg_daily_usage if avg_daily_usage is not None else 0
    return "stock-low" if stock < avg * 7 else "stock-ok"


def _stock_text(current_stock: float | None) -> str:
    return "재고 -" if current_stock is None else f"재고 {current_stock:,.0f}"


def _substitutes_html(detail: dict) -> str:
    """대체 후보 탭 med-tree(기존 HTML 클래스 전부 유지, 값만 substitutes 실데이터)."""
    substitutes = detail["substitutes"]
    if substitutes.empty:
        return '<div class="clinical-warning">등록된 대체 후보가 없습니다</div>'

    item = detail["item"]
    avg_daily_usage = detail["avg_daily_usage"]
    current_stock = detail["current_stock"]
    same = substitutes[substitutes["same_condition"]]
    mismatched = substitutes[~substitutes["same_condition"]]

    html = [
        '<div class="med-tree"><div class="tree-root"><span class="molecule">⌬</span><div>'
        f'{detail["ingredient_name_kr"]}'
        '<small style="display:block;font-family:var(--mono);color:#7d786c;font-size:10px;'
        f'font-weight:500;letter-spacing:.06em">{detail["ingredient_name_en"].upper()} · 동일 성분 관계</small>'
        "</div></div>",
        '<div class="tree-group">'
        f'<div class="tree-condition">{item.get("strength") or "-"} · {item.get("form") or "-"} · {item.get("route") or "-"}</div>'
        '<div class="tree-node current">'
        f'<strong>현재 품목 · {item.get("supplier") or "-"}</strong>'
        f'<span class="{_stock_class(current_stock, avg_daily_usage)}">{_stock_text(current_stock)}</span>'
        "</div>",
    ]
    for row in same.itertuples():
        row_stock = None if pd.isna(row.current_stock) else row.current_stock
        html.append(
            '<div class="tree-node">'
            f'<strong>동일 조건 · {row.supplier}</strong>'
            f'<span class="{_stock_class(row_stock, avg_daily_usage)}">{_stock_text(row_stock)}</span>'
            "</div>"
        )
    html.append("</div>")

    if not mismatched.empty:
        html.append('<div class="tree-section-label">조건이 다른 후보</div>')
        for _, group in mismatched.groupby(["strength", "form", "route"], sort=False):
            first = group.iloc[0]
            html.append(
                '<div class="tree-group">'
                f'<div class="tree-condition" style="color:#59506e">{first.strength} · {first.form} · {first.route}</div>'
            )
            for row in group.itertuples():
                html.append(
                    '<div class="tree-node mismatch">'
                    f"<strong>{row.item_name}</strong>"
                    '<span class="badge inactive">조건 불일치</span>'
                    '<small style="display:block;clear:both;color:#966a15;padding-top:6px">'
                    f"{row.strength}·{row.form} 상이 · 처방 변경 및 임상 검토 필요</small></div>"
                )
            html.append("</div>")

    html.append("</div>")
    return "".join(html)


#: info_tab(AI 근거 설명) 고정 문구(task-M23-brief.md 치환 규칙 §3·§5).
_EXPLANATION_PENDING_NOTICE = "AI 원인 설명이 아직 생성되지 않았습니다."
_EXPLANATION_PENDING_HINT = "API 키 설정 후: python scripts/warm_cache.py --db data/medsupply.db"
_EXPLANATION_SCOPE_NOTE = "AI는 위험등급 판정에 관여하지 않습니다."
_EXPLANATION_GENERATE_LABEL = "설명 생성"


def _explanation_badge_html(flags: list[str]) -> str:
    """hallucination_flags 경고 배지(브리프 §2) — clinical-warning 클래스 재사용.

    verify_explanation_grounding은 role-blind 부분 신호다(medsupply.llm.grounding
    모듈 docstring "구조적 한계" 참조) — "탐지된 경고"로만 표기하고 무결 보증처럼 쓰지
    않는다. 플래그를 숨기지 않는다: 앞 2개를 그대로 요약에 포함한다.
    """
    if not flags:
        return ""
    summary = " · ".join(flags[:2])
    return (
        '<div class="clinical-warning">'
        f"사후 대조 경고 {len(flags)}건 — 아래 설명에 근거 밖 인용이 있을 수 있습니다: {summary}"
        "</div>"
    )


def _explanation_actions_html(actions: list[dict]) -> str:
    """action 블록들 — 기존 01·02·03 마크업 구조 재사용, 개수는 actions 길이(브리프 §1)."""
    return "".join(
        '<div class="action">'
        f'<b>{i:02d} · {a.get("title", "")}</b>'
        f'<p>{a.get("description", "")}</p>'
        "</div>"
        for i, a in enumerate(actions, start=1)
    )


def _explanation_panel_html(explanation_row: dict | None) -> str:
    """AI 근거 설명 탭(info_tab) 패널 — llm_explanations 저장분 유무에 따른 3분기
    (task-M23-brief.md 치환 규칙 1~3). 마크업·클래스는 하드코딩 데모와 동일하게 유지한다.

    explanation_row는 medsupply.data.queries.get_explanation의 반환값 그대로
    (services.workbench.load_item_detail의 "explanation" 키) — None이면 저장분 없음
    (현재 기본 경로, 키 없는 환경).
    """
    header_html = (
        '<div class="panel"><div class="panel-title">왜 위험한가?</div>'
        '<div class="panel-sub">NHS SPS 방식으로 핵심 판단과 권장 조치를 분리했습니다.</div>'
    )
    footer_html = f'<div class="tiny">{_EXPLANATION_SCOPE_NOTE}</div></div>'

    if explanation_row is None:
        body_html = (
            f'<div class="notice">{_EXPLANATION_PENDING_NOTICE}</div>'
            f'<div class="tiny">{_EXPLANATION_PENDING_HINT}</div>'
        )
        return header_html + body_html + footer_html

    payload = explanation_row["payload"]
    explanation = payload["explanation"]
    flags = payload.get("hallucination_flags", [])

    history_note = explanation.get("history_note")
    history_html = f'<div class="tiny">{history_note}</div>' if history_note else ""

    meta_html = (
        '<div class="tiny">생성: '
        f'{explanation_row.get("provider") or "-"}/{explanation_row.get("model") or "-"}'
        f' · 프롬프트 {explanation_row.get("prompt_version") or "-"}'
        f' · {explanation_row.get("generated_at") or "-"}'
        f' · 근거 {len(explanation.get("evidence_refs", []))}건</div>'
    )

    body_html = (
        _explanation_badge_html(flags)
        + f'<div class="notice">{explanation.get("cause_summary", "")}</div>'
        + history_html
        + "<br>"
        + _explanation_actions_html(explanation.get("actions", []))
        + meta_html
    )
    return header_html + body_html + footer_html


def _active_notice_posted_date(conn, item_id: str, base_date: date) -> date | None:
    """활성 공급중단·공급부족 공고의 게시일(조회 불가 시 None — base_date 대체 표기 금지,
    브리프 §6)."""
    active_map = queries.get_active_notice_map(conn, as_of=base_date)
    item_active = active_map.loc[active_map["item_id"] == item_id]
    if item_active.empty:
        return None
    notices_df = queries.get_notices(conn, item_id=item_id)
    matched = notices_df.loc[notices_df["notice_id"].isin(set(item_active["notice_id"]))]
    if matched.empty:
        return None
    posted = matched.sort_values("published_date").iloc[0]["published_date"]
    return date.fromisoformat(posted)


def render() -> None:
    if not settings.DB_PATH.exists():
        st.warning("표준 스냅샷이 없습니다 — README의 생성 명령을 실행하세요")
        return

    conn = inventory.get_conn()
    data_version = inventory.current_data_version(conn)
    meta = queries.get_meta(conn)
    base_date = date.fromisoformat(meta["base_date"])
    overview = inventory.load_overview(data_version=data_version)

    header("약사 검토 워크벤치", "위험 근거와 동일 조건 대체 후보를 확인한 뒤 조치안을 작성합니다.", "검토 대기함")

    has_risk_run = bool(queries.get_latest_runs(conn, 1))
    if not has_risk_run:
        st.info(
            "위험 평가 배치를 실행하세요: python scripts/run_risk_batch.py"
            " --db data/medsupply.db --as-of <YYYY-MM-DD>"
        )

    item_ids = overview["item_id"].tolist()
    labels = {row.item_id: _select_label(row) for row in overview.itertuples()}
    selected_item_id = st.selectbox(
        "품목 선택", item_ids, index=0, format_func=lambda iid: labels[iid],
    )

    detail = workbench.load_item_detail(selected_item_id, data_version=data_version)
    item = detail["item"]
    risk = detail["risk"]
    factors = risk["factors"] if risk else {}
    days_to_stockout = risk["days_to_stockout"] if risk else None
    same_condition_count = int(detail["substitutes"]["same_condition"].sum())

    score_text = str(risk["score"]) if risk and risk["score"] is not None else "-"
    grade_text = risk["grade"] if risk else "미평가"
    stockout_text = "-" if days_to_stockout is None else f"{days_to_stockout}일 후"

    inn_display = (
        detail["ingredient_name_en"].upper()
        if detail["ingredient_name_en"] != "-"
        else detail["ingredient_name_kr"]
    )
    essential_chip = '<span class="meta-chip">필수의약품</span>' if item.get("is_essential") == 1 else ""
    st.markdown(
        '<div class="drug-label"><div class="label-top"><div>'
        f'<div class="label-name">{item["item_name"]}</div>'
        f'<div class="label-inn">{inn_display} · {item.get("atc_code") or "-"}</div>'
        '<div class="label-meta">'
        f'<span class="meta-chip">{item.get("strength") or "-"}</span>'
        f'<span class="meta-chip">{item.get("form") or "-"}</span>'
        f'<span class="meta-chip">{item.get("route") or "-"}</span>'
        f'<span class="meta-chip">전문의약품</span>{essential_chip}'
        '</div></div><div><span class="rx">Rx</span></div></div>'
        '<div class="source-strip">'
        f'<span>품목기준코드 {item["item_id"]}</span>'
        f'<span>제조사 {item.get("supplier") or "-"}</span>'
        f'<span>성분코드 {item.get("ingredient_code") or "-"}</span>'
        f'<span>최종 갱신 {base_date:%Y.%m.%d} 09:30</span>'
        "</div></div>",
        unsafe_allow_html=True,
    )

    overview_row = overview.loc[overview["item_id"] == selected_item_id].iloc[0]
    evidence_count = len(factors.get("anomalies", [])) + (1 if detail["has_active_notice"] else 0)
    st.markdown(
        '<div class="workflow">'
        f'<div class="workflow-step done"><b>1 · 위험 확인 ✓</b>{overview_row.supply_status} · {score_text}</div>'
        f'<div class="workflow-step done"><b>2 · 근거 검토 ✓</b>근거 {evidence_count}건</div>'
        f'<div class="workflow-step current"><b>3 · 대체약 검토</b>동일 조건 {same_condition_count}개</div>'
        '<div class="workflow-step"><b>4 · 조치 확정</b>약사 확인</div>'
        '<div class="workflow-step"><b>5 · 결과 추적</b>이력 관리</div>'
        "</div>",
        unsafe_allow_html=True,
    )

    summary_col, copilot_col = st.columns([1.8, 1])
    with summary_col:
        history = queries.list_action_history(conn, item_id=selected_item_id, limit=1)
        owner_status = history.iloc[0]["status"] if not history.empty else "기록 없음"
        grade_color = _GRADE_COLOR.get(grade_text, _GRADE_COLOR_UNKNOWN)
        st.markdown(
            '<div class="panel"><div class="panel-title">검토 요약</div>'
            '<div class="panel-sub">현재 선택 품목의 핵심 위험 신호</div>'
            f'<div class="score"><span>품절 위험</span><strong style="color:{grade_color}">{score_text} · {grade_text}</strong></div>'
            f'<div class="score"><span>예상 소진</span><strong>{stockout_text}</strong></div>'
            f'<div class="score"><span>동일 조건 대체 후보</span><strong>{same_condition_count}개</strong></div>'
            f'<div class="score"><span>담당자 확인</span><strong>{owner_status}</strong></div>'
            "</div>",
            unsafe_allow_html=True,
        )
    with copilot_col:
        with st.container(border=True):
            st.markdown("#### AI 문의")
            st.caption(f"현재 품목: {item['item_name']} · 근거 확인용 코파일럿")
            if "copilot_messages" not in st.session_state:
                st.session_state.copilot_messages = []
            q1, q2 = st.columns(2)
            suggested = None
            if q1.button("왜 위험한가요?", use_container_width=True):
                suggested = "왜 위험한가요?"
            if q2.button("소진일 계산 근거", use_container_width=True):
                suggested = "예상 소진일 계산 근거를 알려줘"
            q3, q4 = st.columns(2)
            if q3.button("대체 후보 비교", use_container_width=True):
                suggested = "동일 조건 대체 후보를 비교해줘"
            if q4.button("발주량 계산 근거", use_container_width=True):
                suggested = "발주 요청량 계산 근거를 알려줘"
            if suggested:
                st.session_state.copilot_messages.append((suggested, copilot_answer(suggested)))
            for user_text, answer_text in st.session_state.copilot_messages[-2:]:
                with st.chat_message("user"):
                    st.markdown(user_text)
                with st.chat_message("assistant"):
                    st.markdown(answer_text)
            free_question = st.text_input("직접 질문", placeholder="공고 내용을 요약해줘", label_visibility="collapsed")
            if st.button("질문 보내기", type="primary", use_container_width=True):
                if free_question.strip():
                    st.session_state.copilot_messages.append((free_question, copilot_answer(free_question)))
                    st.rerun()
            st.caption("답변은 의사결정 참고용이며 발주·대체를 자동 실행하지 않습니다.")

    series = detail["series"]
    stock_delta = None
    if len(series) > 7:
        current_val = series.iloc[-1]["closing_stock"]
        past_val = series.iloc[-8]["closing_stock"]
        pct = _pct_change(current_val, past_val)
        if pct is not None:
            stock_delta = f"{pct:+.0f}% / 7일"

    usage_pct = _pct_change(detail["avg_daily_usage"], detail["avg_prev"])
    usage_delta = f"{usage_pct:+.0f}% / 4주" if usage_pct is not None else None

    stockout_delta = None
    if risk and risk.get("depletion_date"):
        dep = date.fromisoformat(risk["depletion_date"])
        stockout_delta = f"{dep.month}월 {dep.day}일"

    next_shipment = detail["next_shipment"]
    next_exp_date = date.fromisoformat(next_shipment["expected_date"]) if next_shipment else None
    if next_exp_date is not None:
        next_value = f"{next_exp_date.month}월 {next_exp_date.day}일"
        next_qty = next_shipment.get("qty")
        next_delta = f"{next_qty:,}" if next_qty is not None else None
    else:
        next_value = "미정"
        next_delta = None

    a, b, c, d = st.columns(4)
    stock_value = f"{detail['current_stock']:,.0f}" if detail["current_stock"] is not None else "-"
    a.metric("현재 재고", stock_value, stock_delta, delta_color="inverse")
    usage_value = detail["avg_daily_usage"] if detail["avg_daily_usage"] is not None else "-"
    b.metric("일평균 사용량", f"{usage_value}", usage_delta)
    c.metric("예상 소진", stockout_text, stockout_delta, delta_color="off")
    d.metric("다음 입고", next_value, next_delta)

    left, right = st.columns([1.7, 1])
    with left:
        st.markdown(
            '<div class="panel"><div class="panel-title">Risk Timeline</div>'
            '<div class="panel-sub">재고와 사용량 변화 · 최근 4주</div>',
            unsafe_allow_html=True,
        )
        events = []
        if detail["has_active_notice"]:
            posted = _active_notice_posted_date(conn, selected_item_id, base_date)
            if posted is not None:
                events.append((posted, "공급 공고", "#59506e"))
        forecast_daily = detail["forecast"]["daily"] if detail["forecast"] else None
        st.plotly_chart(
            trend_chart(series, events=events, forecast=forecast_daily),
            use_container_width=True, config={"displayModeBar": False},
        )
        st.markdown("</div>", unsafe_allow_html=True)
    with right:
        st.markdown(
            '<div class="panel"><div class="panel-title">품절 위험점수</div>'
            '<div class="panel-sub">결정적 규칙 기반 산정 · LLM 판정 미관여</div>',
            unsafe_allow_html=True,
        )
        gauge_score = risk["score"] if risk and risk["score"] is not None else 0
        st.plotly_chart(gauge(gauge_score), use_container_width=True, config={"displayModeBar": False})
        st.markdown(_factor_rows_html(factors, days_to_stockout), unsafe_allow_html=True)

    info_tab, alt_tab, source_tab = st.tabs(["AI 근거 설명", "대체 후보", "공고·근거"])
    with info_tab:
        st.markdown(_explanation_panel_html(detail["explanation"]), unsafe_allow_html=True)

        llm_cfg = load_llm_config()
        if llm_cfg.anthropic_key_set or llm_cfg.openai_key_set:
            if st.button(_EXPLANATION_GENERATE_LABEL):
                write_conn = workbench.open_write_conn()
                try:
                    explain_item(write_conn, selected_item_id)
                except Exception as exc:
                    st.error(f"설명 생성 실패: {exc}")
                else:
                    st.cache_data.clear()
                    st.rerun()
                finally:
                    write_conn.close()
    with alt_tab:
        st.markdown(
            '<div class="clinical-warning"><b>약사 확인 필수</b> · 동일 조건 후보와 조건이 다른 후보를 구분하며, 자동 대체 처방을 의미하지 않습니다.</div>'
            + _substitutes_html(detail),
            unsafe_allow_html=True,
        )
    with source_tab:
        usage_change_text = f"{usage_pct:+.0f}%" if usage_pct is not None else "-"
        notice_text = "원문 확인" if detail["has_active_notice"] else "해당 없음"
        shipment_text = f"{next_exp_date:%m.%d} 예정" if next_exp_date is not None else "미정"
        st.markdown(
            '<div class="panel"><div class="panel-title">판단 근거와 출처</div>'
            f'<div class="score"><span>기관 재고 스냅샷</span><strong>{base_date:%m.%d} 09:30</strong></div>'
            f'<div class="score"><span>최근 4주 사용량</span><strong>{usage_change_text}</strong></div>'
            f'<div class="score"><span>제조사 공급중단 공고</span><strong>{notice_text}</strong></div>'
            f'<div class="score"><span>입고예정 데이터</span><strong>{shipment_text}</strong></div>'
            '<br><div class="tiny">AI는 위험등급 판정에 관여하지 않으며 입력 근거를 자연어로 요약합니다.</div></div>',
            unsafe_allow_html=True,
        )

    with st.expander("약사 검토 및 대응 조치 기록", expanded=True):
        col1, col2 = st.columns(2)
        action_type = col1.selectbox("조치 유형", _ACTION_TYPES)
        owner = col2.text_input("담당자", "김약사")
        note = st.text_area("조치 내용", placeholder="확인한 내용과 후속 계획을 입력하세요.")
        reviewed = st.checkbox("위험 근거와 대체 후보 조건을 확인했습니다.")
        if st.button("이력 저장", type="primary"):
            if reviewed:
                write_conn = workbench.open_write_conn()
                try:
                    history_id = writer.save_action_history(
                        write_conn, selected_item_id, action_type, owner, note,
                        status="진행 중", risk_type=risk["risk_type"] if risk else None,
                    )
                finally:
                    write_conn.close()
                st.cache_data.clear()
                st.success(f"{history_id}번 이력이 저장되었습니다")
            else:
                st.warning("약사 검토 확인 후 저장할 수 있습니다.")
