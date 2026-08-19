"""Tests for the analytics assessment pipeline (medsupply.analytics.pipeline).

build_item_inputs·assess_item·assess_all는 순수 함수 결선을 검증하고,
assess_snapshot은 tests/conftest.py의 fixture_conn(시드 데이터)으로 DB 어댑터
경로까지 검증한다. 브리프(.superpowers/sdd/2026-08-19-medsupply-master-plan/briefs/
task-S14-brief.md)의 결선 규칙(날짜 변환·룩어헤드·엄격 bool·needs_review 필터
설계·빈 usage 경로)을 그대로 테스트로 고정한다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pandas as pd
import pytest

from medsupply.analytics.params import (
    AnalyticsParams,
    AnomalyParams,
    DepletionParams,
    ForecastParams,
    GradeParams,
    ScoreParams,
)
from medsupply.analytics.pipeline import assess_all, assess_item, assess_snapshot, build_item_inputs
from medsupply.analytics.types import ItemInputs, RiskGrade
from tests.conftest import AS_OF_TODAY, ITEM_1, ITEM_2, ITEM_3

_RECEIPTS_COLUMNS = (
    "shipment_id",
    "item_id",
    "order_date",
    "expected_date",
    "expected_qty",
    "actual_date",
    "actual_qty",
    "status",
)


def _empty_receipts() -> pd.DataFrame:
    """빈 incoming_shipments 부분집합(컬럼만 정의된 0행 DataFrame)."""
    return pd.DataFrame(columns=_RECEIPTS_COLUMNS)


def _receipts(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_RECEIPTS_COLUMNS)


def _params(
    *,
    grade: GradeParams | None = None,
    forecast: ForecastParams | None = None,
    anomaly: AnomalyParams | None = None,
    depletion: DepletionParams | None = None,
    score: ScoreParams | None = None,
) -> AnalyticsParams:
    """테스트용 AnalyticsParams(config/analytics_params.toml 기본값과 동일한 값)."""
    return AnalyticsParams(
        grade=grade
        or GradeParams(
            danger_days=7,
            warning_days=14,
            watch_days=30,
            escalate_on_notice=True,
            escalate_needs_review=True,
        ),
        forecast=forecast or ForecastParams(method="sma", sma_window=28, ses_alpha=0.3, horizon_days=14),
        anomaly=anomaly
        or AnomalyParams(
            surge_ratio=0.30,
            drop_ratio=0.30,
            recent_window=7,
            baseline_window=28,
            receipt_delay_days=3,
        ),
        depletion=depletion or DepletionParams(reflect_receipts=False),
        score=score
        or ScoreParams(
            base_danger=70,
            base_warning=45,
            base_watch=20,
            base_normal=0,
            per_anomaly=8,
            notice_bonus=15,
        ),
        params_hash="testhash1",
    )


class TestBuildItemInputs:
    """build_item_inputs: 날짜 변환·룩어헤드 절단·재고 산출·has_active_notice·정렬."""

    def test_date_text_converted_to_date_index_not_timestamp(self):
        items_df = pd.DataFrame({"item_id": ["ITEM-A"], "is_essential": [1]})
        usage_df = pd.DataFrame(
            {
                "item_id": ["ITEM-A", "ITEM-A"],
                "date": ["2026-01-01", "2026-01-02"],
                "usage_qty": [10, 12],
                "closing_stock": [90, 78],
            }
        )
        notice_map_df = pd.DataFrame(columns=["notice_id", "item_id", "needs_review"])

        result = build_item_inputs(items_df, usage_df, _empty_receipts(), notice_map_df, date(2026, 1, 2))

        usage = result[0].usage
        assert len(usage) == 2
        for idx in usage.index:
            assert type(idx) is date  # pd.Timestamp 금지
        assert list(usage.index) == [date(2026, 1, 1), date(2026, 1, 2)]
        assert list(usage.values) == [10.0, 12.0]

    def test_lookahead_dates_are_truncated(self):
        """as_of를 초과하는 usage 행은 결과에서 잘라낸다(백테스트 안전)."""
        items_df = pd.DataFrame({"item_id": ["ITEM-A"], "is_essential": [1]})
        usage_df = pd.DataFrame(
            {
                "item_id": ["ITEM-A", "ITEM-A", "ITEM-A"],
                "date": ["2026-01-01", "2026-01-02", "2026-01-03"],
                "usage_qty": [10, 12, 999],
                "closing_stock": [90, 78, 1],
            }
        )
        notice_map_df = pd.DataFrame(columns=["notice_id", "item_id", "needs_review"])

        result = build_item_inputs(items_df, usage_df, _empty_receipts(), notice_map_df, date(2026, 1, 2))

        usage = result[0].usage
        assert list(usage.index) == [date(2026, 1, 1), date(2026, 1, 2)]
        assert 999.0 not in usage.values
        # stock_on_hand도 초과일(2026-01-03)이 아닌 as_of 이하 마지막 값(78)이어야 한다.
        assert result[0].stock_on_hand == 78.0

    def test_stock_on_hand_last_closing_stock_at_or_before_as_of(self):
        items_df = pd.DataFrame({"item_id": ["ITEM-A", "ITEM-B"], "is_essential": [1, 0]})
        usage_df = pd.DataFrame(
            {
                "item_id": ["ITEM-A", "ITEM-A"],
                "date": ["2026-01-01", "2026-01-03"],
                "usage_qty": [10, 12],
                "closing_stock": [90, 70],
            }
        )
        notice_map_df = pd.DataFrame(columns=["notice_id", "item_id", "needs_review"])

        result = build_item_inputs(items_df, usage_df, _empty_receipts(), notice_map_df, date(2026, 1, 3))
        by_id = {r.item_id: r for r in result}

        assert by_id["ITEM-A"].stock_on_hand == 70.0
        # ITEM-B는 usage_df에 데이터가 전혀 없다 → 0.0, usage도 빈 시계열.
        assert by_id["ITEM-B"].stock_on_hand == 0.0
        assert len(by_id["ITEM-B"].usage) == 0

    def test_has_active_notice_is_strict_bool_true_and_false(self):
        items_df = pd.DataFrame({"item_id": ["ITEM-A", "ITEM-B"], "is_essential": [1, 0]})
        usage_df = pd.DataFrame(columns=["item_id", "date", "usage_qty", "closing_stock"])
        notice_map_df = pd.DataFrame(
            {"notice_id": ["NTC-1"], "item_id": ["ITEM-A"], "needs_review": [0]}
        )

        result = build_item_inputs(items_df, usage_df, _empty_receipts(), notice_map_df, date(2026, 1, 3))
        by_id = {r.item_id: r for r in result}

        assert by_id["ITEM-A"].has_active_notice is True
        assert type(by_id["ITEM-A"].has_active_notice) is bool
        assert by_id["ITEM-B"].has_active_notice is False
        assert type(by_id["ITEM-B"].has_active_notice) is bool

    def test_has_active_notice_true_regardless_of_needs_review_value(self):
        """build_item_inputs 자신은 needs_review를 걸러내지 않는다(존재만 판정) —
        제외 여부는 상위(assess_snapshot)가 notice_map_df를 사전 필터해 결정한다."""
        items_df = pd.DataFrame({"item_id": ["ITEM-A"], "is_essential": [1]})
        usage_df = pd.DataFrame(columns=["item_id", "date", "usage_qty", "closing_stock"])
        notice_map_df = pd.DataFrame(
            {"notice_id": ["NTC-1"], "item_id": ["ITEM-A"], "needs_review": [1]}
        )

        result = build_item_inputs(items_df, usage_df, _empty_receipts(), notice_map_df, date(2026, 1, 3))

        assert result[0].has_active_notice is True

    def test_sorted_by_item_id_ascending_regardless_of_input_order(self):
        items_df = pd.DataFrame({"item_id": ["ITEM-C", "ITEM-A", "ITEM-B"], "is_essential": [0, 1, 0]})
        usage_df = pd.DataFrame(columns=["item_id", "date", "usage_qty", "closing_stock"])
        notice_map_df = pd.DataFrame(columns=["notice_id", "item_id", "needs_review"])

        result = build_item_inputs(items_df, usage_df, _empty_receipts(), notice_map_df, date(2026, 1, 3))

        assert [r.item_id for r in result] == ["ITEM-A", "ITEM-B", "ITEM-C"]

    def test_unsorted_usage_rows_are_sorted_ascending_by_date(self):
        """usage_df가 일자 오름차순이 아니어도 내부적으로 정렬해 Series를 만든다."""
        items_df = pd.DataFrame({"item_id": ["ITEM-A"], "is_essential": [1]})
        usage_df = pd.DataFrame(
            {
                "item_id": ["ITEM-A", "ITEM-A", "ITEM-A"],
                "date": ["2026-01-03", "2026-01-01", "2026-01-02"],
                "usage_qty": [30, 10, 20],
                "closing_stock": [50, 90, 80],
            }
        )
        notice_map_df = pd.DataFrame(columns=["notice_id", "item_id", "needs_review"])

        result = build_item_inputs(items_df, usage_df, _empty_receipts(), notice_map_df, date(2026, 1, 3))

        usage = result[0].usage
        assert list(usage.index) == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
        assert list(usage.values) == [10.0, 20.0, 30.0]

    def test_receipts_filtered_per_item(self):
        items_df = pd.DataFrame({"item_id": ["ITEM-A", "ITEM-B"], "is_essential": [1, 0]})
        usage_df = pd.DataFrame(columns=["item_id", "date", "usage_qty", "closing_stock"])
        receipts_df = _receipts(
            [
                {
                    "shipment_id": 1, "item_id": "ITEM-A", "order_date": "2026-01-01",
                    "expected_date": "2026-01-10", "expected_qty": 100, "actual_date": None,
                    "actual_qty": None, "status": "예정",
                },
                {
                    "shipment_id": 2, "item_id": "ITEM-B", "order_date": "2026-01-01",
                    "expected_date": "2026-01-10", "expected_qty": 50, "actual_date": None,
                    "actual_qty": None, "status": "예정",
                },
            ]
        )
        notice_map_df = pd.DataFrame(columns=["notice_id", "item_id", "needs_review"])

        result = build_item_inputs(items_df, usage_df, receipts_df, notice_map_df, date(2026, 1, 3))
        by_id = {r.item_id: r for r in result}

        assert list(by_id["ITEM-A"].receipts["item_id"]) == ["ITEM-A"]
        assert list(by_id["ITEM-B"].receipts["item_id"]) == ["ITEM-B"]


class TestAssessItem:
    """assess_item: 손검산 시나리오 + 공고 상향 + 빈 usage 품목 경로."""

    def test_hand_verified_scenario_stock100_usage25_35days_gives_4days_danger(self):
        """브리프 손검산: 재고 100, 일수요 25 상당 시계열 35일 → days=4·위험."""
        as_of = date(2026, 1, 1)
        dates = [as_of - timedelta(days=(34 - i)) for i in range(35)]
        usage = pd.Series([25.0] * 35, index=dates)
        inputs = ItemInputs(
            item_id="TEST-1",
            as_of=as_of,
            stock_on_hand=100.0,
            usage=usage,
            receipts=_empty_receipts(),
            has_active_notice=False,
            is_essential=True,
        )
        params = _params()

        result = assess_item(inputs, params)

        assert result.days_to_stockout == 4
        assert result.depletion_date == as_of + timedelta(days=4)
        assert result.grade == RiskGrade.DANGER
        assert result.base_grade == RiskGrade.DANGER
        assert result.escalated_by_notice is False
        assert result.anomalies == ()
        assert result.risk_type == "general"
        assert result.reflected_receipts == params.depletion.reflect_receipts
        assert result.forecast.avg_daily == 25.0

    def test_notice_escalation_case_warning_becomes_danger(self):
        """활성 공고가 있으면 소진일 기준 경고(8일)가 위험으로 상향된다."""
        as_of = date(2026, 1, 1)
        dates = [as_of - timedelta(days=(4 - i)) for i in range(5)]
        usage = pd.Series([12.5] * 5, index=dates)
        inputs = ItemInputs(
            item_id="TEST-2",
            as_of=as_of,
            stock_on_hand=100.0,
            usage=usage,
            receipts=_empty_receipts(),
            has_active_notice=True,
            is_essential=False,
        )
        params = _params()

        result = assess_item(inputs, params)

        assert result.days_to_stockout == 8
        assert result.base_grade == RiskGrade.WARNING
        assert result.grade == RiskGrade.DANGER
        assert result.escalated_by_notice is True
        assert result.risk_type == "supply_halt"
        assert result.score == 85  # base_danger(70) + notice_bonus(15) + anomaly(0)

    def test_empty_usage_item_evaluated_as_normal_score_zero(self):
        """usage가 비어 있는 품목은 예측 호출 없이 정상·score 0으로 평가를 계속한다."""
        as_of = date(2026, 1, 1)
        inputs = ItemInputs(
            item_id="TEST-3",
            as_of=as_of,
            stock_on_hand=100.0,
            usage=pd.Series([], dtype=float),
            receipts=_empty_receipts(),
            has_active_notice=False,
            is_essential=False,
        )
        params = _params()

        result = assess_item(inputs, params)

        assert result.grade == RiskGrade.NORMAL
        assert result.base_grade == RiskGrade.NORMAL
        assert result.score == 0
        assert result.days_to_stockout is None
        assert result.depletion_date is None
        assert result.anomalies == ()
        assert result.risk_type == "general"
        # types 계약 유지: method='none' 대신 params.forecast.method를 그대로 쓴 0 예측.
        assert result.forecast.method == params.forecast.method
        assert result.forecast.horizon_days == params.forecast.horizon_days
        assert result.forecast.daily == (0.0,) * params.forecast.horizon_days
        assert result.forecast.avg_daily == 0.0
        assert result.forecast.total == 0.0

    def test_empty_usage_item_does_not_raise(self):
        """빈 usage 품목이어도 예외 없이 평가가 끝난다(전 품목 평가 원칙)."""
        as_of = date(2026, 1, 1)
        inputs = ItemInputs(
            item_id="TEST-3B",
            as_of=as_of,
            stock_on_hand=0.0,
            usage=pd.Series([], dtype=float),
            receipts=_empty_receipts(),
            has_active_notice=False,
            is_essential=False,
        )
        params = _params()

        result = assess_item(inputs, params)  # ValueError가 나면 실패

        assert result.item_id == "TEST-3B"

    def test_empty_usage_item_still_detects_receipt_delay(self):
        """usage가 비어 있어도 입고 지연 이상탐지는 독립적으로 수행된다."""
        as_of = date(2026, 1, 10)
        receipts = _receipts(
            [
                {
                    "shipment_id": 1, "item_id": "TEST-4", "order_date": "2026-01-01",
                    "expected_date": "2026-01-01", "expected_qty": 50, "actual_date": None,
                    "actual_qty": None, "status": "예정",
                }
            ]
        )
        inputs = ItemInputs(
            item_id="TEST-4",
            as_of=as_of,
            stock_on_hand=100.0,
            usage=pd.Series([], dtype=float),
            receipts=receipts,
            has_active_notice=False,
            is_essential=False,
        )
        params = _params()

        result = assess_item(inputs, params)

        assert len(result.anomalies) == 1
        assert result.anomalies[0].kind == "receipt_delay"
        assert result.risk_type == "delivery_delay"

    def test_anomalies_combined_usage_first_then_receipt_delay(self):
        """anomalies는 usage 이상탐지 결과가 먼저, 입고 지연 결과가 그 다음인 tuple이다."""
        as_of = date(2026, 8, 4)
        baseline_dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(28)]
        recent_dates = [date(2026, 7, 29) + timedelta(days=i) for i in range(7)]
        values = [18.0] * 28 + [25.0] * 7
        usage = pd.Series(values, index=baseline_dates + recent_dates)
        receipts = _receipts(
            [
                {
                    "shipment_id": 1, "item_id": "TEST-5", "order_date": "2026-07-01",
                    "expected_date": "2026-07-20", "expected_qty": 50, "actual_date": None,
                    "actual_qty": None, "status": "예정",
                }
            ]
        )
        inputs = ItemInputs(
            item_id="TEST-5",
            as_of=as_of,
            stock_on_hand=1000.0,
            usage=usage,
            receipts=receipts,
            has_active_notice=False,
            is_essential=False,
        )
        params = _params()

        result = assess_item(inputs, params)

        assert len(result.anomalies) == 2
        assert result.anomalies[0].kind == "usage_surge"
        assert result.anomalies[1].kind == "receipt_delay"
        assert result.risk_type == "composite"

    def test_ses_method_used_when_configured(self):
        """params.forecast.method='ses'면 ses_forecast로 예측한다."""
        as_of = date(2026, 1, 1)
        usage = pd.Series([10.0, 20.0], index=[date(2025, 12, 31), date(2026, 1, 1)])
        inputs = ItemInputs(
            item_id="TEST-6",
            as_of=as_of,
            stock_on_hand=1000.0,
            usage=usage,
            receipts=_empty_receipts(),
            has_active_notice=False,
            is_essential=False,
        )
        params = _params(forecast=ForecastParams(method="ses", sma_window=28, ses_alpha=0.5, horizon_days=2))

        result = assess_item(inputs, params)

        assert result.forecast.method == "ses"
        assert result.forecast.avg_daily == 15.0  # level0=10, level1=0.5*20+0.5*10=15


class TestAssessAll:
    """assess_all: 입력 순서 무관 item_id 정렬 + 결정성."""

    def _inputs(self, item_id: str) -> ItemInputs:
        as_of = date(2026, 1, 1)
        return ItemInputs(
            item_id=item_id,
            as_of=as_of,
            stock_on_hand=100.0,
            usage=pd.Series([10.0, 10.0], index=[date(2025, 12, 31), date(2026, 1, 1)]),
            receipts=_empty_receipts(),
            has_active_notice=False,
            is_essential=False,
        )

    def test_sorted_by_item_id_regardless_of_input_order(self):
        items = [self._inputs("ITEM-C"), self._inputs("ITEM-A"), self._inputs("ITEM-B")]
        params = _params()

        results = assess_all(items, params)

        assert [r.item_id for r in results] == ["ITEM-A", "ITEM-B", "ITEM-C"]

    def test_deterministic_same_input_produces_identical_factors_json(self):
        """동일 입력을 2회 실행하면 factors_json 문자열이 완전히 같아야 한다(결정성)."""
        items = [self._inputs("ITEM-B"), self._inputs("ITEM-A")]
        params = _params()

        results1 = assess_all(items, params)
        results2 = assess_all(items, params)

        json1 = [json.dumps(r.to_evidence(), ensure_ascii=False, sort_keys=True) for r in results1]
        json2 = [json.dumps(r.to_evidence(), ensure_ascii=False, sort_keys=True) for r in results2]
        assert json1 == json2


class TestAssessSnapshot:
    """assess_snapshot: fixture_conn 시드로 3품목 평가·컬럼 완비·escalate_needs_review 차이."""

    EXPECTED_COLUMNS = {
        "item_id", "grade", "base_grade", "escalated_by_notice", "risk_type", "score",
        "days_to_stockout", "depletion_date", "factors_json",
        "horizon_days", "avg_daily_forecast", "total_forecast", "daily_json",
    }

    def test_evaluates_all_seeded_items_with_complete_columns(self, fixture_conn):
        as_of = date.fromisoformat(AS_OF_TODAY)

        df = assess_snapshot(fixture_conn, as_of)

        assert set(df["item_id"]) == {ITEM_1, ITEM_2, ITEM_3}
        assert len(df) == 3
        assert self.EXPECTED_COLUMNS.issubset(set(df.columns))
        assert set(df["grade"]).issubset({"위험", "경고", "주의", "정상"})
        assert set(df["base_grade"]).issubset({"위험", "경고", "주의", "정상"})
        assert set(df["escalated_by_notice"]).issubset({0, 1})

    def test_factors_json_contains_reflected_receipts_key(self, fixture_conn):
        as_of = date.fromisoformat(AS_OF_TODAY)

        df = assess_snapshot(fixture_conn, as_of)

        for raw in df["factors_json"]:
            evidence = json.loads(raw)
            assert "reflected_receipts" in evidence

    def test_escalate_needs_review_toggle_changes_item2_grade(self, fixture_conn):
        """시드의 ITEM_2는 needs_review=1 매핑만 갖는다 — 설정에 따라 등급이 달라져야 한다."""
        as_of = date.fromisoformat(AS_OF_TODAY)
        params_on = _params(
            grade=GradeParams(
                danger_days=7, warning_days=14, watch_days=30,
                escalate_on_notice=True, escalate_needs_review=True,
            )
        )
        params_off = _params(
            grade=GradeParams(
                danger_days=7, warning_days=14, watch_days=30,
                escalate_on_notice=True, escalate_needs_review=False,
            )
        )

        df_on = assess_snapshot(fixture_conn, as_of, params_on)
        df_off = assess_snapshot(fixture_conn, as_of, params_off)

        grade_on = df_on.loc[df_on["item_id"] == ITEM_2, "grade"].iloc[0]
        grade_off = df_off.loc[df_off["item_id"] == ITEM_2, "grade"].iloc[0]
        escalated_on = df_on.loc[df_on["item_id"] == ITEM_2, "escalated_by_notice"].iloc[0]
        escalated_off = df_off.loc[df_off["item_id"] == ITEM_2, "escalated_by_notice"].iloc[0]

        assert grade_on != grade_off
        assert bool(escalated_on) is True
        assert bool(escalated_off) is False

    def test_days_to_stockout_is_nullable_int64(self, fixture_conn):
        as_of = date.fromisoformat(AS_OF_TODAY)

        df = assess_snapshot(fixture_conn, as_of)

        assert str(df["days_to_stockout"].dtype) == "Int64"

    def test_params_none_loads_default_params(self, fixture_conn):
        """params=None이면 load_params()로 기본 설정을 로드해 예외 없이 동작한다."""
        as_of = date.fromisoformat(AS_OF_TODAY)

        df = assess_snapshot(fixture_conn, as_of, None)

        assert len(df) == 3

    def test_daily_json_and_factors_json_are_parseable_strings(self, fixture_conn):
        as_of = date.fromisoformat(AS_OF_TODAY)

        df = assess_snapshot(fixture_conn, as_of)

        for raw in df["daily_json"]:
            assert isinstance(raw, str)
            parsed = json.loads(raw)
            assert isinstance(parsed, list)
        for raw in df["factors_json"]:
            assert isinstance(raw, str)
            json.loads(raw)  # 예외 없이 파싱되어야 한다
