from __future__ import annotations

import pytest
from conftest import _external_providers

pytestmark = pytest.mark.packaging


def _configure_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_AGENTS_INTEGRATION_OPENROUTER_MODELS", raising=False)
    monkeypatch.delenv("OPENAI_AGENTS_INTEGRATION_ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_AGENTS_INTEGRATION_GEMINI_MODEL", raising=False)


def test_external_provider_coverage_is_explicitly_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_provider_credentials(monkeypatch)
    monkeypatch.delenv("OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS", raising=False)
    monkeypatch.delenv("OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS", raising=False)

    assert _external_providers() == []


def test_strict_mode_requires_requested_external_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_provider_credentials(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS", raising=False)
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS", "1")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_STRICT", "1")

    with pytest.raises(pytest.fail.Exception, match="External provider coverage requires"):
        _external_providers()


def test_strict_mode_does_not_require_unrequested_external_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_provider_credentials(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS", raising=False)
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_STRICT", "1")

    assert _external_providers() == []


def test_strict_mode_accepts_explicit_direct_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_provider_credentials(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS", "1")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS", "1")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_STRICT", "1")

    providers = _external_providers()

    assert [provider.name for provider in providers] == ["anthropic"]


def test_external_provider_coverage_defaults_to_current_openrouter_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_provider_credentials(monkeypatch)
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS", "1")
    monkeypatch.delenv("OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS", raising=False)

    providers = _external_providers()

    assert [provider.name for provider in providers] == [
        "openrouter-openai-gpt-5.6-luna",
        "openrouter-anthropic-claude-sonnet-5",
        "openrouter-google-gemini-3.6-flash",
    ]
    assert [provider.model for provider in providers] == [
        "openrouter/openai/gpt-5.6-luna",
        "openrouter/anthropic/claude-sonnet-5",
        "openrouter/google/gemini-3.6-flash",
    ]


def test_all_provider_coverage_adds_explicit_direct_provider_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_provider_credentials(monkeypatch)
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS", "1")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS", "1")

    providers = _external_providers()

    assert [provider.name for provider in providers] == [
        "openrouter-openai-gpt-5.6-luna",
        "openrouter-anthropic-claude-sonnet-5",
        "openrouter-google-gemini-3.6-flash",
        "anthropic",
        "gemini",
    ]
    assert [provider.model for provider in providers[-2:]] == [
        "anthropic/claude-sonnet-5",
        "gemini/gemini-3.6-flash",
    ]
