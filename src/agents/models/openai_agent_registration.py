from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

_ENV_HARNESS_ID = "OPENAI_AGENT_HARNESS_ID"
OPENAI_HARNESS_ID_TRACE_METADATA_KEY = "agent_harness_id"


@dataclass(frozen=True)
class OpenAIAgentRegistrationConfig:
    harness_id: str | None


@dataclass(frozen=True)
class ResolvedOpenAIAgentRegistrationConfig:
    harness_id: str


_default_agent_registration: OpenAIAgentRegistrationConfig | None = None


def set_default_openai_agent_registration_config(
    config: OpenAIAgentRegistrationConfig | None,
) -> None:
    global _default_agent_registration
    _default_agent_registration = config


def get_default_openai_agent_registration_config() -> OpenAIAgentRegistrationConfig | None:
    return _default_agent_registration


def resolve_openai_agent_registration_config(
    config: OpenAIAgentRegistrationConfig | None,
) -> ResolvedOpenAIAgentRegistrationConfig | None:
    default = get_default_openai_agent_registration_config()
    harness_id = _resolve_str(
        explicit=config.harness_id if config else None,
        default=default.harness_id if default else None,
        env_name=_ENV_HARNESS_ID,
    )
    if harness_id is None:
        return None
    return ResolvedOpenAIAgentRegistrationConfig(harness_id=harness_id)


def resolve_openai_harness_id_for_model_provider(model_provider: Any) -> str | None:
    """Return the configured harness ID for OpenAI-backed model providers."""
    harness_id = _harness_id_from_model_provider(model_provider)
    if harness_id is not None:
        return harness_id
    resolved = resolve_openai_agent_registration_config(None)
    return resolved.harness_id if resolved is not None else None


def add_openai_harness_id_to_metadata(
    metadata: dict[str, Any] | None,
    *,
    model_provider: Any,
) -> dict[str, Any] | None:
    harness_id = resolve_openai_harness_id_for_model_provider(model_provider)
    if harness_id is None:
        return metadata
    if metadata is not None and OPENAI_HARNESS_ID_TRACE_METADATA_KEY in metadata:
        return metadata

    updated_metadata = dict(metadata or {})
    updated_metadata[OPENAI_HARNESS_ID_TRACE_METADATA_KEY] = harness_id
    return updated_metadata


def _harness_id_from_model_provider(model_provider: Any) -> str | None:
    registration = getattr(model_provider, "agent_registration", None)
    harness_id = _harness_id_from_registration(registration)
    if harness_id is not None:
        return harness_id

    registration = getattr(model_provider, "_agent_registration", None)
    harness_id = _harness_id_from_registration(registration)
    if harness_id is not None:
        return harness_id

    openai_provider = getattr(model_provider, "openai_provider", None)
    if openai_provider is not None and openai_provider is not model_provider:
        return _harness_id_from_model_provider(openai_provider)
    return None


def _harness_id_from_registration(registration: Any) -> str | None:
    if registration is None:
        return None
    harness_id = getattr(registration, "harness_id", None)
    return harness_id if isinstance(harness_id, str) and harness_id.strip() else None


def _resolve_str(*, explicit: str | None, default: str | None, env_name: str) -> str | None:
    for candidate in (explicit, default, os.getenv(env_name)):
        if candidate is None:
            continue
        stripped = candidate.strip()
        if stripped:
            return stripped
    return None
