"""Pure functions for anomaly detection in usage and receipt patterns."""

from __future__ import annotations

from datetime import date

import pandas as pd

from medsupply.analytics.asof import is_overdue_at
from medsupply.analytics.params import AnomalyParams
from medsupply.analytics.types import AnomalyFlag


def detect_usage_anomalies(usage: pd.Series, as_of: date, params: AnomalyParams) -> list[AnomalyFlag]:
    """Detect usage surge or drop anomalies.

    Args:
        usage: Daily usage time series with date index (ascending order).
               Index must be date type, values must be non-negative.
        as_of: Reference date. Raises ValueError if usage contains dates > as_of.
        params: AnomalyParams with surge_ratio, drop_ratio, recent_window, baseline_window.

    Returns:
        List of AnomalyFlag instances for detected surge/drop anomalies.
        Empty list if insufficient data or no anomaly detected.

    Raises:
        ValueError: If usage contains data with dates > as_of (lookahead guard).
    """
    # Check for lookahead: no data beyond as_of
    if len(usage) > 0 and usage.index[-1] > as_of:
        raise ValueError("usage beyond as_of")

    # Need at least (recent_window + baseline_window) days of data
    min_required = params.recent_window + params.baseline_window
    if len(usage) < min_required:
        return []

    # Calculate recent and baseline averages
    recent_window_end = len(usage)
    recent_window_start = recent_window_end - params.recent_window
    baseline_window_end = recent_window_start
    baseline_window_start = baseline_window_end - params.baseline_window

    recent_avg = float(usage.iloc[recent_window_start:recent_window_end].mean())
    baseline_avg = float(usage.iloc[baseline_window_start:baseline_window_end].mean())

    # Handle baseline == 0 case
    if baseline_avg == 0:
        if recent_avg > 0:
            # Surge detected with baseline 0
            anomaly = AnomalyFlag(
                kind="usage_surge",
                detected_on=usage.index[-1],
                metric=1.0,
                detail=f"최근 {params.recent_window}일 평균 {recent_avg:.1f}가 기준 구간 사용량 0 대비 증가 (기준 구간 사용량 0)",
            )
            return [anomaly]
        else:
            # Both are 0, no anomaly
            return []

    # Calculate change rate
    change = (recent_avg - baseline_avg) / baseline_avg

    # Determine anomaly type
    if change >= params.surge_ratio:
        anomaly_kind = "usage_surge"
    elif change <= -params.drop_ratio:
        anomaly_kind = "usage_drop"
    else:
        return []

    # Round metric to 4 decimal places
    metric = round(change, 4)

    # Build detail string
    change_percent = round(abs(change) * 100)
    detail = (
        f"최근 {params.recent_window}일 평균 {recent_avg:.1f}가 "
        f"기준 {params.baseline_window}일 평균 {baseline_avg:.1f} 대비 {change_percent}% "
        f"{'증가' if change > 0 else '감소'}"
    )

    anomaly = AnomalyFlag(
        kind=anomaly_kind,
        detected_on=usage.index[-1],
        metric=metric,
        detail=detail,
    )

    return [anomaly]


def detect_receipt_delay(receipts: pd.DataFrame, as_of: date, params: AnomalyParams) -> list[AnomalyFlag]:
    """Detect receipt delays on incoming shipments.

    Args:
        receipts: DataFrame with columns shipment_id, expected_date, expected_qty,
                  actual_date, status. Dates can be ISO strings or date objects.
        as_of: Reference date for calculating delay (as_of - expected_date).
        params: AnomalyParams with receipt_delay_days threshold.

    Returns:
        List of AnomalyFlag instances for delayed receipts.
        Sorted by delay_days (descending), then by shipment_id (ascending).
        Empty list if no delays detected or DataFrame is empty.

    Notes:
        - 연체 판정은 **as_of 시점 기준으로 재구성**한다(Task S-17d, medsupply.analytics.asof):

              expected_date <= as_of AND (actual_date IS NULL OR actual_date > as_of)

          구 구현은 미도착을 ``actual_date IS NULL``로만 봐서, as_of 시점엔 분명 연체였지만
          **나중에** 도착한 건을 놓쳤다 — 도착 스탬프라는 미래 정보로 과거 시점의 상태를
          소급 왜곡한 것이다(depletion.estimate_depletion이 S-17c에서 같은 이유로 고쳐졌다).
        - as_of 시점에 이미 도착한 건(actual_date <= as_of)은 검사하지 않는다.
        - delay_days = (as_of - expected_date).days 이고 receipt_delay_days 이상일 때만 신호가
          된다. 그래서 expected_date == as_of(delay_days=0)는 임계값이 1 이상인 한 자연히
          걸러진다 — 술어를 ``<=``로 적어도 실질 동작은 종전과 같다.
        - 이 함수는 **등급 판정에 관여하지 않는다**. 산출된 AnomalyFlag는 risk_type 유도와
          점수 가점에만 쓰이므로(grade_risk는 days_to_stockout·공고만 본다), 이 수정은
          감지율·오탐률·선행일수를 바꾸지 않아야 한다.
    """
    if receipts.empty:
        return []

    # Parse dates if they are strings
    receipts = receipts.copy()
    for col in ["expected_date", "actual_date"]:
        if col in receipts.columns:
            receipts[col] = pd.to_datetime(receipts[col], errors="coerce").dt.date

    # as_of 시점 연체 건만 남긴다(asof 모듈이 규칙의 단일 소스).
    overdue_mask = pd.Series(
        [
            is_overdue_at(expected, actual, as_of)
            for expected, actual in zip(receipts["expected_date"], receipts["actual_date"])
        ],
        index=receipts.index,
    )
    delayed_receipts = receipts[overdue_mask].copy()

    if delayed_receipts.empty:
        return []

    # Calculate delay days
    delayed_receipts["delay_days"] = delayed_receipts["expected_date"].apply(
        lambda d: (as_of - d).days if d is not None else 0
    )

    # Filter by threshold
    delayed_receipts = delayed_receipts[
        delayed_receipts["delay_days"] >= params.receipt_delay_days
    ]

    if delayed_receipts.empty:
        return []

    # Sort by delay_days descending, then by shipment_id ascending
    delayed_receipts = delayed_receipts.sort_values(
        by=["delay_days", "shipment_id"], ascending=[False, True]
    )

    # Create AnomalyFlag instances
    anomalies = []
    for _, row in delayed_receipts.iterrows():
        detail = (
            f"입고 예정 {row['expected_date'].isoformat()} 대비 "
            f"{int(row['delay_days'])}일 지연 (예정 수량 {int(row['expected_qty'])})"
        )
        anomaly = AnomalyFlag(
            kind="receipt_delay",
            detected_on=as_of,
            metric=float(row["delay_days"]),
            detail=detail,
        )
        anomalies.append(anomaly)

    return anomalies
