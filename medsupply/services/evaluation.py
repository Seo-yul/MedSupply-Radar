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
import sqlite3
from pathlib import Path

import streamlit as st
import yaml

from medsupply import settings

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

#: LLM 호출 캐시 DB(medsupply/llm/cache.py가 스키마·기본 경로의 원천). settings.LLM_CACHE_PATH를
#: 그대로 재노출하는 모듈 상수다 — REPORT_PATHS 등과 동일하게 여기서 이름을 가져야 테스트가
#: (settings가 아니라) 이 모듈 속성을 monkeypatch해 격리할 수 있다(Task X-1).
LLM_CACHE_PATH: Path = settings.LLM_CACHE_PATH


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
    """REPORT_PATHS 전부 + eval/config.yaml + eval/results/ 최신 파일 + LLM_CACHE_PATH의
    mtime을 모은 튜플 — load_eval_reports·load_llm_usage 호출부(뷰)가 매번 새로 계산해
    인자로 넘기는 캐시 무효화 신호다(services.history.load_history의 data_version
    인자와 동일한 역할). 이 함수 자체는 캐시되지 않는다 — 매 호출마다 디스크를 다시
    stat해야 신호로서 의미가 있다.

    파일이 없으면 그 자리는 None(부재 자체도 신호가 되도록 — 파일이 새로 생기면
    None -> 실수로 바뀌어 캐시 키가 달라지고 캐시가 자동으로 무효화된다). LLM_CACHE_PATH도
    같은 규칙(Task X-1) — 캐시 DB가 새로 생기거나 갱신되면 이 튜플이 바뀌어
    load_llm_usage의 @st.cache_data가 무효화된다.
    """
    latest_result = _latest_eval_result_path()
    return (
        tuple(_mtime_or_none(path) for path in REPORT_PATHS.values()),
        _mtime_or_none(EVAL_CONFIG_PATH),
        str(latest_result) if latest_result is not None else None,
        _mtime_or_none(latest_result),
        _mtime_or_none(LLM_CACHE_PATH),
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


# ---------------------------------------------------------------------------
# LLM 사용량·추정 비용(Task X-1) — settings.LLM_CACHE_PATH(llm_cache 테이블) 집계.
# 이 테이블의 행 1개 = 실제 과금 호출 1건(캐시 적중은 새 행을 만들지 않는다 — client.py의
# cache_get 히트 경로는 cache_put을 호출하지 않는다). 현재 이 DB는 존재하지 않는다(키
# 미설정) — 부재가 기본 경로이므로 load_llm_usage는 파일이 없으면 아무것도 만들지 않고
# {"available": False}만 반환한다(init_cache를 호출하지 않는다 — 호출하면 조회만으로
# 빈 DB 파일이 생겨버려 "부재가 기본"이라는 브리프 원칙이 깨진다).
# ---------------------------------------------------------------------------

#: (input_usd, output_usd) per 1M tokens — 2026-08 공표가 기준, 변동 가능(브리프 §단가표).
#: 화면에는 이 기준 시점을 tiny 캡션으로 명시한다(views/evaluation.py).
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "gpt-5": (1.25, 10.0),
}


def _price_for_model(model: str) -> tuple[float, float] | None:
    """model_used에 대한 (input_usd, output_usd) 단가. 정확히 일치하지 않으면 스냅샷 ID
    변형(예: "claude-opus-5-2026-01-15" -> "claude-opus-5")을 대비해 _PRICE_PER_MTOK
    키 접두 일치로 폴백한다. 그래도 없으면 None — 단가를 추측하지 않는다(브리프)."""
    if model in _PRICE_PER_MTOK:
        return _PRICE_PER_MTOK[model]
    for known_model, price in _PRICE_PER_MTOK.items():
        if model.startswith(known_model):
            return price
    return None


def _estimate_cost_usd(model: str, in_tokens: int, out_tokens: int) -> float | None:
    price = _price_for_model(model)
    if price is None:
        return None
    input_usd_per_mtok, output_usd_per_mtok = price
    return (in_tokens / 1_000_000) * input_usd_per_mtok + (out_tokens / 1_000_000) * output_usd_per_mtok


@st.cache_data
def load_llm_usage(mtimes: tuple = ()) -> dict:
    """LLM_CACHE_PATH(llm_cache 테이블)를 task×model_used로 집계한다.

    반환:
    - 파일 부재: {"available": False}(브리프 — 키 미설정 시 기본 경로).
    - 연결·조회 실패(손상 DB): {"available": False, "error": str(exc)} — 예외를 전파하지
      않는다(load_eval_reports의 손상 파일 격리와 동일한 이 모듈의 불변식).
    - 정상: {"available": True, "rows": [{"task", "model", "calls", "in_tokens",
      "out_tokens", "est_cost_usd"|None}, ...](task×model 오름차순), "totals": {같은 4개
      키의 합 — est_cost_usd는 단가를 아는 행이 하나도 없으면 None}, "generated_basis":
      "누적 호출 N건"}.

    mtimes는 다른 로더와 동일하게 캐시 무효화 신호 전용(current_report_mtimes()가
    LLM_CACHE_PATH의 mtime을 포함해 계산한 값을 호출부가 넘긴다) — 조회 내용에는 쓰지
    않는다. 파일이 없을 때 init_cache를 호출하지 않는다 — 조회만으로 빈 캐시 DB가
    생기면 "부재가 기본"이라는 브리프 원칙이 깨진다.
    """
    del mtimes  # 캐시 키 무효화 전용 — 조회 조건에는 쓰지 않는다.

    if not LLM_CACHE_PATH.exists():
        return {"available": False}

    try:
        conn = sqlite3.connect(LLM_CACHE_PATH)
        try:
            db_rows = conn.execute("SELECT task, model_used, usage_json FROM llm_cache").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"available": False, "error": str(exc)}

    groups: dict[tuple[str, str], dict] = {}
    for task, model_used, usage_json in db_rows:
        try:
            usage = json.loads(usage_json)
        except ValueError:
            usage = {}
        in_tokens = int(usage.get("input_tokens") or 0)
        out_tokens = int(usage.get("output_tokens") or 0)
        group = groups.setdefault(
            (task, model_used),
            {"task": task, "model": model_used, "calls": 0, "in_tokens": 0, "out_tokens": 0},
        )
        group["calls"] += 1
        group["in_tokens"] += in_tokens
        group["out_tokens"] += out_tokens

    rows = []
    total_calls = total_in_tokens = total_out_tokens = 0
    total_cost = 0.0
    cost_known = False
    for key in sorted(groups):
        group = groups[key]
        cost = _estimate_cost_usd(group["model"], group["in_tokens"], group["out_tokens"])
        rows.append({**group, "est_cost_usd": cost})
        total_calls += group["calls"]
        total_in_tokens += group["in_tokens"]
        total_out_tokens += group["out_tokens"]
        if cost is not None:
            cost_known = True
            total_cost += cost

    totals = {
        "calls": total_calls,
        "in_tokens": total_in_tokens,
        "out_tokens": total_out_tokens,
        "est_cost_usd": total_cost if cost_known else None,
    }

    return {
        "available": True,
        "rows": rows,
        "totals": totals,
        "generated_basis": f"누적 호출 {total_calls}건",
    }
