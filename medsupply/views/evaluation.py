"""AI 평가 뷰 — 실측 리포트 파일 연동판(Task S-31).

가공(하드코딩) 지표를 전면 걷어내고 medsupply.services.evaluation.load_eval_reports가
읽어온 저장소 내 리포트 파일 값을 그대로 노출한다. 원칙: **수치는 사실 그대로, 합격/
불합격 단정 문구는 쓰지 않는다**(리포트 자체에 verdict가 있으면 그 값만 표시하고
"목표 달성" 류의 판단 문구를 이 뷰가 새로 만들지 않는다). 리포트 파일이 없거나(부재)
깨져 있으면(JSON 파싱 실패) 그 블록은 "실측 전"으로 정직하게 렌더한다 — 두 상태
(`{"exists": False}`와 `{"exists": True, "error": ...}`)를 이 뷰는 구분하지 않고
`_is_available()`로 통일해 같은 회색 배지로 렌더한다(브리프: "모든 '실측 전' 셀은
동일 문구·회색 계열 기존 클래스").

마크업·CSS 클래스는 기존 evaluation.py(하드코딩 데모판)가 쓰던 어휘를 그대로 재사용한다
— header()·panel/panel-title/panel-sub·score·tiny·badge(inactive, 기존 situation 뷰
등에서 이미 쓰이는 "비활성/회색" 의미의 클래스). 원본이 쓰던 plotly 추세 차트는 실제
데이터 소스가 없는 가공 수치(prompt-v1/v2/v3 가상 실험)였으므로 제거했다 — 브리프의
"가공 수치 전부 제거" 원칙에 따른 것이며 재디자인이 목적이 아니다.
"""

from __future__ import annotations

import streamlit as st

from medsupply.services import evaluation as evaluation_service
from medsupply.ui.components import header

_PENDING_BADGE = '<span class="badge inactive">실측 전</span>'


def _is_available(entry: dict) -> bool:
    """entry가 evaluation_service.load_eval_reports의 리포트별 반환 형태일 때 실제로
    렌더 가능한 데이터를 갖고 있는지. 파일 부재({"exists": False})와 파싱 실패
    ({"exists": True, "error": ...}) 두 경우 모두 False — 이 뷰는 둘을 구분하지 않고
    동일한 '실측 전' 배지로 렌더한다."""
    return bool(entry.get("exists")) and "error" not in entry


def _pct(value: float | None, digits: int = 1) -> str:
    return f"{value:.{digits}%}" if value is not None else "n/a"


def _raw(value: object, suffix: str = "") -> str:
    return f"{value}{suffix}" if value is not None else "n/a"


def _score_row(label: str, value: str) -> str:
    return f'<div class="score"><span>{label}</span><strong>{value}</strong></div>'


# ---------------------------------------------------------------------------
# 1. 감지 성능 — 주의+/경고+ raw·지평 내 병기 + 블라인드 1·2차 요약(메커니즘 문구 필수)
# ---------------------------------------------------------------------------


def _render_detection_section(reports: dict) -> None:
    detection = reports["detection_metrics"]
    st.markdown(
        '<div class="panel"><div class="panel-title">감지 성능</div>', unsafe_allow_html=True
    )

    if _is_available(detection):
        data = detection["data"]
        meta = data.get("meta") or {}
        results = data.get("results") or {}
        within_horizon = results.get("within_horizon") or {}
        watch_horizon = within_horizon.get("threshold_watch") or {}
        warning = results.get("threshold_warning") or {}
        warning_horizon = within_horizon.get("threshold_warning") or {}
        calibration = data.get("calibration") or {}
        lead_days = results.get("lead_days") or {}

        params_hash = meta.get("config_hash", "n/a")
        st.markdown(
            f'<div class="panel-sub">동결 파라미터 {params_hash} · 측정 조건: 공고 추출 미반영</div>',
            unsafe_allow_html=True,
        )
        rows = [
            _score_row(
                "감지율(주의+, raw · 지평 내)",
                f"{_pct(results.get('detection_rate'))} · {_pct(watch_horizon.get('detection_rate'))}",
            ),
            _score_row("오탐률(주의+)", _pct(results.get("false_positive_rate"))),
            _score_row("선행 중앙값(주의+)", _raw(lead_days.get("median"), "일")),
            _score_row(
                "감지율(경고+, raw · 지평 내)",
                f"{_pct(warning.get('detection_rate'))} · {_pct(warning_horizon.get('detection_rate'))}",
            ),
            _score_row("오탐률(경고+)", _pct(warning.get("false_positive_rate"))),
        ]
        st.markdown("".join(rows), unsafe_allow_html=True)
        if calibration.get("adopted"):
            st.markdown(
                f'<div class="tiny">캘리브레이션 채택 후보: {calibration["adopted"]}</div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown(f'<div class="panel-sub">{_PENDING_BADGE}</div>', unsafe_allow_html=True)

    _render_blind_summary(reports)
    st.markdown("</div>", unsafe_allow_html=True)


def _render_blind_summary(reports: dict) -> None:
    """블라인드 1·2차 요약 — S-31 선행 맥락 지시: 2차(감지 100%·오탐 44.67%
    like-for-like)와 1차를 함께 노출하되, "탐지기 동일·라벨 배치 수정" 메커니즘
    문구를 반드시 붙인다(수치만 단독 노출 금지). 두 리포트는 detection_metrics.json과
    별개 파일이라 가용성을 독립적으로 판정한다.

    메커니즘 문구는 **2차 수치가 노출되는 모든 경로에서 무조건 병기**한다(리뷰 F2 —
    1차가 부재라도 2차만으로 이미 "감지율이 왜 이렇게 높은가"라는 오독 위험이 있으므로,
    1차 유무와 무관하게 2차가 있으면 항상 붙인다)."""
    blind1 = reports["blind_summary"]
    blind2 = reports["blind_round2_summary"]
    round1_available = _is_available(blind1)
    round2_available = _is_available(blind2)
    if not round1_available and not round2_available:
        return

    lines: list[str] = []
    if round1_available:
        agg = blind1["data"].get("aggregate") or {}
        detection = (agg.get("detection_rate") or {}).get("mean")
        false_positive = (agg.get("false_positive_rate") or {}).get("mean")
        lines.append(
            f"블라인드 1차: 감지 {_pct(detection)} · 오탐 {_pct(false_positive)}"
            "(라벨 표본이 1·2차 간 상이 — 참고용)"
        )
    else:
        lines.append(f"블라인드 1차: {_PENDING_BADGE}")

    if round2_available:
        agg = blind2["data"].get("aggregate") or {}
        detection = (agg.get("detection_rate") or {}).get("mean")
        false_positive = (agg.get("false_positive_rate") or {}).get("mean")
        lines.append(
            f"블라인드 2차: 감지 {_pct(detection)} · 오탐 {_pct(false_positive, 2)}"
            "(like-for-like — 표준 스냅샷과 동일 조건)"
        )
    else:
        lines.append(f"블라인드 2차: {_PENDING_BADGE}")

    if round2_available:
        lines.append(
            "탐지기 동일(코드·파라미터 무변경) · 라벨 배치 수정(시나리오를 측정 창에"
            " 결합)으로 1차에서 채점 불가였던 라벨이 사라진 결과다 — 감지율 상승을"
            " 탐지 성능 개선으로 해석하지 않는다."
        )

    st.markdown(f'<br><div class="tiny">{"<br>".join(lines)}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 2. 수요예측 — SES/SMA MAPE·개선율(음수 포함 그대로)·승률
# ---------------------------------------------------------------------------


def _render_forecast_section(reports: dict) -> None:
    forecast = reports["forecast_mape"]
    st.markdown(
        '<div class="panel"><div class="panel-title">수요예측</div>'
        '<div class="panel-sub">SES vs SMA 베이스라인 백테스트(MAPE, 낮을수록 정확)</div>',
        unsafe_allow_html=True,
    )
    if _is_available(forecast):
        overall = forecast["data"].get("overall") or {}
        rows = [
            _score_row("SES MAPE(평균)", _raw(overall.get("ses_mape_mean"))),
            _score_row("SMA MAPE(평균)", _raw(overall.get("sma_mape_mean"))),
            _score_row("베이스라인 대비 개선율", _pct(overall.get("baseline_improved"), 2)),
            _score_row("SES 승률", _pct(overall.get("ses_win_rate"))),
        ]
        st.markdown("".join(rows), unsafe_allow_html=True)
    else:
        st.markdown(_score_row("수요예측 MAPE", _PENDING_BADGE), unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 3. 플랫폼 검증 — E2E {passed}/10, p95 최대 대상·값, 재현성 3계열
# ---------------------------------------------------------------------------


def _render_platform_section(reports: dict) -> None:
    e2e = reports["e2e_results"]
    perf = reports["perf_results"]
    repro = reports["reproducibility"]

    st.markdown(
        '<div class="panel"><div class="panel-title">플랫폼 검증</div>'
        '<div class="panel-sub">E2E·성능·재현성 하니스 실측</div>',
        unsafe_allow_html=True,
    )

    if _is_available(e2e):
        data = e2e["data"]
        verdict = data.get("verdict")
        verdict_text = "n/a" if verdict is None else str(verdict).lower()
        st.markdown(
            _score_row(
                "E2E",
                f"{_raw(data.get('passed_runs'))}/{_raw(data.get('runs'))}(verdict={verdict_text})",
            ),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(_score_row("E2E", _PENDING_BADGE), unsafe_allow_html=True)

    if _is_available(perf):
        targets = perf["data"].get("targets") or {}
        if targets:
            max_name, max_stats = max(
                targets.items(), key=lambda kv: (kv[1] or {}).get("p95_ms", 0) or 0
            )
            st.markdown(
                _score_row("p95 최대 대상", f"{max_name} · {_raw(max_stats.get('p95_ms'), 'ms')}"),
                unsafe_allow_html=True,
            )
        else:
            st.markdown(_score_row("p95 최대 대상", "n/a"), unsafe_allow_html=True)
    else:
        st.markdown(_score_row("p95 최대 대상", _PENDING_BADGE), unsafe_allow_html=True)

    repro_available = _is_available(repro)
    repro_data = repro["data"] if repro_available else {}
    for label, key in (("생성 재현", "generation"), ("배치 재현", "batch"), ("측정 재현", "detection")):
        series = repro_data.get(key) if repro_available else None
        if isinstance(series, dict) and "identical" in series:
            value = "일치" if series["identical"] else "불일치"
        else:
            value = _PENDING_BADGE
        st.markdown(_score_row(label, value), unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 4. LLM 평가 — 추출 정확도(실측 전 허용)·judge 요약(실측 전 허용)·judge 구성(config)
# ---------------------------------------------------------------------------


def _render_llm_section(reports: dict) -> None:
    extraction = reports["extraction_accuracy"]
    eval_config = reports["eval_config"]
    eval_latest = reports["eval_latest_result"]

    st.markdown(
        '<div class="panel"><div class="panel-title">LLM 평가</div>'
        '<div class="panel-sub">공고 추출 정확도 · judge 평가</div>',
        unsafe_allow_html=True,
    )

    if _is_available(extraction):
        macro = extraction["data"].get("macro_accuracy")
        st.markdown(
            _score_row("추출 정확도(매크로)", _pct(macro) if macro is not None else "n/a"),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(_score_row("추출 정확도(매크로)", _PENDING_BADGE), unsafe_allow_html=True)

    if _is_available(eval_latest):
        st.markdown(_score_row("judge 요약", "실험 결과 파일 확인됨"), unsafe_allow_html=True)
    else:
        st.markdown(_score_row("judge 요약", _PENDING_BADGE), unsafe_allow_html=True)

    if _is_available(eval_config):
        config_data = eval_config["data"] or {}
        rubric_version = config_data.get("rubric_version", "n/a")
        mapping = config_data.get("judge_by_generation_provider") or {}
        mapping_text = " · ".join(
            f"생성 {generation_provider}→judge {info.get('provider')}({info.get('model')})"
            for generation_provider, info in mapping.items()
        )
        st.markdown(
            f'<br><div class="tiny">judge 구성: 교차 매핑 — {mapping_text} · rubric {rubric_version}</div>',
            unsafe_allow_html=True,
        )

    st.markdown("</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def render() -> None:
    header(
        "AI 평가",
        "공고 추출·수요예측·플랫폼 검증·LLM judge의 실측 리포트를 그대로 보여줍니다.",
        "AI 평가",
    )
    reports = evaluation_service.load_eval_reports(evaluation_service.current_report_mtimes())

    row1_left, row1_right = st.columns(2)
    with row1_left:
        _render_detection_section(reports)
    with row1_right:
        _render_forecast_section(reports)

    row2_left, row2_right = st.columns(2)
    with row2_left:
        _render_platform_section(reports)
    with row2_right:
        _render_llm_section(reports)
