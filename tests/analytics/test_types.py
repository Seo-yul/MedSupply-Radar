"""Test analytics types and evidence contract."""
import json
from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from medsupply.analytics import (
    AnomalyFlag,
    DepletionEstimate,
    ForecastResult,
    GRADE_ORDER,
    GradeDecision,
    ItemInputs,
    RiskAssessment,
    RiskGrade,
)


class TestRiskGrade:
    """Test RiskGrade enum."""

    def test_risk_grade_values(self):
        """Test RiskGrade enum values."""
        assert RiskGrade.DANGER.value == "위험"
        assert RiskGrade.WARNING.value == "경고"
        assert RiskGrade.WATCH.value == "주의"
        assert RiskGrade.NORMAL.value == "정상"

    def test_risk_grade_string_comparison(self):
        """Test RiskGrade string value comparison."""
        assert RiskGrade("위험") == RiskGrade.DANGER
        assert RiskGrade("경고") == RiskGrade.WARNING
        assert RiskGrade("주의") == RiskGrade.WATCH
        assert RiskGrade("정상") == RiskGrade.NORMAL

    def test_grade_order(self):
        """Test GRADE_ORDER tuple ordering (심각한 순)."""
        assert len(GRADE_ORDER) == 4
        assert GRADE_ORDER == (RiskGrade.DANGER, RiskGrade.WARNING, RiskGrade.WATCH, RiskGrade.NORMAL)
        assert GRADE_ORDER[0] == RiskGrade.DANGER
        assert GRADE_ORDER[-1] == RiskGrade.NORMAL


class TestForecastResult:
    """Test ForecastResult dataclass."""

    def test_forecast_result_creation(self):
        """Test ForecastResult creation."""
        forecast = ForecastResult(
            method="sma",
            horizon_days=14,
            daily=(10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0, 23.0),
            avg_daily=16.5,
            total=231.0,
        )
        assert forecast.method == "sma"
        assert forecast.horizon_days == 14
        assert len(forecast.daily) == 14
        assert forecast.avg_daily == 16.5
        assert forecast.total == 231.0

    def test_forecast_result_frozen(self):
        """Test ForecastResult immutability."""
        forecast = ForecastResult(
            method="sma",
            horizon_days=14,
            daily=(10.0,) * 14,
            avg_daily=10.0,
            total=140.0,
        )
        with pytest.raises(FrozenInstanceError):
            forecast.method = "ses"


class TestAnomalyFlag:
    """Test AnomalyFlag dataclass."""

    def test_anomaly_flag_creation(self):
        """Test AnomalyFlag creation."""
        anomaly = AnomalyFlag(
            kind="usage_surge",
            detected_on=date(2026, 7, 28),
            metric=0.41,
            detail="사용량이 급증했습니다.",
        )
        assert anomaly.kind == "usage_surge"
        assert anomaly.detected_on == date(2026, 7, 28)
        assert anomaly.metric == 0.41
        assert anomaly.detail == "사용량이 급증했습니다."

    def test_anomaly_flag_frozen(self):
        """Test AnomalyFlag immutability."""
        anomaly = AnomalyFlag(
            kind="usage_surge",
            detected_on=date(2026, 7, 28),
            metric=0.41,
            detail="test",
        )
        with pytest.raises(FrozenInstanceError):
            anomaly.kind = "usage_drop"


class TestDepletionEstimate:
    """Test DepletionEstimate dataclass."""

    def test_depletion_estimate_with_dates(self):
        """Test DepletionEstimate with depletion date."""
        estimate = DepletionEstimate(
            days_to_stockout=6,
            depletion_date=date(2026, 8, 7),
            stock_on_hand=150.0,
            reflected_receipts=True,
        )
        assert estimate.days_to_stockout == 6
        assert estimate.depletion_date == date(2026, 8, 7)
        assert estimate.stock_on_hand == 150.0
        assert estimate.reflected_receipts is True

    def test_depletion_estimate_no_stockout(self):
        """Test DepletionEstimate with no stockout."""
        estimate = DepletionEstimate(
            days_to_stockout=None,
            depletion_date=None,
            stock_on_hand=500.0,
            reflected_receipts=False,
        )
        assert estimate.days_to_stockout is None
        assert estimate.depletion_date is None


class TestGradeDecision:
    """Test GradeDecision dataclass."""

    def test_grade_decision_creation(self):
        """Test GradeDecision creation."""
        decision = GradeDecision(
            grade=RiskGrade.DANGER,
            base_grade=RiskGrade.WARNING,
            escalated_by_notice=True,
        )
        assert decision.grade == RiskGrade.DANGER
        assert decision.base_grade == RiskGrade.WARNING
        assert decision.escalated_by_notice is True


class TestRiskAssessment:
    """Test RiskAssessment dataclass and to_evidence method."""

    def test_risk_assessment_creation(self):
        """Test RiskAssessment creation."""
        forecast = ForecastResult(
            method="ses",
            horizon_days=14,
            daily=(25.0,) * 14,
            avg_daily=25.4,
            total=355.6,
        )
        anomalies = (
            AnomalyFlag(
                kind="usage_surge",
                detected_on=date(2026, 7, 28),
                metric=0.41,
                detail="사용량 급증",
            ),
        )
        assessment = RiskAssessment(
            item_id="001-ABC",
            as_of=date(2026, 8, 1),
            grade=RiskGrade.DANGER,
            base_grade=RiskGrade.WARNING,
            escalated_by_notice=True,
            risk_type="supply_halt",
            score=92,
            days_to_stockout=6,
            depletion_date=date(2026, 8, 7),
            forecast=forecast,
            anomalies=anomalies,
            reflected_receipts=True,
        )
        assert assessment.item_id == "001-ABC"
        assert assessment.grade == RiskGrade.DANGER
        assert len(assessment.anomalies) == 1
        assert assessment.reflected_receipts is True

    def test_risk_assessment_frozen(self):
        """Test RiskAssessment immutability."""
        forecast = ForecastResult(
            method="sma",
            horizon_days=14,
            daily=(10.0,) * 14,
            avg_daily=10.0,
            total=140.0,
        )
        assessment = RiskAssessment(
            item_id="001-ABC",
            as_of=date(2026, 8, 1),
            grade=RiskGrade.NORMAL,
            base_grade=RiskGrade.NORMAL,
            escalated_by_notice=False,
            risk_type="general",
            score=10,
            days_to_stockout=None,
            depletion_date=None,
            forecast=forecast,
            anomalies=(),
            reflected_receipts=False,
        )
        with pytest.raises(FrozenInstanceError):
            assessment.score = 20

    def test_to_evidence_full(self):
        """Test to_evidence() with all fields populated."""
        forecast = ForecastResult(
            method="ses",
            horizon_days=14,
            daily=(25.0,) * 14,
            avg_daily=25.4,
            total=355.6,
        )
        anomalies = (
            AnomalyFlag(
                kind="usage_surge",
                detected_on=date(2026, 7, 28),
                metric=0.41,
                detail="사용량 급증",
            ),
        )
        assessment = RiskAssessment(
            item_id="001-ABC",
            as_of=date(2026, 8, 1),
            grade=RiskGrade.DANGER,
            base_grade=RiskGrade.WARNING,
            escalated_by_notice=True,
            risk_type="supply_halt",
            score=92,
            days_to_stockout=6,
            depletion_date=date(2026, 8, 7),
            forecast=forecast,
            anomalies=anomalies,
            reflected_receipts=True,
        )
        evidence = assessment.to_evidence()

        # Check all required keys exist
        assert "as_of" in evidence
        assert "grade" in evidence
        assert "base_grade" in evidence
        assert "escalated_by_notice" in evidence
        assert "risk_type" in evidence
        assert "score" in evidence
        assert "days_to_stockout" in evidence
        assert "depletion_date" in evidence
        assert "reflected_receipts" in evidence
        assert "forecast" in evidence
        assert "anomalies" in evidence

        # Check values
        assert evidence["as_of"] == "2026-08-01"
        assert evidence["grade"] == "위험"
        assert evidence["base_grade"] == "경고"
        assert evidence["escalated_by_notice"] is True
        assert evidence["risk_type"] == "supply_halt"
        assert evidence["score"] == 92
        assert evidence["days_to_stockout"] == 6
        assert evidence["depletion_date"] == "2026-08-07"
        assert evidence["reflected_receipts"] is True

        # Check forecast
        assert evidence["forecast"]["method"] == "ses"
        assert evidence["forecast"]["horizon_days"] == 14
        assert evidence["forecast"]["avg_daily"] == 25.4
        assert evidence["forecast"]["total"] == 355.6
        assert "daily" not in evidence["forecast"]  # daily should not be in evidence

        # Check anomalies
        assert len(evidence["anomalies"]) == 1
        assert evidence["anomalies"][0]["kind"] == "usage_surge"
        assert evidence["anomalies"][0]["detected_on"] == "2026-07-28"
        assert evidence["anomalies"][0]["metric"] == 0.41
        assert evidence["anomalies"][0]["detail"] == "사용량 급증"

    def test_to_evidence_no_stockout(self):
        """Test to_evidence() with days_to_stockout=None."""
        forecast = ForecastResult(
            method="sma",
            horizon_days=14,
            daily=(10.0,) * 14,
            avg_daily=10.0,
            total=140.0,
        )
        assessment = RiskAssessment(
            item_id="001-ABC",
            as_of=date(2026, 8, 1),
            grade=RiskGrade.NORMAL,
            base_grade=RiskGrade.NORMAL,
            escalated_by_notice=False,
            risk_type="general",
            score=10,
            days_to_stockout=None,
            depletion_date=None,
            forecast=forecast,
            anomalies=(),
            reflected_receipts=False,
        )
        evidence = assessment.to_evidence()

        assert evidence["days_to_stockout"] is None
        assert evidence["depletion_date"] is None
        assert evidence["reflected_receipts"] is False

    def test_to_evidence_json_serializable(self):
        """Test to_evidence() result is JSON serializable with ensure_ascii=False."""
        forecast = ForecastResult(
            method="ses",
            horizon_days=14,
            daily=(25.0,) * 14,
            avg_daily=25.4,
            total=355.6,
        )
        anomalies = (
            AnomalyFlag(
                kind="usage_surge",
                detected_on=date(2026, 7, 28),
                metric=0.41,
                detail="사용량 급증했습니다",
            ),
        )
        assessment = RiskAssessment(
            item_id="001-ABC",
            as_of=date(2026, 8, 1),
            grade=RiskGrade.DANGER,
            base_grade=RiskGrade.WARNING,
            escalated_by_notice=True,
            risk_type="supply_halt",
            score=92,
            days_to_stockout=6,
            depletion_date=date(2026, 8, 7),
            forecast=forecast,
            anomalies=anomalies,
            reflected_receipts=True,
        )
        evidence = assessment.to_evidence()

        # Should be JSON serializable with ensure_ascii=False
        json_str = json.dumps(evidence, ensure_ascii=False)
        assert json_str is not None

        # Should be able to deserialize
        deserialized = json.loads(json_str)
        assert deserialized["grade"] == "위험"
        assert deserialized["anomalies"][0]["detail"] == "사용량 급증했습니다"
        assert deserialized["reflected_receipts"] is True
