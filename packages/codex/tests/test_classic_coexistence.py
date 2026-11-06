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
