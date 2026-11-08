"""Decisions API declarations and unsupported surfaces fail before native work."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    CapabilityError,
    CodexLogin,
    FunctionTool,
    HttpMCP,
    NativeProvider,
    PromptedOutput,
    Session,
    SessionState,
    function_tool,
)


class Arguments(BaseModel):
    enabled: bool


@function_tool
def toggle(enabled: bool) -> str:
    return str(enabled)


def test_api_exports_defaults_and_immutability():
    import agents
    import agents.agent as agent_module

    for name in ("Decisions", "DecisionModel"):
        assert name not in agents.__all__
        assert not hasattr(agents, name)
        assert not hasattr(agent_module, name)
    agent = Agent(instructions="Classify.", api="decisions", fallback_model="generation")
    assert agent.api == "decisions" and agent.model is None
    assert agent.fallback_model == "generation"
    with pytest.raises(FrozenInstanceError):
        agent.fallback_model = "other"
    assert Agent(instructions="Answer.").api == "responses"
    with pytest.raises(TypeError, match="model must be a string"):
        Agent(instructions="Answer.", model=object())


@pytest.mark.parametrize("value", ["", " "])
async def test_decisions_rejects_blank_model_before_native_work(value):
    agent = Agent(instructions="Classify.", model=value, api="decisions")
    with pytest.raises(ValueError, match="model must be a non-empty string"):
        agent.validate()
    with pytest.raises(ValueError, match="model must be a non-empty string"):
        async with agent.session():
            pytest.fail("Invalid Decisions model acquired native resources")


@pytest.mark.parametrize("api", ["responses", "decisions"])
@pytest.mark.parametrize("value", ["", " ", 1])
def test_invalid_fallback_names(api, value):
    with pytest.raises(ValueError, match="fallback_model must be a non-empty string"):
        Agent(instructions="Answer.", api=api, fallback_model=value)


@pytest.mark.parametrize("api", ["responses", "decisions"])
@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), True, "0.5"])
def test_invalid_route_probability(api, value):
    with pytest.raises(ValueError, match="min_route_probability must be a finite number"):
        Agent(instructions="Answer.", api=api, min_route_probability=value)


@pytest.mark.parametrize("value", [0, 0.5, 1])
def test_probability_bounds(value):
    assert Agent(instructions="Answer.", min_route_probability=value).min_route_probability == value


@pytest.mark.parametrize(
    "options, message",
    [
        ({"programmatic_tool_calling": True}, "programmatic tool calling"),
        ({"tools": [replace(toggle, defer_loading=True)]}, "eager bound FunctionTool"),
        ({"output_type": PromptedOutput(Arguments)}, "native structured output"),
        ({"provider": NativeProvider(auth=CodexLogin())}, "API-key authentication"),
        (
            {"tools": [HttpMCP(name="catalog", url="https://example.invalid/mcp", tools=[])]},
            "eager bound FunctionTool",
        ),
        (
            {
                "tools": [
                    FunctionTool(
                        name="manual", description="Manual.", input_type=Arguments, output_type=str
                    )
                ]
            },
            "eager bound FunctionTool",
        ),
    ],
)
async def test_unsupported_declarations_fail_without_native_work(options, message):
    agent = Agent(instructions="Classify.", model="decision-test", api="decisions", **options)
    with pytest.raises(ValueError, match=message):
        agent.validate()
    with pytest.raises(ValueError, match=message):
        async with agent.session():
            pytest.fail("Unsupported declaration opened a Session")


async def test_environment_and_state_capture_rejected_before_native_work():
    agent = Agent(instructions="Classify.", model="decision-test", api="decisions")
    with pytest.raises(ValueError, match="checkpoints, storage or environments"):
        async with agent.session(environment=object()):
            pytest.fail("Unsupported environment opened a Session")
    state = SessionState(
        format_version=2,
        codex_revision="synthetic-revision",
        source_session_id="test",
        history=[],
        root_metadata={},
    )
    with pytest.raises(ValueError, match="checkpoints, storage or environments"):
        async with agent.session(state=state):
            pytest.fail("Decision checkpoint restored without exact model configuration")
    with pytest.raises(ValueError, match="checkpoint capture or restoration"):
        await agent.run("Classify.", return_state=True)


async def test_run_override_forwards_descriptor_and_keeps_provider_owner():
    provider = NativeProvider(api_key="synthetic-decision-key")
    agent = Agent(instructions="Classify.", tools=[toggle], provider=provider)
    descriptor = "decisions"
    captured = {}
    result = SimpleNamespace(final_output="accepted")

    async def get_result():
        return result

    def prepare(*args, **kwargs):
        return None

    async def submit(input, **options):
        assert input == "Classify."
        assert options["prepare_request"] is prepare
        return SimpleNamespace(get_result=get_result)

    @asynccontextmanager
    async def session(**options):
        captured.update(options)
        yield SimpleNamespace(
            _interaction_handler=None,
            _opening_agent=agent,
            _default_tools=None,
            _bindings=(),
            _native_agents=None,
            _executor=None,
            submit=submit,
        )

    agent.session = session
    assert (
        await agent.run(
            "Classify.", model="override-decision", api=descriptor, prepare_request=prepare
        )
        is result
    )
    assert captured["model"] == "override-decision"
    assert captured["api"] is descriptor
    assert agent.provider is provider
    assert agent.model is None


async def test_decisions_session_rejects_model_mutation_and_capture():
    session = object.__new__(Session)
    session._opening_agent = Agent(instructions="Classify.", model="decision-test", api="decisions")
    session._check_idle_update = lambda: None
    with pytest.raises(ValueError, match="Model updates are unsupported"):
        await session.update(model="another-decision")
    with pytest.raises(ValueError, match="Model updates are unsupported"):
        await session.update(model_settings={"reasoning_effort": "low"})
    with pytest.raises(CapabilityError, match="checkpoint capture or restoration"):
        session._check_portable_capture()
    with pytest.raises(TypeError):
        await session.update(api="responses")


async def test_responses_update_model_keeps_string_contract():
    session = object.__new__(Session)
    session._opening_agent = Agent(instructions="Answer.")
    session._check_idle_update = lambda: None
    with pytest.raises(TypeError, match="model must be a string"):
        await session.update(model=object())


def test_decision_request_observation_preserves_actual_wire():
    from _agents_codex.observation import ModelRequestDispatching, _request_observation

    observation = _request_observation(
        {
            "type": "model_request_dispatching",
            "request_id": "decision-request",
            "thread_id": "decision-thread",
            "turn_id": "decision-turn",
            "model": "decision-test",
            "reasoning_effort": None,
            "reasoning_summary": None,
            "service_tier": None,
            "wire": "decisions",
            "transport": "http_json",
        },
        session_id="decision-session",
        submission_id="decision-submission",
    )
    assert isinstance(observation, ModelRequestDispatching)
    assert observation.model == "decision-test"
    assert observation.wire == "decisions"
    assert observation.transport == "http_json"


async def test_prepared_request_preflight_uses_decision_http_transport(monkeypatch):
    class OpeningReached(Exception):
        pass

    async def stop_before_native(*args, **kwargs):
        raise OpeningReached

    monkeypatch.setattr(Session, "_open", stop_before_native)
    ordinary = Agent(instructions="Classify.")
    with pytest.raises(OpeningReached):
        await ordinary.run(
            "Classify.", model="decision-test", api="decisions", prepare_request=lambda *args: None
        )
    # A string model retains the provider's existing WebSocket admission rule.
    with pytest.raises(ValueError, match="HTTP"):
        await ordinary.run("Classify.", model="responses-test", prepare_request=lambda *args: None)


async def test_per_turn_prompted_output_cannot_bypass_decision_profile():
    session = object.__new__(Session)
    session._opening_agent = Agent(instructions="Classify.", model="decision-test", api="decisions")
    session._check_open = lambda: None
    session._check_graph_admission = lambda: None
    session._check_root_admission = lambda: None
    session._check_callback_operation = lambda *args: None
    session._mutation = None
    session._active = None
    session._pending = []
    session._checkpoint_task = None
    with pytest.raises(ValueError, match="native structured output"):
        await session.submit("Classify.", output_type=PromptedOutput(Arguments))


@pytest.mark.parametrize("api", [None, "unknown", 1])
def test_invalid_agent_api(api):
    with pytest.raises(TypeError, match="api must be"):
        Agent(instructions="Answer.", api=api)


@pytest.mark.parametrize("entry", ["session", "run", "run_sync"])
@pytest.mark.parametrize(
    "declared, overrides, expected_model, expected_config",
    [
        ({"api": "decisions"}, {}, "gpt-6-luna", {"model": "gpt-6-luna"}),
        (
            {"api": "decisions", "model": "custom-alias"},
            {},
            "custom-alias",
            {"model": "custom-alias"},
        ),
        (
            {"api": "decisions", "model": "base-model"},
            {"api": "decisions"},
            "base-model",
            {"model": "base-model"},
        ),
        (
            {"api": "decisions", "model": "base-model"},
            {"model": "other"},
            "other",
            {"model": "other"},
        ),
        ({"model": "responses-model"}, {"api": "decisions"}, "gpt-6-luna", {"model": "gpt-6-luna"}),
        (
            {"model": "responses-model"},
            {"api": "decisions", "model": "explicit"},
            "explicit",
            {"model": "explicit"},
        ),
        ({"api": "decisions", "model": "base-model"}, {"api": "responses"}, None, None),
        ({"api": "decisions"}, {"api": "responses", "model": "generation"}, "generation", None),
        ({"model": "responses-model"}, {"api": "responses"}, "responses-model", None),
        (
            {"api": "decisions", "fallback_model": "fallback", "min_route_probability": 0.8},
            {},
            "fallback",
            {"model": "gpt-6-luna", "fallback_model": "fallback", "min_route_probability": 0.8},
        ),
        ({"fallback_model": "fallback", "min_route_probability": 0.8}, {}, None, None),
        (
            {"fallback_model": "fallback", "min_route_probability": 0.8},
            {"api": "decisions"},
            "fallback",
            {"model": "gpt-6-luna", "fallback_model": "fallback", "min_route_probability": 0.8},
        ),
        ({"api": "decisions", "fallback_model": "fallback"}, {"api": "responses"}, None, None),
        (
            {"api": "decisions", "model": "base-model"},
            {"api": None, "model": None},
            "base-model",
            {"model": "base-model"},
        ),
    ],
)
async def test_opening_and_run_capture_api_model_overrides_atomically(
    monkeypatch, entry, declared, overrides, expected_model, expected_config
):
    import _agents_codex.session as implementation

    original_prepare = implementation._prepare_session_model
    captures = []

    class Captured(Exception):
        pass

    def capture(agent, **options):
        captures.append((agent, original_prepare(agent, **options)))
        raise Captured

    monkeypatch.setattr(implementation, "_prepare_session_model", capture)
    agent = Agent(instructions="Classify.", **declared)
    with pytest.raises(Captured):
        if entry == "session":
            async with agent.session(**overrides):
                pytest.fail("Capture must stop before native acquisition")
        elif entry == "run":
            await agent.run("Input.", **overrides)
        else:
            await asyncio.to_thread(agent.run_sync, "Input.", **overrides)
    effective, (native_model, config) = captures[0]
    assert effective.api == (overrides.get("api") or declared.get("api", "responses"))
    assert native_model == expected_model
    assert config.get("decision_model") == expected_config
    assert agent.model == declared.get("model")
    assert agent.api == declared.get("api", "responses")
    assert effective.fallback_model == declared.get("fallback_model")
    if expected_config is None:
        assert config == {}
    else:
        assert config["supports_websockets"] is False
        assert config["stream_max_retries"] == 0


@pytest.mark.parametrize("captured_restore", [False, True])
async def test_restore_rejects_decisions_and_responses_reset_keeps_saved_model(
    monkeypatch, captured_restore
):
    import _agents_codex.session as implementation

    state = SessionState(
        format_version=2,
        codex_revision="synthetic-revision",
        source_session_id="test",
        history=[],
        root_metadata={},
    )
    agent = Agent(instructions="Restore.", model="decision-model", api="decisions")
    captured = Agent(instructions="Restore.", model="captured-model")
    original_prepare = implementation._prepare_session_model
    captures = []

    class Captured(Exception):
        pass

    def capture(declaration, **options):
        value = original_prepare(declaration, **options)
        captures.append((declaration, options, value))
        raise Captured

    monkeypatch.setattr(implementation, "_prepare_session_model", capture)
    token = None
    if captured_restore:
        selected = SimpleNamespace(
            agent=agent, declaration=captured, state=state, owner=asyncio.current_task()
        )
        token = implementation._restore_declaration.set(selected)
    try:
        with pytest.raises(ValueError, match="checkpoints, storage or environments"):
            async with agent.session(state=state, api="decisions"):
                pytest.fail("Decisions restore acquired resources")
        with pytest.raises(Captured):
            async with agent.session(state=state, api="responses"):
                pytest.fail("Responses reset did not reach restore admission")
        effective, options, (_, config) = captures[0]
        assert effective.api == "responses" and options["state"] is state
        assert config == {}
        assert agent.api == "decisions"
        with pytest.raises(ValueError, match="Opening model"):
            async with agent.session(
                state=state, model="must-not-replace-saved-model", api="responses"
            ):
                pytest.fail("Explicit model override replaced a saved model")
    finally:
        if token is not None:
            implementation._restore_declaration.reset(token)
