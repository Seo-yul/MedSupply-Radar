"""SMA/SES demand forecast functions."""

from __future__ import annotations

import pandas as pd
from medsupply.analytics.types import ForecastResult


def sma_forecast(usage: pd.Series, window: int, horizon: int) -> ForecastResult:
    """Simple Moving Average forecast.

    Args:
        usage: Daily usage time series (ascending date index, no gaps).
        window: Window size for moving average (int >= 1).
        horizon: Number of forecast days (int >= 1).

    Returns:
        ForecastResult with SMA forecast.

    Raises:
        ValueError: If usage is empty, contains negative values, or horizon < 1.
    """
    # Validate horizon
    if horizon < 1:
        raise ValueError(f"horizon must be >= 1. Got {horizon}")

    if window < 1:
        raise ValueError(f"window must be >= 1. Got {window}")

    # Validate usage series
    if len(usage) == 0:
        raise ValueError("usage series is empty")

    if (usage < 0).any():
        raise ValueError("usage must be non-negative")

    # Calculate average of last window values (or all if fewer than window)
    effective_window = min(window, len(usage))
    avg_daily = float(usage.iloc[-effective_window:].mean())

    # Create forecast tuple (all same value)
    daily = tuple([avg_daily] * horizon)

    # Calculate total
    total = avg_daily * horizon

    return ForecastResult(
        method="sma",
        horizon_days=horizon,
        daily=daily,
        avg_daily=avg_daily,
        total=total,
    )


def ses_forecast(usage: pd.Series, alpha: float, horizon: int) -> ForecastResult:
    """Simple Exponential Smoothing forecast.

    Args:
        usage: Daily usage time series (ascending date index, no gaps).
        alpha: Smoothing parameter (0 < alpha <= 1).
        horizon: Number of forecast days (int >= 1).

    Returns:
        ForecastResult with SES forecast.

    Raises:
        ValueError: If usage is empty, contains negative values,
                   alpha not in (0, 1], or horizon < 1.
    """
    # Validate horizon
    if horizon < 1:
        raise ValueError(f"horizon must be >= 1. Got {horizon}")

    # Validate alpha
    if not (0 < alpha <= 1):
        raise ValueError(f"alpha must satisfy 0 < alpha <= 1. Got {alpha}")

    # Validate usage series
    if len(usage) == 0:
        raise ValueError("usage series is empty")

    if (usage < 0).any():
        raise ValueError("usage must be non-negative")

    # Calculate SES level
    level = float(usage.iloc[0])  # level_0
    for i in range(1, len(usage)):
        level = alpha * float(usage.iloc[i]) + (1 - alpha) * level

    avg_daily = level

    # Create forecast tuple (all same value)
    daily = tuple([avg_daily] * horizon)

    # Calculate total
    total = avg_daily * horizon

    return ForecastResult(
        method="ses",
        horizon_days=horizon,
        daily=daily,
        avg_daily=avg_daily,
        total=total,
    )
