import asyncio
from datetime import date
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, BeforeValidator

from agents import Agent, RunConfig, RunHooks, Runner, ToolExecutionConfig, function_tool
from agents.exceptions import UserError
from agents.plugins import Deny, Plugin, PluginHooks, ReplaceArguments, RequireApproval, ToolCall
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext

from .mcp.helpers import FakeMCPServer


def scripted() -> ScriptedModel:
    return ScriptedModel([[function_call("action", {}, call_id="a")], [assistant_message("done")]])


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["run", "stream"])
async def test_date_tool_with_global_policy_fails_before_model_request(stream: bool) -> None:
    effects: list[date] = []

    @function_tool
    def book(day: date) -> str:
        effects.append(day)
        return "booked"

    model = ScriptedModel(
        [[function_call("book", {"day": "2026-05-01"}, call_id="a")], [assistant_message("ok")]]
    )
    gate = Plugin(name="gate", hooks=PluginHooks(check_tool=lambda _: RequireApproval()))
    agent = Agent("calendar", model=model, tools=[book], plugins=[gate])
    with pytest.raises(UserError, match="gate.*book.*JSON"):
        if stream:
            async for _ in Runner.run_streamed(agent, "book").stream_events():
                pass
        else:
            await Runner.run(agent, "book")
    assert not model.calls and not effects

    result = await Runner.run(agent.clone(plugins=[]), "book")
    assert result.final_output == "ok" and effects == [date(2026, 5, 1)]


@pytest.mark.asyncio
async def test_policy_preflight_skips_withheld_tools_and_never_runs_validators() -> None:
    effects: list[str] = []

    def validate(value: str) -> str:
        effects.append(value)
        return value

    @function_tool
    def post(value: Annotated[str, BeforeValidator(validate)]) -> str:
        return value

    async def withhold(_context: Any, _declaration: Any) -> None:
        return None

    model = ScriptedModel([[assistant_message("ok")]])
    gate = Plugin(name="gate", tools=[post], hooks=PluginHooks(check_own_tool=lambda _: None))
    agent = Agent("app", model=model, plugins=[gate])
    config = RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_declaration=withhold))
    assert (await Runner.run(agent, "hi", run_config=config)).final_output == "ok"
    assert not model.calls[0].tools
    with pytest.raises(UserError, match="gate.*post.*JSON"):
        await Runner.run(agent, "hi")
    assert len(model.calls) == 1 and not effects


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["ordinary", "immediate", "after_turn", "immediate_then_graceful", "graceful_then_immediate"],
)
@pytest.mark.parametrize("nested", [False, True])
async def test_synchronous_final_policy_can_cancel_owning_run(mode: str, nested: bool) -> None:
    effects: list[str] = []
    checks: list[str] = []

    @function_tool
    async def action() -> str:
        effects.append("entered")
        return "ok"

    def check(call: ToolCall) -> None:
        checks.append(call.call_id)
        if len(checks) == 2:
            if mode == "ordinary":
                task.cancel()
            elif mode == "immediate":
                stream.cancel()
            elif mode == "immediate_then_graceful":
                stream.cancel()
                stream.cancel(mode="after_turn")
            elif mode == "graceful_then_immediate":
                stream.cancel(mode="after_turn")
                stream.cancel()
            else:
                stream.cancel(mode="after_turn")

    agent = Agent(
        "app",
        model=scripted(),
        plugins=[Plugin(name="gate", tools=[action], hooks=PluginHooks(check_tool=check))],
    )
    if nested:
        agent = Agent(
            "parent",
            model=ScriptedModel(
                [
                    [function_call("child", {"input": "act"}, call_id="c")],
                    [assistant_message("done")],
                ]
            ),
            tools=[agent.as_tool(tool_name="child", tool_description="Ask the specialist")],
        )
    if mode == "ordinary":
        task = asyncio.create_task(Runner.run(agent, "act"))
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        stream = Runner.run_streamed(agent, "act")
        async for _ in stream.stream_events():
            pass
    assert checks == ["a", "a"]
    assert effects == (["entered"] if mode == "after_turn" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["edit", "run", "rename"])
async def test_removing_plugin_during_await_cannot_skip_policy_or_output_filter(phase: str) -> None:
    started, resume = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    async def edit(_: ToolCall) -> None:
        started.set()
        await resume.wait()

    @function_tool
    async def action() -> str:
        effects.append("entered")
        started.set()
        await resume.wait()
        return "PRIVATE"

    hooks = (
        PluginHooks(prepare_tool=edit, check_tool=lambda _: Deny("Forbidden"))
        if phase == "edit"
        else PluginHooks(filter_result=lambda _: "hidden")
    )
    model = scripted()
    agent = Agent("app", model=model, tools=[action], plugins=[Plugin(name="gate", hooks=hooks)])
    task = asyncio.create_task(Runner.run(agent, "act"))
    await asyncio.wait_for(started.wait(), 5)
    if phase == "rename":
        action.name = "alias"
    else:
        agent.plugins = ()
    resume.set()
    with pytest.raises(UserError):
        await task
    assert effects == ([] if phase == "edit" else ["entered"])
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["name", "inventory", "ordinary", "context", "call"])
async def test_approval_pins_live_target_until_first_effect(change: str) -> None:
    effects: list[str] = []

    @function_tool(needs_approval=change == "ordinary")
    async def action() -> str:
        effects.append("entered")
        return "ok"

    class Changed(RunHooks):
        async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
            if change == "name":
                tool.name = "renamed"
            elif change in {"inventory", "ordinary"}:
                agent.tools.clear()
            elif change == "context":
                context.context = "another user"
            else:
                context.tool_call = context.tool_call.model_copy(update={"arguments": '{"a":1}'})

    hooks = None if change == "ordinary" else PluginHooks(check_tool=lambda _: RequireApproval())
    agent = Agent(
        "app", model=scripted(), tools=[action], plugins=[Plugin(name="gate", hooks=hooks)]
    )
    state = (await Runner.run(agent, "act")).to_state()
    state.approve(state.get_interruptions()[0])
    with pytest.raises(UserError):
        await Runner.run(agent, state, hooks=Changed())
    assert not effects


@pytest.mark.asyncio
async def test_two_policies_receive_separate_immutable_fields() -> None:
    fields: dict[str, list[Any]] = {"items": [{"allowed": True}]}
    effects: list[str] = []

    @function_tool
    async def action() -> str:
        effects.append("entered")
        return "ok"

    def first(call: ToolCall) -> RequireApproval:
        assert call.context["items"] == ({"allowed": True},)
        return RequireApproval()

    def second(call: ToolCall) -> Deny:
        assert not call.context
        return Deny("PRIVATE")

    model = scripted()
    agent = Agent(
        "app",
        model=model,
        tools=[action],
        plugins=[
            Plugin(name="first", hooks=PluginHooks(check_tool=first)),
            Plugin(name="second", hooks=PluginHooks(check_tool=second)),
        ],
    )
    config = RunConfig(policy_context=lambda a, p, c, ctx: fields if p == "first" else {})
    result = await Runner.run(agent, "act", run_config=config)
    assert result.final_output == "done" and not effects
    assert not result.interruptions and "PRIVATE" not in str(model.calls)


@pytest.mark.asyncio
async def test_never_skip_a_registered_check_with_false_truth_value() -> None:
    invoked = []

    class Policy:
        def __bool__(self) -> bool:
            return False

        def __call__(self, call: ToolCall) -> Deny:
            return Deny("no")

    @function_tool
    async def action() -> str:
        invoked.append("entered")
        return "ok"

    gate = Plugin(name="policy", hooks=PluginHooks(check_tool=Policy()))
    await Runner.run(Agent("app", tools=[action], model=scripted(), plugins=[gate]), "hi")
    assert invoked == []


@pytest.mark.asyncio
async def test_no_progress_after_synchronous_cancellation_by_a_tool() -> None:
    messages = []

    @function_tool
    async def action(ctx: ToolContext[None]) -> str:
        await ctx.report_progress("started")
        task.cancel()
        await ctx.report_progress("late")
        return "ok"

    plugin = Plugin(
        name="progress",
        hooks=PluginHooks(on_tool_progress=lambda event: messages.append(event.message)),
    )
    task = asyncio.create_task(
        Runner.run(Agent("app", model=scripted(), plugins=[plugin], tools=[action]), "hi")
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    assert messages == ["started"]


@pytest.mark.asyncio
async def test_tool_disabled_during_final_check_cannot_execute() -> None:
    effects = []
    checks = []

    @function_tool
    async def action() -> str:
        effects.append("entered")
        return "ok"

    def check(call: ToolCall) -> None:
        checks.append(call.name)
        if len(checks) == 2:
            asyncio.get_running_loop().call_soon(setattr, action, "is_enabled", False)

    gate = Plugin(name="gate", tools=[action], hooks=PluginHooks(check_tool=check))
    with pytest.raises(UserError):
        await Runner.run(Agent("app", model=scripted(), plugins=[gate]), "hi")
    assert not effects


@pytest.mark.asyncio
async def test_a_missing_local_function_is_not_a_discovered_function() -> None:
    effects = []
    server = FakeMCPServer()
    server.add_tool("lookup", {"type": "object", "properties": {}})

    @function_tool
    async def action() -> str:
        effects.append("entered")
        return "ok"

    class Remove(RunHooks):
        async def on_llm_start(
            self, context: Any, agent: Any, system_prompt: Any, input_items: Any
        ) -> None:
            agent.tools.clear()

    agent = Agent(
        "app",
        model=scripted(),
        tools=[action],
        mcp_servers=[server],
        plugins=[Plugin(name="notes", instructions="Use the tools.")],
    )
    with pytest.raises(UserError):
        await Runner.run(agent, "hi", hooks=Remove())
    assert not effects and not server.tool_calls
    agent.model = ScriptedModel(
        [[function_call("lookup", {}, call_id="b")], [assistant_message("done")]]
    )
    result = await Runner.run(agent, "lookup")
    assert server.tool_calls == ["lookup"]
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_redaction_between_editors_and_result_filters() -> None:
    seen = []

    @function_tool
    async def action(topic: str) -> str:
        seen.append(topic)
        return "original"

    writer = PluginHooks(
        prepare_tool=lambda _: ReplaceArguments({"topic": "private"}),
        filter_result=lambda _: "private",
    )
    reader = PluginHooks(
        prepare_tool=lambda call: seen.append(str(call.arguments["topic"])),
        filter_result=lambda result: seen.append(result.text),
    )
    agent = Agent(
        "app",
        model=ScriptedModel(
            [[function_call("action", {"topic": "old"}, call_id="a")], [assistant_message("ok")]]
        ),
        tools=[action],
        plugins=[Plugin(name="one", hooks=writer), Plugin(name="two", hooks=reader)],
    )
    config = RunConfig(redact_text=lambda text: text.replace("private", "safe"))
    result = await Runner.run(agent, "hi", run_config=config)
    assert seen == ["safe"] * 3
    assert "private" not in str(result.to_input_list())


@pytest.mark.asyncio
async def test_plugin_policy_checks_only_its_own_tools_and_requires_fresh_decisions() -> None:
    effects: list[str] = []

    class Ticket(BaseModel):
        title: str

    @function_tool
    def read(ticket: Ticket) -> str:
        effects.append(ticket.title)
        return ticket.title

    @function_tool
    def send() -> str:
        effects.append("sent")
        return "sent"

    def resolve(agent: Agent[Any], policy: str, call: ToolCall, app: Any) -> dict[str, bool]:
        assert call.owner == policy == "sender"
        return {"allowed": True}

    async def check(call: ToolCall) -> RequireApproval:
        assert call.name == "send" and call.context["allowed"] is True
        return RequireApproval()

    model = ScriptedModel(
        [
            [function_call("read", {"ticket": {"title": "review"}}, call_id="read")],
            [function_call("send", {}, call_id="first")],
            [function_call("send", {}, call_id="second")],
            [assistant_message("done")],
        ]
    )
    sender = Plugin(name="sender", tools=[send], hooks=PluginHooks(check_own_tool=check))
    reader = Plugin(name="reader", tools=[read])
    agent = Agent("desk", model=model, plugins=[sender, reader])
    config = RunConfig(policy_context=resolve)
    first = await Runner.run(agent, "post", run_config=config)
    assert effects == ["review"] and len(first.interruptions) == 1
    state = first.to_state()
    state.approve(first.interruptions[0], always_approve=True)
    second = await Runner.run(agent, state, run_config=config)
    assert effects == ["review", "sent"] and len(second.interruptions) == 1
    state = second.to_state()
    state.reject(second.interruptions[0])
    result = await Runner.run(agent, state, run_config=config)
    assert result.final_output == "done" and effects == ["review", "sent"]


@pytest.mark.asyncio
async def test_global_and_owned_policies_review_final_arguments_together() -> None:
    seen: list[str] = []
    effects: list[str] = []

    @function_tool
    def post(channel: str) -> str:
        effects.append(channel)
        return "sent"

    def request(call: ToolCall) -> RequireApproval:
        seen.append(str(call.arguments["channel"]))
        return RequireApproval()

    owner = Plugin(name="chat", tools=[post], hooks=PluginHooks(check_own_tool=request))
    guard = Plugin(
        name="privacy",
        hooks=PluginHooks(
            prepare_tool=lambda call: ReplaceArguments({"channel": "private"}),
            check_tool=lambda call: (
                Deny("blocked") if call.arguments["channel"] == "private" else None
            ),
        ),
    )
    model = ScriptedModel(
        [[function_call("post", {"channel": "public"}, call_id="a")], [assistant_message("done")]]
    )
    result = await Runner.run(Agent("desk", model=model, plugins=[owner, guard]), "post")
    assert result.final_output == "done" and not result.interruptions
    assert seen == ["private"] and not effects


@pytest.mark.asyncio
async def test_parallel_policies_snapshot_shared_resolver_fields() -> None:
    effects: list[str] = []
    shared = {"allowed": False}

    @function_tool
    async def read(name: str) -> str:
        effects.append(name)
        return name

    async def resolve(agent: Agent[Any], plugin: str, call: ToolCall, app: Any) -> dict[str, bool]:
        shared["allowed"] = call.arguments["name"] == "public"
        return shared

    def check(call: ToolCall) -> Deny | None:
        return None if call.context["allowed"] is True else Deny("Private")

    calls = [function_call("read", {"name": name}, call_id=name) for name in ("private", "public")]
    agent = Agent(
        "reader",
        model=ScriptedModel([calls, [assistant_message("done")]]),
        plugins=[Plugin(name="access", tools=[read], hooks=PluginHooks(check_tool=check))],
    )
    await Runner.run(agent, "read", run_config=RunConfig(policy_context=resolve))
    assert effects == ["public"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["app", "plugin"])
async def test_concurrent_runs_capture_reused_argument_edit(source: str) -> None:
    effects: list[tuple[str, str]] = []
    shared = {"name": ""}
    both = asyncio.Event()

    @function_tool
    async def read(ctx: ToolContext[str], name: str) -> str:
        effects.append((ctx.context, name))
        return name

    async def app_edit(call: Any) -> dict[str, Any]:
        shared["name"] = call.call_id
        return shared

    async def prepare(call: ToolCall) -> ReplaceArguments | None:
        if call.call_id == "one":
            await both.wait()
        else:
            both.set()
        if source == "app":
            return None
        shared["name"] = call.call_id
        return ReplaceArguments(shared)

    settings = ToolExecutionConfig(prepare_tool_input=app_edit) if source == "app" else None
    plugin: Plugin[str] = Plugin(
        name="records", tools=[read], hooks=PluginHooks(prepare_tool=prepare)
    )

    async def submit(user: str) -> None:
        call = function_call("read", {"name": "mine"}, call_id=user)
        model = ScriptedModel([[call], [assistant_message("done")]])
        await Runner.run(
            Agent("reader", plugins=[plugin], model=model),
            "read",
            context=user,
            run_config=RunConfig(tool_execution=settings),
        )

    await asyncio.gather(submit("one"), submit("two"))
    assert sorted(effects) == [("one", "one"), ("two", "two")]
