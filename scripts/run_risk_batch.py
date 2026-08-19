"""CLI 진입점 — 위험 평가 배치 실행기(Task S-15).

스냅샷 DB에 대해 medsupply.analytics.pipeline.assess_snapshot으로 위험 평가를 실행하고,
medsupply.data.writer.save_risk_results·save_forecasts로 결과를 영속화한다. 화면·알림·
측정·재현성 검증이 소비하는 risk_results/forecasts를 만드는 **유일한 실행 주체**다.

사용법:
    python scripts/run_risk_batch.py --db data/medsupply.db \
        --as-of 2026-07-31 --as-of 2026-08-01 [--params config/analytics_params.toml]

--as-of는 복수 지정할 수 있다(action='append', 최소 1개 필수, ISO 날짜 형식 검증 — 형식이
아니면 argparse가 즉시 종료 코드 2로 종료한다). 지정한 순서대로 실행한다. 전일·당일 2개
run을 한 번에 만들면 등급 변동 알림(3주차 태스크)의 기준선이 된다.

--sync-alerts 플래그는 이 태스크에서 만들지 않는다(3주차 알림 태스크가 추가할 예정).

**멱등성**: run_id = f"{as_of.isoformat()}#{params.params_hash[:8]}"는 (as_of, params 내용)
만으로 결정된다. 같은 (as_of, params)로 재실행하면 같은 run_id가 나오고, writer.
save_risk_results·save_forecasts는 같은 run_id의 기존 행을 DELETE한 뒤 새로 INSERT하므로
(멱등 규칙), 같은 스냅샷·같은 파라미터로 몇 번을 재실행해도 risk_results/forecasts의 최종
상태(행수·값)는 항상 동일하다. meta.data_version은 저장 호출마다 증가한다(재실행 여부와
무관 — data_version은 "몇 번 썼는가"를 세는 캐시 무효화 신호이지 "내용이 바뀌었는가"가
아니다).

배치는 앱측 계층이라 medsupply를 직접 import한다(scripts/datagen/의 시나리오 데이터 생성
격리 원칙과는 무관한 층이다). 단, data/scenarios·ground_truth는 이 파일에서도 절대
참조하지 않는다.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# 리포 루트를 sys.path에 올려 `medsupply`를 절대 경로 실행에서도 import할 수 있게 한다
# ("python scripts/run_risk_batch.py"로 직접 실행하면 sys.path[0]이 scripts/가 되어 리포
# 루트가 기본으로는 잡히지 않는다 — scripts/generate_dataset.py와 동일한 처리).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medsupply.analytics.params import AnalyticsParams, load_params  # noqa: E402
from medsupply.analytics.pipeline import assess_snapshot  # noqa: E402
from medsupply.data import db, writer  # noqa: E402

#: assess_snapshot 반환 DataFrame → writer.save_risk_results 필수 컬럼(순서·이름 정확 대조).
_RISK_COLUMNS = [
    "item_id", "grade", "base_grade", "escalated_by_notice", "risk_type",
    "score", "days_to_stockout", "depletion_date", "factors_json",
]

#: assess_snapshot 반환 DataFrame → writer.save_forecasts 필수 컬럼(순서·이름 정확 대조).
_FORECAST_COLUMNS = [
    "item_id", "horizon_days", "avg_daily_forecast", "total_forecast", "daily_json",
]

#: 등급 분포 출력 순서(심각도 순 — medsupply.analytics.types.GRADE_ORDER와 동일).
_GRADE_ORDER = ("위험", "경고", "주의", "정상")


def _iso_date(value: str) -> date:
    """--as-of 값을 ISO 날짜(YYYY-MM-DD)로 검증·변환한다.

    형식이 아니면 ArgumentTypeError를 던져 argparse가 사용법을 출력하고 종료 코드 2로
    종료하게 한다.
    """
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--as-of는 ISO 날짜(YYYY-MM-DD)여야 한다: {value!r}") from exc


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MedSupply Radar 위험 평가 배치 실행기")
    parser.add_argument("--db", required=True, help="평가·저장 대상 SQLite DB 경로")
    parser.add_argument(
        "--as-of",
        dest="as_of",
        action="append",
        required=True,
        type=_iso_date,
        metavar="YYYY-MM-DD",
        help="평가 기준일(복수 지정 가능 — 지정한 순서대로 실행)",
    )
    parser.add_argument(
        "--params",
        default="config/analytics_params.toml",
        help="분석 파라미터 TOML 경로(기본: config/analytics_params.toml)",
    )
    return parser


def _grade_distribution(grades: "pd.Series") -> dict[str, int]:  # noqa: F821 - 타입 힌트 문서용
    counts = grades.value_counts()
    return {grade: int(counts.get(grade, 0)) for grade in _GRADE_ORDER}


def _read_data_version(conn) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    return int(row[0]) if row is not None else 0


def run_batch(conn, as_of_list: list[date], params: AnalyticsParams) -> int:
    """as_of_list 순서대로 평가·저장을 실행하고 요약을 출력한다. 실행한 run 수를 반환한다."""
    run_count = 0
    for as_of in as_of_list:
        run_id = f"{as_of.isoformat()}#{params.params_hash[:8]}"
        snapshot_df = assess_snapshot(conn, as_of, params)

        writer.save_risk_results(conn, snapshot_df[_RISK_COLUMNS], run_id, as_of)
        writer.save_forecasts(conn, snapshot_df[_FORECAST_COLUMNS], run_id, as_of)

        distribution = _grade_distribution(snapshot_df["grade"])
        escalated_count = int(snapshot_df["escalated_by_notice"].sum())

        print(f"run_id: {run_id}")
        print(f"품목 수: {len(snapshot_df)}")
        print(
            "등급 분포: "
            + ", ".join(f"{grade} {distribution[grade]}건" for grade in _GRADE_ORDER)
        )
        print(f"상향(공고) 건수: {escalated_count}건")
        run_count += 1

    return run_count


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        params = load_params(args.params)
    except (OSError, ValueError) as exc:
        print(f"파라미터 로드 실패({args.params}): {exc}", file=sys.stderr)
        return 1

    conn = db.get_connection(args.db)
    try:
        run_count = run_batch(conn, args.as_of, params)
        print(f"총 run 수: {run_count}")
        print(f"최종 data_version: {_read_data_version(conn)}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
