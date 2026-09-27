import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import pytest

from agents import Agent, AgentHooks, RunConfig, RunHooks, Runner, UserError
from agents.decorators import tool
from agents.run_context import RunContextWrapper
from agents.testing import ScriptedModel

from .test_responses import get_function_tool_call, get_text_message


async def _run(
    agent: Agent[Any],
    model: ScriptedModel,
    role: str,
    streamed: bool,
    hooks: RunHooks[Any] | None = None,
) -> None:
    config = RunConfig(model=model, tracing_disabled=True)
    if streamed:
        result = Runner.run_streamed(agent, "go", context=role, hooks=hooks, run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "go", context=role, hooks=hooks, run_config=config)
    assert result.final_output == "done"


def _final_model() -> ScriptedModel:
    return ScriptedModel([[get_text_message("done")]])


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("hook_kind", ["run", "agent"])
@pytest.mark.parametrize("in_place", [False, True])
async def test_shared_agent_tool_changes_reject_both_runs(
    streamed: bool, hook_kind: str, in_place: bool
) -> None:
    low_started = asyncio.Event()
    high_finished = asyncio.Event()
    effects: list[str] = []

    @tool
    async def privileged(context: RunContextWrapper[str]) -> str:
        effects.append(context.context)
        return "synthetic effect"

    agent = Agent(name="shared")

    async def update(context, current_agent) -> None:
        assert current_agent is agent
        if context.context == "low":
            low_started.set()
            await high_finished.wait()
        elif in_place:
            current_agent.tools.append(privileged)
        else:
            current_agent.tools = [privileged]

    class Hooks(RunHooks):
        async def on_agent_start(self, context, current_agent) -> None:
            await update(context, current_agent)

    class LocalHooks(AgentHooks):
        async def on_start(self, context, current_agent) -> None:
            await update(context, current_agent)

    hooks = Hooks() if hook_kind == "run" else None
    agent.hooks = LocalHooks() if hook_kind == "agent" else None
    low_model = ScriptedModel([[get_function_tool_call("privileged")], [get_text_message("done")]])
    high_model = _final_model()
    low = asyncio.create_task(_run(agent, low_model, "low", streamed, hooks))
    await asyncio.wait_for(low_started.wait(), 10)

    async def high_run() -> None:
        try:
            await _run(agent, high_model, "high", streamed, hooks)
        finally:
            high_finished.set()

    outcomes = await asyncio.wait_for(asyncio.gather(low, high_run(), return_exceptions=True), 10)
    assert all(isinstance(outcome, UserError) for outcome in outcomes)
    assert all("concurrent runs" in str(outcome) for outcome in outcomes)
    assert effects == []
    assert not low_model.calls and not high_model.calls
    # The completed competitor must not clear the survivor's conflict or roll back user state.
    assert agent.tools == [privileged]
    agent.tools = []
    agent.hooks = None
    await _run(agent, _final_model(), "later", streamed)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_shared_agent_with_context_enablement_can_run_concurrently(streamed: bool) -> None:
    both_started = asyncio.Event()
    started: list[Agent[Any]] = []

    @tool(is_enabled=lambda context, current_agent: context.context == "high")
    async def privileged() -> str:
        return "synthetic effect"

    agent = Agent(name="shared", tools=[privileged])

    class Hooks(RunHooks):
        async def on_agent_start(self, context, current_agent) -> None:
            started.append(current_agent)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()

    low_model, high_model = _final_model(), _final_model()
    await asyncio.wait_for(
        asyncio.gather(
            _run(agent, low_model, "low", streamed, Hooks()),
            _run(agent, high_model, "high", streamed, Hooks()),
        ),
        10,
    )
    assert all(current is agent for current in started)
    assert low_model.calls[0].tools == []
    assert high_model.calls[0].tools == [privileged]
    assert agent.tools == [privileged]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_surviving_run_can_update_tools_after_harmless_overlap(streamed: bool) -> None:
    first_started = asyncio.Event()
    survivor_started = asyncio.Event()
    first_finished = asyncio.Event()
    effects: list[str] = []

    @tool
    async def added(context: RunContextWrapper[str]) -> str:
        effects.append(context.context)
        return "synthetic effect"

    class Hooks(RunHooks):
        async def on_agent_start(self, context, current_agent) -> None:
            if context.context == "first":
                first_started.set()
                await survivor_started.wait()
            elif context.context == "survivor":
                survivor_started.set()
                await first_finished.wait()
                current_agent.tools = [added]

    agent = Agent(name="shared")
    first_model = _final_model()
    survivor_model = ScriptedModel([[get_function_tool_call("added")], [get_text_message("done")]])
    first = asyncio.create_task(_run(agent, first_model, "first", streamed, Hooks()))
    await asyncio.wait_for(first_started.wait(), 10)
    survivor = asyncio.create_task(_run(agent, survivor_model, "survivor", streamed, Hooks()))
    try:
        await asyncio.wait_for(first, 10)
    finally:
        first_finished.set()
    await asyncio.wait_for(survivor, 10)

    assert first_model.calls[0].tools == []
    assert survivor_model.calls[0].tools == [added]
    assert effects == ["survivor"]
    assert agent.tools == [added]
    await _run(agent, _final_model(), "later", streamed)
    assert agent.tools == [added]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_shared_agent_mutation_during_mcp_discovery_is_rejected(streamed: bool) -> None:
    from agents.mcp import ToolFilterContext

    from .mcp.helpers import FakeMCPServer

    discovering = asyncio.Event()
    competitor_finished = asyncio.Event()

    async def tool_filter(context: ToolFilterContext, mcp_tool) -> bool:
        if context.run_context.context == "low":
            discovering.set()
            await competitor_finished.wait()
        return True

    @tool
    async def privileged() -> str:
        return "synthetic effect"

    class Hooks(RunHooks):
        async def on_agent_start(self, context, current_agent) -> None:
            current_agent.tools = [privileged]

    server = FakeMCPServer(tool_filter=tool_filter)
    server.add_tool("lookup", {})
    agent = Agent(name="shared", mcp_servers=[server])
    first_model, second_model = _final_model(), _final_model()
    first = asyncio.create_task(_run(agent, first_model, "low", streamed))
    await asyncio.wait_for(discovering.wait(), 10)
    try:
        with pytest.raises(UserError, match="concurrent runs"):
            await _run(agent, second_model, "high", streamed, Hooks())
    finally:
        competitor_finished.set()
    with pytest.raises(UserError, match="concurrent runs"):
        await asyncio.wait_for(first, 10)
    assert not first_model.calls and not second_model.calls
    assert server.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
async def test_failed_hook_invalidates_survivor_and_preserves_primary_failure(
    cancel: bool, streamed: bool
) -> None:
    low_started = asyncio.Event()
    high_started = asyncio.Event()
    high_finished = asyncio.Event()

    @tool
    async def privileged() -> str:
        return "synthetic effect"

    class Hooks(RunHooks):
        async def on_agent_start(self, context, current_agent) -> None:
            if context.context == "low":
                low_started.set()
                await high_finished.wait()
            else:
                current_agent.tools = [privileged]
                high_started.set()
                if cancel:
                    await asyncio.Event().wait()
                raise RuntimeError("hook failed")

    agent = Agent(name="shared")
    low_model, high_model = _final_model(), _final_model()
    low = asyncio.create_task(_run(agent, low_model, "low", streamed, Hooks()))
    await asyncio.wait_for(low_started.wait(), 10)
    if streamed:
        high_result = Runner.run_streamed(
            agent,
            "go",
            context="high",
            hooks=Hooks(),
            run_config=RunConfig(model=high_model, tracing_disabled=True),
        )

        async def consume() -> None:
            async for _ in high_result.stream_events():
                pass

        high = asyncio.create_task(consume())
    else:
        high = asyncio.create_task(_run(agent, high_model, "high", False, Hooks()))
    await asyncio.wait_for(high_started.wait(), 10)
    if cancel:
        if streamed:
            high_result.cancel()
        else:
            high.cancel()
    try:
        if cancel and streamed:
            await asyncio.wait_for(high, 10)
        else:
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                await asyncio.wait_for(high, 10)
    finally:
        high_finished.set()
    with pytest.raises(UserError, match="concurrent runs"):
        await asyncio.wait_for(low, 10)
    assert not low_model.calls and not high_model.calls
    assert agent.tools == [privileged]
    agent.tools = []
    await _run(agent, _final_model(), "later", streamed)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_new_run_detects_mutation_in_a_suspended_hook(streamed: bool) -> None:
    first_started = asyncio.Event()
    second_finished = asyncio.Event()

    @tool
    async def privileged() -> str:
        return "synthetic effect"

    class Hooks(RunHooks):
        async def on_agent_start(self, context, current_agent) -> None:
            current_agent.tools = [privileged]
            first_started.set()
            await second_finished.wait()

    agent = Agent(name="shared")
    first_model, second_model = _final_model(), _final_model()
    first = asyncio.create_task(_run(agent, first_model, "first", streamed, Hooks()))
    await asyncio.wait_for(first_started.wait(), 10)
    try:
        with pytest.raises(UserError, match="concurrent runs"):
            await _run(agent, second_model, "second", streamed)
    finally:
        # Restoring the list must not clear the already observed conflict.
        agent.tools = []
        second_finished.set()
    with pytest.raises(UserError, match="concurrent runs"):
        await asyncio.wait_for(first, 10)
    assert not first_model.calls and not second_model.calls
    await _run(agent, _final_model(), "later", streamed)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_restoring_original_tools_does_not_hide_concurrent_replacements(
    streamed: bool,
) -> None:
    both_started = asyncio.Event()
    low_changed = asyncio.Event()
    high_finished = asyncio.Event()
    started: list[str] = []

    @tool
    async def privileged() -> str:
        return "synthetic effect"

    original_tools = [privileged]
    agent = Agent(name="shared", tools=original_tools)

    class Hooks(RunHooks):
        async def on_agent_start(self, context, current_agent) -> None:
            started.append(context.context)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            if context.context == "low":
                current_agent.tools = []
                low_changed.set()
                await high_finished.wait()
            else:
                await low_changed.wait()
                current_agent.tools = original_tools

    low_model, high_model = _final_model(), _final_model()

    async def high_run() -> None:
        try:
            await _run(agent, high_model, "high", streamed, Hooks())
        finally:
            high_finished.set()

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            _run(agent, low_model, "low", streamed, Hooks()), high_run(), return_exceptions=True
        ),
        10,
    )
    assert all(isinstance(outcome, UserError) for outcome in outcomes)
    assert not low_model.calls and not high_model.calls
    assert agent.tools is original_tools


def test_concurrent_run_sync_uses_the_same_tool_conflict_boundary() -> None:
    low_started = Event()
    high_finished = Event()

    @tool
    async def privileged() -> str:
        return "synthetic effect"

    agent = Agent(name="shared")

    class Hooks(RunHooks):
        async def on_agent_start(self, context, current_agent) -> None:
            assert current_agent is agent
            if context.context == "low":
                low_started.set()
                assert await asyncio.to_thread(high_finished.wait, 10)
            else:
                current_agent.tools = [privileged]

    low_model, high_model = _final_model(), _final_model()
    with ThreadPoolExecutor(max_workers=2) as executor:
        low = executor.submit(
            Runner.run_sync,
            agent,
            "go",
            context="low",
            hooks=Hooks(),
            run_config=RunConfig(model=low_model, tracing_disabled=True),
        )
        assert low_started.wait(10)
        high = executor.submit(
            Runner.run_sync,
            agent,
            "go",
            context="high",
            hooks=Hooks(),
            run_config=RunConfig(model=high_model, tracing_disabled=True),
        )
        try:
            with pytest.raises(UserError, match="concurrent runs"):
                high.result(timeout=10)
        finally:
            high_finished.set()
        with pytest.raises(UserError, match="concurrent runs"):
            low.result(timeout=10)
    assert not low_model.calls and not high_model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_model_wait_does_not_release_agent_configuration_ownership(streamed: bool) -> None:
    model_started = asyncio.Event()
    competitor_finished = asyncio.Event()
    effects: list[str] = []

    class WaitingModel(ScriptedModel):
        async def get_response(self, *args, **kwargs):
            model_started.set()
            await competitor_finished.wait()
            return await super().get_response(*args, **kwargs)

        async def stream_response(self, *args, **kwargs):
            model_started.set()
            await competitor_finished.wait()
            async for event in super().stream_response(*args, **kwargs):
                yield event

    @tool
    async def harmless() -> str:
        effects.append("harmless")
        return "ok"

    @tool
    async def privileged() -> str:
        effects.append("privileged")
        return "synthetic effect"

    class Hooks(RunHooks):
        async def on_agent_start(self, context, current_agent) -> None:
            current_agent.tools = [privileged]

    agent = Agent(name="shared", tools=[harmless])
    low_model = WaitingModel(
        [[get_function_tool_call("harmless")], [get_function_tool_call("privileged")]]
    )
    high_model = _final_model()
    low = asyncio.create_task(_run(agent, low_model, "low", streamed))
    await asyncio.wait_for(model_started.wait(), 10)
    try:
        with pytest.raises(UserError, match="concurrent runs"):
            await _run(agent, high_model, "high", streamed, Hooks())
    finally:
        competitor_finished.set()
    with pytest.raises(UserError, match="concurrent runs"):
        await asyncio.wait_for(low, 10)
    # An in-flight turn retains its previously resolved tools, but no later turn can adopt
    # the competing run's list. Neither its model nor a privileged tool is invoked.
    assert len(low_model.calls) == 1
    assert low_model.calls[0].tools == [harmless]
    assert not high_model.calls
    assert effects == ["harmless"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_resumed_agent_is_registered_before_handoff_enablement(streamed: bool) -> None:
    from agents import handoff

    entered = asyncio.Event()
    release = asyncio.Event()
    context = {"phase": "initial"}
    effects: list[str] = []

    @tool(name_override="action", needs_approval=True)
    async def original() -> str:
        effects.append("original")
        return "ok"

    @tool(name_override="action")
    async def replacement() -> str:
        effects.append("replacement")
        return "ok"

    async def enabled(run_context, current_agent) -> bool:
        if run_context.context["phase"] == "resume":
            entered.set()
            await release.wait()
        return False

    agent = Agent(
        name="shared",
        tools=[original],
        handoffs=[handoff(Agent(name="target"), is_enabled=enabled)],
    )

    async def run(value, run_context, model, hooks=None):
        config = RunConfig(model=model, tracing_disabled=True)
        if streamed:
            result = Runner.run_streamed(
                agent, value, context=run_context, hooks=hooks, run_config=config
            )
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, value, context=run_context, hooks=hooks, run_config=config)

    paused = await run("go", context, ScriptedModel([[get_function_tool_call("action")]]))
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    context["phase"] = "resume"
    resumed_model, competing_model = _final_model(), _final_model()
    resumed = asyncio.create_task(run(state, None, resumed_model))
    await asyncio.wait_for(entered.wait(), 10)

    class Hooks(RunHooks):
        async def on_agent_start(self, run_context, current_agent) -> None:
            assert current_agent is agent
            current_agent.tools = [replacement]

    try:
        with pytest.raises(UserError, match="concurrent runs"):
            await run("go", {"phase": "competitor"}, competing_model, Hooks())
    finally:
        release.set()
    with pytest.raises(UserError, match="concurrent runs"):
        await asyncio.wait_for(resumed, 10)
    assert not resumed_model.calls and not competing_model.calls
    assert effects == []
    assert agent.tools == [replacement]
    # Paused state and conflict ownership do not leak into the next independent run.
    context["phase"] = "later"
    await run("go", context, _final_model())


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_agent_is_registered_before_sequential_input_guardrail(streamed: bool) -> None:
    from agents import GuardrailFunctionOutput, InputGuardrail

    entered = asyncio.Event()
    release = asyncio.Event()

    @tool
    async def privileged() -> str:
        return "synthetic effect"

    async def check(context, agent, input):
        if context.context == "low":
            entered.set()
            await release.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    class Hooks(RunHooks):
        async def on_agent_start(self, context, current_agent) -> None:
            current_agent.tools = [privileged]

    agent = Agent(name="shared", input_guardrails=[InputGuardrail(check, run_in_parallel=False)])
    low_model, high_model = _final_model(), _final_model()
    low = asyncio.create_task(_run(agent, low_model, "low", streamed))
    await asyncio.wait_for(entered.wait(), 10)
    try:
        with pytest.raises(UserError, match="concurrent runs"):
            await _run(agent, high_model, "high", streamed, Hooks())
    finally:
        release.set()
    with pytest.raises(UserError, match="concurrent runs"):
        await asyncio.wait_for(low, 10)
    assert not low_model.calls and not high_model.calls
    await _run(agent, _final_model(), "later", streamed)


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize(
    "streamed,boundary",
    [(False, "callback"), (True, "callback"), (False, "hook"), (True, "hook"), (True, "event")],
)
async def test_handoff_target_is_registered_before_transition_awaits(
    resumed: bool, streamed: bool, boundary: str
) -> None:
    from agents import handoff

    effects: list[str] = []
    competing_errors: list[UserError | None] = []

    @tool
    async def privileged() -> str:
        effects.append("privileged")
        return "ok"

    @tool(needs_approval=True)
    async def approval_tool() -> str:
        return "approved"

    target = Agent(name="target")
    source = Agent(name="source", tools=[approval_tool] if resumed else [], handoffs=[target])
    first_response = [get_function_tool_call("transfer_to_target", call_id="handoff")]
    if resumed:
        first_response.insert(0, get_function_tool_call("approval_tool", call_id="approval"))
    model = ScriptedModel(
        [
            first_response,
            [get_function_tool_call("privileged", call_id="replacement")],
            [get_text_message("done")],
        ]
    )
    config = RunConfig(model=model, tracing_disabled=True)
    competing_model = _final_model()

    class Replace(RunHooks):
        async def on_agent_start(self, context, agent) -> None:
            assert agent is target
            agent.tools = [privileged]

    async def compete() -> None:
        try:
            await Runner.run(
                target,
                "go",
                hooks=Replace(),
                run_config=RunConfig(model=competing_model, tracing_disabled=True),
            )
        except UserError as error:
            competing_errors.append(error)
        else:
            competing_errors.append(None)

    async def on_handoff(context) -> None:
        if boundary == "callback":
            await compete()

    source.handoffs = [handoff(target, on_handoff=on_handoff)]

    class Hooks(RunHooks):
        async def on_handoff(self, context, from_agent, to_agent) -> None:
            assert from_agent is source and to_agent is target
            if boundary == "hook":
                await compete()

    value: Any = "go"
    if resumed:
        paused = await Runner.run(source, value, run_config=config)
        assert len(paused.interruptions) == 1
        value = paused.to_state()
        value.approve(value.get_interruptions()[0])

    error: UserError | None = None
    try:
        if streamed:
            result = Runner.run_streamed(source, value, hooks=Hooks(), run_config=config)
            async for event in result.stream_events():
                if (
                    boundary == "event"
                    and event.type == "agent_updated_stream_event"
                    and event.new_agent is target
                ):
                    assert result.current_agent is target
                    await compete()
        else:
            await Runner.run(source, value, hooks=Hooks(), run_config=config)
    except UserError as caught:
        error = caught

    assert isinstance(error, UserError) and "concurrent runs" in str(error)
    assert len(competing_errors) == 1
    assert isinstance(competing_errors[0], UserError)
    assert "concurrent runs" in str(competing_errors[0])
    assert len(model.calls) == 1
    assert not competing_model.calls
    assert effects == []
    assert target.tools == [privileged]
    await _run(target, _final_model(), "later", streamed)
