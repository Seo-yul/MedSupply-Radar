"""Analytics parameter configuration and loader."""
from __future__ import annotations

import json
import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GradeParams:
    """Risk grade parameters."""

    danger_days: int
    warning_days: int
    watch_days: int
    escalate_on_notice: bool
    escalate_needs_review: bool


@dataclass(frozen=True)
class ForecastParams:
    """Forecast parameters."""

    method: str
    sma_window: int
    ses_alpha: float
    horizon_days: int


@dataclass(frozen=True)
class AnomalyParams:
    """Anomaly detection parameters."""

    surge_ratio: float
    drop_ratio: float
    recent_window: int
    baseline_window: int
    receipt_delay_days: int


@dataclass(frozen=True)
class DepletionParams:
    """Depletion parameters.

    overdue_cutoff는 기본값 False를 갖는다 — 이 스위치를 모르는 기존 호출부(테스트 픽스처
    포함)가 DepletionParams(reflect_receipts=...)만으로 계속 동작하게 하기 위해서다.
    TOML에서는 다른 키와 동일하게 명시 필수다(누락 시 load_params가 ValueError).
    """

    reflect_receipts: bool
    overdue_cutoff: bool = False


@dataclass(frozen=True)
class ScoreParams:
    """Risk score parameters."""

    base_danger: int
    base_warning: int
    base_watch: int
    base_normal: int
    per_anomaly: int
    notice_bonus: int


@dataclass(frozen=True)
class AnalyticsParams:
    """Complete analytics parameters."""

    grade: GradeParams
    forecast: ForecastParams
    anomaly: AnomalyParams
    depletion: DepletionParams
    score: ScoreParams
    params_hash: str


def load_params(path: str | Path = Path("config/analytics_params.toml")) -> AnalyticsParams:
    """Load analytics parameters from TOML file.

    Args:
        path: Path to analytics_params.toml file

    Returns:
        AnalyticsParams instance with all configuration values

    Raises:
        ValueError: If validation fails (bad values, unknown keys, etc.)
    """
    if isinstance(path, str):
        path = Path(path)

    # Load TOML
    with open(path, "rb") as f:
        data = tomllib.load(f)

    # Validate structure - check for unknown top-level keys
    allowed_sections = {"grade", "forecast", "anomaly", "depletion", "score"}
    for key in data.keys():
        if key not in allowed_sections:
            raise ValueError(f"Unknown top-level key: {key}")

    # Define required and allowed keys for each section
    required_and_allowed_keys = {
        "grade": {"danger_days", "warning_days", "watch_days", "escalate_on_notice", "escalate_needs_review"},
        "forecast": {"method", "sma_window", "ses_alpha", "horizon_days"},
        "anomaly": {"surge_ratio", "drop_ratio", "recent_window", "baseline_window", "receipt_delay_days"},
        "depletion": {"reflect_receipts", "overdue_cutoff"},
        "score": {"base_danger", "base_warning", "base_watch", "base_normal", "per_anomaly", "notice_bonus"},
    }

    # Validate required keys and unknown keys
    for section, allowed_keys in required_and_allowed_keys.items():
        if section in data:
            # Check for unknown keys
            for key in data[section].keys():
                if key not in allowed_keys:
                    raise ValueError(f"Unknown key in [{section}]: {key}")

            # Check for missing required keys
            section_data = data[section]
            for required_key in allowed_keys:
                if required_key not in section_data:
                    raise ValueError(f"Missing required key: {section}.{required_key}")

    # Extract section data
    grade_data = data.get("grade", {})
    forecast_data = data.get("forecast", {})
    anomaly_data = data.get("anomaly", {})
    depletion_data = data.get("depletion", {})
    score_data = data.get("score", {})

    # Validate grade parameters
    danger_days = grade_data.get("danger_days")
    warning_days = grade_data.get("warning_days")
    watch_days = grade_data.get("watch_days")

    if not (0 < danger_days < warning_days < watch_days):
        raise ValueError(
            f"Grade days must satisfy 0 < danger_days < warning_days < watch_days. "
            f"Got danger_days={danger_days}, warning_days={warning_days}, watch_days={watch_days}"
        )

    # Validate forecast parameters
    ses_alpha = forecast_data.get("ses_alpha")
    if not (0 < ses_alpha <= 1):
        raise ValueError(f"ses_alpha must satisfy 0 < ses_alpha <= 1. Got {ses_alpha}")

    sma_window = forecast_data.get("sma_window")
    if sma_window < 1:
        raise ValueError(f"sma_window must be >= 1. Got {sma_window}")

    horizon_days = forecast_data.get("horizon_days")
    if horizon_days < 1:
        raise ValueError(f"horizon_days must be >= 1. Got {horizon_days}")

    method = forecast_data.get("method")
    if method not in {"sma", "ses"}:
        raise ValueError(f"method must be 'sma' or 'ses'. Got {method}")

    # Validate anomaly parameters
    surge_ratio = anomaly_data.get("surge_ratio")
    if surge_ratio <= 0:
        raise ValueError(f"surge_ratio must be > 0. Got {surge_ratio}")

    drop_ratio = anomaly_data.get("drop_ratio")
    if drop_ratio <= 0:
        raise ValueError(f"drop_ratio must be > 0. Got {drop_ratio}")

    recent_window = anomaly_data.get("recent_window")
    if recent_window < 1:
        raise ValueError(f"recent_window must be >= 1. Got {recent_window}")

    baseline_window = anomaly_data.get("baseline_window")
    if baseline_window < recent_window:
        raise ValueError(
            f"baseline_window must be >= recent_window. "
            f"Got baseline_window={baseline_window}, recent_window={recent_window}"
        )

    # Create dataclass instances
    grade_params = GradeParams(
        danger_days=grade_data["danger_days"],
        warning_days=grade_data["warning_days"],
        watch_days=grade_data["watch_days"],
        escalate_on_notice=grade_data["escalate_on_notice"],
        escalate_needs_review=grade_data["escalate_needs_review"],
    )

    forecast_params = ForecastParams(
        method=forecast_data["method"],
        sma_window=forecast_data["sma_window"],
        ses_alpha=forecast_data["ses_alpha"],
        horizon_days=forecast_data["horizon_days"],
    )

    anomaly_params = AnomalyParams(
        surge_ratio=anomaly_data["surge_ratio"],
        drop_ratio=anomaly_data["drop_ratio"],
        recent_window=anomaly_data["recent_window"],
        baseline_window=anomaly_data["baseline_window"],
        receipt_delay_days=anomaly_data["receipt_delay_days"],
    )

    depletion_params = DepletionParams(
        reflect_receipts=depletion_data["reflect_receipts"],
        overdue_cutoff=depletion_data["overdue_cutoff"],
    )

    score_params = ScoreParams(
        base_danger=score_data["base_danger"],
        base_warning=score_data["base_warning"],
        base_watch=score_data["base_watch"],
        base_normal=score_data["base_normal"],
        per_anomaly=score_data["per_anomaly"],
        notice_bonus=score_data["notice_bonus"],
    )

    # Generate params_hash from normalized data
    # Sort all values and compute sha256
    normalized = {
        "grade": {
            "danger_days": grade_params.danger_days,
            "warning_days": grade_params.warning_days,
            "watch_days": grade_params.watch_days,
            "escalate_on_notice": grade_params.escalate_on_notice,
            "escalate_needs_review": grade_params.escalate_needs_review,
        },
        "forecast": {
            "method": forecast_params.method,
            "sma_window": forecast_params.sma_window,
            "ses_alpha": forecast_params.ses_alpha,
            "horizon_days": forecast_params.horizon_days,
        },
        "anomaly": {
            "surge_ratio": anomaly_params.surge_ratio,
            "drop_ratio": anomaly_params.drop_ratio,
            "recent_window": anomaly_params.recent_window,
            "baseline_window": anomaly_params.baseline_window,
            "receipt_delay_days": anomaly_params.receipt_delay_days,
        },
        "depletion": {
            "reflect_receipts": depletion_params.reflect_receipts,
            "overdue_cutoff": depletion_params.overdue_cutoff,
        },
        "score": {
            "base_danger": score_params.base_danger,
            "base_warning": score_params.base_warning,
            "base_watch": score_params.base_watch,
            "base_normal": score_params.base_normal,
            "per_anomaly": score_params.per_anomaly,
            "notice_bonus": score_params.notice_bonus,
        },
    }

    # Serialize with sorted keys and compute hash
    serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    hash_value = hashlib.sha256(serialized.encode()).hexdigest()[:8]

    return AnalyticsParams(
        grade=grade_params,
        forecast=forecast_params,
        anomaly=anomaly_params,
        depletion=depletion_params,
        score=score_params,
        params_hash=hash_value,
    )
