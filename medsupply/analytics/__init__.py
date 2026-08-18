"""Analytics package for risk assessment and forecasting.

**Discipline: Pure functions only**
- No file or database I/O
- No datetime.now() or time-dependent calls
- No random number generation
- No global mutable state

This package provides types and pure analytical functions consumed by
prediction, anomaly detection, depletion estimation, grading, and
pipeline tasks.
"""

from medsupply.analytics.types import (
    AnomalyFlag,
    DepletionEstimate,
    ForecastResult,
    GRADE_ORDER,
    GradeDecision,
    ItemInputs,
    RiskAssessment,
    RiskGrade,
)

__all__ = [
    "RiskGrade",
    "GRADE_ORDER",
    "ForecastResult",
    "AnomalyFlag",
    "DepletionEstimate",
    "GradeDecision",
    "ItemInputs",
    "RiskAssessment",
]
