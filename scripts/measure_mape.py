"""CLI 진입점 — 수요예측 MAPE 백테스트(SES vs SMA 베이스라인 병기, Task S-19).

기획서 지표 "베이스라인 대비 개선율"의 실측 산출 경로다. **ground truth 라벨은 전혀 읽지
않는다** — stock_usage_daily의 실측 사용량만 대조하는 백테스트라 scripts/measure_detection.py·
eval/에 한정된 라벨 접근 허용 경로(docs/data-model.md §4)와 무관하다. data/scenarios·
ground_truth 어느 경로도 참조하지 않는다(tests/test_isolation.py의 SCRIPTS_PATH_TARGETS가
이 파일도 정적으로 검사한다).

사용법:
    python scripts/measure_mape.py --db data/medsupply.db \\
        --as-of 2026-07-01 --as-of 2026-07-15 \\
        --out reports/analytics/forecast_mape.json [--params config/analytics_params.toml]

## 측정 정의(task-S19-brief.md §측정 정의가 산식의 법전)
- 백테스트 시점: --as-of를 복수 지정(≥1). as_of별로 품목별 usage(date<=as_of)에서 SES·SMA
  예측(horizon=params.forecast.horizon_days)을 생성한다.
- 실측 대조: as_of+1~as_of+horizon의 stock_usage_daily.usage_qty와 일자별로 대조한다. 그
  날짜의 행이 없으면(실측 구간이 horizon보다 짧음) 조용히 건너뛴다. actual=0인 날은 분모
  불능이라 제외하고 zero_actual_days에 집계한다(그 날이 유일한 후보였어도 마찬가지). 유효
  대조일이 결국 0이면(실측 행 자체가 없거나, 있어도 전부 0) 그 품목·as_of는 SES·SMA 양쪽
  모두 통째로 제외하고 items_excluded에 집계한다 — 대조 대상 날짜 집합은 두 모델이 항상
  같으므로 한쪽만 제외되는 경우는 없다. as_of 이하 사용량 이력이 전혀 없어 예측 자체가
  불가능한 품목도 같은 items_excluded에 묶인다.
- MAPE: mean(|forecast_d - actual_d| / actual_d)를 유효 대조일에 대해서만 계산한다. 품목별
  MAPE를 모아 전 품목 평균(단순 평균)·중앙값을 병기하고 소수 4자리로 반올림한다.
- baseline_improved = (sma_mape_mean - ses_mape_mean) / sma_mape_mean — **이미 반올림된(4자리)
  두 평균값**으로 계산한다. 리포트에 노출되는 숫자만으로 손검산이 그대로 재현되게 하기
  위한 설계다(원값으로 계산하면 표시된 두 숫자로 역산했을 때 아주 근소하게 어긋날 수 있다).
  sma_mape_mean이 0이거나 품목 0건으로 None이면 baseline_improved도 null이다.
  ses_win_rate는 SES 품목 MAPE < SMA 품목 MAPE인 품목의 비율이다(반올림 전 원값으로 비교 —
  동률은 승리로 세지 않는다).
- per_as_of는 as_of별로 위 지표 전부를 담고(overall과 동일 스키마), overall은 전 as_of의
  "품목·as_of" 쌍을 하나의 모집단으로 단순 풀링해 같은 스키마로 재계산한다(as_of마다 품목
  수가 달라도 as_of 간 가중치를 별도로 주지 않는다).

## 결정성
동일 입력(같은 DB·as_of 집합·params) → 동일 출력(measured_at 제외). --as-of는 중복 제거·
오름차순 정렬 후 처리하므로 CLI 인자 순서는 결과에 영향을 주지 않는다. now() 호출은
measured_at 스탬프에만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

# 리포 루트를 sys.path에 올려 `medsupply`를 절대 경로 실행에서도 import할 수 있게 한다
# (scripts/measure_detection.py·scripts/run_risk_batch.py와 동일한 처리).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from medsupply.analytics.forecast import sma_forecast, ses_forecast  # noqa: E402
from medsupply.analytics.params import AnalyticsParams, load_params  # noqa: E402
from medsupply.data import db, queries  # noqa: E402


@dataclass(frozen=True)
class ItemBacktestResult:
    """단일 품목·as_of의 백테스트 결과(제외되지 않은 경우만 존재)."""

    ses_mape: float
    sma_mape: float
    zero_actual_days: int


# ---------------------------------------------------------------------------
# 순수 함수 — MAPE 조립(DB·파일 I/O 없음)
# ---------------------------------------------------------------------------


def valid_actual_days(
    actual_by_offset: Sequence[float | None],
) -> tuple[list[int], list[float], int]:
    """actual_by_offset(길이 <= horizon, None=실측 행 없음)에서 유효(0이 아닌 실측 존재)
    오프셋 인덱스·값 목록과 zero_actual_days(실측이 0이라 제외된 날 수)를 뽑는다.

    None(행 없음)과 0.0(행은 있지만 사용량 0)을 구분한다 — 전자는 "실측 구간 부족", 후자만
    zero_actual_days로 집계한다.
    """
    valid_offsets: list[int] = []
    valid_actuals: list[float] = []
    zero_actual_days = 0
    for offset, actual in enumerate(actual_by_offset):
        if actual is None:
            continue
        if actual == 0:
            zero_actual_days += 1
            continue
        valid_offsets.append(offset)
        valid_actuals.append(actual)
    return valid_offsets, valid_actuals, zero_actual_days


def mape(
    forecast_daily: Sequence[float], valid_offsets: Sequence[int], valid_actuals: Sequence[float]
) -> float:
    """valid_offsets가 가리키는 날짜만 forecast_daily·valid_actuals를 대조한 MAPE.

    valid_offsets가 비어 있으면 호출하지 않는다(호출부 책임 — backtest_item이 그 경우 품목
    자체를 제외하고 이 함수를 부르지 않는다).
    """
    errors = [
        abs(forecast_daily[offset] - actual) / actual
        for offset, actual in zip(valid_offsets, valid_actuals)
    ]
    return statistics.mean(errors)


def backtest_item(
    usage_as_of: pd.Series,
    actual_by_offset: Sequence[float | None],
    sma_window: int,
    ses_alpha: float,
    horizon: int,
) -> ItemBacktestResult | None:
    """단일 품목·as_of의 SES·SMA 예측 생성 + 실측 대조.

    제외 규칙(브리프 §2·§3):
    - usage_as_of가 비어 있으면(사용량 이력 없음) 예측 자체가 불가능 — None.
    - 유효 대조일이 0이면(실측 행이 없거나 있어도 전부 0) — None.
    이 두 경우 모두 SES·SMA 어느 한쪽만 제외되는 일은 없다(같은 실측 집합을 공유하므로).
    """
    if len(usage_as_of) == 0:
        return None

    sma_result = sma_forecast(usage_as_of, sma_window, horizon)
    ses_result = ses_forecast(usage_as_of, ses_alpha, horizon)

    valid_offsets, valid_actuals, zero_actual_days = valid_actual_days(actual_by_offset)
    if not valid_offsets:
        return None

    return ItemBacktestResult(
        ses_mape=mape(ses_result.daily, valid_offsets, valid_actuals),
        sma_mape=mape(sma_result.daily, valid_offsets, valid_actuals),
        zero_actual_days=zero_actual_days,
    )


def _mean_median(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return round(statistics.mean(values), 4), round(statistics.median(values), 4)


def baseline_improved(sma_mape_mean: float | None, ses_mape_mean: float | None) -> float | None:
    """(sma_mape_mean - ses_mape_mean) / sma_mape_mean. sma_mape_mean이 0/None이거나
    ses_mape_mean이 None이면 null(분모 불능 또는 집계 대상 없음)."""
    if sma_mape_mean is None or ses_mape_mean is None or sma_mape_mean == 0:
        return None
    return round((sma_mape_mean - ses_mape_mean) / sma_mape_mean, 4)


def summarize_bucket(results: list[ItemBacktestResult], items_excluded: int) -> dict:
    """품목별 결과 목록 → overall/per_as_of가 공유하는 지표 dict(BUCKET_SCHEMA_KEYS와 1:1)."""
    ses_mean, ses_median = _mean_median([r.ses_mape for r in results])
    sma_mean, sma_median = _mean_median([r.sma_mape for r in results])
    wins = sum(1 for r in results if r.ses_mape < r.sma_mape)

    return {
        "ses_mape_mean": ses_mean,
        "ses_mape_median": ses_median,
        "sma_mape_mean": sma_mean,
        "sma_mape_median": sma_median,
        "baseline_improved": baseline_improved(sma_mean, ses_mean),
        "ses_win_rate": (wins / len(results)) if results else None,
        "items_evaluated": len(results),
        "items_excluded": items_excluded,
        "zero_actual_days": sum(r.zero_actual_days for r in results),
    }


# ---------------------------------------------------------------------------
# DB I/O — stock_usage_daily·items만 읽는다(읽기 전용)
# ---------------------------------------------------------------------------


def _load_usage(conn) -> pd.DataFrame:
    """stock_usage_daily 전체를 1회 SELECT로 읽는다(medsupply.analytics.pipeline._load_all_usage와
    동일한 이유 — 품목별 반복 조회 회피). 이 스크립트는 이 테이블을 읽기 전용으로만 참조한다."""
    query = "SELECT item_id, date, usage_qty FROM stock_usage_daily ORDER BY item_id, date"
    return pd.read_sql_query(query, conn)


def _group_usage_by_item(usage_df: pd.DataFrame) -> dict[str, list[tuple[str, float]]]:
    """item_id -> [(date_iso, usage_qty), ...] 오름차순 목록(usage_df가 이미 item_id·date
    정렬이므로 추가 정렬은 불필요)."""
    grouped: dict[str, list[tuple[str, float]]] = {}
    for row in usage_df.itertuples(index=False):
        grouped.setdefault(row.item_id, []).append((row.date, float(row.usage_qty)))
    return grouped


def _split_usage(
    item_rows: list[tuple[str, float]], as_of: date, horizon: int
) -> tuple[pd.Series, list[float | None]]:
    """item_rows(오름차순 (date_iso, usage_qty))를 as_of 기준으로 예측 입력·실측 대조용으로 나눈다.

    Returns:
        (usage_as_of, actual_by_offset)
        - usage_as_of: date <= as_of인 값들의 pd.Series(예측 입력). forecast.py의 sma_forecast·
          ses_forecast는 위치 기반(.iloc)이라 인덱스 자체는 무관하다 — 오름차순 값 순서만
          보장하면 된다.
        - actual_by_offset: 길이 horizon 리스트. actual_by_offset[i]는 as_of+1+i의 usage_qty,
          그 날짜의 행이 stock_usage_daily에 없으면 None(브리프 §2 "실측 구간 부족").
    """
    as_of_iso = as_of.isoformat()
    horizon_end_iso = (as_of + timedelta(days=horizon)).isoformat()

    past_values = [qty for day_iso, qty in item_rows if day_iso <= as_of_iso]
    future_map = {
        day_iso: qty for day_iso, qty in item_rows if as_of_iso < day_iso <= horizon_end_iso
    }

    actual_by_offset: list[float | None] = [
        future_map.get((as_of + timedelta(days=offset)).isoformat())
        for offset in range(1, horizon + 1)
    ]

    return pd.Series(past_values, dtype=float), actual_by_offset


def run_backtest(conn, as_of_list: list[date], params: AnalyticsParams) -> dict:
    """전 as_of × 전 품목 MAPE 백테스트. stock_usage_daily·items만 읽는다(라벨 미참조).

    Returns:
        {as_of_list(정렬·중복 제거된 date 목록), per_as_of({as_of_iso: bucket}),
        overall(bucket)} — bucket 스키마는 summarize_bucket 참조.
    """
    normalized_as_of = sorted(set(as_of_list))

    items_df = queries.list_items(conn)
    item_ids = sorted(items_df["item_id"])
    usage_by_item = _group_usage_by_item(_load_usage(conn))

    horizon = params.forecast.horizon_days
    sma_window = params.forecast.sma_window
    ses_alpha = params.forecast.ses_alpha

    per_as_of: dict[str, dict] = {}
    pooled_results: list[ItemBacktestResult] = []
    pooled_excluded = 0

    for as_of in normalized_as_of:
        results: list[ItemBacktestResult] = []
        excluded = 0
        for item_id in item_ids:
            usage_as_of, actual_by_offset = _split_usage(
                usage_by_item.get(item_id, []), as_of, horizon
            )
            result = backtest_item(usage_as_of, actual_by_offset, sma_window, ses_alpha, horizon)
            if result is None:
                excluded += 1
            else:
                results.append(result)

        per_as_of[as_of.isoformat()] = summarize_bucket(results, excluded)
        pooled_results.extend(results)
        pooled_excluded += excluded

    overall = summarize_bucket(pooled_results, pooled_excluded)
    return {"as_of_list": normalized_as_of, "per_as_of": per_as_of, "overall": overall}


# ---------------------------------------------------------------------------
# 파일 I/O·사람이 읽는 stdout 요약
# ---------------------------------------------------------------------------


def _write_json(path: str | Path, data: dict) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _format_pct(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "n/a"


def _format_mape(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "n/a"


def _bucket_line(label: str, bucket: dict) -> str:
    return (
        f"[{label}] SES MAPE 평균 {_format_mape(bucket['ses_mape_mean'])}"
        f"(중앙값 {_format_mape(bucket['ses_mape_median'])}) / "
        f"SMA MAPE 평균 {_format_mape(bucket['sma_mape_mean'])}"
        f"(중앙값 {_format_mape(bucket['sma_mape_median'])}) / "
        f"개선율 {_format_pct(bucket['baseline_improved'])} / "
        f"승률 {_format_pct(bucket['ses_win_rate'])} / "
        f"평가 {bucket['items_evaluated']}품목"
        f"(제외 {bucket['items_excluded']}, 실측0일 {bucket['zero_actual_days']})"
    )


def _human_summary(payload: dict) -> str:
    lines = [
        f"백테스트: horizon {payload['horizon_days']}일, "
        f"as_of {len(payload['as_of_list'])}개 {payload['as_of_list']}"
    ]
    for as_of_iso in payload["as_of_list"]:
        lines.append(_bucket_line(as_of_iso, payload["per_as_of"][as_of_iso]))
    lines.append(_bucket_line("전체", payload["overall"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"ISO 날짜(YYYY-MM-DD)여야 한다: {value!r}") from exc


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MedSupply Radar 수요예측 MAPE 백테스트 CLI(SES vs SMA 베이스라인 병기)"
    )
    parser.add_argument("--db", required=True, help="백테스트 대상 SQLite DB 경로")
    parser.add_argument(
        "--as-of",
        dest="as_of",
        action="append",
        type=_iso_date,
        required=True,
        metavar="YYYY-MM-DD",
        help="백테스트 시점(복수 지정 가능, 최소 1개 필수)",
    )
    parser.add_argument("--out", required=True, help="결과 JSON 출력 경로")
    parser.add_argument(
        "--params",
        default="config/analytics_params.toml",
        help="분석 파라미터 TOML 경로(기본: config/analytics_params.toml)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    params = load_params(args.params)
    conn = db.get_connection(args.db)
    try:
        backtest = run_backtest(conn, args.as_of, params)
    finally:
        conn.close()

    payload = {
        "measured_at": _now_iso(),
        "db": args.db,
        "params_hash": params.params_hash,
        "as_of_list": [d.isoformat() for d in backtest["as_of_list"]],
        "horizon_days": params.forecast.horizon_days,
        "per_as_of": backtest["per_as_of"],
        "overall": backtest["overall"],
    }
    _write_json(args.out, payload)
    print(_human_summary(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
