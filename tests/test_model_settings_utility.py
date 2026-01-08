"""Tests for ModelSettings utility methods."""

from __future__ import annotations

from agents.model_settings import ModelSettings


class TestModelSettingsUtilityMethods:
    """Tests for ModelSettings utility methods."""

    def test_is_default_true_for_empty(self) -> None:
        """is_default should be True for default ModelSettings."""
        settings = ModelSettings()
        assert settings.is_default is True

    def test_is_default_false_with_values(self) -> None:
        """is_default should be False when values are set."""
        settings = ModelSettings(temperature=0.7)
        assert settings.is_default is False

    def test_repr_empty_settings(self) -> None:
        """repr should be concise for default settings."""
        settings = ModelSettings()
        assert repr(settings) == "ModelSettings()"

    def test_repr_with_temperature(self) -> None:
        """repr should show non-None values."""
        settings = ModelSettings(temperature=0.5)
        assert "temperature=0.5" in repr(settings)

    def test_repr_with_multiple_values(self) -> None:
        """repr should show multiple non-None values."""
        settings = ModelSettings(temperature=0.5, max_tokens=100)
        result = repr(settings)
        assert "temperature=0.5" in result
        assert "max_tokens=100" in result

    def test_copy_creates_new_instance(self) -> None:
        """copy() should create a new instance."""
        original = ModelSettings(temperature=0.7)
        copied = original.copy()
        assert copied is not original
        assert copied.temperature == original.temperature

    def test_copy_with_overrides(self) -> None:
        """copy() should apply overrides."""
        original = ModelSettings(temperature=0.7, max_tokens=100)
        copied = original.copy(temperature=0.5)
        
        assert original.temperature == 0.7
        assert copied.temperature == 0.5
        assert copied.max_tokens == 100
