"""Tests for SMA/SES demand forecast functions."""

import pytest
import pandas as pd
from medsupply.analytics.forecast import sma_forecast, ses_forecast
from medsupply.analytics.types import ForecastResult


class TestSMAForecast:
    """Tests for sma_forecast function."""

    def test_basic_sma(self):
        """Test SMA with usage=[10,20,30], window=2, horizon=3.

        Expected: last 2 values are [20,30], average=25.0
        """
        usage = pd.Series([10.0, 20.0, 30.0])
        result = sma_forecast(usage, window=2, horizon=3)

        assert result.method == "sma"
        assert result.horizon_days == 3
        assert result.avg_daily == 25.0
        assert result.daily == (25.0, 25.0, 25.0)
        assert result.total == 75.0

    def test_sma_window_larger_than_length(self):
        """Test SMA when window > len(usage).

        Expected: average of all available values
        usage=[10,20], window=28 → avg_daily=15.0
        """
        usage = pd.Series([10.0, 20.0])
        result = sma_forecast(usage, window=28, horizon=2)

        assert result.method == "sma"
        assert result.avg_daily == 15.0
        assert result.daily == (15.0, 15.0)
        assert result.total == 30.0

    def test_sma_single_value(self):
        """Test SMA with single value."""
        usage = pd.Series([42.0])
        result = sma_forecast(usage, window=5, horizon=2)

        assert result.avg_daily == 42.0
        assert result.daily == (42.0, 42.0)
        assert result.total == 84.0

    def test_sma_empty_series_raises_error(self):
        """Test that empty series raises ValueError."""
        usage = pd.Series([], dtype=float)
        with pytest.raises(ValueError, match="usage series is empty"):
            sma_forecast(usage, window=2, horizon=3)

    def test_sma_negative_values_raises_error(self):
        """Test that negative values raise ValueError."""
        usage = pd.Series([10.0, -5.0, 20.0])
        with pytest.raises(ValueError, match="usage must be non-negative"):
            sma_forecast(usage, window=2, horizon=3)

    def test_sma_horizon_zero_raises_error(self):
        """Test that horizon < 1 raises ValueError."""
        usage = pd.Series([10.0, 20.0])
        with pytest.raises(ValueError):
            sma_forecast(usage, window=2, horizon=0)

    def test_sma_determinism(self):
        """Test that same input yields same output."""
        usage = pd.Series([10.0, 20.0, 30.0])
        result1 = sma_forecast(usage, window=2, horizon=3)
        result2 = sma_forecast(usage, window=2, horizon=3)

        assert result1.avg_daily == result2.avg_daily
        assert result1.daily == result2.daily
        assert result1.total == result2.total

    def test_sma_frozen_dataclass(self):
        """Test that ForecastResult is frozen (immutable)."""
        usage = pd.Series([10.0, 20.0, 30.0])
        result = sma_forecast(usage, window=2, horizon=3)

        with pytest.raises(Exception):  # FrozenInstanceError
            result.avg_daily = 999.0


class TestSESForecast:
    """Tests for ses_forecast function."""

    def test_basic_ses_two_values(self):
        """Test SES with usage=[10,20], alpha=0.5.

        Expected: level_0=10, level_1=0.5*20 + 0.5*10 = 15
        """
        usage = pd.Series([10.0, 20.0])
        result = ses_forecast(usage, alpha=0.5, horizon=2)

        assert result.method == "ses"
        assert result.horizon_days == 2
        assert result.avg_daily == 15.0
        assert result.daily == (15.0, 15.0)
        assert result.total == 30.0

    def test_ses_three_values(self):
        """Test SES with usage=[10,20,30], alpha=0.5.

        Expected: level_0=10, level_1=0.5*20+0.5*10=15, level_2=0.5*30+0.5*15=22.5
        """
        usage = pd.Series([10.0, 20.0, 30.0])
        result = ses_forecast(usage, alpha=0.5, horizon=3)

        assert result.method == "ses"
        assert result.horizon_days == 3
        assert result.avg_daily == 22.5
        assert result.daily == (22.5, 22.5, 22.5)
        assert result.total == 67.5

    def test_ses_alpha_one(self):
        """Test SES with alpha=1.0 (should return last observation)."""
        usage = pd.Series([10.0, 20.0, 30.0])
        result = ses_forecast(usage, alpha=1.0, horizon=3)

        # With alpha=1.0: level_t = 1.0*y_t + 0*level_{t-1} = y_t (always last value)
        assert result.avg_daily == 30.0
        assert result.daily == (30.0, 30.0, 30.0)
        assert result.total == 90.0

    def test_ses_alpha_small(self):
        """Test SES with small alpha (heavy smoothing)."""
        usage = pd.Series([10.0, 20.0])
        result = ses_forecast(usage, alpha=0.1, horizon=2)

        # level_0=10, level_1=0.1*20 + 0.9*10 = 2 + 9 = 11
        assert result.avg_daily == 11.0
        assert result.daily == (11.0, 11.0)

    def test_ses_single_value(self):
        """Test SES with single value."""
        usage = pd.Series([42.0])
        result = ses_forecast(usage, alpha=0.5, horizon=2)

        # level_0=42, no updates since only one value
        assert result.avg_daily == 42.0
        assert result.daily == (42.0, 42.0)

    def test_ses_empty_series_raises_error(self):
        """Test that empty series raises ValueError."""
        usage = pd.Series([], dtype=float)
        with pytest.raises(ValueError, match="usage series is empty"):
            ses_forecast(usage, alpha=0.5, horizon=3)

    def test_ses_negative_values_raises_error(self):
        """Test that negative values raise ValueError."""
        usage = pd.Series([10.0, -5.0, 20.0])
        with pytest.raises(ValueError, match="usage must be non-negative"):
            ses_forecast(usage, alpha=0.5, horizon=3)

    def test_ses_alpha_zero_raises_error(self):
        """Test that alpha=0 raises ValueError."""
        usage = pd.Series([10.0, 20.0])
        with pytest.raises(ValueError):
            ses_forecast(usage, alpha=0.0, horizon=3)

    def test_ses_alpha_greater_than_one_raises_error(self):
        """Test that alpha > 1 raises ValueError."""
        usage = pd.Series([10.0, 20.0])
        with pytest.raises(ValueError):
            ses_forecast(usage, alpha=1.5, horizon=3)

    def test_ses_horizon_zero_raises_error(self):
        """Test that horizon < 1 raises ValueError."""
        usage = pd.Series([10.0, 20.0])
        with pytest.raises(ValueError):
            ses_forecast(usage, alpha=0.5, horizon=0)

    def test_ses_determinism(self):
        """Test that same input yields same output."""
        usage = pd.Series([10.0, 20.0, 30.0])
        result1 = ses_forecast(usage, alpha=0.5, horizon=3)
        result2 = ses_forecast(usage, alpha=0.5, horizon=3)

        assert result1.avg_daily == result2.avg_daily
        assert result1.daily == result2.daily
        assert result1.total == result2.total

    def test_ses_frozen_dataclass(self):
        """Test that ForecastResult is frozen (immutable)."""
        usage = pd.Series([10.0, 20.0])
        result = ses_forecast(usage, alpha=0.5, horizon=2)

        with pytest.raises(Exception):  # FrozenInstanceError
            result.avg_daily = 999.0
