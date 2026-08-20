"""Pure function for stock depletion estimation."""

from __future__ import annotations

from datetime import date, timedelta
from collections.abc import Sequence

import pandas as pd

from medsupply.analytics.asof import is_overdue_at, is_pending_at
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
        - Receipts (reflect_receipts=True일 때만): **as_of 시점 기준으로 pending을 재구성해서**
          반영한다(Task S-17c 정합성 수정).

              expected_date > as_of AND (actual_date IS NULL OR actual_date > as_of)

          `actual_date > as_of`를 pending으로 되살리는 것이 핵심이다. actual_date(도착 스탬프)는
          as_of 시점에는 **아직 존재하지 않는 미래 정보**인데, 예전 구현은 `actual_date IS NULL`만
          pending으로 쳐서 "나중에 도착했다"는 사실로 과거 시점의 입고 예정을 소급 제외했다.
          표준 스냅샷처럼 도착분에 actual_date가 채워진 데이터에서는 백테스트 시 임박한 정상
          입고가 통째로 무시돼, 정상 품목이 재발주 저점마다 허위 '경고'로 잡혔다.

          반영하지 않는 것(의도된 설계):
            * as_of 이전 도착분(actual_date <= as_of) — 이미 closing_stock에 반영돼 있어
              다시 더하면 이중 계상이 된다.
            * 연체 건(expected_date <= as_of이고 as_of 시점 미도착) — 지연 자체의 감지·판단은
              anomaly.detect_receipt_delay의 소관이다.

        - overdue_cutoff=True(선택 스위치)이면 위 pending을 계산하기 전에 **연체 건이 1건이라도
          있는지** 보고, 있으면 그 품목의 미래 예정 입고를 **전부** 반영하지 않는다. 공급 신뢰가
          무너진 품목에서 "예정일은 잡혀 있으나 실제로 올지 알 수 없는" 입고를 낙관적으로 세지
          않기 위한 보수적 전환이다. reflect_receipts=False이면 이 스위치는 무의미하다
          (애초에 아무 입고도 반영하지 않는다).
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

            # as_of 시점 상태 재구성(medsupply.analytics.asof가 규칙의 단일 소스).
            expected = receipts_copy["expected_date"]
            actual = receipts_copy["actual_date"]

            if params.overdue_cutoff and any(
                is_overdue_at(e, a, as_of) for e, a in zip(expected, actual)
            ):
                # 연체가 1건이라도 있으면 이 품목의 미래 예정 입고를 전부 미반영한다.
                pending = pd.Series(False, index=receipts_copy.index)
            else:
                pending = pd.Series(
                    [is_pending_at(e, a, as_of) for e, a in zip(expected, actual)],
                    index=receipts_copy.index,
                )

            undelivered = receipts_copy[pending]

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
