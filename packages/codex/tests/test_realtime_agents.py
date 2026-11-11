"""Contextual voice declarations retain exact captured execution sources."""

import asyncio
import json
from contextvars import copy_context
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field, RootModel, ValidationError
from typing_extensions import TypedDict

from _agents_codex._agent_profiles import _capture_agent_graph
from _agents_codex.realtime_agents import _capture_voice_graph, _resolve_realtime_configuration
from agents import Agent, function_tool
from agents.realtime import RealtimeAgentConfig, RealtimeConfig, realtime_handoff


@pytest.mark.parametrize("prompt", [{"id": "explicit-connection-prompt"}, None])
async def test_profile_update_keeps_explicit_prompt_after_startup_settings_cleanup(prompt):
    from _agents_codex.realtime import RealtimeSession

    agent = Agent(
        instructions="Backend", realtime=RealtimeAgentConfig(prompt={"id": "agent-default"})
    )
    session = source_session(agent, [])
    owner = RealtimeSession(session, config=RealtimeConfig(settings={"prompt": prompt}))
    owner._settings.clear()
    owner._config = RealtimeConfig()
    owner._live = True
    updates = []

    async def update(operation, **payload):
        assert operation == "update"
        updates.append(payload["update"])

    owner._call = update
    await owner._apply_profile(None, None)
    assert updates[0]["prompt"] == prompt
    assert owner._settings == {}


async def test_superseded_handoff_configuration_preserves_new_acknowledged_source(monkeypatch):
    from _agents_codex import realtime

    current = {"profile_id": "voice_new", "epoch": 2}
    old = {"profile_id": "voice_old_target", "epoch": 1}
    requests = []

    async def source(*_):
        return "{}"

    async def resolve(*_, **__):
        return "Old target", {}, [], "old-selection"

    async def update(operation, **payload):
        requests.append((operation, payload))
        return {"applied": False}

    monkeypatch.setattr(realtime, "_resolve_realtime_configuration", resolve)
    session = SimpleNamespace(
        _voice_profiles={old["profile_id"]: SimpleNamespace(profile=object())},
        _native=SimpleNamespace(_realtime_agent_source=source),
    )
    owner = realtime.RealtimeSession(session, config=RealtimeConfig())
    owner._voice_profile_ids.add(old["profile_id"])
    owner._voice_target = current
    owner._live = True
    owner._call = update
    await owner._apply_profile("old-response", old)
    assert owner._voice_target is current
    assert owner._live
    assert requests[0][1]["profile"]["response_id"] == "old-response"


async def test_handoff_configuration_waits_for_app_agent_update():
    from _agents_codex.realtime import RealtimeSession

    configured = []

    async def apply(response_id, target, *, source_work=None):
        assert source_work is None
        configured.append((response_id, target))

    owner = SimpleNamespace(
        _functions={},
        _profile_updates=set(),
        _PENDING_LIMIT=4,
        _context=copy_context(),
        _agent_update_lock=asyncio.Lock(),
        _apply_profile=apply,
        _delegation_done=lambda _: None,
    )
    target = {"profile_id": "old-target", "epoch": 1}
    async with owner._agent_update_lock:
        RealtimeSession._finish_function_batch(owner, {"response_id": "old", "target": target})
        await asyncio.sleep(0)
        assert configured == []
        tasks = tuple(owner._profile_updates)
    await asyncio.gather(*tasks)
    assert configured == [("old", target)]


class TransferInput(BaseModel):
    reason: str = Field(min_length=1)


@dataclass
class DataclassTransferInput:
    reason: str


class TypedDictTransferInput(TypedDict):
    reason: str


class RootTransferInput(RootModel[TransferInput]):
    pass


@pytest.mark.parametrize(
    "input_type", [DataclassTransferInput, TypedDictTransferInput, RootTransferInput]
)
async def test_structured_handoff_preserves_declared_shape_and_callback_value(input_type):
    received = []
    target = Agent(name="Target", instructions="Target")
    source = Agent(
        name="Source",
        instructions="Source",
        handoffs=[
            realtime_handoff(
                target,
                input_type=input_type,
                on_handoff=lambda context, payload: received.append(payload),
            )
        ],
    )
    tool = _capture_agent_graph(source)[0].declaration.tools[0]
    schema = json.loads(tool._spec_json())["inputSchema"]
    object_schema = (
        schema["$defs"][schema["$ref"].rsplit("/", 1)[1]] if "$ref" in schema else schema
    )
    assert set(object_schema["properties"]) == {"reason"}
    captured = tool._capture({})
    with pytest.raises(ValidationError):
        captured._parse_arguments({"reason": 123})
    parsed = captured._parse_arguments('{"reason":"requested"}')
    assert captured.callback(**captured._arguments(parsed)) == "Handoff reserved."
    assert received == [input_type(reason="requested")]


def source_session(agent, definitions, *, context=None, profile=None):
    captured = profile.declaration if profile is not None else agent
    receipt = {"profile_id": profile.id, "epoch": 0} if profile is not None else None

    async def call(operation, request):
        assert operation == "realtime_tool_selection_sdk"
        return json.dumps(
            {
                "profile_receipt": receipt,
                "selection_id": "exact-source",
                "catalog_generation": 0,
                "function_tools": [
                    {
                        "name": definition["name"],
                        "parameters": definition.get("inputSchema", {}),
                        "native_ref": definition.get(
                            "native_ref", {"name": definition["name"], "namespace": None}
                        ),
                    }
                    for definition in definitions
                ],
            }
        )

    async def select(tools, **kwargs):
        return (None, None) if tools is None else (definitions[:1], "exact-source")

    return SimpleNamespace(
        _effective_agent=lambda: captured,
        _agent_for_profile=lambda _: captured,
        _agent=agent,
        _context=context,
        _native=SimpleNamespace(call=call),
        _profiles={profile.id: SimpleNamespace(profile=profile)} if profile is not None else {},
        _profile_current=(profile.id, 0) if profile is not None else None,
        _realtime_function_tools=select,
    )


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_contextual_instructions_and_predicates_bind_exact_source(asynchronous):
    application = {"locale": "French"}
    seen = []

    def resolve(context):
        seen.append(context)
        return "Speak " + context.context["locale"]

    async def async_resolve(context):
        await asyncio.sleep(0)
        return resolve(context)

    agent = Agent(
        instructions="Backend instructions.",
        realtime=RealtimeAgentConfig(
            instructions=async_resolve if asynchronous else resolve,
            prompt={"id": "voice-prompt"},
            tool_enabled=lambda context, tool: (
                tool.name == "visible" or (tool.name, tool.namespace) == ("read", "mcp__records")
            ),
        ),
    )
    offered = [
        {"name": "visible"},
        {"name": "hidden"},
        {
            "name": "mcp__records__read",
            "native_ref": {"name": "read", "namespace": "mcp__records"},
        },
    ]
    result = await _resolve_realtime_configuration(
        source_session(agent, offered, context=application), None, {}, None
    )
    assert result == (
        "Speak French",
        {"prompt": {"id": "voice-prompt"}},
        [
            {"name": definition["name"], "description": "", "inputSchema": {}}
            for definition in (offered[0], offered[2])
        ],
        "exact-source",
    )
    assert seen[0].context is application
    assert seen[0].agent is agent
    assert agent.instructions == "Backend instructions."


async def test_explicit_connection_overrides_keep_tool_ceiling():
    def unexpected(_):
        raise AssertionError("An overridden instruction resolver must not run")

    prompt = {"id": "captured", "variables": {"language": "French"}}
    agent = Agent(
        instructions="Backend", realtime=RealtimeAgentConfig(instructions=unexpected, prompt=prompt)
    )
    prompt["variables"]["language"] = "changed"
    assert agent.realtime.prompt["variables"]["language"] == "French"
    result = await _resolve_realtime_configuration(
        source_session(agent, [{"name": "one"}, {"name": "two"}]),
        "Explicit",
        {"prompt": None},
        (object(),),
    )
    assert result == ("Explicit", {"prompt": None}, [{"name": "one"}], "exact-source")


async def test_callback_failure_and_invalid_result_prevent_configuration():
    async def fail(_):
        raise ValueError("synthetic resolver failure")

    agent = Agent(instructions="Backend", realtime=RealtimeAgentConfig(instructions=fail))
    with pytest.raises(ValueError, match="synthetic resolver failure"):
        await _resolve_realtime_configuration(source_session(agent, []), None, {}, None)
    for value in (None, 123):
        agent = Agent(
            instructions="Backend",
            realtime=RealtimeAgentConfig(instructions=lambda _, result=value: result),
        )
        with pytest.raises(TypeError, match="must return a string"):
            await _resolve_realtime_configuration(source_session(agent, []), None, {}, None)
    agent = Agent(
        instructions="Backend", realtime=RealtimeAgentConfig(tool_enabled=lambda *_: "yes")
    )
    with pytest.raises(TypeError, match="must return a bool"):
        await _resolve_realtime_configuration(
            source_session(agent, [{"name": "one"}]), None, {}, None
        )


async def test_custom_handoff_reuses_validated_callback_and_exact_source():
    application = {"marker": "source-context"}
    received = []

    async def transfer(context, payload):
        await asyncio.sleep(0)
        received.append((context, payload))
        return "A callback result cannot select another target"

    target = Agent(name="B", instructions="Target")
    source = Agent(
        name="A",
        instructions="Source",
        handoffs=[
            realtime_handoff(
                target,
                name="transfer_record",
                description="Transfer a validated record",
                input_type=TransferInput,
                on_handoff=transfer,
            )
        ],
    )
    profiles = _capture_agent_graph(source)
    profile = profiles[0]
    assert profile.handoffs == {"transfer_record": profiles[1].id}
    assert profile.handoff_callbacks == {"transfer_record": 0}
    tool = profile.declaration.tools[0]
    assert tool.description == "Transfer a validated record"
    captured = tool._capture(application)
    with pytest.raises(ValidationError):
        captured._parse_arguments({"reason": ""})
    assert received == []
    parsed = captured._parse_arguments({"reason": "requested"})
    assert await captured.callback(**captured._arguments(parsed)) == "Handoff reserved."
    assert received[0][0].agent is source
    assert received[0][0].context is application
    assert received[0][1] is parsed
    assert profile.handoffs == {"transfer_record": profiles[1].id}


async def test_disabled_handoff_is_not_advertised():
    target = Agent(name="B", instructions="Target")
    source = Agent(
        name="A",
        instructions="Source",
        handoffs=[
            realtime_handoff(
                target,
                name="transfer",
                is_enabled=lambda context: context.context["allowed"],
            )
        ],
    )
    profile = _capture_agent_graph(source)[0]
    session = source_session(
        source, [{"name": "transfer"}], context={"allowed": False}, profile=profile
    )
    result = await _resolve_realtime_configuration(session, None, {}, None)
    assert result[2:] == ([], "exact-source")
    captured = profile.declaration.tools[0]._capture(session._context)
    assert type(await captured.before(None)).__name__ == "Deny"


def test_handoff_names_cannot_replace_ordinary_source_tools():
    @function_tool
    def transfer() -> str:
        return "ordinary callback"

    target = Agent(name="B", instructions="Target")
    source = Agent(
        instructions="Source",
        tools=[transfer],
        handoffs=[realtime_handoff(target, name="transfer")],
    )
    with pytest.raises(ValueError, match="collides"):
        _capture_agent_graph(source)


@pytest.mark.parametrize("session_guards", [False, True])
def test_live_graph_capture_preserves_provider_output_and_runtime_contracts(session_guards):
    from agents import GuardrailResult, InputGuardrail, NativeProvider, OutputGuardrail

    provider = NativeProvider(api_key="synthetic-live-graph-key")
    opening = Agent(instructions="Original", provider=provider)
    session = SimpleNamespace(
        _opening_agent=opening,
        _context=None,
        _executor=None,
        _input_guardrails=(
            InputGuardrail(
                "global-input",
                lambda *_: GuardrailResult(False),
                child_callback=lambda *_: GuardrailResult(False),
            ),
        )
        if session_guards
        else (),
        _output_guardrails=(OutputGuardrail("global-output", lambda *_: GuardrailResult(False)),)
        if session_guards
        else (),
    )
    target = Agent(name="New", instructions="New source")
    runtimes, roster = _capture_voice_graph(session, target, "voice_fresh_")
    assert list(runtimes) == ["voice_fresh_0"]
    assert runtimes["voice_fresh_0"].profile.agent is target
    assert runtimes["voice_fresh_0"].profile.declaration.provider is provider
    assert roster[0]["instructions"] == "New source"
    assert roster[0]["before_complete"] is session_guards
    assert roster[0]["selecting_steps"] is session_guards
    assert opening.instructions == "Original"
    with pytest.raises(ValueError, match="provider"):
        _capture_voice_graph(
            session,
            Agent(instructions="Other", provider=NativeProvider(api_key="synthetic-other")),
            "voice_other_",
        )
    with pytest.raises(ValueError, match="output contract"):
        _capture_voice_graph(session, Agent(instructions="Typed", output_type=int), "voice_typed_")


async def test_agent_guardrail_defaults_follow_issuing_receipt_after_source_change():
    from contextvars import copy_context

    from _agents_codex.realtime import RealtimeSession
    from agents.realtime import RealtimeGuardrailResult, RealtimeOutputGuardrail
    from tests.test_realtime_guardrail_retention import output, settle

    seen = []

    def check(name):
        def run(value):
            seen.append((name, value.response_id, value.agent.instructions))
            return RealtimeGuardrailResult(False)

        return RealtimeOutputGuardrail(name, run)

    original = Agent(
        instructions="Old", realtime=RealtimeAgentConfig(output_guardrails=(check("old"),))
    )
    target = Agent(
        instructions="New", realtime=RealtimeAgentConfig(output_guardrails=(check("new"),))
    )
    sources = {"old": original, "new": target}
    session = SimpleNamespace(
        _opening_agent=original,
        _context=None,
        _agent_for_profile=lambda receipt: sources[receipt["profile_id"]],
    )
    owner = RealtimeSession(session, config=RealtimeConfig(output_guardrails=(check("global"),)))
    owner._live = True
    owner._context = copy_context()
    owner._record_guardrail_text(
        {**output("old-response", "old"), "profile_receipt": {"profile_id": "old", "epoch": 0}},
        final=True,
    )
    session._opening_agent = target
    owner._record_guardrail_text(
        {**output("new-response", "new"), "profile_receipt": {"profile_id": "new", "epoch": 1}},
        final=True,
    )
    await settle(owner)
    assert sorted(seen) == sorted(
        [
            ("old", "old-response", "Old"),
            ("global", "old-response", "Old"),
            ("new", "new-response", "New"),
            ("global", "new-response", "New"),
        ]
    )
