"""Task S-19: scripts/measure_mape.py 수요예측 MAPE 백테스트 CLI 테스트.

task-S19-brief.md 계약을 검증한다. 세 계층으로 나뉜다.

- 순수 함수 단위 테스트(TestValidActualDays 이하): DB·CLI 없이 MAPE 조립 로직만 검증한다.
  브리프가 요구하는 합성 시계열 3종(등차·계단·정상+노이즈) 손검산, actual=0 제외 규칙,
  실측 부족 시 부분 대조, 품목 제외 집계, baseline_improved 산식을 각각 고정한다.
- TestRunBacktest: :memory: DB(품목 2개 직접 INSERT)로 DB I/O 경로(run_backtest)를
  검증한다 — 실측 없는 품목의 제외, as_of 중복 제거·정렬, 결정성(2회 동일 출력).
- TestCLISmoke: tmp 파일 DB + subprocess로 CLI 계약(exit 0, JSON 스키마 키 완비, 복수
  --as-of, 필수 인자 누락 시 실패)을 검증한다.

scripts/measure_mape.py는 stock_usage_daily 실측만 대조하는 백테스트라 ground truth 라벨을
전혀 읽지 않는다(브리프 §목표) — scripts/measure_detection.py·eval/의 라벨 접근 허용 경로와는
무관한 별도 스크립트다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from medsupply.analytics.forecast import sma_forecast, ses_forecast
from medsupply.analytics.params import (
    AnalyticsParams,
    AnomalyParams,
    DepletionParams,
    ForecastParams,
    GradeParams,
    ScoreParams,
)
from medsupply.data import db
from scripts import measure_mape as md

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "measure_mape.py"

_BUCKET_KEYS = {
    "ses_mape_mean",
    "ses_mape_median",
    "sma_mape_mean",
    "sma_mape_median",
    "baseline_improved",
    "ses_win_rate",
    "items_evaluated",
    "items_excluded",
    "zero_actual_days",
}


def _params(*, horizon_days: int, sma_window: int, ses_alpha: float) -> AnalyticsParams:
    """run_backtest 테스트용 최소 AnalyticsParams(forecast 섹션만 실제로 쓰인다)."""
    return AnalyticsParams(
        grade=GradeParams(
            danger_days=7, warning_days=14, watch_days=30,
            escalate_on_notice=True, escalate_needs_review=True,
        ),
        forecast=ForecastParams(
            method="ses", sma_window=sma_window, ses_alpha=ses_alpha, horizon_days=horizon_days
        ),
        anomaly=AnomalyParams(
            surge_ratio=0.3, drop_ratio=0.3, recent_window=7, baseline_window=28, receipt_delay_days=3
        ),
        depletion=DepletionParams(reflect_receipts=False),
        score=ScoreParams(
            base_danger=70, base_warning=45, base_watch=20, base_normal=0, per_anomaly=8, notice_bonus=15
        ),
        params_hash="testhash",
    )


# ---------------------------------------------------------------------------
# valid_actual_days — missing(None) vs zero(0.0) vs 유효값 구분
# ---------------------------------------------------------------------------


class TestValidActualDays:
    def test_separates_missing_zero_and_valid(self) -> None:
        offsets, actuals, zero_days = md.valid_actual_days([10.0, None, 0.0, 5.0, None])

        assert offsets == [0, 3]
        assert actuals == [10.0, 5.0]
        assert zero_days == 1

    def test_all_missing_yields_empty_with_no_zero_count(self) -> None:
        """행 자체가 없는 날(실측 구간 부족)은 zero_actual_days에 들어가지 않는다."""
        offsets, actuals, zero_days = md.valid_actual_days([None, None])

        assert offsets == []
        assert actuals == []
        assert zero_days == 0

    def test_all_zero_counts_as_zero_actual_days(self) -> None:
        offsets, actuals, zero_days = md.valid_actual_days([0.0, 0.0, 0.0])

        assert offsets == []
        assert actuals == []
        assert zero_days == 3


# ---------------------------------------------------------------------------
# mape — 합성 시계열 3종(등차·계단·정상+노이즈) 손검산
# ---------------------------------------------------------------------------


class TestMapeHandCalculated:
    """브리프 §테스트: "합성 시계열(등차·계단·정상+노이즈 3종)로 MAPE 손검산 일치"."""

    def test_arithmetic_series_with_ses(self) -> None:
        """등차수열 usage=[10,20,30], alpha=0.5 → level_2=22.5(test_forecast.py 기존 케이스와
        동일 산식으로 검증됨). actual=[25,20,15] 각 일자 APE를 손으로 계산해 합산한다."""
        usage = pd.Series([10.0, 20.0, 30.0])
        forecast = ses_forecast(usage, alpha=0.5, horizon=3)
        assert forecast.avg_daily == 22.5  # 선행 조건(이미 test_forecast.py가 검증한 값)

        actual = [25.0, 20.0, 15.0]
        offsets, actuals, zero_days = md.valid_actual_days(actual)
        assert zero_days == 0

        # APE_0=|22.5-25.0|/25.0=0.1, APE_1=|22.5-20.0|/20.0=0.125, APE_2=|22.5-15.0|/15.0=0.5
        expected = (0.1 + 0.125 + 0.5) / 3
        result = md.mape(forecast.daily, offsets, actuals)
        assert result == pytest.approx(expected)

    def test_step_series_with_sma(self) -> None:
        """계단형 usage=[5,5,5,5,20,20,20,20], window=4 → 최근 4개 평균=20.0.
        actual=[25,16] 각 일자 APE를 손으로 계산한다."""
        usage = pd.Series([5.0, 5.0, 5.0, 5.0, 20.0, 20.0, 20.0, 20.0])
        forecast = sma_forecast(usage, window=4, horizon=2)
        assert forecast.avg_daily == 20.0  # 선행 조건

        actual = [25.0, 16.0]
        offsets, actuals, zero_days = md.valid_actual_days(actual)
        assert zero_days == 0

        # APE_0=|20-25|/25=0.2, APE_1=|20-16|/16=0.25
        expected = (0.2 + 0.25) / 2
        result = md.mape(forecast.daily, offsets, actuals)
        assert result == pytest.approx(expected)

    def test_stationary_with_noise_series_with_ses(self) -> None:
        """정상(평균 회귀)+노이즈 usage. SES 자체 산식은 test_forecast.py가 이미 검증하므로,
        여기서는 그 신뢰된 forecast 값을 가져와 MAPE 조립(개별 APE 항의 평균)만 손으로
        재확인한다 — mape()가 엉뚱한 날짜 인덱스를 쓰거나 분모를 뒤집는 버그를 잡아낸다."""
        usage = pd.Series([10.0, 12.0, 9.0, 11.0, 10.0, 13.0, 8.0, 10.0, 11.0, 9.0])
        forecast = ses_forecast(usage, alpha=0.3, horizon=4)
        level = forecast.avg_daily

        actual = [10.0, 9.0, 15.0, 12.0]
        offsets, actuals, zero_days = md.valid_actual_days(actual)
        assert zero_days == 0

        expected_terms = [
            abs(level - 10.0) / 10.0,
            abs(level - 9.0) / 9.0,
            abs(level - 15.0) / 15.0,
            abs(level - 12.0) / 12.0,
        ]
        expected = sum(expected_terms) / 4
        result = md.mape(forecast.daily, offsets, actuals)
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 실측 부족(부분 대조) — horizon보다 짧은 실측 구간
# ---------------------------------------------------------------------------


class TestPartialActualWindow:
    def test_missing_future_days_are_skipped_not_treated_as_zero(self) -> None:
        """실측 5일 중 2일치만 존재하면 그 2일만 대조한다(브리프 §2)."""
        usage = pd.Series([10.0, 10.0, 10.0])
        forecast = sma_forecast(usage, window=3, horizon=5)  # avg=10.0, daily=(10.0,)*5
        actual_by_offset = [11.0, None, None, 9.0, None]

        offsets, actuals, zero_days = md.valid_actual_days(actual_by_offset)
        assert offsets == [0, 3]
        assert zero_days == 0

        # APE_0=|10-11|/11=1/11, APE_3=|10-9|/9=1/9
        expected = (1 / 11 + 1 / 9) / 2
        result = md.mape(forecast.daily, offsets, actuals)
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 품목 제외 — 예측 불가(사용량 이력 없음) / 유효 대조일 0
# ---------------------------------------------------------------------------


class TestItemExclusion:
    def test_backtest_item_returns_none_when_no_valid_actual_days(self) -> None:
        """실측이 전부 결측이거나 0이면(유효 대조일 0) 품목·as_of를 제외한다."""
        usage = pd.Series([10.0, 10.0])
        actual_by_offset = [None, None, 0.0]

        result = md.backtest_item(usage, actual_by_offset, sma_window=2, ses_alpha=0.3, horizon=3)

        assert result is None

    def test_backtest_item_returns_none_when_no_usage_history(self) -> None:
        """as_of 이하 사용량 이력이 아예 없으면 예측 자체가 불가능하다 — 제외."""
        usage = pd.Series([], dtype=float)
        actual_by_offset = [10.0, 12.0]

        result = md.backtest_item(usage, actual_by_offset, sma_window=2, ses_alpha=0.3, horizon=2)

        assert result is None

    def test_backtest_item_returns_result_when_at_least_one_valid_day(self) -> None:
        usage = pd.Series([10.0, 10.0, 10.0])
        actual_by_offset = [None, 0.0, 12.0]

        result = md.backtest_item(usage, actual_by_offset, sma_window=3, ses_alpha=0.5, horizon=3)

        assert result is not None
        assert result.zero_actual_days == 1
        # forecast 10.0 vs actual 12.0 → APE = 2/12 = 1/6 (SES·SMA 모두 avg=10.0이라 동일)
        assert result.ses_mape == pytest.approx(1 / 6)
        assert result.sma_mape == pytest.approx(1 / 6)

    def test_summarize_bucket_counts_excluded_items_separately_from_evaluated(self) -> None:
        results = [md.ItemBacktestResult(ses_mape=0.1, sma_mape=0.2, zero_actual_days=0)]

        bucket = md.summarize_bucket(results, items_excluded=3)

        assert bucket["items_evaluated"] == 1
        assert bucket["items_excluded"] == 3


# ---------------------------------------------------------------------------
# baseline_improved — 개선율 산식
# ---------------------------------------------------------------------------


class TestBaselineImproved:
    def test_positive_improvement_when_ses_better_than_sma(self) -> None:
        # sma=0.20, ses=0.10 → (0.20-0.10)/0.20 = 0.5
        assert md.baseline_improved(0.20, 0.10) == pytest.approx(0.5)

    def test_negative_when_ses_worse_than_sma(self) -> None:
        # sma=0.10, ses=0.20 → (0.10-0.20)/0.10 = -1.0 (합격선 단정 없이 그대로 기록)
        assert md.baseline_improved(0.10, 0.20) == pytest.approx(-1.0)

    def test_none_when_sma_mape_is_zero(self) -> None:
        assert md.baseline_improved(0.0, 0.05) is None

    def test_none_when_either_input_is_none(self) -> None:
        assert md.baseline_improved(None, 0.1) is None
        assert md.baseline_improved(0.1, None) is None


# ---------------------------------------------------------------------------
# summarize_bucket — 평균·중앙값·승률 집계
# ---------------------------------------------------------------------------


class TestSummarizeBucketAggregates:
    def test_mean_median_and_win_rate(self) -> None:
        results = [
            md.ItemBacktestResult(ses_mape=0.1, sma_mape=0.2, zero_actual_days=1),  # SES 승
            md.ItemBacktestResult(ses_mape=0.3, sma_mape=0.2, zero_actual_days=0),  # SES 패
            md.ItemBacktestResult(ses_mape=0.2, sma_mape=0.2, zero_actual_days=2),  # 동률(승 아님)
        ]

        bucket = md.summarize_bucket(results, items_excluded=0)

        assert bucket["ses_mape_mean"] == pytest.approx(round((0.1 + 0.3 + 0.2) / 3, 4))
        assert bucket["sma_mape_mean"] == pytest.approx(0.2)
        assert bucket["ses_mape_median"] == pytest.approx(0.2)
        assert bucket["ses_win_rate"] == pytest.approx(1 / 3)
        assert bucket["zero_actual_days"] == 3
        assert bucket["items_evaluated"] == 3
        assert bucket["baseline_improved"] == pytest.approx(
            md.baseline_improved(bucket["sma_mape_mean"], bucket["ses_mape_mean"])
        )

    def test_empty_results_yields_none_fields_not_zero(self) -> None:
        """0/0을 0.0으로 위장하지 않는다(measure_detection.py와 동일 관례)."""
        bucket = md.summarize_bucket([], items_excluded=5)

        assert bucket["ses_mape_mean"] is None
        assert bucket["ses_mape_median"] is None
        assert bucket["sma_mape_mean"] is None
        assert bucket["baseline_improved"] is None
        assert bucket["ses_win_rate"] is None
        assert bucket["items_evaluated"] == 0
        assert bucket["items_excluded"] == 5
        assert bucket["zero_actual_days"] == 0

    def test_rounds_to_four_decimal_places(self) -> None:
        results = [
            md.ItemBacktestResult(ses_mape=1 / 3, sma_mape=1 / 6, zero_actual_days=0),
        ]

        bucket = md.summarize_bucket(results, items_excluded=0)

        assert bucket["ses_mape_mean"] == round(1 / 3, 4)
        assert bucket["sma_mape_mean"] == round(1 / 6, 4)


# ---------------------------------------------------------------------------
# run_backtest — DB I/O 경로(:memory: 소형 시드)
# ---------------------------------------------------------------------------


class TestRunBacktest:
    """ITM-A는 30일치 실측(as_of 앞뒤 모두 존재), ITM-B는 앞 10일치만(이후 실측 없음)."""

    @pytest.fixture()
    def small_conn(self):
        conn = db.get_connection(":memory:")
        db.init_db(conn, drop=False)
        conn.executemany(
            "INSERT INTO items(item_id, item_name, is_essential) VALUES (?, ?, ?)",
            [("ITM-A", "품목A", 0), ("ITM-B", "품목B", 0)],
        )

        rows: list[tuple[str, str, int, int, int]] = []
        start = date(2026, 1, 1)
        for i in range(30):
            day = (start + timedelta(days=i)).isoformat()
            rows.append(("ITM-A", day, 10, 0, 100))
        for i in range(10):
            day = (start + timedelta(days=i)).isoformat()
            rows.append(("ITM-B", day, 5, 0, 50))

        conn.executemany(
            "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        try:
            yield conn
        finally:
            conn.close()

    def test_excludes_item_with_no_future_actuals(self, small_conn) -> None:
        params = _params(horizon_days=5, sma_window=5, ses_alpha=0.3)

        result = md.run_backtest(small_conn, [date(2026, 1, 15)], params)

        bucket = result["per_as_of"]["2026-01-15"]
        assert bucket["items_evaluated"] == 1  # ITM-A만
        assert bucket["items_excluded"] == 1  # ITM-B(실측 구간이 as_of 이전에서 끝남)

    def test_deduplicates_and_sorts_as_of_list(self, small_conn) -> None:
        params = _params(horizon_days=5, sma_window=5, ses_alpha=0.3)

        result = md.run_backtest(
            small_conn, [date(2026, 1, 15), date(2026, 1, 10), date(2026, 1, 10)], params
        )

        assert result["as_of_list"] == [date(2026, 1, 10), date(2026, 1, 15)]
        assert set(result["per_as_of"].keys()) == {"2026-01-10", "2026-01-15"}

    def test_overall_pools_items_across_as_of(self, small_conn) -> None:
        params = _params(horizon_days=5, sma_window=5, ses_alpha=0.3)

        result = md.run_backtest(small_conn, [date(2026, 1, 12), date(2026, 1, 15)], params)

        # 두 as_of 모두 ITM-A만 평가 가능(ITM-B는 항상 제외) → overall은 2건 풀링.
        assert result["overall"]["items_evaluated"] == 2
        assert result["overall"]["items_excluded"] == 2

    def test_determinism_same_input_same_output(self, small_conn) -> None:
        params = _params(horizon_days=5, sma_window=5, ses_alpha=0.3)

        first = md.run_backtest(small_conn, [date(2026, 1, 15)], params)
        second = md.run_backtest(small_conn, [date(2026, 1, 15)], params)

        assert first == second


# ---------------------------------------------------------------------------
# CLI 스모크 — subprocess exit code + JSON 스키마
# ---------------------------------------------------------------------------


class TestCLISmoke:
    @pytest.fixture()
    def tiny_db(self, tmp_path: Path) -> Path:
        """tmp 소형 DB — stock_usage_daily 직접 INSERT(브리프 §테스트)."""
        db_path = tmp_path / "tiny.db"
        conn = db.get_connection(str(db_path))
        db.init_db(conn, drop=False)
        conn.executemany(
            "INSERT INTO items(item_id, item_name, is_essential) VALUES (?, ?, ?)",
            [("ITM-A", "품목A", 0), ("ITM-B", "품목B", 0)],
        )

        rows: list[tuple[str, str, int, int, int]] = []
        start = date(2026, 1, 1)
        for i in range(45):
            day = start + timedelta(days=i)
            usage = 0 if day == date(2026, 1, 20) else 10  # zero_actual_days 경로도 함께 검증
            rows.append(("ITM-A", day.isoformat(), usage, 0, 100))
        for i in range(10):  # ITM-B는 앞 10일만 → as_of 이후 실측 없음(제외 경로)
            day = start + timedelta(days=i)
            rows.append(("ITM-B", day.isoformat(), 5, 0, 50))

        conn.executemany(
            "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()
        return db_path

    def test_cli_exits_zero_and_writes_complete_schema(self, tiny_db: Path, tmp_path: Path) -> None:
        out_path = tmp_path / "forecast_mape.json"

        proc = subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--db", str(tiny_db),
                "--as-of", "2026-01-15",
                "--out", str(out_path),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(out_path.read_text(encoding="utf-8"))

        assert set(payload.keys()) == {
            "measured_at", "db", "dataset_content_hash", "params_hash", "as_of_list",
            "horizon_days", "per_as_of", "overall",
        }
        assert set(payload["overall"].keys()) == _BUCKET_KEYS
        assert set(payload["per_as_of"]["2026-01-15"].keys()) == _BUCKET_KEYS
        assert payload["as_of_list"] == ["2026-01-15"]
        assert payload["horizon_days"] == 14  # config/analytics_params.toml 기본값
        assert payload["overall"]["items_evaluated"] == 1  # ITM-A
        assert payload["overall"]["items_excluded"] == 1  # ITM-B
        assert payload["overall"]["zero_actual_days"] == 1  # 2026-01-20 usage=0
        assert proc.stdout.strip() != ""  # human summary가 stdout에 찍힌다

    def test_cli_accepts_multiple_as_of_flags(self, tiny_db: Path, tmp_path: Path) -> None:
        out_path = tmp_path / "forecast_mape_multi.json"

        proc = subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--db", str(tiny_db),
                "--as-of", "2026-01-15",
                "--as-of", "2026-01-20",
                "--out", str(out_path),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["as_of_list"] == ["2026-01-15", "2026-01-20"]
        assert set(payload["per_as_of"].keys()) == {"2026-01-15", "2026-01-20"}

    def test_cli_requires_at_least_one_as_of(self, tiny_db: Path, tmp_path: Path) -> None:
        out_path = tmp_path / "forecast_mape_missing.json"

        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(tiny_db), "--out", str(out_path)],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

        assert proc.returncode != 0
        assert not out_path.exists()


class TestCLIDeterminism:
    """브리프 §결정성: 동일 입력 → 동일 출력(measured_at 제외). main(argv) 인프로세스 호출."""

    def test_two_runs_produce_identical_output_except_measured_at(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "det.db"
        conn = db.get_connection(str(db_path))
        db.init_db(conn, drop=False)
        conn.execute("INSERT INTO items(item_id, item_name, is_essential) VALUES (?, ?, ?)", ("ITM-A", "품목A", 0))
        start = date(2026, 1, 1)
        rows = [("ITM-A", (start + timedelta(days=i)).isoformat(), 10, 0, 100) for i in range(30)]
        conn.executemany(
            "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

        out1, out2 = tmp_path / "run1.json", tmp_path / "run2.json"
        rc1 = md.main(["--db", str(db_path), "--as-of", "2026-01-15", "--out", str(out1)])
        rc2 = md.main(["--db", str(db_path), "--as-of", "2026-01-15", "--out", str(out2)])

        assert rc1 == 0
        assert rc2 == 0
        payload1 = json.loads(out1.read_text(encoding="utf-8"))
        payload2 = json.loads(out2.read_text(encoding="utf-8"))
        payload1.pop("measured_at")
        payload2.pop("measured_at")
        assert payload1 == payload2


# ---------------------------------------------------------------------------
# dataset_content_hash — meta.content_hash 앵커(F9)
# ---------------------------------------------------------------------------


class TestDatasetContentHashAnchor:
    def test_payload_includes_dataset_content_hash_from_meta(self, tmp_path: Path) -> None:
        """F9: 출력 payload에 meta.content_hash를 dataset_content_hash로 그대로 실어
        리포트를 특정 데이터셋 상태에 앵커링한다."""
        db_path = tmp_path / "anchor.db"
        conn = db.get_connection(str(db_path))
        db.init_db(conn, drop=False)
        conn.execute(
            "INSERT INTO items(item_id, item_name, is_essential) VALUES (?, ?, ?)",
            ("ITM-A", "품목A", 0),
        )
        start = date(2026, 1, 1)
        rows = [
            ("ITM-A", (start + timedelta(days=i)).isoformat(), 10, 0, 100) for i in range(30)
        ]
        conn.executemany(
            "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        expected_hash = "deadbeef" * 8
        conn.execute("INSERT INTO meta(key, value) VALUES ('content_hash', ?)", (expected_hash,))
        conn.commit()
        conn.close()

        out_path = tmp_path / "anchor_out.json"
        rc = md.main(["--db", str(db_path), "--as-of", "2026-01-15", "--out", str(out_path)])

        assert rc == 0
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["dataset_content_hash"] == expected_hash

    def test_dataset_content_hash_is_none_when_meta_missing(self, tmp_path: Path) -> None:
        """meta.content_hash가 없는 DB(스키마만 적재되고 meta 행이 전혀 없는 경우)에서도
        KeyError 없이 None으로 채워야 한다."""
        db_path = tmp_path / "no_meta.db"
        conn = db.get_connection(str(db_path))
        db.init_db(conn, drop=False)
        conn.execute(
            "INSERT INTO items(item_id, item_name, is_essential) VALUES (?, ?, ?)",
            ("ITM-A", "품목A", 0),
        )
        start = date(2026, 1, 1)
        rows = [
            ("ITM-A", (start + timedelta(days=i)).isoformat(), 10, 0, 100) for i in range(30)
        ]
        conn.executemany(
            "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

        out_path = tmp_path / "no_meta_out.json"
        rc = md.main(["--db", str(db_path), "--as-of", "2026-01-15", "--out", str(out_path)])

        assert rc == 0
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["dataset_content_hash"] is None
