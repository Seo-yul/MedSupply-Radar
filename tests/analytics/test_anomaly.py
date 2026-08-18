"""Tests for medsupply.analytics.anomaly module."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from medsupply.analytics.anomaly import detect_receipt_delay, detect_usage_anomalies
from medsupply.analytics.params import AnomalyParams
from medsupply.analytics.types import AnomalyFlag


@pytest.fixture
def default_params() -> AnomalyParams:
    """Default anomaly detection parameters."""
    return AnomalyParams(
        surge_ratio=0.35,
        drop_ratio=0.25,
        recent_window=7,
        baseline_window=28,
        receipt_delay_days=3,
    )


class TestDetectUsageAnomalies:
    """Tests for detect_usage_anomalies function."""

    def test_usage_surge(self, default_params):
        """Detect usage surge: recent 7-day avg 25, baseline 28-day avg 18."""
        # Create 35 days of data: baseline 28 days avg 18, recent 7 days avg 25
        baseline_data = [18.0] * 28
        recent_data = [25.0] * 7
        values = baseline_data + recent_data

        dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(35)]
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 4)
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 1
        anomaly = anomalies[0]
        assert anomaly.kind == "usage_surge"
        assert anomaly.detected_on == dates[-1]  # Last date in usage series
        assert anomaly.metric == pytest.approx(0.3889, abs=0.0001)
        assert "39%" in anomaly.detail

    def test_usage_drop(self, default_params):
        """Detect usage drop: recent 7-day avg 18, baseline 28-day avg 25."""
        baseline_data = [25.0] * 28
        recent_data = [18.0] * 7
        values = baseline_data + recent_data

        dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(35)]
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 4)
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 1
        anomaly = anomalies[0]
        assert anomaly.kind == "usage_drop"
        assert anomaly.metric == pytest.approx(-0.28, abs=0.0001)
        assert "28%" in anomaly.detail

    def test_boundary_surge_ratio_exact(self, default_params):
        """Detect when change equals surge_ratio exactly."""
        # change = 0.35 (surge_ratio)
        # (recent - baseline) / baseline = 0.35
        # recent = baseline * (1 + 0.35) = baseline * 1.35
        baseline_data = [20.0] * 28
        recent_data = [27.0] * 7  # 27 / 20 = 1.35, change = 0.35
        values = baseline_data + recent_data

        dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(35)]
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 4)
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 1
        anomaly = anomalies[0]
        assert anomaly.kind == "usage_surge"
        assert anomaly.metric == pytest.approx(0.35, abs=0.0001)

    def test_insufficient_data(self, default_params):
        """Return empty list when data is less than (recent_window + baseline_window) days."""
        # Only 34 days, need 35
        dates = [date(2026, 7, 2) + timedelta(days=i) for i in range(34)]
        values = [20.0] * 34
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 4)
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 0

    def test_baseline_zero_recent_positive(self, default_params):
        """When baseline is 0 and recent > 0, detect surge with metric=1.0."""
        baseline_data = [0.0] * 28
        recent_data = [10.0] * 7
        values = baseline_data + recent_data

        dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(35)]
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 4)
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 1
        anomaly = anomalies[0]
        assert anomaly.kind == "usage_surge"
        assert anomaly.metric == 1.0
        assert "기준 구간 사용량 0" in anomaly.detail

    def test_baseline_zero_recent_zero(self, default_params):
        """When baseline and recent are both 0, return empty list."""
        dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(35)]
        values = [0.0] * 35
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 4)
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 0

    def test_lookahead_prevention(self, default_params):
        """Raise ValueError when usage contains data beyond as_of."""
        dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(36)]
        values = [20.0] * 36
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 4)  # But data goes to 2026-08-05

        with pytest.raises(ValueError, match="usage beyond as_of"):
            detect_usage_anomalies(usage, as_of, default_params)

    def test_no_surge_no_drop(self, default_params):
        """When change is between -drop_ratio and surge_ratio, return empty list."""
        # change = 0.1 (neither surge nor drop)
        baseline_data = [20.0] * 28
        recent_data = [22.0] * 7  # (22-20)/20 = 0.1
        values = baseline_data + recent_data

        dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(35)]
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 4)
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 0

    def test_detected_on_is_last_usage_date(self, default_params):
        """detected_on should be the last date in usage series, not as_of."""
        dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(35)]
        baseline_data = [18.0] * 28
        recent_data = [25.0] * 7
        values = baseline_data + recent_data
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 10)  # Different from last date
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 1
        assert anomalies[0].detected_on == dates[-1]
        assert anomalies[0].detected_on != as_of

    def test_metric_rounded_four_decimals(self, default_params):
        """metric should be rounded to 4 decimal places."""
        # Create data where change is irrational and would produce many decimals
        # (100 * 1.3667 - 100) / 100 = 0.36666... -> rounds to 0.3667
        baseline_data = [100.0] * 28
        recent_data = [136.67] * 7  # (136.67-100)/100 = 0.36666...
        values = baseline_data + recent_data

        dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(35)]
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 4)
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 1
        # Should be rounded to 4 decimals
        assert anomalies[0].metric == 0.3667

    def test_empty_series(self, default_params):
        """Empty series returns empty list."""
        usage = pd.Series([], dtype=float)
        as_of = date(2026, 8, 4)
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 0

    def test_metric_no_cap_for_normal_case(self, default_params):
        """Metric should not be capped at 1.0 for baseline != 0 case (regression test)."""
        # baseline 20, recent 50 => change = (50-20)/20 = 1.5 (150% increase)
        baseline_data = [20.0] * 28
        recent_data = [50.0] * 7
        values = baseline_data + recent_data

        dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(35)]
        usage = pd.Series(values, index=dates)

        as_of = date(2026, 8, 4)
        anomalies = detect_usage_anomalies(usage, as_of, default_params)

        assert len(anomalies) == 1
        anomaly = anomalies[0]
        assert anomaly.kind == "usage_surge"
        # Metric should be 1.5, not capped to 1.0
        assert anomaly.metric == 1.5
        assert "150%" in anomaly.detail


class TestDetectReceiptDelay:
    """Tests for detect_receipt_delay function."""

    def test_receipt_delay_detected(self, default_params):
        """Detect receipt delay when expected_date < as_of and actual_date is NULL."""
        receipts = pd.DataFrame({
            "shipment_id": ["S001"],
            "expected_date": ["2026-07-27"],
            "expected_qty": [300.0],
            "actual_date": [None],
            "status": ["예정"],
        })

        as_of = date(2026, 8, 1)
        anomalies = detect_receipt_delay(receipts, as_of, default_params)

        assert len(anomalies) == 1
        anomaly = anomalies[0]
        assert anomaly.kind == "receipt_delay"
        assert anomaly.detected_on == as_of
        assert anomaly.metric == 5.0  # (2026-08-01 - 2026-07-27).days = 5
        assert "2026-07-27" in anomaly.detail
        assert "5" in anomaly.detail
        assert "300" in anomaly.detail

    def test_receipt_delay_below_threshold(self, default_params):
        """Do not detect delay when delay_days < receipt_delay_days."""
        receipts = pd.DataFrame({
            "shipment_id": ["S001"],
            "expected_date": ["2026-07-30"],
            "expected_qty": [300.0],
            "actual_date": [None],
            "status": ["예정"],
        })

        as_of = date(2026, 8, 1)  # Only 2 days delay, threshold is 3
        anomalies = detect_receipt_delay(receipts, as_of, default_params)

        assert len(anomalies) == 0

    def test_receipt_already_arrived(self, default_params):
        """Do not detect delay if actual_date exists, regardless of delay."""
        receipts = pd.DataFrame({
            "shipment_id": ["S001"],
            "expected_date": ["2026-07-25"],
            "expected_qty": [300.0],
            "actual_date": ["2026-08-05"],
            "status": ["도착"],
        })

        as_of = date(2026, 8, 10)
        anomalies = detect_receipt_delay(receipts, as_of, default_params)

        assert len(anomalies) == 0

    def test_receipt_expected_after_as_of(self, default_params):
        """Do not detect delay if expected_date >= as_of."""
        receipts = pd.DataFrame({
            "shipment_id": ["S001"],
            "expected_date": ["2026-08-05"],
            "expected_qty": [300.0],
            "actual_date": [None],
            "status": ["예정"],
        })

        as_of = date(2026, 8, 1)  # Expected is after as_of
        anomalies = detect_receipt_delay(receipts, as_of, default_params)

        assert len(anomalies) == 0

    def test_empty_receipts(self, default_params):
        """Empty DataFrame returns empty list."""
        receipts = pd.DataFrame({
            "shipment_id": [],
            "expected_date": [],
            "expected_qty": [],
            "actual_date": [],
            "status": [],
        })

        as_of = date(2026, 8, 1)
        anomalies = detect_receipt_delay(receipts, as_of, default_params)

        assert len(anomalies) == 0

    def test_sorting_by_delay_descending(self, default_params):
        """Sort results by delay_days descending, then by shipment_id ascending."""
        receipts = pd.DataFrame({
            "shipment_id": ["S003", "S001", "S002"],
            "expected_date": ["2026-07-20", "2026-07-25", "2026-07-28"],
            "expected_qty": [100.0, 300.0, 200.0],
            "actual_date": [None, None, None],
            "status": ["예정", "예정", "예정"],
        })

        as_of = date(2026, 8, 1)
        anomalies = detect_receipt_delay(receipts, as_of, default_params)

        # Expected delays: S003=12days, S001=7days, S002=4days
        assert len(anomalies) == 3
        assert anomalies[0].metric == 12.0  # S003
        assert anomalies[1].metric == 7.0   # S001
        assert anomalies[2].metric == 4.0   # S002

    def test_sorting_same_delay_by_shipment_id(self, default_params):
        """When delays are equal, sort by shipment_id ascending."""
        receipts = pd.DataFrame({
            "shipment_id": ["S002", "S001"],
            "expected_date": ["2026-07-25", "2026-07-25"],
            "expected_qty": [200.0, 100.0],
            "actual_date": [None, None],
            "status": ["예정", "예정"],
        })

        as_of = date(2026, 8, 1)
        anomalies = detect_receipt_delay(receipts, as_of, default_params)

        assert len(anomalies) == 2
        assert anomalies[0].metric == 7.0
        assert anomalies[1].metric == 7.0
        # Verify sorting by shipment_id ascending via expected_qty in detail string
        # anomalies[0] should be S001 with expected_qty=100
        # anomalies[1] should be S002 with expected_qty=200
        assert "100" in anomalies[0].detail
        assert "200" in anomalies[1].detail

    def test_iso_string_date_parsing(self, default_params):
        """Handle dates as ISO strings in DataFrame."""
        receipts = pd.DataFrame({
            "shipment_id": ["S001"],
            "expected_date": ["2026-07-27"],  # ISO string
            "expected_qty": [300.0],
            "actual_date": [None],
            "status": ["예정"],
        })

        as_of = date(2026, 8, 1)
        anomalies = detect_receipt_delay(receipts, as_of, default_params)

        assert len(anomalies) == 1
        assert anomalies[0].metric == 5.0

    def test_determinism(self, default_params):
        """Same input produces same output across multiple calls."""
        receipts = pd.DataFrame({
            "shipment_id": ["S003", "S001", "S002"],
            "expected_date": ["2026-07-20", "2026-07-25", "2026-07-28"],
            "expected_qty": [100.0, 300.0, 200.0],
            "actual_date": [None, None, None],
            "status": ["예정", "예정", "예정"],
        })

        as_of = date(2026, 8, 1)

        result1 = detect_receipt_delay(receipts, as_of, default_params)
        result2 = detect_receipt_delay(receipts, as_of, default_params)

        assert result1 == result2

    def test_multiple_delays_with_mixed_conditions(self, default_params):
        """Complex scenario with multiple delays and mixed statuses."""
        receipts = pd.DataFrame({
            "shipment_id": ["S001", "S002", "S003", "S004", "S005"],
            "expected_date": ["2026-07-25", "2026-07-20", "2026-07-30", "2026-07-31", "2026-08-05"],
            "expected_qty": [100.0, 200.0, 150.0, 250.0, 300.0],
            "actual_date": ["2026-08-01", None, None, None, None],
            "status": ["도착", "예정", "예정", "예정", "예정"],
        })

        as_of = date(2026, 8, 1)
        anomalies = detect_receipt_delay(receipts, as_of, default_params)

        # S001: arrived, skip
        # S002: 12 days delay (threshold 3) -> detect
        # S003: 2 days delay (threshold 3) -> skip
        # S004: 1 day delay (threshold 3) -> skip
        # S005: future expected date -> skip
        assert len(anomalies) == 1
        assert anomalies[0].metric == 12.0

    def test_boundary_delay_exactly_threshold(self, default_params):
        """Detect when delay_days exactly equals receipt_delay_days."""
        receipts = pd.DataFrame({
            "shipment_id": ["S001"],
            "expected_date": ["2026-07-29"],
            "expected_qty": [300.0],
            "actual_date": [None],
            "status": ["예정"],
        })

        as_of = date(2026, 8, 1)  # Exactly 3 days delay
        anomalies = detect_receipt_delay(receipts, as_of, default_params)

        assert len(anomalies) == 1
        assert anomalies[0].metric == 3.0
