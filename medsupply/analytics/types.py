"""Type definitions for analytics domain."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class RiskGrade(str, Enum):
    """Risk grade classification (값이 곧 DB 저장 문자열)."""

    DANGER = "위험"
    WARNING = "경고"
    WATCH = "주의"
    NORMAL = "정상"


GRADE_ORDER: tuple[RiskGrade, ...] = (RiskGrade.DANGER, RiskGrade.WARNING, RiskGrade.WATCH, RiskGrade.NORMAL)
"""Risk grades in order of severity (심각한 순)."""


@dataclass(frozen=True)
class ForecastResult:
    """Forecast result containing daily predictions."""

    method: str  # 'sma' | 'ses'
    horizon_days: int
    daily: tuple[float, ...]  # 길이 == horizon_days, 각 값 >= 0
    avg_daily: float
    total: float


@dataclass(frozen=True)
class AnomalyFlag:
    """Detected anomaly in usage or delivery patterns."""

    kind: str  # 'usage_surge' | 'usage_drop' | 'receipt_delay'
    detected_on: date
    metric: float  # 급증/급감: 변화율(예: 0.41), 지연: 지연 일수
    detail: str  # 사람이 읽는 한 줄(한국어)


@dataclass(frozen=True)
class DepletionEstimate:
    """Estimated depletion date and time to stockout."""

    days_to_stockout: int | None  # None = 예측 수요 0 등으로 소진 없음
    depletion_date: date | None
    stock_on_hand: float
    reflected_receipts: bool


@dataclass(frozen=True)
class GradeDecision:
    """Risk grade decision with escalation flag."""

    grade: RiskGrade  # 최종 등급(상향 반영)
    base_grade: RiskGrade  # 소진일 기준 등급
    escalated_by_notice: bool


@dataclass(frozen=True)
class ItemInputs:
    """Input data for a single item analysis."""

    item_id: str
    as_of: date
    stock_on_hand: float
    usage: "pd.Series"  # 일자 오름차순 인덱스(date), as_of 이하만
    receipts: "pd.DataFrame"  # incoming_shipments 부분집합(as_of 기준)
    has_active_notice: bool
    is_essential: bool


@dataclass(frozen=True)
class RiskAssessment:
    """Complete risk assessment for a single item."""

    item_id: str
    as_of: date
    grade: RiskGrade
    base_grade: RiskGrade
    escalated_by_notice: bool
    risk_type: str  # 'demand_surge'|'supply_halt'|'delivery_delay'|'composite'|'general'
    score: int  # 0~100
    days_to_stockout: int | None
    depletion_date: date | None
    forecast: ForecastResult
    anomalies: tuple[AnomalyFlag, ...]
    reflected_receipts: bool  # depletion.DepletionEstimate.reflected_receipts를 그대로 옮긴 값

    def to_evidence(self) -> dict:
        """Convert to JSON-serializable evidence dict for risk_results.factors_json.

        - date fields converted to isoformat strings
        - forecast.daily array excluded (reserved for forecasts table)
        - JSON serializable with ensure_ascii=False
        """
        return {
            "as_of": self.as_of.isoformat(),
            "grade": self.grade.value,
            "base_grade": self.base_grade.value,
            "escalated_by_notice": self.escalated_by_notice,
            "risk_type": self.risk_type,
            "score": self.score,
            "days_to_stockout": self.days_to_stockout,
            "depletion_date": self.depletion_date.isoformat() if self.depletion_date else None,
            "reflected_receipts": self.reflected_receipts,
            "forecast": {
                "method": self.forecast.method,
                "horizon_days": self.forecast.horizon_days,
                "avg_daily": self.forecast.avg_daily,
                "total": self.forecast.total,
            },
            "anomalies": [
                {
                    "kind": anomaly.kind,
                    "detected_on": anomaly.detected_on.isoformat(),
                    "metric": anomaly.metric,
                    "detail": anomaly.detail,
                }
                for anomaly in self.anomalies
            ],
        }
