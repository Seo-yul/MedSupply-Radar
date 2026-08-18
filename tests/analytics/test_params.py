"""Tests for analytics parameter configuration and loader."""
import json
from pathlib import Path
from dataclasses import fields

import pytest

from medsupply.analytics.params import (
    GradeParams,
    ForecastParams,
    AnomalyParams,
    DepletionParams,
    ScoreParams,
    AnalyticsParams,
    load_params,
)


class TestLoadParams:
    """Test parameter file loading."""

    def test_load_default_params(self, tmp_path):
        """Test loading default analytics_params.toml."""
        params = load_params()

        # Check basic structure
        assert isinstance(params, AnalyticsParams)
        assert isinstance(params.grade, GradeParams)
        assert isinstance(params.forecast, ForecastParams)
        assert isinstance(params.anomaly, AnomalyParams)
        assert isinstance(params.depletion, DepletionParams)
        assert isinstance(params.score, ScoreParams)

    def test_grade_params_defaults(self):
        """Test grade parameter defaults."""
        params = load_params()
        assert params.grade.danger_days == 7
        assert params.grade.warning_days == 14
        assert params.grade.watch_days == 30
        assert params.grade.escalate_on_notice is True
        assert params.grade.escalate_needs_review is True

    def test_forecast_params_defaults(self):
        """Test forecast parameter defaults."""
        params = load_params()
        assert params.forecast.method == "ses"
        assert params.forecast.sma_window == 28
        assert params.forecast.ses_alpha == 0.3
        assert params.forecast.horizon_days == 14

    def test_anomaly_params_defaults(self):
        """Test anomaly parameter defaults."""
        params = load_params()
        assert params.anomaly.surge_ratio == 0.30
        assert params.anomaly.drop_ratio == 0.30
        assert params.anomaly.recent_window == 7
        assert params.anomaly.baseline_window == 28
        assert params.anomaly.receipt_delay_days == 3

    def test_depletion_params_defaults(self):
        """Test depletion parameter defaults."""
        params = load_params()
        assert params.depletion.reflect_receipts is False

    def test_score_params_defaults(self):
        """Test score parameter defaults."""
        params = load_params()
        assert params.score.base_danger == 70
        assert params.score.base_warning == 45
        assert params.score.base_watch == 20
        assert params.score.base_normal == 0
        assert params.score.per_anomaly == 8
        assert params.score.notice_bonus == 15


class TestParamsHash:
    """Test params_hash determinism and change detection."""

    def test_params_hash_exists(self):
        """Test that params_hash is present and non-empty."""
        params = load_params()
        assert params.params_hash
        assert isinstance(params.params_hash, str)
        assert len(params.params_hash) == 8

    def test_params_hash_deterministic(self):
        """Test that same params file produces same hash."""
        params1 = load_params()
        params2 = load_params()
        assert params1.params_hash == params2.params_hash

    def test_params_hash_changes_on_value_change(self, tmp_path):
        """Test that hash changes when params values change."""
        # Load original
        params1 = load_params()
        hash1 = params1.params_hash

        # Create modified copy
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        # Modify a value
        modified_content = content.replace("danger_days = 7", "danger_days = 5")

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(modified_content)

        # Load modified params
        params2 = load_params(tmp_config)
        hash2 = params2.params_hash

        # Hashes should be different
        assert hash1 != hash2
        assert params2.grade.danger_days == 5


class TestValidation:
    """Test parameter validation."""

    def test_danger_days_order(self, tmp_path):
        """Test that danger_days < warning_days < watch_days."""
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        # Set danger_days = 20 (violates order)
        bad_content = content.replace("danger_days = 7", "danger_days = 20")

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(bad_content)

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        assert "danger_days" in str(exc_info.value).lower()

    def test_ses_alpha_validation(self, tmp_path):
        """Test that 0 < ses_alpha <= 1."""
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        # Set ses_alpha = 1.5 (violates range)
        bad_content = content.replace("ses_alpha = 0.3", "ses_alpha = 1.5")

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(bad_content)

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        assert "ses_alpha" in str(exc_info.value).lower()

    def test_forecast_method_validation(self, tmp_path):
        """Test that forecast.method is in {'sma', 'ses'}."""
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        # Set method = 'arima' (invalid)
        bad_content = content.replace('method = "ses"', 'method = "arima"')

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(bad_content)

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        assert "method" in str(exc_info.value).lower()

    def test_unknown_key_validation(self, tmp_path):
        """Test that unknown keys raise ValueError."""
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        # Add unknown key
        bad_content = content + "\nunknown_key = 42\n"

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(bad_content)

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        # Should mention unknown key
        assert "unknown" in str(exc_info.value).lower() or "key" in str(exc_info.value).lower()


class TestImmutability:
    """Test that params dataclasses are frozen."""

    def test_grade_params_frozen(self):
        """Test that GradeParams is immutable."""
        params = load_params()
        with pytest.raises((AttributeError, Exception)):
            params.grade.danger_days = 10  # type: ignore

    def test_forecast_params_frozen(self):
        """Test that ForecastParams is immutable."""
        params = load_params()
        with pytest.raises((AttributeError, Exception)):
            params.forecast.ses_alpha = 0.5  # type: ignore

    def test_anomaly_params_frozen(self):
        """Test that AnomalyParams is immutable."""
        params = load_params()
        with pytest.raises((AttributeError, Exception)):
            params.anomaly.surge_ratio = 0.5  # type: ignore

    def test_depletion_params_frozen(self):
        """Test that DepletionParams is immutable."""
        params = load_params()
        with pytest.raises((AttributeError, Exception)):
            params.depletion.reflect_receipts = True  # type: ignore

    def test_score_params_frozen(self):
        """Test that ScoreParams is immutable."""
        params = load_params()
        with pytest.raises((AttributeError, Exception)):
            params.score.base_danger = 80  # type: ignore

    def test_analytics_params_frozen(self):
        """Test that AnalyticsParams is immutable."""
        params = load_params()
        with pytest.raises((AttributeError, Exception)):
            params.params_hash = "different"  # type: ignore


class TestBaselineWindow:
    """Test baseline_window validation."""

    def test_baseline_window_must_be_gte_recent_window(self, tmp_path):
        """Test that baseline_window >= recent_window."""
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        # Set baseline_window < recent_window
        bad_content = content.replace("baseline_window = 28", "baseline_window = 3")

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(bad_content)

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        assert "baseline_window" in str(exc_info.value).lower()


class TestPositiveValues:
    """Test that ratios are positive."""

    def test_surge_ratio_positive(self, tmp_path):
        """Test that surge_ratio > 0."""
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        bad_content = content.replace("surge_ratio = 0.30", "surge_ratio = 0")

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(bad_content)

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        assert "surge_ratio" in str(exc_info.value).lower()

    def test_drop_ratio_positive(self, tmp_path):
        """Test that drop_ratio > 0."""
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        bad_content = content.replace("drop_ratio = 0.30", "drop_ratio = 0")

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(bad_content)

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        assert "drop_ratio" in str(exc_info.value).lower()
