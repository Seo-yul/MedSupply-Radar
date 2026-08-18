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

    def test_ignore_receipt_with_actual_date(self):
        """Test that receipts with actual_date are not reflected.

        Stock=50, forecast=[25]*14, 입고 60 (이미 받음 - actual_date != NULL)
        Expected: days=2 (입고 무시)
        """
        as_of = date(2026, 1, 1)
        stock_on_hand = 50.0
        daily_forecast = [25.0] * 14
        receipts = pd.DataFrame({
            'shipment_id': ['S1'],
            'expected_date': [date(2026, 1, 3)],
            'expected_qty': [60.0],
            'actual_date': [date(2026, 1, 2)],  # Already received
            'status': ['received']
        })
        params = DepletionParams(reflect_receipts=True)

        result = estimate_depletion(stock_on_hand, daily_forecast, receipts, as_of, params)

        assert result.days_to_stockout == 2
        assert result.depletion_date == date(2026, 1, 3)
        assert result.reflected_receipts == True

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
