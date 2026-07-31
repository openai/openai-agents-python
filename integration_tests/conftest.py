from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

LIVE_MARKERS = frozenset({"core", "providers", "realtime", "voice", "hosted"})


@dataclass(frozen=True)
class ExternalProvider:
    name: str
    model: str
    api_key_name: str

    @property
    def api_key(self) -> str:
        return os.environ[self.api_key_name]


def _external_providers_enabled() -> bool:
    return os.environ.get("OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _direct_providers_enabled() -> bool:
    return os.environ.get("OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _external_providers() -> list[ExternalProvider]:
    if not _external_providers_enabled():
        return []

    providers: list[ExternalProvider] = []
    if os.environ.get("OPENROUTER_API_KEY"):
        configured = os.environ.get(
            "OPENAI_AGENTS_INTEGRATION_OPENROUTER_MODELS",
            "openai/gpt-5.6-luna,anthropic/claude-sonnet-5,google/gemini-3.6-flash",
        )
        for model in configured.split(","):
            if model.strip():
                providers.append(
                    ExternalProvider(
                        name=f"openrouter-{model.strip().replace('/', '-')}",
                        model=f"openrouter/{model.strip()}",
                        api_key_name="OPENROUTER_API_KEY",
                    )
                )

    if _direct_providers_enabled():
        if os.environ.get("ANTHROPIC_API_KEY"):
            providers.append(
                ExternalProvider(
                    name="anthropic",
                    model="anthropic/"
                    + os.environ.get(
                        "OPENAI_AGENTS_INTEGRATION_ANTHROPIC_MODEL", "claude-sonnet-5"
                    ),
                    api_key_name="ANTHROPIC_API_KEY",
                )
            )

        gemini_key = "GEMINI_API_KEY" if os.environ.get("GEMINI_API_KEY") else "GOOGLE_API_KEY"
        if os.environ.get(gemini_key):
            providers.append(
                ExternalProvider(
                    name="gemini",
                    model="gemini/"
                    + os.environ.get("OPENAI_AGENTS_INTEGRATION_GEMINI_MODEL", "gemini-3.6-flash"),
                    api_key_name=gemini_key,
                )
            )

    return providers


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "external_provider" not in metafunc.fixturenames:
        return

    providers = _external_providers()
    if providers:
        metafunc.parametrize("external_provider", providers, ids=[item.name for item in providers])
        return

    metafunc.parametrize("external_provider", [None], ids=["unconfigured"])


def _strict() -> bool:
    return os.environ.get("OPENAI_AGENTS_INTEGRATION_STRICT", "").lower() in {
        "1",
        "true",
        "yes",
    }


def skip_or_fail(reason: str) -> None:
    if _strict():
        pytest.fail(reason)
    pytest.skip(reason)


def _provider_model_credentials(model: str) -> tuple[str, ...]:
    provider = model.partition("/")[0]
    if provider == "openrouter":
        return ("OPENROUTER_API_KEY",)
    if provider == "anthropic":
        return ("ANTHROPIC_API_KEY",)
    if provider in {"gemini", "google"}:
        return ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    return ("OPENAI_API_KEY",)


def _has_provider_credential(credential: str) -> bool:
    value = os.environ.get(credential)
    if credential == "OPENAI_API_KEY":
        return value not in {None, "", "test_key", "fake-for-tests"}
    return bool(value)


def pytest_runtest_setup(item: pytest.Item) -> None:
    if not any(item.get_closest_marker(marker) for marker in LIVE_MARKERS):
        return
    if item.get_closest_marker("providers"):
        fixture_names = getattr(item, "fixturenames", ())
        if "external_provider" in fixture_names:
            callspec = getattr(item, "callspec", None)
            provider = getattr(callspec, "params", {}).get("external_provider")
            if provider is None:
                if _external_providers_enabled():
                    skip_or_fail(
                        "External provider coverage requires OPENROUTER_API_KEY or explicitly "
                        "enabled direct-provider credentials."
                    )
                pytest.skip(
                    "Enable external provider coverage and set OPENROUTER_API_KEY, "
                    "or explicitly include configured direct providers."
                )
            return
        for fixture_name, environment_name in (
            ("any_llm_models", "OPENAI_AGENTS_INTEGRATION_ANY_LLM_MODELS"),
            ("litellm_models", "OPENAI_AGENTS_INTEGRATION_LITELLM_MODELS"),
        ):
            if fixture_name not in fixture_names:
                continue
            configured_models = os.environ.get(environment_name, "")
            if not configured_models.strip():
                break
            for model in configured_models.split(","):
                if not model.strip():
                    continue
                credentials = _provider_model_credentials(model.strip())
                if not any(_has_provider_credential(credential) for credential in credentials):
                    skip_or_fail(
                        f"Set {' or '.join(credentials)} to execute configured provider "
                        f"model {model.strip()!r}."
                    )
            return
    if os.environ.get("OPENAI_API_KEY") in {None, "", "test_key", "fake-for-tests"}:
        skip_or_fail("Set a real OPENAI_API_KEY to execute live integration tests.")


@pytest.fixture(scope="session", autouse=True)
def verify_installed_sdk() -> Iterator[None]:
    agents = importlib.import_module("agents")
    if agents.__file__ is None:
        pytest.fail("agents does not expose an installed module path.")
    installed_path = Path(agents.__file__).resolve()
    environment = Path(sys.prefix).resolve()
    if not installed_path.is_relative_to(environment):
        pytest.fail(f"agents resolved outside the isolated environment: {installed_path}")
    if "site-packages" not in installed_path.parts:
        pytest.fail(f"agents did not resolve from an installed distribution: {installed_path}")
    yield


@pytest.fixture(scope="session")
def integration_model() -> str:
    return os.environ.get("OPENAI_AGENTS_INTEGRATION_MODEL", "gpt-5.6")


@pytest.fixture(scope="session")
def integration_realtime_model() -> str:
    return os.environ.get("OPENAI_AGENTS_INTEGRATION_REALTIME_MODEL", "gpt-realtime-2.1")


@pytest.fixture(scope="session")
def any_llm_models(integration_model: str) -> list[str]:
    configured = os.environ.get("OPENAI_AGENTS_INTEGRATION_ANY_LLM_MODELS", "")
    return [model.strip() for model in configured.split(",") if model.strip()] or [
        f"openai/{integration_model}"
    ]


@pytest.fixture(scope="session")
def litellm_models() -> list[str]:
    configured = os.environ.get("OPENAI_AGENTS_INTEGRATION_LITELLM_MODELS", "")
    return [model.strip() for model in configured.split(",") if model.strip()] or [
        "openai/gpt-4.1-mini"
    ]


@pytest.fixture(scope="session")
async def integration_pcm_audio() -> bytes:
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    audio = bytearray()
    request = client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice="alloy",
        input="Please say the words packaged voice ready.",
        response_format="pcm",
    )
    async with request as response:
        async for chunk in response.iter_bytes():
            audio.extend(chunk)

    if len(audio) % 2:
        audio.append(0)
    return bytes(audio)
