from dataclasses import replace
from pathlib import Path

import pytest

from agents import (
    Agent,
    Handoff,
    RunConfig,
    RunContextWrapper,
    Runner,
    RunState,
    UserError,
    handoff,
)
from agents.decorators import tool
from agents.plugins import Plugin, PluginHooks, RequireApproval
from agents.testing import ScriptedModel, assistant_message, function_call


def reader(effects: list[int], *, policy: bool) -> Agent:
    @tool(needs_approval=True)
    def read(number: int) -> str:
        effects.append(number)
        return "ok"

    model = ScriptedModel(
        [
            [function_call("read", {"number": 1}, call_id="first")],
            [function_call("read", {"number": 2}, call_id="second")],
            [assistant_message("done")],
        ]
    )
    hooks = PluginHooks(check_tool=lambda _: RequireApproval()) if policy else None
    return Agent("reader", model=model, plugins=[Plugin(name="records", tools=[read], hooks=hooks)])


@pytest.mark.asyncio
@pytest.mark.parametrize("persist", [False, True], ids=["memory", "checkpoint"])
async def test_wrapper_approval_satisfies_only_the_current_plugin_vote(persist: bool) -> None:
    effects: list[int] = []
    agent = reader(effects, policy=True)
    config = RunConfig(tracing_disabled=True, plugin_revision="r1")
    first = await Runner.run(agent, "read", run_config=config)
    (item,) = first.interruptions
    first.context_wrapper.approve_tool(item, always_approve=True)
    state = first.to_state()
    if persist:
        state = await RunState.from_string(agent, state.to_string(), plugin_revision="r1")
        with pytest.raises(UserError, match="current plugin approval"):
            state.reject(item)
    second = await Runner.run(agent, state, run_config=config)
    (next_item,) = second.interruptions
    assert next_item.call_id == "second" and effects == [1]
    second.context_wrapper.reject_tool(next_item)
    finished = await Runner.run(agent, second.to_state(), run_config=config)
    assert finished.final_output == "done" and effects == [1]


@pytest.mark.asyncio
async def test_completed_plugin_item_cannot_set_a_standing_decision() -> None:
    effects: list[int] = []
    agent = reader(effects, policy=False)
    config = RunConfig(tracing_disabled=True)
    first = await Runner.run(agent, "read", run_config=config)
    (item,) = first.interruptions
    first.context_wrapper.approve_tool(item)
    second = await Runner.run(agent, first.to_state(), run_config=config)
    assert effects == [1] and second.interruptions[0].call_id == "second"
    with pytest.raises(UserError, match="current plugin approval"):
        second.context_wrapper.approve_tool(item, always_approve=True)
    with pytest.raises(UserError, match="current plugin approval"):
        second.context_wrapper.reject_tool(item, always_reject=True)
    pending = await Runner.run(agent, second.to_state(), run_config=config)
    assert effects == [1] and pending.interruptions[0].call_id == "second"


@pytest.mark.asyncio
async def test_plugin_agent_preserves_native_receipt_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The native peer supplies the adapter receipt that ScriptedModel cannot create.
    from .memory.test_native_approval_recovery import setup_native

    agent, _, session, config, raw, calls, _ = await setup_native(tmp_path, monkeypatch)
    agent.plugins = (Plugin(name="records"),)
    config = replace(config, plugin_revision="r1")
    paused = await Runner.run(agent, "act", session=session, run_config=config)
    (item,) = paused.interruptions
    with pytest.raises(UserError, match="current plugin approval"):
        paused.context_wrapper.approve_tool(replace(item, raw_item=raw.model_dump()))
    with pytest.raises(UserError, match="original call"):
        paused.context_wrapper.approve_tool(item, always_approve=True)
    state = await session.load_state(agent, plugin_revision="r1")
    (current,) = state.get_interruptions()
    altered = raw.model_copy(update={"arguments": '{"cmd":"echo changed"}'})
    with pytest.raises(UserError, match="original call"):
        state.approve(replace(current, raw_item=altered))
    assert not calls
    state.approve(current)
    finished = await Runner.run(agent, state, run_config=config)
    assert finished.final_output == "finished" and calls == [raw.checkpoint]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["caller", "result", "plain-target"])
async def test_fresh_run_cannot_inherit_a_plugin_grant(source: str) -> None:
    effects: list[str] = []
    app = RunContextWrapper(context=effects)

    @tool(needs_approval=True)
    def read(number: int) -> str:
        effects.append("original")
        return str(number)

    @tool(name_override="read", needs_approval=True)
    def replacement(number: int) -> str:
        effects.append("replacement")
        return str(number)

    call = function_call("read", {"number": 1}, call_id="same")
    agent = Agent(
        "reader",
        plugins=[Plugin(name="first", tools=[read])],
        model=ScriptedModel([[call], [call], [assistant_message("done")]]),
    )
    first = await Runner.run(
        agent,
        "read",
        context=app,
        run_config=RunConfig(plugin_revision="r1", tracing_disabled=True),
    )
    first.context_wrapper.approve_tool(first.interruptions[0])
    if source == "plain-target":
        agent.plugins = ()
        agent.tools = [replacement]
    else:
        agent.plugins = (Plugin(name="replacement", tools=[replacement]),)
    config = RunConfig(plugin_revision="r2", tracing_disabled=True)
    wrapper = app if source == "caller" else first.context_wrapper
    second = await Runner.run(agent, "read", context=wrapper, run_config=config)
    assert second.context_wrapper.context is effects and second.context_wrapper.usage is app.usage
    assert not effects and second.interruptions[0].call_id == "same"
    state = second.to_state()
    state.approve(second.interruptions[0])
    finished = await Runner.run(agent, state, run_config=config)
    assert finished.final_output == "done" and effects == ["replacement"]


@pytest.mark.asyncio
async def test_dynamic_handoff_detaches_inherited_standing_grant() -> None:
    effects: list[int] = []
    agent = reader(effects, policy=False)
    (plugin,) = agent.plugins
    agent.plugins = ()
    agent.tools = list(plugin.tools)
    cfg = RunConfig(tracing_disabled=True)
    first = await Runner.run(agent, "read", run_config=cfg)
    first.context_wrapper.approve_tool(first.interruptions[0], always_approve=True)
    agent.tools = []
    agent.plugins = (plugin,)

    async def transfer(*_: object) -> Agent:
        return agent

    route = Handoff(
        tool_name="route",
        tool_description="Select a reader.",
        input_json_schema={},
        on_invoke_handoff=transfer,
        agent_name="reader",
    )
    manager = Agent(
        "manager",
        model=ScriptedModel([[function_call("route", {}, call_id="go")]]),
        handoffs=[route],
    )
    second = await Runner.run(manager, "read", context=first.context_wrapper, run_config=cfg)
    assert not effects and second.interruptions[0].call_id == "second"
    assert first.context_wrapper.is_tool_approved("read", "second") is True
    state = second.to_state()
    state.approve(second.interruptions[0])
    finished = await Runner.run(manager, state, run_config=cfg)
    assert finished.final_output == "done" and effects == [2]


@pytest.mark.asyncio
async def test_first_plugin_handoff_preserves_completed_calls_even_without_history() -> None:
    effects: list[str] = []

    @tool
    def write() -> str:
        effects.append("entered")
        return "ok"

    call = function_call("write", {}, call_id="same")
    writer = Agent(
        name="writer",
        model=ScriptedModel([[call], [assistant_message("done")]]),
        plugins=[Plugin(name="records", tools=[write])],
    )
    manager = Agent(
        name="manager",
        model=ScriptedModel([[call], [function_call("transfer_to_writer", {}, call_id="go")]]),
        tools=[write],
        handoffs=[
            handoff(
                writer, input_filter=lambda data: data.clone(pre_handoff_items=(), new_items=())
            )
        ],
    )
    result = await Runner.run(manager, "write", run_config=RunConfig(tracing_disabled=True))
    assert not result.interruptions and effects == ["entered"]
