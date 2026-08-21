"""CLI 진입점 — 성능 측정 하니스(핵심 조회 p95, Task M-29).

표준 스냅샷(--db)의 핵심 데이터 경로 5종을 각 --repeats(기본 30)회 반복 실측한다.
읽기 전용이다 — 이 스크립트의 어떤 코드 경로도 --db에 쓰기 연결을 열지 않는다(직접
여는 커넥션은 SQLite URI ``mode=ro``로 강제하고, 호출하는 서비스 함수들도 전부
SELECT뿐인 조회 경로다).

측정 대상 5종(task-M29-brief.md):
1. list_items       — medsupply.data.queries.list_items(전 품목, 최신 run 조인).
2. load_overview     — medsupply.services.inventory.load_overview의 캐시 미적용
   원 로직. ``@st.cache_data``가 감싼 결과를 반복 호출해서는 2회차부터 캐시 히트만
   재는 셈이 되므로, 데코레이터가 원본 함수에 보존하는 ``__wrapped__``를 직접
   호출해 매 반복 실제 조회가 수행되게 한다(streamlit.testing가 아니라
   streamlit.runtime.caching.cache_utils.CachedFunc의 표준 동작 — functools.wraps
   관례와 동일하게 원본 함수 참조를 attribute로 노출한다). 서비스 코드 자체는
   전혀 수정하지 않는다(브리프가 허용한 ``_load_overview_uncached`` 추출은
   ``__wrapped__``로 동일 효과를 얻을 수 있어 채택하지 않았다 — 동작 불변을 가장
   보수적으로 만족하는 선택).
3. load_item_detail  — medsupply.services.workbench.load_item_detail의 캐시 미적용
   원 로직(위와 동일하게 ``__wrapped__`` 사용). 대상 품목은 score 최상위(브리프 §2).
4. assess_snapshot   — medsupply.analytics.pipeline.assess_snapshot(전 품목 1회
   산정). 이 함수는 원래 DB 쓰기를 전혀 하지 않는다(run_id 생성·저장은
   scripts/run_risk_batch.py의 몫) — 그래서 "writer 호출 제외"를 위해 별도로 뺄
   코드가 없다. as_of는 meta.base_date, params는 config/analytics_params.toml
   기본값(scripts/run_risk_batch.py와 동일 관례)이다.
5. notice_detail_sweep — medsupply.data.queries.get_notice_detail을 DB의 전체
   공고 건수만큼 순회(측정 대상 1회 = 전 공고 1바퀴). 원래 브리프 초안의 "1일
   스윕 상당"은 4번(assess_snapshot)과 사실상 같은 측정이라 공고 상세 순회로
   대체됐다(브리프 §측정 대상 5).

사용법:
    python scripts/measure_perf.py --db data/medsupply.db --repeats 30 \
        --out reports/platform/perf_results.json

판정: 5개 대상 전부 p95_ms <= P95_THRESHOLD_MS(2000, 마스터 플랜 고정값 "핵심 조회
p95 ≤ 2초") — 조정하지 않는다. 미달이어도 결과는 사실 그대로 기록하고, stdout
요약 후 종료 코드 1을 반환한다.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import sqlite3
import statistics
import sys
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

# 리포 루트를 sys.path에 올려 `medsupply`를 절대 경로 실행에서도 import할 수 있게 한다
# (scripts/run_risk_batch.py와 동일 처리).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medsupply import settings  # noqa: E402
from medsupply.analytics.params import load_params  # noqa: E402
from medsupply.analytics.pipeline import assess_snapshot  # noqa: E402
from medsupply.data import queries  # noqa: E402
from medsupply.services import inventory, workbench  # noqa: E402

#: 판정 기준(마스터 플랜 고정값, task-M29-brief.md "핵심 조회 p95 ≤ 2초") — 조정 금지.
P95_THRESHOLD_MS = 2000.0

#: 측정 대상 실행 순서(브리프 §측정 대상 1~5 순서 그대로) — 결과 JSON의 targets 키.
TARGET_ORDER = (
    "list_items", "load_overview", "load_item_detail", "assess_snapshot",
    "notice_detail_sweep",
)


# ---------------------------------------------------------------------------
# 통계 헬퍼 — 순수 함수(DB·시각 의존 없음)
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    """선형 보간 백분위수(numpy.percentile 기본 방식과 동일). values는 최소 1개.

    repeats=1로 호출돼도(하니스 자체 검증의 소형 반복 등) 안전하게 그 값을 그대로
    반환한다.
    """
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (pct / 100) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    frac = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * frac


def _time_repeats(func: Callable[[], object], repeats: int) -> dict[str, float]:
    """1회 워밍(통계 제외) + repeats회 반복 측정 → {mean_ms, p50_ms, p95_ms, max_ms}
    (perf_counter 기반, 소수 1자리).

    GC 개입을 최소화하기 위해 반복 구간에서만 gc.disable()한다 — 예외 발생 여부와
    무관하게(finally) 원래 활성화 상태로 복원해, 이 대상의 측정이 이후 대상·
    프로세스 나머지 실행에 부작용을 남기지 않는다.
    """
    func()  # 워밍 1회 — 콜드 스타트 비용을 통계에서 배제한다.

    durations_ms: list[float] = []
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(repeats):
            start = time.perf_counter()
            func()
            durations_ms.append((time.perf_counter() - start) * 1000)
    finally:
        if gc_was_enabled:
            gc.enable()

    return {
        "mean_ms": round(statistics.mean(durations_ms), 1),
        "p50_ms": round(_percentile(durations_ms, 50), 1),
        "p95_ms": round(_percentile(durations_ms, 95), 1),
        "max_ms": round(max(durations_ms), 1),
    }


def compute_verdict(targets: dict[str, dict[str, float]]) -> bool:
    """판정 — 전 대상 p95_ms <= P95_THRESHOLD_MS(2000, 마스터 플랜 고정값).

    대상이 하나도 없으면(빈 dict) 공허하게 True가 되지 않도록 방어한다 — 실제
    호출부(run_measurement)는 항상 TARGET_ORDER의 5개를 채우므로 정상 경로에서는
    발생하지 않지만, 이 함수 자체는 순수 함수로 독립 테스트되므로 방어적으로 둔다.
    """
    if not targets:
        return False
    return all(stats["p95_ms"] <= P95_THRESHOLD_MS for stats in targets.values())


# ---------------------------------------------------------------------------
# 측정 대상 결선 — DB I/O
# ---------------------------------------------------------------------------


def _top_item_id() -> str:
    """score 최상위 품목 id(review.py의 selectbox 기본 index 0과 동일 선택)."""
    overview = inventory.load_overview.__wrapped__()
    if overview.empty:
        raise AssertionError("DB에 품목이 없다 — load_item_detail 대상을 정할 수 없다")
    return overview.iloc[0]["item_id"]


def run_measurement(db_path: Path, repeats: int) -> dict:
    """5개 대상을 각 repeats회 실측해 결과 dict(브리프 출력 스키마)를 만든다.

    settings.DB_PATH를 db_path로 바꾸고(inventory.get_conn()·workbench 계열이
    참조하는 경로 — CLI 단독 프로세스 실행을 전제해 monkeypatch가 아니라 직접
    대입한다, scripts/run_e2e.py와 동일 관례) 두 st.cache_*를 초기화해 이전
    프로세스 상태의 잔여 캐시가 섞이지 않게 한다. 직접 여는 sqlite3 커넥션은
    URI mode=ro로 열어 이 스크립트가 원본 DB에 쓰기를 시도하면 즉시 예외로
    실패하게 만든다(방어적 이중 보장 — 애초에 아래 호출 전부가 SELECT뿐이다).
    """
    import streamlit as st

    settings.DB_PATH = db_path
    st.cache_data.clear()
    st.cache_resource.clear()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = queries.get_meta(conn)
        as_of = date.fromisoformat(meta["base_date"])
        params = load_params()
        notice_ids = queries.get_notices(conn)["notice_id"].tolist()
        top_item_id = _top_item_id()

        targets = {
            "list_items": _time_repeats(lambda: queries.list_items(conn), repeats),
            "load_overview": _time_repeats(
                lambda: inventory.load_overview.__wrapped__(), repeats
            ),
            "load_item_detail": _time_repeats(
                lambda: workbench.load_item_detail.__wrapped__(top_item_id), repeats
            ),
            "assess_snapshot": _time_repeats(
                lambda: assess_snapshot(conn, as_of, params), repeats
            ),
            "notice_detail_sweep": _time_repeats(
                lambda: [queries.get_notice_detail(conn, nid) for nid in notice_ids],
                repeats,
            ),
        }
    finally:
        conn.close()

    # inventory.get_conn()의 st.cache_resource 커넥션(load_overview/load_item_detail이
    # __wrapped__ 경유로도 여전히 참조한다)을 여기서 비워, 이 프로세스가 측정이 끝난
    # 뒤에도 db_path에 대한 열린 커넥션을 계속 들고 있지 않게 한다. 다만 WAL 모드
    # DB는 마지막 연결이 읽기만 했어도 리더 프로토콜상 -wal/-shm 사이드카를 만들 수
    # 있고(체크포인트는 쓰기 연결이 있어야 일어난다), 이 스크립트는 쓰기 연결을 절대
    # 열지 않으므로 사이드카를 능동적으로 지우지 않는다 — 사이드카가 남아 있어도 그
    # 안에 반영할 변경 내용 자체가 없으므로(어떤 INSERT/UPDATE/DELETE도 실행하지
    # 않았다) 원본 DB의 실제 내용은 100% 그대로다(테스트가 바이트 해시로 직접
    # 확인한다).
    st.cache_data.clear()
    st.cache_resource.clear()

    return {
        "measured_at": _now_iso(),
        "repeats": repeats,
        "db": str(db_path),
        "targets": {name: targets[name] for name in TARGET_ORDER},
        "verdict": compute_verdict(targets),
        "environment": _environment_info(),
    }


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _environment_info() -> dict:
    return {"python_version": platform.python_version(), "platform": platform.platform()}


# ---------------------------------------------------------------------------
# stdout 요약 + 파일 I/O + CLI
# ---------------------------------------------------------------------------


def _human_summary(results: dict) -> str:
    lines = [f"측정 반복: {results['repeats']}회", f"DB: {results['db']}"]
    for name, stats in results["targets"].items():
        mark = "OK" if stats["p95_ms"] <= P95_THRESHOLD_MS else "FAIL"
        lines.append(
            f"  {name}: mean={stats['mean_ms']}ms p50={stats['p50_ms']}ms"
            f" p95={stats['p95_ms']}ms max={stats['max_ms']}ms [{mark}]"
        )
    lines.append(
        f"판정: {'PASS' if results['verdict'] else 'FAIL'}"
        f" (기준 p95 <= {P95_THRESHOLD_MS:.0f}ms, 전 대상)"
    )
    env = results["environment"]
    lines.append(f"환경: python={env['python_version']} · {env['platform']}")
    return "\n".join(lines)


def _write_json(path: str | Path, data: dict) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MedSupply Radar 성능 측정 하니스(핵심 조회 p95)"
    )
    parser.add_argument("--db", required=True, help="측정 대상 DB 경로(읽기 전용 — 쓰기 없음)")
    parser.add_argument("--repeats", type=int, default=30, help="대상별 반복 횟수(기본 30)")
    parser.add_argument("--out", required=True, help="결과 JSON 출력 경로")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB를 찾을 수 없다: {db_path}", file=sys.stderr)
        return 1

    results = run_measurement(db_path, args.repeats)
    _write_json(args.out, results)
    print(_human_summary(results))

    return 0 if results["verdict"] else 1


if __name__ == "__main__":
    sys.exit(main())
