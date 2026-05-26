from agents.model_settings import ModelSettings
from agents.models._trace import model_config_for_trace, sanitize_url_for_trace


def test_sanitize_url_for_trace_strips_auth_query_and_fragment() -> None:
    assert (
        sanitize_url_for_trace("https://user:pass@example.com/v1?api-key=secret#fragment")
        == "https://example.com/v1"
    )
    assert sanitize_url_for_trace("https://example.com/v1?token=secret") == "https://example.com/v1"


def test_model_config_for_trace_sanitizes_base_url_and_omits_request_extras() -> None:
    config = model_config_for_trace(
        ModelSettings(
            temperature=0.5,
            extra_headers={"Authorization": "Bearer provider-token"},
            extra_query={"api-key": "query-token"},
            extra_body={"secret": "body-token"},
            extra_args={"api_key": "arg-token"},
        ),
        base_url="https://user:pass@example.com/v1?api-key=secret#fragment",
        extra_config={"model_impl": "test-model"},
    )

    assert config["temperature"] == 0.5
    assert config["base_url"] == "https://example.com/v1"
    assert config["model_impl"] == "test-model"
    assert "extra_headers" not in config
    assert "extra_query" not in config
    assert "extra_body" not in config
    assert "extra_args" not in config
