import copy
import json
from dataclasses import replace
from typing import Any

import pytest

from agents import Agent, AgentHooks, RunConfig, Runner, RunState, function_tool
from agents.exceptions import UserError
from agents.plugins import Deny, Plugin, PluginHooks, RequireApproval, ToolCall
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext


def reader(effects: list[str], discoveries: list[str]) -> Agent:
    def check(call: ToolCall) -> RequireApproval | Deny:
        if call.context.get("allowed") is True:
            return RequireApproval()
        return Deny("Forbidden")

    def enabled(*_: object) -> bool:
        discoveries.append("checked")
        return True

    @function_tool(is_enabled=enabled)
    def read(note: str) -> str:
        effects.append(note)
        return "ok"

    return Agent(
        "reader",
        model=ScriptedModel(
            [[function_call("read", {"note": "N42"}, call_id="id")], [assistant_message("done")]]
        ),
        plugins=[
            Plugin(name="notes", tools=[read]),
            Plugin(name="review", hooks=PluginHooks(check_tool=check)),
        ],
    )


def config() -> RunConfig:
    return RunConfig(
        tracing_disabled=True,
        plugin_revision="r1",
        policy_context=lambda a, p, call, ctx: {"allowed": ctx["allowed"]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approve_first", [True, False])
async def test_restore_plugin_approval_on_fresh_worker(approve_first: bool) -> None:
    effects: list[str] = []
    first = reader(effects, [])
    result = await Runner.run(
        first, "read", context={"allowed": True, "key": "secret-key"}, run_config=config()
    )
    assert result.interruptions and not effects
    state = result.to_state()
    if approve_first:
        state.approve(result.interruptions[0])
    wire = state.to_string()
    assert "secret-key" not in wire and '"allowed"' not in wire
    second = reader(effects, []).clone(model=ScriptedModel([[assistant_message("done")]]))
    restored = await RunState.from_string(
        second, wire, context_override={"allowed": True}, plugin_revision="r1"
    )
    if not approve_first:
        restored.approve(restored.get_interruptions()[0])
    finished = await Runner.run(second, restored, run_config=config())
    assert finished.final_output == "done"
    assert effects == ["N42"]


@pytest.mark.asyncio
async def test_plugin_restore_checks_binding_before_discovery() -> None:
    calls: list[str] = []
    effects: list[str] = []
    agent = reader(effects, calls)
    context = {"allowed": True}
    result = await Runner.run(agent, "read", context=context, run_config=config())
    state = result.to_state()
    state.approve(result.interruptions[0])
    original = json.loads(state.to_string())
    assert original["$schemaVersion"] == "1.26+plugins.1"
    calls.clear()
    tool = agent.plugins[0].tools[0]
    ordinary = agent.clone(plugins=[], tools=[tool])
    for changed in (
        ordinary,
        agent.clone(plugins=[agent.plugins[0]]),
        agent.clone(plugins=[Plugin(name="notes"), agent.plugins[1]], tools=[tool]),
        agent.clone(plugins=list(reversed(agent.plugins))),
    ):
        with pytest.raises(UserError, match="Plugin checkpoint"):
            await RunState.from_json(
                changed, original, context_override=context, plugin_revision="r1"
            )
    for rev in (None, "r2"):
        with pytest.raises(UserError, match="Plugin checkpoint"):
            await RunState.from_json(agent, original, context_override=context, plugin_revision=rev)
    with pytest.raises(UserError, match="Plugin checkpoint"):
        await RunState.from_json(agent, original, plugin_revision="r1")
    wrong_args = copy.deepcopy(original)
    wrong_args["last_processed_response"]["functions"][0]["tool_call"]["arguments"] = (
        '{"note":"N43"}'
    )
    missing = copy.deepcopy(original)
    del missing["plugin_state"]
    for changed_json in (wrong_args, missing, {**original, "$schemaVersion": "1.22"}):
        with pytest.raises(UserError, match="Plugin checkpoint"):
            await RunState.from_json(
                agent, changed_json, context_override=context, plugin_revision="r1"
            )
    for changed_json in (
        missing,
        {**original, "plugin_state": None},
        {**original, "$schemaVersion": "1.26"},
        {**original, "$schemaVersion": "1.26", "plugin_state": None},
    ):
        with pytest.raises(UserError, match="Plugin checkpoint"):
            await RunState.from_json(ordinary, changed_json)
    with pytest.raises(UserError, match="schema version is not supported"):
        await RunState.from_json(
            agent, {**original, "$schemaVersion": "1.29"}, plugin_revision="r1"
        )
    assert not calls and not effects


@pytest.mark.asyncio
async def test_in_memory_plugin_approval_cannot_transfer_to_changed_owner() -> None:
    effects: list[str] = []
    agent = reader(effects, [])
    result = await Runner.run(agent, "read", context={"allowed": True}, run_config=config())
    state = result.to_state()
    state.approve(result.interruptions[0])
    with pytest.raises(UserError):
        await Runner.run(agent, state, run_config=replace(config(), plugin_revision="other"))
    agent.tools = [agent.plugins[0].tools[0]]
    agent.plugins = ()
    with pytest.raises(UserError):
        await Runner.run(agent, state, run_config=config())
    assert not effects


@pytest.mark.asyncio
async def test_detached_proposal_cannot_approve_another_checkpoint() -> None:
    effects: list[str] = []
    agent = reader(effects, [])
    context = {"allowed": True}
    result = await Runner.run(agent, "read", context=context, run_config=config())
    first = result.to_state()
    second = await RunState.from_json(
        agent, first.to_json(), context_override=context, plugin_revision="r1"
    )
    pending = first.get_interruptions()[0]
    for wrong in (pending, result.interruptions[0]):
        with pytest.raises(UserError):
            second.approve(wrong)
    first.approve(pending)
    assert not second.to_json()["plugin_state"]["calls"]["id"]["approved"]
    restored = await RunState.from_json(
        agent, first.to_json(), context_override={"allowed": False}, plugin_revision="r1"
    )
    resumed = Runner.run_streamed(agent, restored, run_config=config())
    async for _ in resumed.stream_events():
        pass
    assert resumed.final_output == "done"
    assert not effects


@pytest.mark.asyncio
async def test_ordinary_approval_cannot_become_a_plugin_checkpoint() -> None:
    effects: list[str] = []
    discoveries: list[str] = []
    target = reader(effects, discoveries)
    tool = target.plugins[0].tools[0]
    tool.needs_approval = True
    ordinary = target.clone(plugins=[], tools=[tool])
    result = await Runner.run(ordinary, "read", run_config=config())
    state = result.to_state()
    state.approve(result.interruptions[0])
    current = state.to_json()
    assert current["$schemaVersion"] == "1.26"
    assert "plugin_state" not in current
    discoveries.clear()
    for snapshot in (current, {**current, "$schemaVersion": "1.21"}):
        with pytest.raises(UserError, match="Plugin checkpoint"):
            await RunState.from_json(target, snapshot, plugin_revision="r1")
    with pytest.raises(UserError, match="Plugin checkpoint"):
        await RunState.from_json(ordinary, current, plugin_revision="r1")
    assert not effects and not discoveries
    restored = await RunState.from_json(ordinary, current)
    finished = await Runner.run(ordinary, restored, run_config=config())
    assert finished.final_output == "done" and effects == ["N42"]


@pytest.mark.asyncio
@pytest.mark.parametrize("redact", [False, True])
async def test_plugin_requires_revision_only_for_durable_state(redact: bool) -> None:
    effects: list[str] = []
    agent = reader(effects, [])
    cfg = replace(
        config(), plugin_revision=None, redact_text=(lambda text: text) if redact else None
    )
    result = await Runner.run(agent, "read", context={"allowed": True}, run_config=cfg)
    state = result.to_state()
    with pytest.raises(UserError):
        state.to_json()
    state.approve(state.get_interruptions()[0])
    await Runner.run(agent, state, run_config=cfg)
    assert effects == ["N42"]


@pytest.mark.asyncio
async def test_intermediate_plugin_cannot_drop_nested_ordinary_approval() -> None:
    effects: list[str] = []
    cfg = replace(config(), redact_text=lambda text: text)

    def build(with_plugin: bool) -> Agent:
        leaf = reader(effects, [])
        tool = leaf.plugins[0].tools[0]
        tool.needs_approval = True
        agent = leaf.clone(tools=[tool], plugins=[])
        for name in ("inner", "middle", "outer"):
            agent = Agent(
                "new-inner" if with_plugin and name == "inner" else name,
                model=ScriptedModel([[function_call("delegate", {"input": "read"}, call_id=name)]]),
                tools=[agent.as_tool("delegate", "Read a note", run_config=cfg)],
                plugins=[Plugin(name="helper")] if with_plugin and name == "inner" else [],
            )
        return agent

    ordinary = await Runner.run(build(False), "read", run_config=cfg)
    snapshot = ordinary.to_state().to_json()
    with pytest.raises(UserError, match="Plugin checkpoint"):
        await RunState.from_json(build(True), snapshot)
    # An unresolved parent must not hide a plugin binding in a deeper approval.
    for fields in ({"plugin_state": None}, {"$schemaVersion": "1.99+plugins.1"}):
        changed = copy.deepcopy(snapshot)
        child = changed["last_processed_response"]["functions"][0]["agent_run_state"]
        child["current_agent"] = {"name": "unknown"}
        child["last_processed_response"]["functions"][0]["agent_run_state"].update(fields)
        with pytest.raises(UserError, match="Plugin checkpoint"):
            await RunState.from_json(build(False), changed)
    mixed = await Runner.run(build(True), "read", run_config=cfg)
    assert mixed.interruptions and not effects
    with pytest.raises(UserError, match="Resume nested plugin approvals in memory"):
        mixed.to_state().to_json()


@pytest.mark.asyncio
async def test_writer_cannot_drop_plugin_approval_in_grandchild() -> None:
    effects: list[str] = []
    agent = reader(effects, [])
    for name in ("middle", "outer"):
        delegate = agent.as_tool("delegate", "Read a note", run_config=config())
        agent = Agent(
            name,
            model=ScriptedModel([[function_call("delegate", {"input": "read"}, call_id=name)]]),
            tools=[delegate],
        )
    result = await Runner.run(agent, "read", context={"allowed": True}, run_config=config())
    assert result.interruptions and not effects
    with pytest.raises(UserError):
        result.to_state().to_json()


@pytest.mark.asyncio
async def test_revisionless_resume_still_checks_redaction() -> None:
    effects: list[str] = []
    agent = reader(effects, [])
    cfg = replace(config(), plugin_revision=None)
    result = await Runner.run(agent, "read", context={"allowed": True}, run_config=cfg)
    state = result.to_state()
    state.approve(result.interruptions[0])
    protected = replace(cfg, redact_text=lambda text: text.replace("N42", "[redacted]"))
    with pytest.raises(UserError):
        await Runner.run(agent, state, run_config=protected)
    assert not effects


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [0, 1, 2])
@pytest.mark.parametrize("policy", [False, True])
async def test_plugin_decisions_preserve_origin_through_delegation(
    depth: int, policy: bool
) -> None:
    effects: list[str] = []
    contexts: list[ToolContext[str]] = []

    class Capture(AgentHooks[str]):
        async def on_tool_start(self, context: Any, agent: Agent[str], tool: Any) -> None:
            contexts.append(context)

    @function_tool(needs_approval=not policy)
    def read(ctx: ToolContext[str], value: str) -> str:
        effects.append(ctx.context)
        return value

    agent = Agent[str](
        name="reader",
        model=ScriptedModel(
            [[function_call("read", {"value": "same"}, call_id="mod")]] * 2
            + [[assistant_message("done")]]
        ),
        plugins=[
            Plugin(
                name="notes",
                tools=[read],
                hooks=PluginHooks(check_tool=lambda _: RequireApproval()) if policy else None,
            )
        ],
    )
    for level in range(depth):
        call = function_call("delegate", {"input": "go"}, call_id=f"delegate-{level}")
        agent = Agent(
            name=f"parent-{level}",
            model=ScriptedModel([[call], [call], [assistant_message("done")]]),
            tools=[agent.as_tool("delegate", "Read")],
            hooks=Capture() if level == depth - 1 else None,
        )
    config = RunConfig(tracing_disabled=True, plugin_revision="r1")
    first = await Runner.run(agent, "go", context="first", run_config=config)
    second = await Runner.run(agent, "go", context="second", run_config=config)
    assert not effects and len(first.interruptions) == len(second.interruptions) == 1
    foreign = (first.interruptions[0], first.to_state().get_interruptions()[0])
    for wrong in foreign:
        with pytest.raises(UserError, match="current plugin approval"):
            second.context_wrapper.approve_tool(wrong)
        with pytest.raises(UserError, match="current plugin approval"):
            second.context_wrapper.reject_tool(wrong)
    if depth:
        for wrong in foreign:
            with pytest.raises(UserError, match="current plugin approval"):
                contexts[-1].approve_tool(wrong)
            with pytest.raises(UserError, match="current plugin approval"):
                contexts[-1].reject_tool(wrong)
    if depth == 1:
        contexts[-1].approve_tool(second.interruptions[0])
    state = second.to_state()
    for wrong in foreign:
        with pytest.raises(UserError, match="current plugin approval"):
            state.approve(wrong, always_approve=True)
        with pytest.raises(UserError, match="current plugin approval"):
            state.reject(wrong, always_reject=True)
    if depth == 0:
        forged = replace(
            first.interruptions[0],
            raw_item={
                "type": "function_call",
                "call_id": "unknown",
                "name": "read",
                "arguments": '{"value":"same"}',
            },
        )
        with pytest.raises(UserError, match="current plugin approval"):
            state.approve(forged, always_approve=True)
    if depth != 1:
        state.approve(state.get_interruptions()[0])
    # A different caller must not inherit this decision, even through a plain manager.
    with pytest.raises(UserError, match="original application context"):
        await Runner.run(agent, state, context="first", run_config=config)
    with pytest.raises(UserError, match="original application context"):
        stream = Runner.run_streamed(agent, state, context="first", run_config=config)
        async for _ in stream.stream_events():
            pass
    assert not effects
    finished = await Runner.run(agent, state, run_config=config)
    assert finished.final_output == "done" and effects == ["second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [False, True])
async def test_sticky_function_approval_never_satisfies_a_new_policy_vote(policy: bool) -> None:
    effects: list[int] = []

    @function_tool(needs_approval=True)
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
    plugin = Plugin(
        name="notes",
        tools=[read],
        hooks=PluginHooks(check_tool=lambda _: RequireApproval()) if policy else None,
    )
    agent = Agent("a", model=model, plugins=[plugin])
    cfg = RunConfig(tracing_disabled=True)
    result = await Runner.run(agent, "go", run_config=cfg)
    if policy:
        state = result.to_state()
        state.approve(result.interruptions[0], always_approve=True)
    else:
        result.context_wrapper.approve_tool(result.interruptions[0], always_approve=True)
        state = result.to_state()
    next_result = await Runner.run(agent, state, run_config=cfg)
    if policy:
        assert effects == [1]
        state = next_result.to_state()
        state.approve(next_result.interruptions[0])
        next_result = await Runner.run(agent, state, run_config=cfg)
    assert effects == [1, 2] and next_result.final_output == "done"
