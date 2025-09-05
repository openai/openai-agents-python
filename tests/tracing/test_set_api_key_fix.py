import os

from agents.tracing.processors import BackendSpanExporter


def test_set_api_key_preserves_env_fallback():
    """Test that set_api_key doesn't break environment variable fallback."""
    # Set up environment
    original_key = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "env-key"

    try:
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

    finally:
        if original_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = original_key
