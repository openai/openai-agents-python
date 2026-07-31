from __future__ import annotations

import pytest

from agents import (
    Agent,
    RunConfig,
    Runner,
    SessionSettings,
    ToolExecutionConfig,
    ToolNotFoundBehavior,
)
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelProvider
from agents.run_config import SandboxConcurrencyLimits, SandboxRunConfig
from agents.sandbox.manifest import Manifest
from agents.sandbox.snapshot import NoopSnapshotSpec

from .fake_model import FakeModel
from .test_responses import get_text_message


class DummyProvider(ModelProvider):
    """A simple model provider that always returns the same model, and
    records the model name it was asked to provide."""

    def __init__(self, model_to_return: Model | None = None) -> None:
        self.last_requested: str | None = None
        self.model_to_return: Model = model_to_return or FakeModel()

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


def test_run_config_preserves_typed_configuration_instances() -> None:
    settings = ModelSettings(temperature=0.2)
    session_settings = SessionSettings(limit=3)
    config = RunConfig(model_settings=settings, session_settings=session_settings)

    assert config.model_settings is settings
    assert config.session_settings is session_settings


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
    model = FakeModel(initial_output=[get_text_message("done")])
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
    fake_model = FakeModel(initial_output=[get_text_message("from-provider")])
    provider = DummyProvider(model_to_return=fake_model)
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
    fake_model = FakeModel(initial_output=[get_text_message("override-name")])
    provider = DummyProvider(model_to_return=fake_model)
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
    fake_model = FakeModel(initial_output=[get_text_message("override-name")])
    provider = DummyProvider(model_to_return=fake_model)
    agent = Agent(name="test")
    run_config = RunConfig(model=model_name, model_provider=provider)
    result = await Runner.run(agent, input="any", run_config=run_config)
    assert result.final_output == "override-name"
    assert fake_model.first_turn_args is not None
    model_settings = fake_model.first_turn_args["model_settings"]
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
    fake_model = FakeModel(initial_output=[get_text_message("override-name")])
    provider = DummyProvider(model_to_return=fake_model)
    agent = Agent(name="test")
    run_config = RunConfig(
        model="gpt-5",
        model_provider=provider,
        model_settings=ModelSettings(temperature=0.3),
    )
    result = await Runner.run(agent, input="any", run_config=run_config)
    assert result.final_output == "override-name"
    assert fake_model.first_turn_args is not None
    model_settings = fake_model.first_turn_args["model_settings"]
    assert model_settings.reasoning.effort == "low"
    assert model_settings.verbosity == "low"
    assert model_settings.temperature == 0.3


@pytest.mark.asyncio
async def test_run_config_model_override_object_takes_precedence() -> None:
    """
    When a concrete Model instance is set on the RunConfig, then that instance should be
    returned by AgentRunner._get_model regardless of the agent's model.
    """
    fake_model = FakeModel(initial_output=[get_text_message("override-object")])
    agent = Agent(name="test", model="agent-model")
    run_config = RunConfig(model=fake_model)
    result = await Runner.run(agent, input="any", run_config=run_config)
    # Our FakeModel on the RunConfig should have been used.
    assert result.final_output == "override-object"


@pytest.mark.asyncio
async def test_agent_model_object_is_used_when_present() -> None:
    """
    If the agent has a concrete Model object set as its model, and the RunConfig does
    not specify a model override, then that object should be used directly without
    consulting the RunConfig's model_provider.
    """
    fake_model = FakeModel(initial_output=[get_text_message("from-agent-object")])
    provider = DummyProvider()
    agent = Agent(name="test", model=fake_model)
    run_config = RunConfig(model_provider=provider)
    result = await Runner.run(agent, input="any", run_config=run_config)
    # The dummy provider should never have been called, and the output should come from
    # the FakeModel on the agent.
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
