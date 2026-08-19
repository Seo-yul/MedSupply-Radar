"""Pure function for stock depletion estimation."""

from __future__ import annotations

from datetime import date, timedelta
from collections.abc import Sequence

import pandas as pd

from medsupply.analytics.params import DepletionParams
from medsupply.analytics.types import DepletionEstimate


def estimate_depletion(
    stock_on_hand: float,
    daily_forecast: Sequence[float],
    receipts: pd.DataFrame,
    as_of: date,
    params: DepletionParams,
) -> DepletionEstimate:
    """Estimate stock depletion date based on current stock and forecasted demand.

    Args:
        stock_on_hand: Current stock quantity (float).
        daily_forecast: Daily demand forecast sequence (non-negative values, non-empty).
        receipts: DataFrame with incoming shipments.
                  Columns: shipment_id, expected_date, expected_qty, actual_date, status.
                  Dates can be ISO strings or date objects.
        as_of: Reference date. Simulation starts from as_of + 1 day.
        params: DepletionParams with reflect_receipts flag.

    Returns:
        DepletionEstimate with days_to_stockout, depletion_date, and metadata.

    Raises:
        ValueError: If daily_forecast is empty, contains negative values.

    Notes:
        - Simulation: day 1 = as_of + 1 day.
        - Stock depletes when remaining <= 0.
        - If horizon not sufficient, extends with average demand up to 365 days.
        - If average demand is 0 or no depletion within 365 days: days_to_stockout=None.
        - If stock_on_hand <= 0: days_to_stockout=0, depletion_date=as_of (already depleted).
        - Receipts: if reflect_receipts=True, includes undelivered (actual_date NULL)
          shipments with expected_date > as_of at their scheduled date.
          already received (actual_date != NULL) or delayed (expected_date <= as_of)
          shipments are not reflected.
          지연 입고(expected_date <= as_of)를 소진일 추정에 반영하지 않는 것은 의도된
          설계다 — 지연 자체의 감지·판단은 anomaly.detect_receipt_delay의 소관이다.
    """
    # Validate daily_forecast
    if len(daily_forecast) == 0:
        raise ValueError("daily_forecast is empty")

    if any(d < 0 for d in daily_forecast):
        raise ValueError("daily_forecast contains negative values")

    # Check if already depleted
    if stock_on_hand <= 0:
        return DepletionEstimate(
            days_to_stockout=0,
            depletion_date=as_of,
            stock_on_hand=stock_on_hand,
            reflected_receipts=params.reflect_receipts,
        )

    # Prepare receipts for simulation (if reflect_receipts=True)
    receipt_map: dict[date, float] = {}
    if params.reflect_receipts:
        if not receipts.empty:
            # Parse dates if they are strings
            receipts_copy = receipts.copy()
            for col in ["expected_date", "actual_date"]:
                if col in receipts_copy.columns:
                    receipts_copy[col] = pd.to_datetime(receipts_copy[col], errors="coerce").dt.date

            # Filter for undelivered (actual_date NULL) with expected_date > as_of
            undelivered = receipts_copy[
                (receipts_copy["actual_date"].isna()) & (receipts_copy["expected_date"] > as_of)
            ]

            if not undelivered.empty:
                # Build a map: expected_date -> sum of expected_qty
                for exp_date in undelivered["expected_date"].unique():
                    qty = undelivered[undelivered["expected_date"] == exp_date][
                        "expected_qty"
                    ].sum()
                    receipt_map[exp_date] = qty

    # Calculate average daily demand (for horizon extension)
    avg_daily = sum(daily_forecast) / len(daily_forecast)

    # Handle zero average demand (no depletion)
    if avg_daily == 0:
        return DepletionEstimate(
            days_to_stockout=None,
            depletion_date=None,
            stock_on_hand=stock_on_hand,
            reflected_receipts=params.reflect_receipts,
        )

    # Simulate stock depletion
    remaining = stock_on_hand
    day = 0
    max_days = 365

    while day < max_days:
        day += 1
        current_date = as_of + timedelta(days=day)

        # Add receipts on this day (before demand deduction)
        if current_date in receipt_map:
            remaining += receipt_map[current_date]

        # Get demand for this day
        if day <= len(daily_forecast):
            demand = daily_forecast[day - 1]
        else:
            demand = avg_daily

        # Deduct demand
        remaining -= demand

        # Check if depleted
        if remaining <= 0:
            return DepletionEstimate(
                days_to_stockout=day,
                depletion_date=current_date,
                stock_on_hand=stock_on_hand,
                reflected_receipts=params.reflect_receipts,
            )

    # No depletion within 365 days
    return DepletionEstimate(
        days_to_stockout=None,
        depletion_date=None,
        stock_on_hand=stock_on_hand,
        reflected_receipts=params.reflect_receipts,
    )
