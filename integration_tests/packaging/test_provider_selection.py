from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from conftest import _external_providers, pytest_runtest_setup

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


@pytest.mark.parametrize(
    "credential_name", ["OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"]
)
def test_external_provider_tests_do_not_require_an_openai_api_key(
    monkeypatch: pytest.MonkeyPatch,
    credential_name: str,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv(credential_name, "provider-test-key")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_STRICT", "1")
    item = SimpleNamespace(
        fixturenames=["external_provider"],
        callspec=SimpleNamespace(params={"external_provider": object()}),
        get_closest_marker=lambda name: name == "providers",
    )

    pytest_runtest_setup(cast(pytest.Item, item))


def test_openai_backed_provider_tests_require_an_openai_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-test-key")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_STRICT", "1")
    item = SimpleNamespace(
        fixturenames=["integration_model"],
        get_closest_marker=lambda name: name == "providers",
    )

    with pytest.raises(pytest.fail.Exception, match="Set a real OPENAI_API_KEY"):
        pytest_runtest_setup(cast(pytest.Item, item))


@pytest.mark.parametrize(
    ("fixture_name", "model_environment", "model_name", "credential_name"),
    [
        (
            "any_llm_models",
            "OPENAI_AGENTS_INTEGRATION_ANY_LLM_MODELS",
            "openrouter/openai/gpt-5.6-luna",
            "OPENROUTER_API_KEY",
        ),
        (
            "any_llm_models",
            "OPENAI_AGENTS_INTEGRATION_ANY_LLM_MODELS",
            "anthropic/claude-sonnet-5",
            "ANTHROPIC_API_KEY",
        ),
        (
            "any_llm_models",
            "OPENAI_AGENTS_INTEGRATION_ANY_LLM_MODELS",
            "gemini/gemini-3.6-flash",
            "GEMINI_API_KEY",
        ),
        (
            "litellm_models",
            "OPENAI_AGENTS_INTEGRATION_LITELLM_MODELS",
            "openrouter/google/gemini-3.6-flash",
            "OPENROUTER_API_KEY",
        ),
        (
            "litellm_models",
            "OPENAI_AGENTS_INTEGRATION_LITELLM_MODELS",
            "gemini/gemini-3.6-flash",
            "GOOGLE_API_KEY",
        ),
    ],
)
def test_configured_provider_models_use_provider_specific_credentials(
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    model_environment: str,
    model_name: str,
    credential_name: str,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv(model_environment, model_name)
    monkeypatch.setenv(credential_name, "provider-test-key")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_STRICT", "1")
    item = SimpleNamespace(
        fixturenames=[fixture_name],
        get_closest_marker=lambda name: name == "providers",
    )

    pytest_runtest_setup(cast(pytest.Item, item))


def test_configured_provider_models_require_their_own_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_ANY_LLM_MODELS", "openrouter/openai/gpt-5.6-luna")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_STRICT", "1")
    item = SimpleNamespace(
        fixturenames=["any_llm_models"],
        get_closest_marker=lambda name: name == "providers",
    )

    with pytest.raises(pytest.fail.Exception, match="Set OPENROUTER_API_KEY"):
        pytest_runtest_setup(cast(pytest.Item, item))


def test_configured_openai_provider_rejects_placeholder_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test_key")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_LITELLM_MODELS", "openai/gpt-4.1-mini")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_STRICT", "1")
    item = SimpleNamespace(
        fixturenames=["litellm_models"],
        get_closest_marker=lambda name: name == "providers",
    )

    with pytest.raises(pytest.fail.Exception, match="Set OPENAI_API_KEY"):
        pytest_runtest_setup(cast(pytest.Item, item))


def test_strict_mode_requires_requested_external_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_provider_credentials(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS", raising=False)
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS", "1")
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_STRICT", "1")

    assert _external_providers() == []
    item = SimpleNamespace(
        fixturenames=["external_provider"],
        callspec=SimpleNamespace(params={"external_provider": None}),
        get_closest_marker=lambda name: name == "providers",
    )

    with pytest.raises(pytest.fail.Exception, match="External provider coverage requires"):
        pytest_runtest_setup(cast(pytest.Item, item))


@pytest.mark.parametrize(
    ("model", "expected_extra"),
    [
        ("anthropic/claude-sonnet-5", "anthropic"),
        ("gemini/gemini-3.6-flash", "gemini"),
        ("google/gemini-3.6-flash", "gemini"),
        ("openrouter/openai/gpt-5.6-luna", "openrouter"),
    ],
)
def test_configured_any_llm_models_install_provider_extras_without_external_matrix(
    monkeypatch: pytest.MonkeyPatch, model: str, expected_extra: str
) -> None:
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_ANY_LLM_MODELS", model)
    runner_path = Path(__file__).resolve().parents[2] / ".github/scripts/run_integration_tests.py"
    runner = runpy.run_path(str(runner_path))

    assert runner["_any_llm_provider_extras"](
        external_providers_enabled=False, direct_providers_enabled=False
    ) == [expected_extra]


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
