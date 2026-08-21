"""CLI 진입점 — E2E 하니스(핵심 사용자 여정 5단계 × N회, Task M-28).

표준 스냅샷(--db)을 회차마다 tmp 파일로 복사해, 그 사본 위에서만 핵심 사용자 여정
5단계(상황실 → 워크벤치 → 공고 → 조치·발주 → 이력·알림)를 순차 실행한다. 원본
DB(--db)는 복사 소스로만 열릴 뿐, 이 스크립트의 어떤 코드 경로도 그 파일에 쓰기
연결을 열지 않는다(alerts 렌더의 자동 sync_alerts를 포함한 모든 쓰기는 회차별 사본
위에서만 일어난다).

각 단계는 "무예외 + 핵심 표식 단언" 계약이다(task-M28-brief.md):
1. 상황실 — situation 렌더. KPI 메트릭 4개(빈 값 없음) + 위험 품목 리스트 ≥1행.
2. 워크벤치 — review 렌더(품목 selectbox 기본 index 0 = score 최상위 품목이 자동
   선택된다). 라벨 카드에 그 품목명 + 게이지 차트(plotly 'gauge+number') + '대체 후보'
   탭 표식.
3. 공고 — notices 렌더. 목록 표 행 수가 사본 DB의 실제 notices 행 수와 일치(하드코딩
   상수가 아니라 자기일관성 검증 — 실측 시 표준 스냅샷 기준 20건으로 관측된다) +
   상세 원문(text_area) 로드. 추출이 없으면 뜨는 "추출 미실행" 안내 경로도 정상
   통과로 허용한다(브리프: "미추출 안내 경로 허용" — 키 없는 현 환경의 정상 경로).
4. 조치·발주 — AppTest 렌더가 아니라 워크벤치 조치 저장(writer.save_action_history)과
   발주 요청안 계산·저장(orders 서비스 + writer.save_order_request) 함수를 직접
   호출한다(브리프 명시). action_history·order_requests 행 수가 늘었는지로 판정한다.
5. 이력·알림 — history 렌더(방금 4단계에서 저장한 조치 행이 사본 DB 기준으로 노출되는지),
   alerts 렌더(뷰의 자동 sync_alerts가 예외 없이 완료되는지).

사용법:
    python scripts/run_e2e.py --db data/medsupply.db --runs 10 \
        --out reports/platform/e2e_results.json

판정: passed_runs(5단계 모두 통과한 회차 수) >= PASSED_RUNS_THRESHOLD(9) — 마스터
플랜 고정값이라 CLI 인자로 조정하지 않는다(브리프: "판정 기준(≥9/10)은 마스터 플랜
고정값 — 조정 금지", 결과는 미달이어도 사실 그대로 기록한다). --runs를 9 미만으로
주면(하니스 자체 검증용 소형 픽스처 등) passed_runs가 구조적으로 이 상수에 못
미치므로 verdict는 항상 False다 — 이는 버그가 아니라 고정 판정선의 자연스러운
귀결이다. stdout에 사람이 읽는 요약을 출력하고, verdict 미달 시 종료 코드 1을 반환한다.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

# 리포 루트를 sys.path에 올려 `medsupply`를 절대 경로 실행에서도 import할 수 있게 한다
# (scripts/run_risk_batch.py와 동일 처리).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from medsupply import settings  # noqa: E402
from medsupply.data import writer  # noqa: E402
from medsupply.llm.config import load_llm_config  # noqa: E402
from medsupply.services import inventory, workbench  # noqa: E402
from medsupply.services import orders as orders_service  # noqa: E402

#: 판정 기준(마스터 플랜 고정값, task-M28-brief.md) — 조정 금지.
PASSED_RUNS_THRESHOLD = 9


# ---------------------------------------------------------------------------
# AppTest 대상 함수 — from_function이 소스를 추출해 별도 스레드에서 재실행하므로
# 필요한 import는 함수 본문 안에서 해야 한다(tests/platform/test_situation_live.py와
# 동일 관례). settings.DB_PATH는 sys.modules에 캐시된 같은 모듈 객체를 통해 그대로
# 보인다 — 이 스크립트가 직접 대입한 값이 별도 스레드에서도 유효하다.
# ---------------------------------------------------------------------------


def _run_situation() -> None:
    from medsupply import theme
    from medsupply.views import situation

    theme.inject_css()
    situation.render()


def _run_review() -> None:
    from medsupply import theme
    from medsupply.views import review

    theme.inject_css()
    review.render()


def _run_notices() -> None:
    from medsupply import theme
    from medsupply.views import notices

    theme.inject_css()
    notices.render()


def _run_history() -> None:
    from medsupply import theme
    from medsupply.views import history

    theme.inject_css()
    history.render()


def _run_alerts() -> None:
    from medsupply import theme
    from medsupply.views import alerts

    theme.inject_css()
    alerts.render()


# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------


def _activate(db_path: Path) -> None:
    """settings.DB_PATH를 db_path로 바꾸고 캐시를 초기화한다.

    tests/platform/test_*_live.py의 monkeypatch(settings, "DB_PATH", ...) + cache
    clear 관례와 동일한 효과를 낸다 — 이 스크립트는 pytest 밖에서 실행되므로
    monkeypatch 대신 모듈 속성을 직접 대입한다(회차·단계가 끝나도 원복하지 않는다 —
    다음 단계·회차가 항상 자신의 db_path로 다시 _activate를 호출하므로 문제되지 않는다).
    """
    settings.DB_PATH = db_path
    st.cache_data.clear()
    st.cache_resource.clear()


def _require_no_exception(at: AppTest, step: str) -> None:
    if at.exception:
        details = "; ".join(exc.message for exc in at.exception)
        raise AssertionError(f"{step} 렌더 중 예외 발생: {details}")


def _top_item() -> tuple[str, str]:
    """score 내림차순 최상위 품목(item_id, item_name).

    호출 전 _activate(db_path)가 이미 실행되어 있어야 한다(현재 활성 DB 기준으로
    조회한다). load_overview는 score 내림차순 정렬을 보장하므로(services/inventory.py
    계약) iloc[0]가 곧 "score 최상위 품목"이다 — review.py의 selectbox 기본 index 0과
    동일한 선택이다.
    """
    data_version = inventory.current_data_version()
    overview = inventory.load_overview(data_version=data_version)
    if overview.empty:
        raise AssertionError("표준 스냅샷에 품목이 없다")
    row = overview.iloc[0]
    return row["item_id"], row["item_name"]


# ---------------------------------------------------------------------------
# 5단계
# ---------------------------------------------------------------------------


def step_situation(db_path: Path) -> None:
    """1. 상황실 — KPI 메트릭 4개(빈 값 없음) + 위험 품목 리스트 ≥1행."""
    _activate(db_path)
    at = AppTest.from_function(_run_situation)
    at.run()
    _require_no_exception(at, "situation")

    metrics = at.metric
    if len(metrics) != 4:
        raise AssertionError(f"KPI 메트릭이 4개가 아니다(관측 {len(metrics)}개)")
    if any(not m.value for m in metrics):
        raise AssertionError("빈 값의 KPI 메트릭이 있다")

    rendered = "\n".join(md.value for md in at.markdown)
    if rendered.count('class="risk-row"') < 1:
        raise AssertionError("위험 품목 리스트가 1행도 렌더되지 않았다")


def step_workbench(db_path: Path) -> None:
    """2. 워크벤치 — 라벨 카드 품목명(score 최상위) + 게이지 + '대체 후보' 탭 표식."""
    _activate(db_path)
    _item_id, top_name = _top_item()

    at = AppTest.from_function(_run_review)
    at.run()
    _require_no_exception(at, "workbench")

    rendered = "\n".join(md.value for md in at.markdown)
    if top_name not in rendered:
        raise AssertionError(f"라벨 카드에 최상위 품목명이 없다: {top_name!r}")

    plotly_elements = at.get("plotly_chart")
    if len(plotly_elements) != 2 or not any(
        "gauge+number" in el.proto.spec for el in plotly_elements
    ):
        raise AssertionError("게이지 차트(gauge+number)가 렌더되지 않았다")

    tab_labels = [tab.label for tab in at.tabs]
    if "대체 후보" not in tab_labels:
        raise AssertionError(f"'대체 후보' 탭 표식이 없다(관측 탭: {tab_labels})")


def step_notices(db_path: Path) -> None:
    """3. 공고 — 목록 건수가 사본 DB의 실제 공고 수와 일치 + 상세 원문 로드."""
    _activate(db_path)

    conn = sqlite3.connect(db_path)
    try:
        expected_count = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
    finally:
        conn.close()
    if expected_count < 1:
        raise AssertionError("사본 DB에 공고가 없다 — 이 단계를 검증할 수 없다")

    at = AppTest.from_function(_run_notices)
    at.run()
    _require_no_exception(at, "notices")

    dataframes = at.dataframe
    if not dataframes:
        raise AssertionError("공고 목록 표가 렌더되지 않았다")
    list_df = dataframes[0].value
    if len(list_df) != expected_count:
        raise AssertionError(
            f"공고 목록 건수 불일치: 렌더 {len(list_df)}건 != DB {expected_count}건"
        )
    if len(at.text_area) < 1:
        raise AssertionError("공고 상세 원문(text_area)이 로드되지 않았다")


def step_action_order(db_path: Path) -> None:
    """4. 조치·발주 — AppTest가 아니라 함수 직접 호출(브리프 명시).

    워크벤치 조치 저장(writer.save_action_history)과 발주 요청안 계산·저장
    (orders_service.compute_order_proposal + writer.save_order_request +
    연계 이력 writer.save_action_history)을 차례로 호출한다 — review.py·orders.py의
    저장 버튼 콜백과 동일한 함수 시퀀스다. action_history·order_requests 행 수 증가로
    판정한다(브리프: "action_history·order_requests 증가 확인").
    """
    _activate(db_path)
    item_id, _item_name = _top_item()

    conn = sqlite3.connect(db_path)
    try:
        before_actions = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
        before_orders = conn.execute("SELECT COUNT(*) FROM order_requests").fetchone()[0]
        base_date_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'base_date'"
        ).fetchone()
    finally:
        conn.close()
    base_date = date.fromisoformat(base_date_row[0]) if base_date_row else date.today()
    desired_date = (base_date + timedelta(days=7)).isoformat()

    write_conn = workbench.open_write_conn()
    try:
        writer.save_action_history(
            write_conn, item_id, "발주량 조정", "김약사",
            "E2E 하니스 — 워크벤치 조치 기록", status="진행 중", risk_type=None,
        )
    finally:
        write_conn.close()
    st.cache_data.clear()

    proposal = orders_service.compute_order_proposal(
        item_id, data_version=inventory.current_data_version()
    )
    supplier = proposal["suppliers"][0] if proposal["suppliers"] else "-"
    quantity = proposal["suggested_qty"] or 50

    write_conn = workbench.open_write_conn()
    try:
        order_id = writer.save_order_request(
            write_conn, item_id, supplier, quantity, desired_date, "김약사",
            "E2E 하니스 — 발주 요청 사유",
        )
        writer.save_action_history(
            write_conn, item_id, "발주 요청", "김약사",
            note="E2E 하니스 — 발주 요청 연계 이력", status="진행 중",
            order_id=order_id, risk_type=proposal["risk_type"],
        )
    finally:
        write_conn.close()
    st.cache_data.clear()

    conn = sqlite3.connect(db_path)
    try:
        after_actions = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
        after_orders = conn.execute("SELECT COUNT(*) FROM order_requests").fetchone()[0]
    finally:
        conn.close()

    if after_actions <= before_actions:
        raise AssertionError("action_history가 증가하지 않았다")
    if after_orders <= before_orders:
        raise AssertionError("order_requests가 증가하지 않았다")


def step_history_alerts(db_path: Path) -> None:
    """5. 이력·알림 — history 렌더(방금 저장한 행 노출), alerts 렌더(sync 후 무예외)."""
    _activate(db_path)
    _item_id, item_name = _top_item()

    at_history = AppTest.from_function(_run_history)
    at_history.run()
    _require_no_exception(at_history, "history")
    if not at_history.dataframe:
        raise AssertionError("이력 표가 렌더되지 않았다")
    history_df = at_history.dataframe[0].value
    if item_name not in set(history_df["품목"]):
        raise AssertionError(f"방금 저장한 이력 행이 보이지 않는다: {item_name!r}")

    _activate(db_path)
    at_alerts = AppTest.from_function(_run_alerts)
    at_alerts.run()
    _require_no_exception(at_alerts, "alerts")


#: 실행 순서(브리프의 5단계 정의 순서 그대로) — 모듈 전역이라 테스트가 monkeypatch로
#: 개별 단계를 교체해 실패 주입을 검증할 수 있다.
STEP_FUNCS: list[tuple[str, Callable[[Path], None]]] = [
    ("situation", step_situation),
    ("workbench", step_workbench),
    ("notices", step_notices),
    ("action_order", step_action_order),
    ("history_alerts", step_history_alerts),
]


# ---------------------------------------------------------------------------
# 회차 실행 — 표준 DB를 tmp 사본으로 복사한 뒤에만 5단계를 실행한다
# ---------------------------------------------------------------------------


def run_steps(db_path: Path) -> dict[str, dict]:
    """5단계를 순서대로 실행해 {step: {passed, duration_ms, error}}를 만든다.

    한 단계가 실패해도 나머지 단계는 계속 실행한다 — 어느 단계가 깨졌는지 전부
    보여야 진단에 유용하기 때문이다(이후 단계가 그 실패의 자연스러운 부작용으로
    연쇄 실패할 수는 있다 — 예: 4단계 실패 시 5단계의 "방금 저장한 행" 단언도 함께
    실패한다. 이는 하니스 버그가 아니라 여정의 순차 의존을 그대로 반영한 것이다).
    STEP_FUNCS를 모듈 전역으로 매 호출마다 새로 조회한다(monkeypatch로 개별 단계를
    바꿔치기하는 테스트를 지원하기 위해 — 함수 정의 시점에 미리 바인딩하지 않는다).
    """
    steps: dict[str, dict] = {}
    for name, func in STEP_FUNCS:
        start = time.perf_counter()
        try:
            func(db_path)
        except Exception as exc:  # noqa: BLE001 - 하니스는 어떤 단계 실패든 기록해야 한다
            steps[name] = {
                "passed": False,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                "error": str(exc),
            }
        else:
            steps[name] = {
                "passed": True,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                "error": None,
            }
    return steps


def run_once(source_db: Path, run_no: int) -> dict:
    """표준 DB를 tmp 파일로 복사한 뒤 5단계를 실행해 1회차 결과를 만든다.

    tmp 디렉터리는 이 함수 종료 시 자동 삭제된다 — source_db는 복사 소스로만 열리고
    이 함수 안의 어떤 코드 경로도 그 경로에 쓰기 연결을 열지 않는다(원본 DB는 절대
    수정하지 않는다는 브리프 필수 불변식). WAL 사이드카(-wal/-shm)가 남아 있으면
    같이 복사한다(SQLite WAL 모드는 체크포인트되지 않은 최신 쓰기가 사이드카에만
    있을 수 있다 — 메인 파일만 복사하면 그 최신 내용이 누락될 수 있어 사이드카도
    존재하는 것만 함께 복사한다).
    """
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="medsupply-e2e-") as tmp_dir:
        tmp_db = Path(tmp_dir) / f"run_{run_no}.db"
        shutil.copy2(source_db, tmp_db)
        for suffix in ("-wal", "-shm"):
            sidecar = source_db.with_name(source_db.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, tmp_db.with_name(tmp_db.name + suffix))
        steps = run_steps(tmp_db)
    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    all_passed = all(step["passed"] for step in steps.values())
    return {
        "run": run_no,
        "steps": steps,
        "all_passed": all_passed,
        "duration_ms": duration_ms,
    }


def compute_verdict(passed_runs: int) -> bool:
    """판정 — passed_runs >= PASSED_RUNS_THRESHOLD(9, 마스터 플랜 고정값).

    runs 인자(총 회차 수)와 무관하게 항상 이 고정 상수와 비교한다(브리프: "판정
    기준(≥9/10)은 마스터 플랜 고정값 — 조정 금지"). 그래서 --runs를 9 미만으로 주면
    (하니스 자체 검증용 소형 픽스처 등) 구조적으로 항상 False가 나온다 — 버그가
    아니라 고정 판정선의 자연스러운 귀결이다.
    """
    return passed_runs >= PASSED_RUNS_THRESHOLD


def _environment_info() -> dict:
    llm_cfg = load_llm_config()
    return {
        "llm_keys": bool(llm_cfg.anthropic_key_set or llm_cfg.openai_key_set),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }


def run_harness(source_db: Path, runs: int) -> dict:
    """runs회 반복해 결과 dict(브리프 §하니스의 출력 JSON 스키마 그대로)를 만든다."""
    per_run = [run_once(source_db, run_no) for run_no in range(1, runs + 1)]
    passed_runs = sum(1 for run in per_run if run["all_passed"])
    return {
        "runs": runs,
        "per_run": per_run,
        "passed_runs": passed_runs,
        "verdict": compute_verdict(passed_runs),
        "environment": _environment_info(),
    }


# ---------------------------------------------------------------------------
# stdout 요약 + 파일 I/O + CLI
# ---------------------------------------------------------------------------


def _human_summary(results: dict) -> str:
    lines = [
        f"실행 회차: {results['runs']}",
        f"통과 회차: {results['passed_runs']}/{results['runs']}"
        f" (판정 기준 passed_runs >= {PASSED_RUNS_THRESHOLD})",
        f"판정: {'PASS' if results['verdict'] else 'FAIL'}",
    ]
    for run in results["per_run"]:
        status = "OK" if run["all_passed"] else "FAIL"
        failed_steps = [name for name, step in run["steps"].items() if not step["passed"]]
        detail = f" — 실패 단계: {', '.join(failed_steps)}" if failed_steps else ""
        lines.append(f"  회차 {run['run']}: {status} ({run['duration_ms']}ms){detail}")

    env = results["environment"]
    lines.append(
        f"환경: llm_keys={env['llm_keys']} · python={env['python_version']} · {env['platform']}"
    )
    return "\n".join(lines)


def _write_json(path: str | Path, data: dict) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MedSupply Radar E2E 하니스(핵심 사용자 여정 5단계 × N회)"
    )
    parser.add_argument(
        "--db", required=True, help="표준 스냅샷 DB 경로(복사 소스 — 절대 직접 열어 쓰지 않는다)"
    )
    parser.add_argument("--runs", type=int, default=10, help="반복 회차 수(기본 10)")
    parser.add_argument("--out", required=True, help="결과 JSON 출력 경로")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    source_db = Path(args.db)
    if not source_db.exists():
        print(f"DB를 찾을 수 없다: {source_db}", file=sys.stderr)
        return 1

    results = run_harness(source_db, args.runs)
    _write_json(args.out, results)
    print(_human_summary(results))

    return 0 if results["verdict"] else 1


if __name__ == "__main__":
    sys.exit(main())
