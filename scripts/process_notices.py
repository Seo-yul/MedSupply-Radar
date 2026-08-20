"""CLI 진입점 — 공고 추출→매핑→영속화 일괄 처리기(Task M-14).

공고 원문(notices.raw_text)에서 medsupply.llm.mapping.process_notice로 구조화 추출
(M-13, LLM 호출) → 결정적 품목 매핑(medsupply/llm/mapping.py, LLM 미관여) → 영속화
(writer.save_notice_extraction)를 한 번에 수행한다.

사용법:
    python scripts/process_notices.py --db data/medsupply.db --notice-id N-001
    python scripts/process_notices.py --db data/medsupply.db --all [--force-refresh]

--notice-id·--all 중 정확히 하나를 지정해야 한다. --all은 notices 테이블 전 건을
notice_id 오름차순으로 처리한다. 건별로 LLM 키 미설정·호출 실패·LLM_MODE=offline
캐시 미스 등으로 실패해도 그 건만 에러로 표시하고 전체 실행은 계속된다(한 건의
실패가 나머지 건 처리를 막지 않는다) — 종료 코드는 실패 건수가 1건이라도 있으면 1,
전 건 성공(또는 --notice-id 단건 성공)이면 0이다.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 리포 루트를 sys.path에 올려 `medsupply`를 절대 경로 실행에서도 import할 수 있게 한다
# ("python scripts/process_notices.py"로 직접 실행하면 sys.path[0]이 scripts/가 되어
# 리포 루트가 기본으로는 잡히지 않는다 — scripts/run_risk_batch.py와 동일한 처리).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medsupply.data import db  # noqa: E402
from medsupply.llm.mapping import NoticeProcessingResult, process_notice  # noqa: E402


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MedSupply Radar 공고 추출·매핑 일괄 처리기")
    parser.add_argument("--db", required=True, help="처리 대상 SQLite DB 경로")

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--notice-id", dest="notice_id", metavar="N", help="단일 공고 ID만 처리")
    target.add_argument(
        "--all", action="store_true", help="notices 테이블 전 건을 notice_id 오름차순으로 처리"
    )

    parser.add_argument(
        "--force-refresh",
        dest="force_refresh",
        action="store_true",
        help="LLM 캐시를 무시하고 항상 재호출한다(complete_json에 그대로 전파)",
    )
    return parser


def _list_notice_ids(conn: sqlite3.Connection) -> list[str]:
    return [
        row["notice_id"]
        for row in conn.execute("SELECT notice_id FROM notices ORDER BY notice_id")
    ]


def _print_summary(result: NoticeProcessingResult) -> None:
    print(
        f"{result.notice_id}: status={result.status} confidence={result.confidence}"
        f" mapped_count={result.mapped_count}"
    )


def process_all(conn: sqlite3.Connection, *, force_refresh: bool = False) -> int:
    """notices 전 건을 notice_id 오름차순으로 처리하고 건별 요약 + 최종 집계를 출력한다.

    건별 처리 중 예외(LLM 키 미설정·호출 실패·LLMOfflineError·LLMUnavailableError 등)가
    나도 그 건만 에러로 표시하고 다음 건 처리를 계속한다(전체 중단 금지). 반환값은
    실패 건수다(0이면 전 건 성공).
    """
    status_counts: dict[str, int] = {}
    total_mapped = 0
    fail_count = 0

    for notice_id in _list_notice_ids(conn):
        try:
            result = process_notice(conn, notice_id, force_refresh=force_refresh)
        except Exception as exc:  # noqa: BLE001 - 건별 실패를 격리하고 계속 진행하는 게 목적
            print(f"{notice_id}: 처리 실패 - {exc}")
            fail_count += 1
            continue

        _print_summary(result)
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
        total_mapped += result.mapped_count

    print(f"자동확정 건수: {status_counts.get('자동확정', 0)}")
    print(f"확인 필요 건수: {status_counts.get('확인 필요', 0)}")
    print(f"총 매핑 행: {total_mapped}")
    print(f"실패 건수: {fail_count}")

    return fail_count


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    conn = db.get_connection(args.db)
    try:
        if args.all:
            fail_count = process_all(conn, force_refresh=args.force_refresh)
            return 1 if fail_count > 0 else 0

        try:
            result = process_notice(conn, args.notice_id, force_refresh=args.force_refresh)
        except Exception as exc:  # noqa: BLE001 - CLI 최상위 경계, 에러를 표시하고 종료 코드로 반영
            print(f"{args.notice_id}: 처리 실패 - {exc}")
            return 1

        _print_summary(result)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
