"""AI 평가 리포트 로딩 서비스 — views/evaluation.py가 소비하는 실측 리포트 파일 계층
(Task S-31).

다른 services/*.py(inventory·history·orders 등)와 달리 **DB가 아니라 저장소 내 파일**
(JSON 리포트 + eval/config.yaml + eval/results/)을 읽는다. 그래서 캐시 무효화 신호도
data_version(DB 쓰기 카운터, writer.py가 증가시킴)이 아니라 **각 파일의 mtime 튜플**을
쓴다(task-S31-brief.md §산출물의 "주의" — data_version 대신 mtime 튜플, 구현 재량).
data_version은 DB 쓰기와만 연동되므로 리포트 파일이 갱신돼도(예: 측정 CLI 재실행) 절대
바뀌지 않아 신호로 쓸 수 없다 — mtime은 파일이 바뀌는 순간 그 자체로 바뀌므로 정확한
무효화 신호가 된다.

history.load_history의 data_version 관례와 동일한 형태를 따른다: `current_report_mtimes()`
(캐시되지 않는 일반 함수)가 매 호출마다 신선한 mtime 튜플을 계산하고, 호출부(뷰)가 그
값을 `load_eval_reports(mtimes=...)`에 인자로 넘긴다 — `@st.cache_data`는 인자 해시로만
캐시를 무효화하므로, 이 인자 전달 없이는 파일이 바뀌어도 캐시가 갱신되지 않는다.

리포트 파일이 없으면 그 자리는 `{"exists": False}`, 있지만 JSON 파싱이 깨지면
`{"exists": True, "error": "..."}`로 **격리**한다 — 어느 경우도 예외를 전파하지 않는다
(한 리포트의 부재·손상이 나머지 리포트 렌더를 막지 않는다, 브리프 §산출물).
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: 브리프 §데이터 소스가 지정한 리포트 7종(+ S-31 선행 맥락이 추가한 블라인드 1·2차
#: 요약 2종) — 키는 load_eval_reports 반환 dict·뷰가 조회에 쓰는 논리 이름.
REPORT_PATHS: dict[str, Path] = {
    "detection_metrics": REPO_ROOT / "reports" / "analytics" / "detection_metrics.json",
    "blind_summary": REPO_ROOT / "reports" / "analytics" / "blind_summary.json",
    "blind_round2_summary": REPO_ROOT / "reports" / "analytics" / "blind_round2_summary.json",
    "forecast_mape": REPO_ROOT / "reports" / "analytics" / "forecast_mape.json",
    "e2e_results": REPO_ROOT / "reports" / "platform" / "e2e_results.json",
    "perf_results": REPO_ROOT / "reports" / "platform" / "perf_results.json",
    "reproducibility": REPO_ROOT / "reports" / "platform" / "reproducibility.json",
    "extraction_accuracy": REPO_ROOT / "reports" / "llm" / "extraction_accuracy.json",
}

#: eval/results/ 최신 실험 요약은 "디렉터리 안의 최신 파일 1개"라 REPORT_PATHS(고정
#: 경로 1:1)와 처리가 달라 별도 상수로 분리한다.
EVAL_RESULTS_DIR: Path = REPO_ROOT / "eval" / "results"
EVAL_CONFIG_PATH: Path = REPO_ROOT / "eval" / "config.yaml"


def _mtime_or_none(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _load_json_file(path: Path) -> dict:
    """{"exists": False} | {"exists": True, "data": dict} | {"exists": True, "error": str}.
    OSError(권한 등)·JSONDecodeError 어느 쪽도 예외로 전파하지 않는다."""
    if not path.exists():
        return {"exists": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"exists": True, "error": str(exc)}
    return {"exists": True, "data": data}


def _latest_eval_result_path() -> Path | None:
    """eval/results/ 아래 *.json 중 파일명 사전순으로 가장 마지막(파일명이 실험
    타임스탬프를 포함하는 명명 규칙을 전제 — eval/build_cases.py·eval/judge.py의
    기존 산출물 명명 관례와 동일). 디렉터리가 없거나 비어 있으면 None."""
    if not EVAL_RESULTS_DIR.exists():
        return None
    candidates = sorted(EVAL_RESULTS_DIR.glob("*.json"))
    return candidates[-1] if candidates else None


def _load_eval_config() -> dict:
    """{"exists": False} | {"exists": True, "data": dict} | {"exists": True, "error": str}.
    _load_json_file과 대칭: OSError(디렉터리를 가리키는 등 read_text 자체가 실패하는
    경우 포함)·yaml.YAMLError 어느 쪽도 예외로 전파하지 않는다."""
    if not EVAL_CONFIG_PATH.exists():
        return {"exists": False}
    try:
        data = yaml.safe_load(EVAL_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {"exists": True, "error": str(exc)}
    return {"exists": True, "data": data}


def current_report_mtimes() -> tuple:
    """REPORT_PATHS 전부 + eval/config.yaml + eval/results/ 최신 파일의 mtime을 모은
    튜플 — load_eval_reports 호출부(뷰)가 매번 새로 계산해 인자로 넘기는 캐시 무효화
    신호다(services.history.load_history의 data_version 인자와 동일한 역할). 이 함수
    자체는 캐시되지 않는다 — 매 호출마다 디스크를 다시 stat해야 신호로서 의미가 있다.

    파일이 없으면 그 자리는 None(부재 자체도 신호가 되도록 — 파일이 새로 생기면
    None -> 실수로 바뀌어 캐시 키가 달라지고 캐시가 자동으로 무효화된다).
    """
    latest_result = _latest_eval_result_path()
    return (
        tuple(_mtime_or_none(path) for path in REPORT_PATHS.values()),
        _mtime_or_none(EVAL_CONFIG_PATH),
        str(latest_result) if latest_result is not None else None,
        _mtime_or_none(latest_result),
    )


@st.cache_data
def load_eval_reports(mtimes: tuple = ()) -> dict:
    """REPORT_PATHS 전부 + eval/config.yaml + eval/results/ 최신 실험 요약을 읽어 하나의
    dict로 묶는다. mtimes는 캐시 무효화 신호 전용(호출부가 current_report_mtimes()의
    값을 넘긴다) — 조회 내용 자체에는 쓰이지 않는다(services.history.load_history의
    data_version 인자와 동일한 관례).

    반환: {report_key: {"exists": False} | {"exists": True, "data": ...} |
    {"exists": True, "error": ...}, ...} — 키는 REPORT_PATHS와 동일 + "eval_config" +
    "eval_latest_result". 어떤 파일이 없거나 깨져 있어도 예외를 던지지 않는다.
    """
    del mtimes  # 캐시 키 무효화 전용 — 조회 조건에는 쓰지 않는다.

    reports = {key: _load_json_file(path) for key, path in REPORT_PATHS.items()}
    reports["eval_config"] = _load_eval_config()

    latest_result_path = _latest_eval_result_path()
    reports["eval_latest_result"] = (
        _load_json_file(latest_result_path) if latest_result_path is not None else {"exists": False}
    )
    return reports
