"""CLI 진입점 — LLM 캐시 선워밍 일괄 실행기(Task M-27).

medsupply.llm.warm.warm_cache로 공고 추출(process_notice)·위험 원인 설명(explain_item)
생성물을 일괄 선계산해 캐시·DB를 채운다. 시연·오프라인 모드 준비용 — 실제 워밍 실행은
LLM 키 확보 후 별도 런북으로 수행한다(이 스크립트·테스트 자체는 모킹 기반이라 키가 없어도
개발·검증할 수 있다).

사용법:
    python scripts/warm_cache.py --db data/medsupply.db
    python scripts/warm_cache.py --db data/medsupply.db --scope explanations --force-refresh

--scope 기본값은 all(공고를 먼저 처리한 뒤 설명 — medsupply.llm.warm.warm_cache의 순서
고정 사유 참조: 공고 매핑이 먼저 반영돼야 설명 근거의 활성 공고 목록이 정확하다).

건별 실패(LLM 키 미설정·호출 실패 등)는 warm_cache 내부에서 이미 격리되므로, 키가 전혀
없어 전 건이 실패해도 이 CLI는 크래시하지 않고 요약을 출력한 뒤 종료 코드 1로 끝난다.
실패가 하나도 없으면(대상이 0건인 scope 포함) 종료 코드 0이다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 리포 루트를 sys.path에 올려 `medsupply`를 절대 경로 실행에서도 import할 수 있게 한다
# ("python scripts/warm_cache.py"로 직접 실행하면 sys.path[0]이 scripts/가 되어 리포
# 루트가 기본으로는 잡히지 않는다 — scripts/process_notices.py와 동일한 처리).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medsupply.data import db  # noqa: E402
from medsupply.llm.warm import WarmReport, warm_cache  # noqa: E402


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MedSupply Radar LLM 캐시 선워밍 CLI")
    parser.add_argument("--db", required=True, help="워밍 대상 SQLite DB 경로")
    parser.add_argument(
        "--scope",
        choices=("all", "notices", "explanations"),
        default="all",
        help="워밍 범위(기본: all — 공고를 먼저 처리한 뒤 설명)",
    )
    parser.add_argument(
        "--force-refresh",
        dest="force_refresh",
        action="store_true",
        help="LLM 캐시를 무시하고 항상 재호출한다(warm_cache에 그대로 전파)",
    )
    return parser


def _print_summary(report: WarmReport) -> None:
    print(
        f"공고: 총 {report.notices_total}건 · 성공 {report.notices_ok}건 ·"
        f" 실패 {len(report.notices_failed)}건"
    )
    if report.notices_failed:
        print(f"  실패 공고: {', '.join(report.notices_failed)}")

    print(
        f"설명: 총 {report.explanations_total}건 · 성공 {report.explanations_ok}건 ·"
        f" 실패 {len(report.explanations_failed)}건"
    )
    if report.explanations_failed:
        print(f"  실패 품목: {', '.join(report.explanations_failed)}")

    print(f"캐시 적중: {report.cache_hits}건")


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    conn = db.get_connection(args.db)
    try:
        report = warm_cache(
            conn, scope=args.scope, force_refresh=args.force_refresh, progress=print
        )
    finally:
        conn.close()

    _print_summary(report)

    total_failed = len(report.notices_failed) + len(report.explanations_failed)
    return 1 if total_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
