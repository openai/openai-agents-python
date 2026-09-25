import asyncio
from typing import Any

import pytest

from agents import Agent, AgentBase, RunConfig, RunContextWrapper, Runner
from agents.decorators import tool
from agents.testing import ModelStep, ScriptedModel

from .test_responses import get_final_output_message


@pytest.mark.asyncio
async def test_get_all_tools_keeps_enablement_with_original_tools_after_callback_reorders() -> None:
    async def enabled(_ctx: RunContextWrapper[Any], agent: AgentBase) -> bool:
        agent.tools.reverse()
        return True

    @tool(is_enabled=enabled)
    def allowed() -> str:
        return "allowed"

    @tool(is_enabled=False)
    def disabled() -> str:
        return "disabled"

    agent = Agent(name="test", tools=[allowed, disabled])
    resolved = await agent.get_all_tools(RunContextWrapper(None))

    assert len(resolved) == 1
    assert resolved[0] is allowed
    assert agent.tools == [disabled, allowed]


@pytest.mark.asyncio
async def test_get_all_tools_keeps_snapshot_during_replacement_and_refreshes_next_call() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def enabled(_ctx: RunContextWrapper[Any], _agent: AgentBase) -> bool:
        started.set()
        await release.wait()
        return True

    @tool(is_enabled=enabled)
    def original() -> str:
        return "original"

    @tool(is_enabled=False)
    def disabled() -> str:
        return "disabled"

    @tool
    def replacement() -> str:
        return "replacement"

    agent = Agent(name="test", tools=[original])
    pending = asyncio.create_task(agent.get_all_tools(RunContextWrapper(None)))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        agent.tools = [disabled, replacement]
        release.set()
        resolved = await asyncio.wait_for(pending, timeout=5)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    assert len(resolved) == 1
    assert resolved[0] is original
    refreshed = await agent.get_all_tools(RunContextWrapper(None))
    assert len(refreshed) == 1
    assert refreshed[0] is replacement


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_runner_exposes_evaluated_tools_when_enablement_changes_configuration(
    streamed: bool,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def enabled(_ctx: RunContextWrapper[Any], agent: AgentBase) -> bool:
        started.set()
        await release.wait()
        agent.tools[:] = [disabled, replacement]
        return True

    @tool(is_enabled=enabled)
    def original() -> str:
        return "original"

    @tool(is_enabled=False)
    def disabled() -> str:
        return "disabled"

    @tool
    def replacement() -> str:
        return "replacement"

    model = ScriptedModel([ModelStep(output=[get_final_output_message("done")]) for _ in range(2)])
    agent = Agent(name="test", tools=[original], model=model)

    async def run() -> None:
        config = RunConfig(tracing_disabled=True)
        if streamed:
            result = Runner.run_streamed(agent, "hello", run_config=config)
            async for _ in result.stream_events():
                pass
            assert result.final_output == "done"
        else:
            result = await Runner.run(agent, "hello", run_config=config)
            assert result.final_output == "done"

    pending = asyncio.create_task(run())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert model.calls == ()
        release.set()
        await asyncio.wait_for(pending, timeout=5)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    assert [t.name for t in model.calls[0].tools] == ["original"]
    await run()
    assert [t.name for t in model.calls[1].tools] == ["replacement"]
