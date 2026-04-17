from __future__ import annotations

import pytest

from agents import (
    OpenAIAgentRegistrationConfig,
    RunConfig,
    set_default_openai_agent_registration,
    set_default_openai_harness,
)
from agents.models.multi_provider import MultiProvider
from agents.models.openai_agent_registration import (
    OPENAI_HARNESS_ID_TRACE_METADATA_KEY,
    resolve_openai_agent_registration_config,
)
from agents.models.openai_provider import OpenAIProvider
from agents.run_internal.agent_runner_helpers import resolve_trace_settings
from agents.tracing import agent_span, trace


def test_agent_registration_config_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_AGENT_HARNESS_ID", "env-harness")
    set_default_openai_agent_registration(
        OpenAIAgentRegistrationConfig(harness_id="default-harness")
    )

    try:
        resolved = resolve_openai_agent_registration_config(
            OpenAIAgentRegistrationConfig(harness_id="explicit-harness")
        )
    finally:
        set_default_openai_agent_registration(None)

    assert resolved is not None
    assert resolved.harness_id == "explicit-harness"


def test_agent_registration_uses_default_before_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_AGENT_HARNESS_ID", "env-harness")
    set_default_openai_agent_registration(
        OpenAIAgentRegistrationConfig(harness_id="default-harness")
    )

    try:
        resolved = resolve_openai_agent_registration_config(None)
    finally:
        set_default_openai_agent_registration(None)

    assert resolved is not None
    assert resolved.harness_id == "default-harness"


def test_agent_registration_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_AGENT_HARNESS_ID", "env-harness")

    resolved = resolve_openai_agent_registration_config(None)

    assert resolved is not None
    assert resolved.harness_id == "env-harness"


def test_set_default_openai_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_AGENT_HARNESS_ID", "env-harness")
    set_default_openai_harness("helper-harness")

    try:
        resolved = resolve_openai_agent_registration_config(None)
    finally:
        set_default_openai_harness(None)

    assert resolved is not None
    assert resolved.harness_id == "helper-harness"


def test_agent_registration_disabled_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_AGENT_HARNESS_ID", raising=False)

    assert resolve_openai_agent_registration_config(None) is None


def test_agent_registration_provider_constructor_config() -> None:
    config = OpenAIAgentRegistrationConfig(harness_id="provider-harness")

    openai_provider = OpenAIProvider(agent_registration=config)
    multi_provider = MultiProvider(openai_agent_registration=config)

    assert openai_provider.agent_registration is not None
    assert openai_provider.agent_registration.harness_id == "provider-harness"
    assert multi_provider.openai_provider.agent_registration is not None
    assert multi_provider.openai_provider.agent_registration.harness_id == "provider-harness"


def test_harness_id_is_added_to_trace_metadata() -> None:
    provider = OpenAIProvider(
        agent_registration=OpenAIAgentRegistrationConfig(harness_id="provider-harness")
    )

    _, _, _, metadata, _ = resolve_trace_settings(
        run_state=None,
        run_config=RunConfig(model_provider=provider),
    )

    assert metadata == {OPENAI_HARNESS_ID_TRACE_METADATA_KEY: "provider-harness"}


def test_harness_id_preserves_explicit_trace_metadata() -> None:
    provider = OpenAIProvider(
        agent_registration=OpenAIAgentRegistrationConfig(harness_id="provider-harness")
    )

    _, _, _, metadata, _ = resolve_trace_settings(
        run_state=None,
        run_config=RunConfig(
            model_provider=provider,
            trace_metadata={
                OPENAI_HARNESS_ID_TRACE_METADATA_KEY: "explicit-harness",
                "source": "test",
            },
        ),
    )

    assert metadata == {
        OPENAI_HARNESS_ID_TRACE_METADATA_KEY: "explicit-harness",
        "source": "test",
    }


def test_env_harness_id_is_added_to_trace_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_AGENT_HARNESS_ID", "env-harness")

    _, _, _, metadata, _ = resolve_trace_settings(
        run_state=None,
        run_config=RunConfig(),
    )

    assert metadata == {OPENAI_HARNESS_ID_TRACE_METADATA_KEY: "env-harness"}


def test_harness_id_trace_metadata_propagates_to_spans() -> None:
    provider = OpenAIProvider(
        agent_registration=OpenAIAgentRegistrationConfig(harness_id="provider-harness")
    )
    workflow_name, trace_id, group_id, metadata, _ = resolve_trace_settings(
        run_state=None,
        run_config=RunConfig(model_provider=provider),
    )

    with trace(
        workflow_name=workflow_name,
        trace_id=trace_id,
        group_id=group_id,
        metadata=metadata,
    ):
        with agent_span(name="agent") as span:
            assert span.trace_metadata == {OPENAI_HARNESS_ID_TRACE_METADATA_KEY: "provider-harness"}
            span_export = span.export()
            assert span_export is not None
            assert span_export["metadata"] == {
                OPENAI_HARNESS_ID_TRACE_METADATA_KEY: "provider-harness"
            }
