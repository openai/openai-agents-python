from __future__ import annotations

import json
from pathlib import PureWindowsPath
from typing import Any, cast

import httpx2
import pytest
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from agents import (
    Agent,
    OpenAIChatCompletionsModel,
    OutputGuardrailBlockedMessageArgs,
    RunConfig,
    Runner,
    SessionSettings,
    ToolExecutionConfig,
    ToolNameCollisionPolicy,
    ToolNotFoundBehavior,
)
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelProvider
from agents.run import __all__ as run_exports
from agents.run_config import SandboxConcurrencyLimits, SandboxRunConfig
from agents.sandbox.manifest import Manifest
from agents.sandbox.snapshot import NoopSnapshotSpec
from agents.testing import ScriptedModel

from .test_responses import get_text_message


class DummyProvider(ModelProvider):
    """A simple model provider that always returns the same model, and
    records the model name it was asked to provide."""

    def __init__(self, model_to_return: Model | None = None) -> None:
        self.last_requested: str | None = None
        self.model_to_return: Model = model_to_return or ScriptedModel()

    def get_model(self, model_name: str | None) -> Model:
        # record the requested model name and return our test model
        self.last_requested = model_name
        return self.model_to_return


def test_run_config_normalizes_first_party_dictionary_settings() -> None:
    config = RunConfig(
        model_settings={"reasoning": {"context": "all_turns"}, "temperature": 0.0},
        session_settings={"limit": 5},
        tool_execution={"max_function_tool_concurrency": 2},
        sandbox={
            "manifest": {"root": "/workspace"},
            "snapshot": {"type": "noop"},
            "concurrency_limits": {"manifest_entries": 3},
            "cwd": "tasks/a",
        },
    )

    assert isinstance(config.model_settings, ModelSettings)
    assert config.model_settings.reasoning is not None
    assert config.model_settings.reasoning.context == "all_turns"
    assert config.model_settings.temperature == 0.0
    assert isinstance(config.session_settings, SessionSettings)
    assert config.session_settings.limit == 5
    assert isinstance(config.tool_execution, ToolExecutionConfig)
    assert config.tool_execution.max_function_tool_concurrency == 2
    assert isinstance(config.sandbox, SandboxRunConfig)
    assert isinstance(config.sandbox.manifest, Manifest)
    assert isinstance(config.sandbox.snapshot, NoopSnapshotSpec)
    assert isinstance(config.sandbox.concurrency_limits, SandboxConcurrencyLimits)
    assert config.sandbox.concurrency_limits.manifest_entries == 3
    assert config.sandbox.cwd == "tasks/a"


def test_sandbox_run_config_normalizes_typed_cwd() -> None:
    config = SandboxRunConfig(cwd=PureWindowsPath("tasks/a"))

    assert config.cwd == "tasks/a"


@pytest.mark.parametrize(
    ("cwd", "message"),
    [
        ("", "sandbox.cwd must be non-empty"),
        ("/workspace/tasks/a", "sandbox.cwd must be workspace-relative"),
        ("tasks/../a", "sandbox.cwd must not contain parent segments"),
        (r"tasks\a", "sandbox.cwd must use POSIX path separators"),
        (PureWindowsPath("C:/tasks/a"), "sandbox.cwd must be workspace-relative"),
    ],
)
def test_sandbox_run_config_rejects_invalid_cwd(cwd: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SandboxRunConfig(cwd=cast(Any, cwd))


def test_run_config_preserves_typed_configuration_instances() -> None:
    settings = ModelSettings(temperature=0.2)
    session_settings = SessionSettings(limit=3)
    config = RunConfig(model_settings=settings, session_settings=session_settings)

    assert config.model_settings is settings
    assert config.session_settings is session_settings


def test_run_config_accepts_output_guardrail_blocked_message_customizers() -> None:
    def formatter(_args: OutputGuardrailBlockedMessageArgs[Any]) -> str:
        return "custom"

    assert RunConfig(
        output_guardrail_blocked_message="custom"
    ).output_guardrail_blocked_message == ("custom")
    assert RunConfig(
        output_guardrail_blocked_message=formatter
    ).output_guardrail_blocked_message is (formatter)


def test_run_config_rejects_async_output_guardrail_blocked_message_formatter() -> None:
    async def formatter(_args: OutputGuardrailBlockedMessageArgs[Any]) -> str:
        return "custom"

    with pytest.raises(
        TypeError,
        match="output_guardrail_blocked_message formatter must be synchronous",
    ):
        RunConfig(output_guardrail_blocked_message=cast(Any, formatter))


class _BlockedMessageStringSubclass(str):
    def __len__(self) -> int:
        raise RuntimeError("string-subclass-hook")


@pytest.mark.parametrize("value", ["", 123, _BlockedMessageStringSubclass("custom")])
def test_run_config_rejects_invalid_output_guardrail_blocked_message(value: object) -> None:
    with pytest.raises(
        (TypeError, ValueError),
        match="output_guardrail_blocked_message",
    ):
        RunConfig(output_guardrail_blocked_message=cast(Any, value))


def test_run_config_rejects_untrusted_manifest_path_grants() -> None:
    with pytest.raises(
        TypeError,
        match=r"sandbox\.manifest\.extra_path_grants must be configured on a trusted Manifest",
    ):
        RunConfig(sandbox={"manifest": {"extra_path_grants": [{"path": "/tmp"}]}})


@pytest.mark.parametrize(
    "manifest",
    [
        Manifest(root="/workspace").model_dump(),
        Manifest(root="/workspace").model_dump(mode="json"),
    ],
)
def test_run_config_accepts_serialized_manifest_without_path_grants(
    manifest: dict[str, object],
) -> None:
    config = RunConfig(sandbox={"manifest": manifest})

    assert config.sandbox is not None
    assert isinstance(config.sandbox.manifest, Manifest)
    assert config.sandbox.manifest.extra_path_grants == ()


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"model_settings": {"temperatur": 0.2}}, "Unknown model settings: temperatur"),
        ({"session_settings": {"limitt": 2}}, "Unknown session settings: limitt"),
        (
            {"tool_execution": {"max_function_tool_concurrenc": 2}},
            "Unknown run_config.tool_execution settings: max_function_tool_concurrenc",
        ),
    ],
)
def test_run_config_rejects_unknown_first_party_dictionary_fields(
    settings: dict[str, object], message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        RunConfig(**settings)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runner_accepts_dictionary_run_configuration() -> None:
    model = ScriptedModel(steps=[[get_text_message("done")]])
    agent = Agent(name="test", model=model)

    result = await Runner.run(
        agent,
        "hello",
        run_config={"model_settings": {"temperature": 0.0}},
    )

    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_model_provider_on_run_config_is_used_for_agent_model_name() -> None:
    """
    When the agent's ``model`` attribute is a string and no explicit model override is
    provided in the ``RunConfig``, the ``Runner`` should resolve the model using the
    ``model_provider`` on the ``RunConfig``.
    """
    scripted_model = ScriptedModel(steps=[[get_text_message("from-provider")]])
    provider = DummyProvider(model_to_return=scripted_model)
    agent = Agent(name="test", model="test-model")
    run_config = RunConfig(model_provider=provider)
    result = await Runner.run(agent, input="any", run_config=run_config)
    # We picked up the model from our dummy provider
    assert provider.last_requested == "test-model"
    assert result.final_output == "from-provider"


@pytest.mark.asyncio
async def test_run_config_model_name_override_takes_precedence() -> None:
    """
    When a model name string is set on the RunConfig, then that name should be looked up
    using the RunConfig's model_provider, and should override any model on the agent.
    """
    scripted_model = ScriptedModel(steps=[[get_text_message("override-name")]])
    provider = DummyProvider(model_to_return=scripted_model)
    agent = Agent(name="test", model="agent-model")
    run_config = RunConfig(model="override-name", model_provider=provider)
    result = await Runner.run(agent, input="any", run_config=run_config)
    # We should have requested the override name, not the agent.model
    assert provider.last_requested == "override-name"
    assert result.final_output == "override-name"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "reasoning_effort"),
    [("gpt-5", "low"), ("gpt-5.6", "none")],
)
async def test_run_config_model_name_override_uses_model_specific_default_settings(
    monkeypatch,
    model_name,
    reasoning_effort,
) -> None:
    """
    When RunConfig sets a model name, implicit settings should match that model name rather
    than the default fallback model.
    """
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-5.4-mini")
    scripted_model = ScriptedModel(steps=[[get_text_message("override-name")]])
    provider = DummyProvider(model_to_return=scripted_model)
    agent = Agent(name="test")
    run_config = RunConfig(model=model_name, model_provider=provider)
    result = await Runner.run(agent, input="any", run_config=run_config)
    assert result.final_output == "override-name"
    assert bool(scripted_model.calls)
    model_settings = scripted_model.calls[0].model_settings
    assert model_settings.reasoning is not None
    assert model_settings.reasoning.effort == reasoning_effort
    assert model_settings.verbosity == "low"


@pytest.mark.asyncio
async def test_run_config_model_settings_override_implicit_model_specific_defaults(
    monkeypatch,
) -> None:
    """
    RunConfig model settings should overlay the implicit defaults for the resolved model name.
    """
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-5.4-mini")
    scripted_model = ScriptedModel(steps=[[get_text_message("override-name")]])
    provider = DummyProvider(model_to_return=scripted_model)
    agent = Agent(name="test")
    run_config = RunConfig(
        model="gpt-5",
        model_provider=provider,
        model_settings=ModelSettings(temperature=0.3),
    )
    result = await Runner.run(agent, input="any", run_config=run_config)
    assert result.final_output == "override-name"
    assert bool(scripted_model.calls)
    model_settings = scripted_model.calls[0].model_settings
    assert model_settings.reasoning is not None
    assert model_settings.reasoning.effort == "low"
    assert model_settings.verbosity == "low"
    assert model_settings.temperature == 0.3


class NamedScriptedModel(ScriptedModel):
    def __init__(self, model: str) -> None:
        super().__init__(steps=[[get_text_message("chosen")]])
        self.model = model


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "run_streamed"])
@pytest.mark.parametrize(
    ("model_name", "agent_settings", "run_settings", "expected"),
    [
        pytest.param("other-model", None, None, ModelSettings(), id="non-gpt5"),
        pytest.param(
            "gpt-5",
            None,
            None,
            ModelSettings(reasoning=Reasoning(effort="low"), verbosity="low"),
            id="gpt5",
        ),
        pytest.param(
            "other-model",
            ModelSettings(temperature=0.3),
            None,
            ModelSettings(temperature=0.3),
            id="customized-agent",
        ),
        pytest.param("gpt-5", ModelSettings(), None, ModelSettings(), id="explicit-empty-agent"),
        pytest.param(
            "other-model",
            None,
            ModelSettings(reasoning=Reasoning(effort="none")),
            ModelSettings(reasoning=Reasoning(effort="none")),
            id="run-override-matches-old-default",
        ),
        pytest.param(
            None,
            None,
            None,
            ModelSettings(reasoning=Reasoning(effort="none"), verbosity="low"),
            id="unnamed-model",
        ),
    ],
)
async def test_provider_chosen_model_settings(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
    model_name: str | None,
    agent_settings: ModelSettings | None,
    run_settings: ModelSettings | None,
    expected: ModelSettings,
) -> None:
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-5.6-luna")
    model = (
        NamedScriptedModel(model_name)
        if model_name is not None
        else ScriptedModel(steps=[[get_text_message("chosen")]])
    )
    provider = DummyProvider(model)
    agent = (
        Agent(name="test", model_settings=agent_settings)
        if agent_settings is not None
        else Agent(name="test")
    )
    original_settings = agent.model_settings
    run_config = RunConfig(model_provider=provider, model_settings=run_settings)
    if streamed:
        result = Runner.run_streamed(agent, "hi", run_config=run_config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "hi", run_config=run_config)
    assert result.final_output == "chosen"
    assert provider.last_requested is None
    assert model.calls[0].model_settings == expected
    assert agent.model_settings is original_settings


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["agent-name", "run-name", "agent-object", "run-object"])
async def test_explicit_model_settings_precede_provider_reported_name(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-5.6-luna")
    model = NamedScriptedModel("other-model" if source.endswith("name") else "gpt-5")
    provider = DummyProvider(model)
    selection = "gpt-5" if source.endswith("name") else model
    agent = Agent(name="test", model=selection if source.startswith("agent") else None)
    config = RunConfig(
        model=selection if source.startswith("run") else None, model_provider=provider
    )
    result = await Runner.run(agent, "hi", run_config=config)
    assert result.final_output == "chosen"
    expected = (
        ModelSettings(reasoning=Reasoning(effort="low"), verbosity="low")
        if source.endswith("name")
        else ModelSettings()
    )
    assert model.calls[0].model_settings == expected


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_provider_chosen_chat_model_omits_gpt5_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-5.6-luna")
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "other-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "chosen"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    async with AsyncOpenAI(
        api_key="test-key",
        base_url="https://provider.example.test/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        provider = DummyProvider(
            OpenAIChatCompletionsModel(model="other-model", openai_client=client)
        )
        result = await Runner.run(
            Agent(name="test"), "hi", run_config=RunConfig(model_provider=provider)
        )
    assert result.final_output == "chosen"
    assert len(bodies) == 1
    assert bodies[0]["model"] == "other-model"
    assert "reasoning_effort" not in bodies[0]
    assert "verbosity" not in bodies[0]


@pytest.mark.asyncio
async def test_run_config_model_override_object_takes_precedence() -> None:
    """
    When a concrete Model instance is set on the RunConfig, then that instance should be
    returned by AgentRunner._get_model regardless of the agent's model.
    """
    scripted_model = ScriptedModel(steps=[[get_text_message("override-object")]])
    agent = Agent(name="test", model="agent-model")
    run_config = RunConfig(model=scripted_model)
    result = await Runner.run(agent, input="any", run_config=run_config)
    # The ScriptedModel on the RunConfig should have been used.
    assert result.final_output == "override-object"


@pytest.mark.asyncio
async def test_agent_model_object_is_used_when_present() -> None:
    """
    If the agent has a concrete Model object set as its model, and the RunConfig does
    not specify a model override, then that object should be used directly without
    consulting the RunConfig's model_provider.
    """
    scripted_model = ScriptedModel(steps=[[get_text_message("from-agent-object")]])
    provider = DummyProvider()
    agent = Agent(name="test", model=scripted_model)
    run_config = RunConfig(model_provider=provider)
    result = await Runner.run(agent, input="any", run_config=run_config)
    # The dummy provider should never have been called, and the output should come from
    # the ScriptedModel on the agent.
    assert provider.last_requested is None
    assert result.final_output == "from-agent-object"


def test_trace_include_sensitive_data_defaults_to_true_when_env_not_set(monkeypatch):
    """By default, trace_include_sensitive_data should be True when the env is not set."""
    monkeypatch.delenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", raising=False)
    config = RunConfig()
    assert config.trace_include_sensitive_data is True


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
    ids=[
        "lowercase-true",
        "capital-True",
        "numeric-1",
        "text-yes",
        "text-on",
        "lowercase-false",
        "capital-False",
        "numeric-0",
        "text-no",
        "text-off",
    ],
)
def test_trace_include_sensitive_data_follows_env_value(env_value, expected, monkeypatch):
    """trace_include_sensitive_data should follow the environment variable if not explicitly set."""
    monkeypatch.setenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", env_value)
    config = RunConfig()
    assert config.trace_include_sensitive_data is expected


def test_trace_include_sensitive_data_explicit_override_takes_precedence(monkeypatch):
    """Explicit value passed to RunConfig should take precedence over the environment variable."""
    monkeypatch.setenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "false")
    config = RunConfig(trace_include_sensitive_data=True)
    assert config.trace_include_sensitive_data is True

    monkeypatch.setenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "true")
    config = RunConfig(trace_include_sensitive_data=False)
    assert config.trace_include_sensitive_data is False


def test_tool_execution_config_rejects_invalid_function_tool_concurrency() -> None:
    with pytest.raises(
        ValueError,
        match="tool_execution.max_function_tool_concurrency must be at least 1",
    ):
        ToolExecutionConfig(max_function_tool_concurrency=0)


def test_tool_execution_config_is_public_from_agents_package() -> None:
    config = RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=2))

    assert config.tool_execution is not None
    assert config.tool_execution.max_function_tool_concurrency == 2


def test_tool_not_found_behavior_defaults_to_raise_error() -> None:
    config = RunConfig()

    assert config.tool_not_found_behavior == "raise_error"


def test_tool_not_found_behavior_is_public_from_agents_package() -> None:
    behavior: ToolNotFoundBehavior = "return_error_to_model"
    config = RunConfig(tool_not_found_behavior=behavior)

    assert config.tool_not_found_behavior == "return_error_to_model"


def test_tool_name_collision_policy_defaults_to_warn() -> None:
    config = RunConfig()

    assert config.tool_name_collision_policy == "warn"


def test_tool_name_collision_policy_is_public_from_agents_package() -> None:
    policy: ToolNameCollisionPolicy = "error"
    config = RunConfig(tool_name_collision_policy=policy)

    assert config.tool_name_collision_policy == "error"
    assert "ToolNameCollisionPolicy" in run_exports


def test_tool_name_collision_policy_rejects_invalid_value() -> None:
    with pytest.raises(
        ValueError,
        match="tool_name_collision_policy must be either 'warn' or 'error'",
    ):
        RunConfig(tool_name_collision_policy=cast(Any, "erorr"))


@pytest.mark.asyncio
async def test_runner_dictionary_rejects_invalid_tool_name_collision_policy() -> None:
    model = ScriptedModel(steps=[[get_text_message("done")]])
    agent = Agent(name="test", model=model)

    with pytest.raises(
        ValueError,
        match="tool_name_collision_policy must be either 'warn' or 'error'",
    ):
        await Runner.run(
            agent,
            "hello",
            run_config={"tool_name_collision_policy": cast(Any, "erorr")},
        )

    assert not model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("default_name", ["gpt-4o", "gpt-5.6-luna"])
async def test_provider_preserves_explicit_settings_equal_to_environment_defaults(
    monkeypatch: pytest.MonkeyPatch, streamed: bool, default_name: str
) -> None:
    from agents.models.default_models import get_default_model_settings

    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", default_name)
    explicit = get_default_model_settings()
    agent = Agent(name="test", model_settings=explicit)
    model = NamedScriptedModel("gpt-5" if default_name == "gpt-4o" else "other-model")
    config = RunConfig(model_provider=DummyProvider(model))
    if streamed:
        result = Runner.run_streamed(agent, "hi", run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "hi", run_config=config)
    assert result.final_output == "chosen"
    assert model.calls[0].model_settings == explicit
    assert agent.model_settings is explicit


def test_any_llm_provider_default_uses_canonical_model_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("any_llm")
    from agents.extensions.models.any_llm_provider import AnyLLMProvider
    from agents.run_internal.turn_preparation import get_model_settings

    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-5.6-luna")
    config = RunConfig(model_provider=AnyLLMProvider())
    model = config.model_provider.get_model(None)
    assert get_model_settings(Agent(name="test"), config, model) == ModelSettings(
        reasoning=Reasoning(effort="none"), verbosity="low"
    )


@pytest.mark.asyncio
async def test_provider_model_can_supply_defaults_without_a_string_identifier() -> None:
    class ProviderModel(ScriptedModel):
        def get_default_model_settings(self) -> ModelSettings:
            return ModelSettings(temperature=0.2)

    model = ProviderModel(steps=[[get_text_message("chosen")]])
    await Runner.run(
        Agent(name="test"),
        "hi",
        run_config=RunConfig(model_provider=DummyProvider(model), model_settings={"top_p": 0.5}),
    )
    assert model.calls[0].model_settings == ModelSettings(temperature=0.2, top_p=0.5)


def test_invalid_settings_are_validated_before_implicit_default_detection() -> None:
    from unittest.mock import Mock

    with pytest.raises(TypeError, match="Agent model_settings must be a ModelSettings instance"):
        Agent(name="test", model_settings=Mock())


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["any-llm", "litellm"])
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        (
            "openrouter/openai/gpt-5.4-mini",
            ModelSettings(reasoning=Reasoning(effort="none"), verbosity="low"),
        ),
        (
            "openai/gpt-5",
            ModelSettings(reasoning=Reasoning(effort="low"), verbosity="low"),
        ),
        ("openrouter/anthropic/claude-sonnet-4.5", ModelSettings()),
        ("openrouter/other-vendor/gpt-5.4-mini", ModelSettings()),
        ("ollama/gpt-5.4-mini", ModelSettings()),
    ],
)
async def test_provider_selected_routed_model_defaults(
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
    streamed: bool,
    model_name: str,
    expected: ModelSettings,
) -> None:
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-5.6-luna")
    model: Model
    if adapter == "any-llm":
        pytest.importorskip("any_llm")
        from agents.extensions.models.any_llm_model import AnyLLMModel

        model = AnyLLMModel(model=model_name)
    else:
        pytest.importorskip("litellm")
        from agents.extensions.models.litellm_model import LitellmModel

        model = LitellmModel(model=model_name)
    scripted = ScriptedModel(steps=[[get_text_message("chosen")]])
    monkeypatch.setattr(model, "get_response", scripted.get_response)
    monkeypatch.setattr(model, "stream_response", scripted.stream_response)
    config = RunConfig(model_provider=DummyProvider(model))
    agent = Agent(name="test")
    if streamed:
        result = Runner.run_streamed(agent, "hi", run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "hi", run_config=config)
    assert result.final_output == "chosen"
    assert scripted.calls[0].model_settings == expected
    assert model.model == model_name


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    ("initial_model", "later_model", "expected"),
    [
        (
            "gpt-5.4-mini",
            "gpt-4.1",
            ModelSettings(reasoning=Reasoning(effort="none"), verbosity="low"),
        ),
        ("gpt-4.1", "gpt-5.4-mini", ModelSettings()),
    ],
)
async def test_unnamed_provider_model_retains_captured_defaults(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
    initial_model: str,
    later_model: str,
    expected: ModelSettings,
) -> None:
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", initial_model)
    agent = Agent(name="test")
    captured_settings = agent.model_settings
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", later_model)
    model = ScriptedModel(steps=[[get_text_message("chosen")]])
    config = RunConfig(model_provider=DummyProvider(model))
    if streamed:
        result = Runner.run_streamed(agent, "hi", run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "hi", run_config=config)
    assert result.final_output == "chosen"
    assert model.calls[0].model_settings == expected
    assert agent.model_settings is captured_settings
    assert agent.model_settings == expected
