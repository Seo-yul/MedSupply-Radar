"""Tests for stock depletion estimation functions."""

import pytest
import pandas as pd
from datetime import date
from collections.abc import Sequence

from medsupply.analytics.depletion import estimate_depletion
from medsupply.analytics.params import DepletionParams
from medsupply.analytics.types import DepletionEstimate


class TestEstimateDepletionBasic:
    """Basic depletion estimation tests."""

    def test_basic_depletion(self):
        """Test basic: stock=100, forecast=[25,25,25,25,...] (horizon 14).

        Expected: 4일째 0 도달 → days=4, depletion_date=as_of+4
        Simulation:
        - day1: 100 - 25 = 75
        - day2: 75 - 25 = 50
        - day3: 50 - 25 = 25
        - day4: 25 - 25 = 0 (stockout)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 100.0
        daily_forecast = [25.0] * 14
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=False)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 4
        assert result.depletion_date == date(2026, 1, 5)
        assert result.stock_on_hand == 100.0
        assert result.reflected_receipts == False

    def test_exact_boundary_depletion(self):
        """Test exact boundary: stock=50, forecast=[25,25].

        Expected: 2일째 정확히 0 → days=2 (0 이하 포함 규칙)
        Simulation:
        - day1: 50 - 25 = 25
        - day2: 25 - 25 = 0 (stockout)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 50.0
        daily_forecast = [25.0, 25.0]
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=False)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 2
        assert result.depletion_date == date(2026, 1, 3)
        assert result.stock_on_hand == 50.0
        assert result.reflected_receipts == False

    def test_already_depleted(self):
        """Test already depleted: stock=0.

        Expected: days=0, date=as_of (이미 소진)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 0.0
        daily_forecast = [25.0, 25.0, 25.0]
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=False)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 0
        assert result.depletion_date == as_of
        assert result.stock_on_hand == 0.0
        assert result.reflected_receipts == False

    def test_negative_stock(self):
        """Test negative stock: stock=-10.

        Expected: days=0, date=as_of (stock_on_hand <= 0)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = -10.0
        daily_forecast = [25.0, 25.0, 25.0]
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=False)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 0
        assert result.depletion_date == as_of
        assert result.stock_on_hand == -10.0
        assert result.reflected_receipts == False


class TestEstimateDepletionExtension:
    """Tests for horizon extension with average forecast."""

    def test_horizon_extension(self):
        """Test horizon extension: stock=1000, forecast=[10]*14 (평균 10).

        Expected: days=100 (1000 / 10 = 100 days)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 1000.0
        daily_forecast = [10.0] * 14
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=False)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 100
        assert result.depletion_date == date(2026, 4, 11)  # as_of + 100 days
        assert result.stock_on_hand == 1000.0
        assert result.reflected_receipts == False

    def test_no_depletion_zero_forecast(self):
        """Test no depletion when forecast is all zeros.

        Expected: None/None (소진 없음)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 100.0
        daily_forecast = [0.0] * 14
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=False)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout is None
        assert result.depletion_date is None
        assert result.stock_on_hand == 100.0
        assert result.reflected_receipts == False

    def test_no_depletion_beyond_365_days(self):
        """Test no depletion when consumption beyond 365 days.

        stock=10000, forecast=[10]*14 (평균 10)
        10000 / 10 = 1000 days, but max 365 days → None/None
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 10000.0
        daily_forecast = [10.0] * 14
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=False)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout is None
        assert result.depletion_date is None
        assert result.stock_on_hand == 10000.0
        assert result.reflected_receipts == False


class TestEstimateDepletionReceipts:
    """Tests for receipt reflection."""

    def test_reflect_receipts_true(self):
        """Test reflect_receipts=True with incoming shipment.

        stock=50, forecast=[25]*14, 입고 60이 day2 예정
        Simulation with receipts:
        - day1: 50 - 25 = 25
        - day2: 25 (before receipt), +60 (receipt), 85 - 25 = 60
        - day3: 60 - 25 = 35
        - day4: 35 - 25 = 10
        - day5: 10 - 25 = -15 (stockout)
        Expected: days=5
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 50.0
        daily_forecast = [25.0] * 14
        receipts = pd.DataFrame({
            'shipment_id': ['S1'],
            'expected_date': [date(2026, 1, 3)],  # day 2 in simulation (as_of + 2 = day2)
            'expected_qty': [60.0],
            'actual_date': [pd.NaT],
            'status': ['pending']
        })
        params = DepletionParams(reflect_receipts=True)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 5
        assert result.depletion_date == date(2026, 1, 6)
        assert result.stock_on_hand == 50.0
        assert result.reflected_receipts == True

    def test_reflect_receipts_false(self):
        """Test reflect_receipts=False ignores incoming shipments.

        Same inputs as test_reflect_receipts_true but with reflect_receipts=False.
        Expected: days=2 (no receipt consideration)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 50.0
        daily_forecast = [25.0] * 14
        receipts = pd.DataFrame({
            'shipment_id': ['S1'],
            'expected_date': [date(2026, 1, 3)],  # day 2 in simulation
            'expected_qty': [60.0],
            'actual_date': [pd.NaT],
            'status': ['pending']
        })
        params = DepletionParams(reflect_receipts=False)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 2
        assert result.depletion_date == date(2026, 1, 3)
        assert result.stock_on_hand == 50.0
        assert result.reflected_receipts == False

    def test_ignore_receipt_already_arrived_by_as_of(self):
        """as_of 시점에 이미 도착한 입고(actual_date <= as_of)는 반영하지 않는다.

        closing_stock에 이미 반영된 물량이라 다시 더하면 이중 계상이 된다.

        Stock=50, forecast=[25]*14, 입고 60이 2025-12-30에 이미 도착(as_of=2026-01-01)
        Expected: days=2 (입고 무시)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 50.0
        daily_forecast = [25.0] * 14
        receipts = pd.DataFrame({
            'shipment_id': ['S1'],
            'expected_date': [date(2026, 1, 3)],
            'expected_qty': [60.0],
            'actual_date': [date(2025, 12, 30)],  # as_of 이전 도착 → 재고에 이미 포함
            'status': ['received']
        })
        params = DepletionParams(reflect_receipts=True)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 2
        assert result.depletion_date == date(2026, 1, 3)
        assert result.reflected_receipts == True

    def test_receipt_arriving_after_as_of_is_still_pending_at_as_of(self):
        """**의미론 갱신(Task S-17c)**: actual_date > as_of인 입고는 as_of 시점에는 아직
        도착하지 않았으므로 pending으로 반영한다.

        구 구현은 ``actual_date IS NULL``만 pending으로 쳐서, "나중에(01-02) 도착했다"는
        **미래 정보**로 as_of(01-01) 시점의 입고 예정을 소급 제외했다 — 백테스트 as_of
        재구성 버그. 이 테스트는 그 구 의미론(days=2)을 하드코딩하고 있었고, 단언을 약화하지
        않고 새 의미론 값(days=5)으로 갱신했다.

        Stock=50, forecast=[25]*14, 입고 60 (expected 01-03, actual 01-02 — 둘 다 as_of 이후)
        Expected: days=5 (test_reflect_receipts_true와 동일한 궤적)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 50.0
        daily_forecast = [25.0] * 14
        receipts = pd.DataFrame({
            'shipment_id': ['S1'],
            'expected_date': [date(2026, 1, 3)],
            'expected_qty': [60.0],
            'actual_date': [date(2026, 1, 2)],  # as_of 시점에는 아직 미래 = 미도착
            'status': ['received']
        })
        params = DepletionParams(reflect_receipts=True)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 5
        assert result.depletion_date == date(2026, 1, 6)
        assert result.reflected_receipts == True

    def test_arrival_stamp_does_not_change_result_versus_null(self):
        """as_of 이후 도착 스탬프의 유무가 as_of 시점 판정을 바꾸면 안 된다(룩어헤드 차단).

        같은 입고를 actual_date=NULL로 둔 경우와 actual_date=2026-01-02(미래 도착)로 둔
        경우의 소진 추정이 동일해야 한다.
        """
        as_of = date(2026, 1, 1)
        base = {
            'shipment_id': ['S1'],
            'expected_date': [date(2026, 1, 3)],
            'expected_qty': [60.0],
            'status': ['pending'],
        }
        params = DepletionParams(reflect_receipts=True)

        with_null = estimate_depletion(
            50.0, [25.0] * 14, pd.DataFrame({**base, 'actual_date': [pd.NaT]}), as_of, params
        )
        with_future_stamp = estimate_depletion(
            50.0, [25.0] * 14,
            pd.DataFrame({**base, 'actual_date': [date(2026, 1, 2)]}), as_of, params
        )

        assert with_null.days_to_stockout == with_future_stamp.days_to_stockout
        assert with_null.depletion_date == with_future_stamp.depletion_date

    def test_ignore_receipt_with_past_expected_date(self):
        """Test that receipts with expected_date <= as_of are not reflected.

        Stock=50, forecast=[25]*14, 입고 60 (expected_date <= as_of)
        Expected: days=2 (이미 지연된 입고 무시, 이상탐지 소관)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 50.0
        daily_forecast = [25.0] * 14
        receipts = pd.DataFrame({
            'shipment_id': ['S1'],
            'expected_date': [as_of],  # expected_date == as_of
            'expected_qty': [60.0],
            'actual_date': [pd.NaT],
            'status': ['pending']
        })
        params = DepletionParams(reflect_receipts=True)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 2
        assert result.depletion_date == date(2026, 1, 3)
        assert result.reflected_receipts == True

    def test_receipts_date_string_parsing(self):
        """Test that date strings in receipts are parsed correctly.

        Expected: ISO date strings should be parsed as date objects.
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 50.0
        daily_forecast = [25.0] * 14
        receipts = pd.DataFrame({
            'shipment_id': ['S1'],
            'expected_date': ['2026-01-03'],  # ISO string
            'expected_qty': [60.0],
            'actual_date': [None],
            'status': ['pending']
        })
        params = DepletionParams(reflect_receipts=True)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 5
        assert result.depletion_date == date(2026, 1, 6)
        assert result.reflected_receipts == True


class TestOverdueCutoff:
    """overdue_cutoff 스위치(Task S-17c) — 연체 건이 있으면 미래 예정 입고를 전부 미반영."""

    @staticmethod
    def _receipts(rows):
        return pd.DataFrame({
            'shipment_id': [r[0] for r in rows],
            'expected_date': [r[1] for r in rows],
            'expected_qty': [r[2] for r in rows],
            'actual_date': [r[3] for r in rows],
            'status': ['pending'] * len(rows),
        })

    def test_off_by_default(self):
        """기본값은 False — 연체가 있어도 미래 예정 입고를 반영한다."""
        params = DepletionParams(reflect_receipts=True)
        assert params.overdue_cutoff is False

    def test_overdue_blocks_future_receipt_when_enabled(self):
        """연체 1건(예정 2025-12-28, 미도착) + 미래 예정 60(01-03).

        cutoff=True → 미래 입고 미반영 → days=2
        """
        as_of = date(2026, 1, 1)
        receipts = self._receipts([
            ('S-OVERDUE', date(2025, 12, 28), 100.0, pd.NaT),
            ('S-FUTURE', date(2026, 1, 3), 60.0, pd.NaT),
        ])
        params = DepletionParams(reflect_receipts=True, overdue_cutoff=True)

        result = estimate_depletion(50.0, [25.0] * 14, receipts, as_of, params)

        assert result.days_to_stockout == 2
        assert result.depletion_date == date(2026, 1, 3)

    def test_same_input_reflects_future_receipt_when_disabled(self):
        """같은 입력에 cutoff=False → 미래 입고 반영 → days=5 (스위치만의 차이 확인)."""
        as_of = date(2026, 1, 1)
        receipts = self._receipts([
            ('S-OVERDUE', date(2025, 12, 28), 100.0, pd.NaT),
            ('S-FUTURE', date(2026, 1, 3), 60.0, pd.NaT),
        ])
        params = DepletionParams(reflect_receipts=True, overdue_cutoff=False)

        result = estimate_depletion(50.0, [25.0] * 14, receipts, as_of, params)

        assert result.days_to_stockout == 5

    def test_no_overdue_means_cutoff_has_no_effect(self):
        """연체가 없으면 cutoff=True여도 미래 입고를 정상 반영한다."""
        as_of = date(2026, 1, 1)
        receipts = self._receipts([
            ('S-DONE', date(2025, 12, 28), 100.0, date(2025, 12, 28)),  # 정시 도착(연체 아님)
            ('S-FUTURE', date(2026, 1, 3), 60.0, pd.NaT),
        ])
        params = DepletionParams(reflect_receipts=True, overdue_cutoff=True)

        result = estimate_depletion(50.0, [25.0] * 14, receipts, as_of, params)

        assert result.days_to_stockout == 5

    def test_late_arrival_stamp_counts_as_overdue_at_as_of(self):
        """예정 12-28 건이 01-10에 도착했다면 as_of(01-01) 시점에는 '연체'다.

        도착 스탬프가 as_of 이후이므로 as_of 시점 판정에서는 미도착으로 재구성된다.
        """
        as_of = date(2026, 1, 1)
        receipts = self._receipts([
            ('S-LATE', date(2025, 12, 28), 100.0, date(2026, 1, 10)),  # as_of엔 아직 미도착
            ('S-FUTURE', date(2026, 1, 3), 60.0, pd.NaT),
        ])
        params = DepletionParams(reflect_receipts=True, overdue_cutoff=True)

        result = estimate_depletion(50.0, [25.0] * 14, receipts, as_of, params)

        assert result.days_to_stockout == 2  # 연체로 판정 → 미래 입고 차단

    def test_cutoff_is_inert_when_reflect_receipts_false(self):
        """reflect_receipts=False면 애초에 아무 입고도 반영하지 않으므로 스위치는 무의미하다."""
        as_of = date(2026, 1, 1)
        receipts = self._receipts([
            ('S-OVERDUE', date(2025, 12, 28), 100.0, pd.NaT),
            ('S-FUTURE', date(2026, 1, 3), 60.0, pd.NaT),
        ])

        off = estimate_depletion(
            50.0, [25.0] * 14, receipts, as_of,
            DepletionParams(reflect_receipts=False, overdue_cutoff=False),
        )
        on = estimate_depletion(
            50.0, [25.0] * 14, receipts, as_of,
            DepletionParams(reflect_receipts=False, overdue_cutoff=True),
        )

        assert off.days_to_stockout == on.days_to_stockout == 2


class TestEstimateDepletionValidation:
    """Tests for input validation."""

    def test_negative_forecast_raises_error(self):
        """Test that negative values in forecast raise ValueError."""
        as_of = date(2026, 1, 1)
        stock_on_hand = 100.0
        daily_forecast = [25.0, -5.0, 25.0]
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=False)

        with pytest.raises(ValueError, match="negative"):
            estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

    def test_empty_forecast_raises_error(self):
        """Test that empty forecast raises ValueError."""
        as_of = date(2026, 1, 1)
        stock_on_hand = 100.0
        daily_forecast: Sequence[float] = []
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=False)

        with pytest.raises(ValueError, match="empty"):
            estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

    def test_empty_receipts_allowed(self):
        """Test that empty receipts DataFrame is allowed."""
        as_of = date(2026, 1, 1)
        stock_on_hand = 100.0
        daily_forecast = [25.0, 25.0, 25.0, 25.0]
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=True)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 4
        assert result.depletion_date == date(2026, 1, 5)
        assert result.reflected_receipts == True


class TestEstimateDepletionDeterminism:
    """Tests for deterministic behavior."""

    def test_determinism(self):
        """Test that same input yields same output."""
        as_of = date(2026, 1, 1)
        stock_on_hand = 100.0
        daily_forecast = [25.0] * 14
        receipts = pd.DataFrame({
            'shipment_id': ['S1'],
            'expected_date': [date(2026, 1, 3)],
            'expected_qty': [60.0],
            'actual_date': [pd.NaT],
            'status': ['pending']
        })
        params = DepletionParams(reflect_receipts=True)

        result1 = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)
        result2 = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result1.days_to_stockout == result2.days_to_stockout
        assert result1.depletion_date == result2.depletion_date
        assert result1.stock_on_hand == result2.stock_on_hand
        assert result1.reflected_receipts == result2.reflected_receipts

    def test_frozen_dataclass(self):
        """Test that DepletionEstimate is frozen (immutable)."""
        as_of = date(2026, 1, 1)
        stock_on_hand = 100.0
        daily_forecast = [25.0] * 14
        receipts = pd.DataFrame()
        params = DepletionParams(reflect_receipts=False)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        with pytest.raises(Exception):  # FrozenInstanceError
            result.days_to_stockout = 999
