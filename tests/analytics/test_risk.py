"""Tests for risk grade, type, and score decision logic (medsupply.analytics.risk).

경계값 하드코딩 손검산 테스트. 기본 파라미터(config/analytics_params.toml 기본값)를
GradeParams/ScoreParams로 직접 구성해 사용한다(로더 경유하지 않음).
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from medsupply.analytics.params import GradeParams, ScoreParams
from medsupply.analytics.risk import compute_score, derive_risk_type, grade_risk
from medsupply.analytics.types import AnomalyFlag, GradeDecision, RiskGrade


def _anomaly(kind: str, detected_on: date = date(2026, 8, 1), metric: float = 0.4) -> AnomalyFlag:
    """테스트용 최소 AnomalyFlag 생성 헬퍼."""
    return AnomalyFlag(kind=kind, detected_on=detected_on, metric=metric, detail=f"{kind} 감지")


@pytest.fixture
def grade_params() -> GradeParams:
    """기본 등급 파라미터(danger=7/warning=14/watch=30, 상향 활성)."""
    return GradeParams(
        danger_days=7,
        warning_days=14,
        watch_days=30,
        escalate_on_notice=True,
        escalate_needs_review=True,
    )


@pytest.fixture
def score_params() -> ScoreParams:
    """기본 점수 파라미터(base_danger=70/warning=45/watch=20/normal=0, per_anomaly=8, notice_bonus=15)."""
    return ScoreParams(
        base_danger=70,
        base_warning=45,
        base_watch=20,
        base_normal=0,
        per_anomaly=8,
        notice_bonus=15,
    )


class TestGradeRiskBoundaries:
    """grade_risk 등급 경계 전수 테스트(공고 없음)."""

    @pytest.mark.parametrize(
        "days_to_stockout, expected_grade",
        [
            (None, RiskGrade.NORMAL),
            (0, RiskGrade.DANGER),
            (7, RiskGrade.DANGER),
            (8, RiskGrade.WARNING),
            (14, RiskGrade.WARNING),
            (15, RiskGrade.WATCH),
            (30, RiskGrade.WATCH),
            (31, RiskGrade.NORMAL),
        ],
    )
    def test_boundary(self, grade_params, days_to_stockout, expected_grade):
        decision = grade_risk(days_to_stockout, has_active_notice=False, params=grade_params)

        assert decision.grade == expected_grade
        assert decision.base_grade == expected_grade
        assert decision.escalated_by_notice is False

    def test_negative_days_is_danger(self, grade_params):
        """음수 소진일수도 danger_days 이하이므로 위험으로 분류된다."""
        decision = grade_risk(-3, has_active_notice=False, params=grade_params)

        assert decision.grade == RiskGrade.DANGER
        assert decision.base_grade == RiskGrade.DANGER
        assert decision.escalated_by_notice is False


class TestGradeRiskEscalation:
    """활성 공고에 의한 상향(escalate_on_notice) 테스트."""

    def test_warning_escalates_to_danger(self, grade_params):
        """8일(base=경고) + 공고 → 위험으로 상향, escalated=True."""
        decision = grade_risk(8, has_active_notice=True, params=grade_params)

        assert decision.base_grade == RiskGrade.WARNING
        assert decision.grade == RiskGrade.DANGER
        assert decision.escalated_by_notice is True

    def test_normal_escalates_to_watch(self, grade_params):
        """31일(base=정상) + 공고 → 주의로 상향."""
        decision = grade_risk(31, has_active_notice=True, params=grade_params)

        assert decision.base_grade == RiskGrade.NORMAL
        assert decision.grade == RiskGrade.WATCH
        assert decision.escalated_by_notice is True

    def test_danger_is_capped_not_escalated(self, grade_params):
        """5일(base=위험) + 공고 → 이미 최상위라 grade==base_grade, escalated=False(캡)."""
        decision = grade_risk(5, has_active_notice=True, params=grade_params)

        assert decision.base_grade == RiskGrade.DANGER
        assert decision.grade == RiskGrade.DANGER
        assert decision.escalated_by_notice is False

    def test_escalate_on_notice_false_disables_escalation(self, grade_params):
        """escalate_on_notice=False면 활성 공고가 있어도 상향하지 않는다."""
        params = dataclasses.replace(grade_params, escalate_on_notice=False)

        decision = grade_risk(8, has_active_notice=True, params=params)

        assert decision.base_grade == RiskGrade.WARNING
        assert decision.grade == RiskGrade.WARNING
        assert decision.escalated_by_notice is False

    def test_no_active_notice_never_escalates(self, grade_params):
        """has_active_notice=False면 escalate_on_notice=True여도 상향하지 않는다."""
        decision = grade_risk(8, has_active_notice=False, params=grade_params)

        assert decision.grade == decision.base_grade == RiskGrade.WARNING
        assert decision.escalated_by_notice is False


class TestGradeRiskDeterminismAndType:
    """반환 타입·결정성 테스트."""

    def test_returns_grade_decision_with_riskgrade_instances(self, grade_params):
        decision = grade_risk(8, has_active_notice=True, params=grade_params)

        assert isinstance(decision, GradeDecision)
        assert isinstance(decision.grade, RiskGrade)
        assert isinstance(decision.base_grade, RiskGrade)

    def test_deterministic(self, grade_params):
        d1 = grade_risk(8, has_active_notice=True, params=grade_params)
        d2 = grade_risk(8, has_active_notice=True, params=grade_params)

        assert d1 == d2


class TestDeriveRiskType:
    """derive_risk_type 요인 조합 테스트(마스터 플랜 결정 27)."""

    def test_notice_only_is_supply_halt(self):
        assert derive_risk_type([], has_active_notice=True) == "supply_halt"

    def test_surge_only_is_demand_surge(self):
        anomalies = [_anomaly("usage_surge")]
        assert derive_risk_type(anomalies, has_active_notice=False) == "demand_surge"

    def test_delay_only_is_delivery_delay(self):
        anomalies = [_anomaly("receipt_delay")]
        assert derive_risk_type(anomalies, has_active_notice=False) == "delivery_delay"

    def test_notice_and_surge_is_composite(self):
        anomalies = [_anomaly("usage_surge")]
        assert derive_risk_type(anomalies, has_active_notice=True) == "composite"

    def test_surge_and_delay_is_composite(self):
        anomalies = [_anomaly("usage_surge"), _anomaly("receipt_delay")]
        assert derive_risk_type(anomalies, has_active_notice=False) == "composite"

    def test_no_factors_is_general(self):
        assert derive_risk_type([], has_active_notice=False) == "general"

    def test_drop_only_is_general(self):
        """usage_drop은 위험 유형 요인에 포함되지 않는다."""
        anomalies = [_anomaly("usage_drop")]
        assert derive_risk_type(anomalies, has_active_notice=False) == "general"

    def test_drop_and_surge_is_demand_surge(self):
        """drop은 요인이 아니므로 surge 1개만 인정되어 composite이 아니다."""
        anomalies = [_anomaly("usage_drop"), _anomaly("usage_surge")]
        assert derive_risk_type(anomalies, has_active_notice=False) == "demand_surge"

    def test_all_three_factors_is_composite(self):
        anomalies = [_anomaly("usage_surge"), _anomaly("receipt_delay")]
        assert derive_risk_type(anomalies, has_active_notice=True) == "composite"


class TestDeriveRiskTypeIsCheckSetMember:
    """반환값이 risk_results.risk_type CHECK 제약 집합의 원소인지 검증."""

    ALLOWED = {"demand_surge", "supply_halt", "delivery_delay", "composite", "general"}

    @pytest.mark.parametrize(
        "anomalies, has_active_notice",
        [
            ([], False),
            ([], True),
            ([_anomaly("usage_surge")], False),
            ([_anomaly("receipt_delay")], False),
            ([_anomaly("usage_drop")], False),
            ([_anomaly("usage_surge"), _anomaly("receipt_delay")], True),
        ],
    )
    def test_result_in_allowed_set(self, anomalies, has_active_notice):
        result = derive_risk_type(anomalies, has_active_notice)

        assert isinstance(result, str)
        assert result in self.ALLOWED


class TestComputeScore:
    """compute_score 손검산 테스트(기본 파라미터)."""

    def test_danger_two_anomalies_notice_caps_at_100(self, score_params):
        """위험+이상2+공고 → min(100, 70+16+15)=100(캡 발동)."""
        decision = GradeDecision(grade=RiskGrade.DANGER, base_grade=RiskGrade.DANGER, escalated_by_notice=False)
        anomalies = [_anomaly("usage_surge"), _anomaly("receipt_delay")]

        score = compute_score(decision, anomalies, has_active_notice=True, params=score_params)

        assert score == 100

    def test_warning_one_anomaly_no_notice(self, score_params):
        """경고+이상1 → 45+8=53."""
        decision = GradeDecision(grade=RiskGrade.WARNING, base_grade=RiskGrade.WARNING, escalated_by_notice=False)
        anomalies = [_anomaly("usage_surge")]

        score = compute_score(decision, anomalies, has_active_notice=False, params=score_params)

        assert score == 53

    def test_normal_no_anomaly_no_notice(self, score_params):
        """정상+무 → 0."""
        decision = GradeDecision(grade=RiskGrade.NORMAL, base_grade=RiskGrade.NORMAL, escalated_by_notice=False)

        score = compute_score(decision, [], has_active_notice=False, params=score_params)

        assert score == 0

    def test_watch_base_score(self, score_params):
        """주의 등급 기본점 20 확인(이상·공고 없음)."""
        decision = GradeDecision(grade=RiskGrade.WATCH, base_grade=RiskGrade.WATCH, escalated_by_notice=False)

        score = compute_score(decision, [], has_active_notice=False, params=score_params)

        assert score == 20

    def test_usage_drop_counts_toward_score(self, score_params):
        """usage_drop은 risk_type 요인에서는 제외되지만 점수 가점 개수에는 포함된다."""
        decision = GradeDecision(grade=RiskGrade.NORMAL, base_grade=RiskGrade.NORMAL, escalated_by_notice=False)
        anomalies = [_anomaly("usage_drop")]

        score = compute_score(decision, anomalies, has_active_notice=False, params=score_params)

        assert score == 8

    def test_score_uses_final_grade_not_base_grade(self, score_params):
        """base 산정은 최종 grade 기준이어야 한다(escalated 케이스에서 base_grade가 아님)."""
        decision = GradeDecision(grade=RiskGrade.DANGER, base_grade=RiskGrade.WARNING, escalated_by_notice=True)

        score = compute_score(decision, [], has_active_notice=False, params=score_params)

        assert score == 70  # base_danger(70). base_grade(경고)의 45가 아님을 확인.

    def test_score_never_exceeds_100(self, score_params):
        """가점이 커도 상한 100을 넘지 않는다."""
        decision = GradeDecision(grade=RiskGrade.DANGER, base_grade=RiskGrade.DANGER, escalated_by_notice=False)
        anomalies = [_anomaly("usage_surge") for _ in range(10)]

        score = compute_score(decision, anomalies, has_active_notice=True, params=score_params)

        assert score == 100

    def test_returns_int(self, score_params):
        decision = GradeDecision(grade=RiskGrade.NORMAL, base_grade=RiskGrade.NORMAL, escalated_by_notice=False)

        score = compute_score(decision, [], has_active_notice=False, params=score_params)

        assert isinstance(score, int)
        assert type(score) is int

    def test_determinism(self, score_params):
        decision = GradeDecision(grade=RiskGrade.WARNING, base_grade=RiskGrade.WARNING, escalated_by_notice=False)
        anomalies = [_anomaly("usage_surge")]

        s1 = compute_score(decision, anomalies, has_active_notice=False, params=score_params)
        s2 = compute_score(decision, anomalies, has_active_notice=False, params=score_params)

        assert s1 == s2
