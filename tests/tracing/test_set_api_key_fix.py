import pytest

from agents.tracing.processors import BackendSpanExporter


def test_set_api_key_preserves_env_fallback(monkeypatch: pytest.MonkeyPatch):
    """Test that set_api_key doesn't break environment variable fallback."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")

    exporter = BackendSpanExporter()

    # Initially should use env var
    assert exporter.api_key == "env-key"

    # Set explicit key
    exporter.set_api_key("explicit-key")
    assert exporter.api_key == "explicit-key"

    # Clear explicit key and verify env fallback works
    exporter._api_key = None
    if "api_key" in exporter.__dict__:
        del exporter.__dict__["api_key"]
    assert exporter.api_key == "env-key"
