"""Tests for analytics parameter configuration and loader."""
import json
from pathlib import Path
from dataclasses import FrozenInstanceError

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

    def test_depletion_params_match_frozen_adoption(self):
        """[depletion]은 **S-17d 채택(cand-F) 값으로 동결**돼 있다.

        갱신 사유: 이 테스트는 원래 v1 값(reflect_receipts=False)을 하드코딩하고 있었다.
        S-17d에서 cand-F(reflect_receipts=true + overdue_cutoff=true)가 채택되면서 값이
        바뀌었으므로, 단언을 약화(값 비교 삭제)하지 않고 **채택된 값으로 갱신**한다.
        동결 선언이 있는 지금은 이 핀 자체가 "3주차 평가 기준을 말없이 바꾸지 못하게 하는"
        가드로 기능한다 — 값을 또 바꾸려면 이 테스트를 함께 바꿔야 하고, 그때 동결 위반이
        드러난다. 로더 자체의 동작은 아래 명시 주입 테스트들이 config 값과 무관하게 검증한다.
        """
        params = load_params()
        assert params.depletion.reflect_receipts is True
        assert params.depletion.overdue_cutoff is True

    def test_frozen_params_hash(self):
        """S-17d 동결 시점의 params_hash를 고정한다(어떤 파라미터가 바뀌어도 여기서 걸린다)."""
        assert load_params().params_hash == "6ec9bf05"

    @staticmethod
    def _config_without(key: str) -> str:
        """현행 config에서 `key = ...` 줄만 제거한 TOML 문자열(주석 문구에 의존하지 않는다)."""
        content = Path("config/analytics_params.toml").read_text(encoding="utf-8")
        return "".join(
            line for line in content.splitlines(keepends=True)
            if not line.lstrip().startswith(f"{key} =")
        )

    def test_depletion_overdue_cutoff_is_required_in_toml(self, tmp_path):
        """[depletion].overdue_cutoff는 다른 키와 동일하게 TOML에 명시 필수다.

        dataclass 기본값(False)은 기존 호출부 호환용이지 TOML 생략 허용이 아니다.
        """
        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(self._config_without("overdue_cutoff"))

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        assert "overdue_cutoff" in str(exc_info.value)

    @pytest.mark.parametrize("value", [True, False])
    def test_depletion_booleans_round_trip(self, tmp_path, value):
        """reflect_receipts·overdue_cutoff가 TOML 값 그대로 로드된다(config 현재 값과 무관).

        명시 주입 방식이라 채택 파라미터가 어느 쪽으로 바뀌어도 이 테스트는 계속 유효하다.
        """
        literal = "true" if value else "false"
        content = (
            self._config_without("reflect_receipts").replace(
                "[depletion]", f"[depletion]\nreflect_receipts = {literal}", 1
            )
        )
        content = "".join(
            line for line in content.splitlines(keepends=True)
            if not line.lstrip().startswith("overdue_cutoff =")
        ).replace("[depletion]", f"[depletion]\noverdue_cutoff = {literal}", 1)

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(content)

        params = load_params(tmp_config)

        assert params.depletion.reflect_receipts is value
        assert params.depletion.overdue_cutoff is value

    def test_depletion_change_alters_params_hash(self, tmp_path):
        """[depletion] 값이 달라지면 params_hash도 달라진다(현행 값 반대로 뒤집어 확인)."""
        current = load_params()
        flipped_reflect = "false" if current.depletion.reflect_receipts else "true"
        content = self._config_without("reflect_receipts").replace(
            "[depletion]", f"[depletion]\nreflect_receipts = {flipped_reflect}", 1
        )

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(content)

        assert load_params(tmp_config).params_hash != current.params_hash

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
        with pytest.raises(FrozenInstanceError):
            params.grade.danger_days = 10  # type: ignore

    def test_forecast_params_frozen(self):
        """Test that ForecastParams is immutable."""
        params = load_params()
        with pytest.raises(FrozenInstanceError):
            params.forecast.ses_alpha = 0.5  # type: ignore

    def test_anomaly_params_frozen(self):
        """Test that AnomalyParams is immutable."""
        params = load_params()
        with pytest.raises(FrozenInstanceError):
            params.anomaly.surge_ratio = 0.5  # type: ignore

    def test_depletion_params_frozen(self):
        """Test that DepletionParams is immutable."""
        params = load_params()
        with pytest.raises(FrozenInstanceError):
            params.depletion.reflect_receipts = True  # type: ignore

    def test_score_params_frozen(self):
        """Test that ScoreParams is immutable."""
        params = load_params()
        with pytest.raises(FrozenInstanceError):
            params.score.base_danger = 80  # type: ignore

    def test_analytics_params_frozen(self):
        """Test that AnalyticsParams is immutable."""
        params = load_params()
        with pytest.raises(FrozenInstanceError):
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


class TestMissingRequiredKeys:
    """Test that missing required keys raise ValueError."""

    def test_missing_grade_danger_days(self, tmp_path):
        """Test that missing grade.danger_days raises ValueError."""
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        # Remove danger_days line
        bad_content = content.replace("danger_days = 7          # 소진 예상 ≤7일 → 위험\n", "")

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(bad_content)

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        error_msg = str(exc_info.value).lower()
        assert "missing required key" in error_msg or "danger_days" in error_msg

    def test_missing_score_per_anomaly(self, tmp_path):
        """Test that missing score.per_anomaly raises ValueError."""
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        # Remove per_anomaly line
        bad_content = content.replace("per_anomaly = 8          # 이상신호 1건당 가점\n", "")

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(bad_content)

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        error_msg = str(exc_info.value).lower()
        assert "missing required key" in error_msg or "per_anomaly" in error_msg


class TestUnknownTopLevelKey:
    """Test that unknown top-level keys raise ValueError."""

    def test_unknown_top_level_key(self, tmp_path):
        """Test that unknown top-level keys raise ValueError."""
        config_path = Path("config/analytics_params.toml")
        with open(config_path, "r") as f:
            content = f.read()

        # Add unknown key at the beginning
        bad_content = "unknown_top = 1\n" + content

        tmp_config = tmp_path / "analytics_params.toml"
        tmp_config.write_text(bad_content)

        with pytest.raises(ValueError) as exc_info:
            load_params(tmp_config)

        error_msg = str(exc_info.value).lower()
        assert "unknown" in error_msg
