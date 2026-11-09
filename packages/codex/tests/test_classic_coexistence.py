"""Run both SDKs together without sharing types, defaults, or trace scopes."""

import asyncio

import pytest

from tests.native_fixtures import assistant_output, responses as responses
from tests.test_classic_imports import run_isolated


@pytest.mark.parametrize("native_first", [False, True])
def test_both_import_orders_keep_types_defaults_and_traces_separate(native_first: bool) -> None:
    run_isolated(
        f"""
import asyncio, importlib
names = ['agents', 'agents.classic'] if {native_first!r} else ['agents.classic', 'agents']
for name in names:
    runtime = importlib.import_module(name)
    runtime.Agent  # Resolve the native package's lazy export too.
import agents
from agents import telemetry
from agents import classic

classic.set_default_openai_key('synthetic-classic-key', use_for_tracing=False)
native_provider = agents.NativeProvider(api_key='synthetic-native-key')
native_agent = agents.Agent(instructions='Native instructions.', provider=native_provider)
classic_agent = classic.Agent(name='Classic', instructions='Classic instructions.')
assert type(native_agent) is agents.Agent
assert type(classic_agent) is classic.Agent
assert agents.Agent is not classic.Agent
assert agents.FunctionTool is not classic.FunctionTool
assert native_provider.api_key == 'synthetic-native-key'
assert classic.models._openai_shared.get_default_openai_key() == 'synthetic-classic-key'

async def check_trace_scopes():
    await telemetry.configure()
    try:
        assert telemetry.current_context() is None
        assert classic.get_current_trace() is None
        with telemetry.trace('native', options=telemetry.TraceOptions(disabled=True)):
            native_context = telemetry.current_context()
            assert native_context is not None
            with classic.trace('classic', disabled=True) as classic_trace:
                assert classic.get_current_trace() is classic_trace
                assert telemetry.current_context() is native_context
            assert classic.get_current_trace() is None
        assert telemetry.current_context() is None
    finally:
        await telemetry.shutdown()

asyncio.run(check_trace_scopes())
"""
    )


async def test_native_and_classic_runs_share_a_process(responses) -> None:
    from agents import Agent as NativeAgent, NativeProvider
    from agents.classic import Agent as ClassicAgent, RunConfig, Runner
    from agents.classic.testing import ScriptedModel, assistant_message

    responses.respond(assistant_output("native answer"))
    native_agent = NativeAgent(
        instructions="Answer briefly.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-native-key"),
    )
    model = ScriptedModel([[assistant_message("classic answer")]])
    classic_agent = ClassicAgent(name="Classic", model=model)
    async with native_agent.session() as session:
        turn = await session.submit("native input")
        native_result, classic_result = await asyncio.gather(
            turn.get_result(),
            Runner.run(classic_agent, "classic input", run_config=RunConfig(tracing_disabled=True)),
        )
    assert native_result.final_output == "native answer"
    assert classic_result.final_output == "classic answer"
    model.assert_complete()
    assert len(responses.requests) == 1
    assert responses.request_headers[0]["authorization"] == "Bearer synthetic-native-key"


@pytest.mark.parametrize("entry", ["async", "sync", "streamed"])
@pytest.mark.parametrize(
    "mixed", ["agent", "tool", "state", "handoff", "wrapper", "resumed", "agent_tool"]
)
async def test_classic_runner_rejects_native_objects_before_session_or_model_access(
    entry: str,
    mixed: str,
) -> None:
    from agents import Agent as NativeAgent, SessionState, function_tool
    from agents.classic import Agent as ClassicAgent, ModelProvider, RunConfig, Runner

    effects: list[str] = []

    class UnusedProvider(ModelProvider):
        def get_model(self, model_name):
            effects.append("model")
            raise AssertionError("The invalid agent reached the model provider")

    class UnusedSession:
        session_id = "mixed-runtime"

        async def get_items(self, limit=None):
            effects.append("history")
            raise AssertionError("The invalid agent read session history")

        async def add_items(self, items):
            effects.append("write")
            raise AssertionError("The invalid agent wrote session history")

    @function_tool
    def native_tool() -> str:
        effects.append("native tool")
        return "native output"

    native_agent = NativeAgent(instructions="Use native execution.")
    agent = native_agent if mixed == "agent" else ClassicAgent(name="Classic")
    run_input = "input"
    if mixed == "tool":
        agent.tools = [native_tool]
    elif mixed == "state":
        run_input = SessionState(
            format_version=2,
            codex_revision="boundary-probe",
            source_session_id="native",
            history=[],
            root_metadata={},
        )
    elif mixed == "handoff":
        agent.handoffs = [native_agent]
    elif mixed == "wrapper":
        from types import SimpleNamespace

        agent.handoffs = [SimpleNamespace(agent=native_agent)]
    elif mixed == "resumed":
        from agents.classic import RunContextWrapper, RunState

        run_input = RunState(RunContextWrapper(None), "input", agent)
        run_input._current_agent = native_agent
    elif mixed == "agent_tool":
        target = ClassicAgent(name="Target")
        selected = target.as_tool(tool_name="nested", tool_description="Nested Classic agent")
        selected._agent_instance = native_agent
        agent.tools = [selected]
    config = RunConfig(model_provider=UnusedProvider(), tracing_disabled=True)
    with pytest.raises(TypeError, match="Classic|agents.classic"):
        if entry == "async":
            await Runner.run(agent, run_input, run_config=config, session=UnusedSession())
        elif entry == "sync":
            await asyncio.to_thread(
                Runner.run_sync, agent, run_input, run_config=config, session=UnusedSession()
            )
        else:
            streamed = Runner.run_streamed(
                agent, run_input, run_config=config, session=UnusedSession()
            )
            async for _ in streamed.stream_events():
                pass
    assert effects == []


async def test_classic_runner_preserves_agent_subclasses() -> None:
    from agents.classic import Agent as ClassicAgent, RunConfig, Runner
    from agents.classic.testing import ScriptedModel, assistant_message

    class ApplicationAgent(ClassicAgent):
        pass

    model = ScriptedModel([[assistant_message("subclass answer")]])
    result = await Runner.run(
        ApplicationAgent(name="Application", model=model),
        "input",
        run_config=RunConfig(tracing_disabled=True),
    )
    assert result.final_output == "subclass answer"
    model.assert_complete()


async def test_classic_tool_subclass_executes_through_its_existing_owner() -> None:
    from agents.classic import Agent, FunctionTool, RunConfig, Runner
    from agents.classic.testing import ScriptedModel, assistant_message, function_call

    calls = []

    class ApplicationTool(FunctionTool):
        pass

    async def invoke(context, arguments):
        calls.append(arguments)
        return "classic result"

    declaration = ApplicationTool(
        name="extension",
        description="Classic extension",
        params_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_tool=invoke,
    )
    model = ScriptedModel(
        [
            [function_call("extension", {}, call_id="extension-call")],
            [assistant_message("done")],
        ]
    )
    result = await Runner.run(
        Agent(name="Classic", model=model, tools=[declaration]),
        "input",
        run_config=RunConfig(tracing_disabled=True),
    )
    assert result.final_output == "done"
    assert calls == ["{}"]
    model.assert_complete()


def test_preflight_keeps_legacy_graph_and_declared_lazy_extensions() -> None:
    from types import SimpleNamespace

    from agents.classic import Agent, ComputerTool, Handoff
    from agents.classic._run_state_agent_identity import (
        _iter_agent_graph,
        _validate_classic_agent_graph,
    )
    from agents.classic.sandbox import SandboxAgent

    class ApplicationComputerTool(ComputerTool):
        pass

    def unopened_computer():
        raise AssertionError("Preflight opened a computer")

    class LegacyHandoff(Handoff):
        def __init__(self, agent):
            # Preserve the released identity walk's bypass-initialization fallback.
            self.agent = agent
            self.name = agent.name

    child = Agent(name="Child")
    root = Agent(
        name="Classic",
        tools=[ApplicationComputerTool(computer=unopened_computer)],
        handoffs=[SimpleNamespace(agent=child), LegacyHandoff(child)],
    )
    _validate_classic_agent_graph(root)
    assert list(_iter_agent_graph(root)) == [root, child]
    _validate_classic_agent_graph(SandboxAgent(name="Sandbox"))


@pytest.mark.parametrize("target_kind", ["classic", "native"])
async def test_dynamic_handoff_validates_target_before_transfer_effects(target_kind: str) -> None:
    from agents import Agent as NativeAgent
    from agents.classic import Agent, Handoff, RunConfig, RunHooks, Runner
    from agents.classic.testing import ScriptedModel, assistant_message, function_call

    effects = []
    target_model = ScriptedModel([[assistant_message("target answer")]])
    target = (
        Agent(name="Target", model=target_model)
        if target_kind == "classic"
        else NativeAgent(instructions="Do not enter.")
    )

    async def choose(context, arguments):
        effects.append("callback")
        return target

    def fallback(context, error):
        effects.append("fallback")
        return "must not repair an invalid runtime target"

    class ApplicationHandoff(Handoff):
        def get_transfer_message(self, agent):
            effects.append("transfer")
            return super().get_transfer_message(agent)

    class Hooks(RunHooks):
        async def on_handoff(self, context, from_agent, to_agent):
            effects.append("hook")

    route = ApplicationHandoff(
        tool_name="route",
        tool_description="Choose a Classic target",
        input_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_handoff=choose,
        agent_name="Target",
        failure_error_function=fallback,
    )
    model = ScriptedModel([[function_call("route", {}, call_id="route-call")]])
    operation = Runner.run(
        Agent(name="Origin", model=model, handoffs=[route]),
        "input",
        hooks=Hooks(),
        run_config=RunConfig(tracing_disabled=True),
    )
    if target_kind == "native":
        with pytest.raises(TypeError, match="Classic Runner"):
            await operation
        assert effects == ["callback"]
        assert not target_model.calls
    else:
        assert (await operation).final_output == "target answer"
        assert effects == ["callback", "transfer", "hook"]
        target_model.assert_complete()
    assert len(model.calls) == 1
    model.assert_complete()


@pytest.mark.parametrize("producer", ["mcp", "all"])
async def test_dynamic_tool_producer_cannot_publish_native_tools_to_classic_model(producer) -> None:
    from agents import function_tool
    from agents.classic import Agent, RunConfig, Runner
    from agents.classic.testing import ScriptedModel, assistant_message

    calls = []

    @function_tool
    def native_tool() -> str:
        calls.append("native invocation")
        return "must not enter"

    class ApplicationAgent(Agent):
        async def get_mcp_tools(self, context):
            calls.append("discovery")
            return [native_tool]

    class CompleteToolProducer(Agent):
        async def get_all_tools(self, context):
            calls.append("discovery")
            return [native_tool]

    agent_type = ApplicationAgent if producer == "mcp" else CompleteToolProducer
    model = ScriptedModel([[assistant_message("must not sample")]])
    with pytest.raises(TypeError, match="Classic Runner requires Classic tool"):
        await Runner.run(
            agent_type(name="Classic", model=model),
            "input",
            run_config=RunConfig(tracing_disabled=True),
        )
    assert calls == ["discovery"]
    assert not model.calls


async def test_classic_realtime_agent_collects_declared_function_tools() -> None:
    from agents.classic import RunContextWrapper, function_tool
    from agents.classic.realtime import RealtimeAgent

    @function_tool
    def supported_tool() -> str:
        return "Classic realtime output"

    agent = RealtimeAgent(name="Classic realtime", tools=[supported_tool])
    assert await agent.get_all_tools(RunContextWrapper(None)) == [supported_tool]
