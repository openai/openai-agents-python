import asyncio
from typing import Literal

import pytest
from openai.types.responses import ResponseOutputText
from pydantic import BaseModel

from agents import (
    Agent,
    AgentHooks,
    GuardrailFunctionOutput,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    RunHooks,
    Runner,
    RunState,
    SettledStep,
    SQLiteSession,
    UserError,
)
from agents.decorators import output_guardrail, tool
from agents.testing import ModelStep, ScriptedModel

from .model_test_helpers import get_exact_output_stream_step
from .test_responses import (
    get_function_tool_call,
    get_handoff_tool_call,
    get_refusal_message,
    get_text_message,
)

pytestmark = pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])


class EndHooks(RunHooks):
    def __init__(self):
        self.outputs = []

    async def on_agent_end(self, context, agent, output):
        self.outputs.append(output)


async def run(streamed, agent, value="start", **kwargs):
    if not streamed:
        return await Runner.run(agent, value, **kwargs)
    result = Runner.run_streamed(agent, value, **kwargs)
    async for _ in result.stream_events():
        pass
    return result


async def test_require_two_dependent_specialists(streamed):
    context: dict[str, str] = {}
    decisions = []
    effects = []

    @tool
    def measure(ctx: RunContextWrapper[dict[str, str]]) -> str:
        effects.append("measure")
        ctx.context["width"] = "8"
        return "8"

    @tool(is_enabled=lambda ctx, agent: "width" in ctx.context)
    def calculate(ctx: RunContextWrapper[dict[str, str]]) -> str:
        effects.append("calculate")
        ctx.context["area"] = str(int(ctx.context["width"]) ** 2)
        return ctx.context["area"]

    async def require_area(
        ctx: RunContextWrapper[dict[str, str]], step: SettledStep[dict[str, str]]
    ) -> Literal["continue", "finish"]:
        assert step.agent is agent
        decisions.append((step.turn, step.kind, step.output))
        if "area" in ctx.context:
            return "finish"
        ctx.context["next"] = "Calculate area" if "width" in ctx.context else "Measure width"
        return "continue"

    model = ScriptedModel(
        [
            [get_text_message("guess")],
            [get_function_tool_call("measure", "{}", "m")],
            [get_text_message("another guess")],
            [get_function_tool_call("calculate", "{}", "c")],
        ]
    )
    agent = Agent[dict[str, str]](
        name="square",
        model=model,
        tools=[measure, calculate],
        instructions=lambda ctx, agent: ctx.context.get("next", "Find the area"),
        tool_use_behavior="stop_on_first_tool",
        hooks=AgentHooks(step_policy=require_area),
    )
    hooks = EndHooks()
    session = SQLiteSession("square")
    try:
        result = await run(streamed, agent, context=context, hooks=hooks, session=session)
        assert "guess" in str(await session.get_items())
    finally:
        session.close()

    assert result.final_output == "64"
    assert hooks.outputs == ["64"]
    assert effects == ["measure", "calculate"]
    assert decisions == [
        (1, "text", "guess"),
        (2, "tool", "8"),
        (3, "text", "another guess"),
        (4, "tool", "64"),
    ]
    assert [c.system_instructions for c in model.calls] == [
        "Find the area",
        "Measure width",
        "Calculate area",
        "Calculate area",
    ]
    assert calculate not in model.calls[1].tools
    assert calculate in model.calls[2].tools
    assert "guess" in str(model.calls[1].input)


async def test_approval_resume_decides_once_with_ordered_siblings(streamed):
    effects = []
    steps = []

    @tool(needs_approval=True)
    def gated() -> str:
        effects.append("gated")
        return "first"

    @tool
    def ready() -> str:
        effects.append("ready")
        return "second"

    def decide(ctx, step):
        steps.append(step)
        return "finish" if step.output == "done" else "continue"

    model = ScriptedModel(
        [
            [get_text_message("draft")],
            ModelStep(
                output=[
                    get_function_tool_call("gated", "{}", "a"),
                    get_function_tool_call("ready", "{}", "b"),
                ],
                response_id="batch",
                request_id="request",
            ),
            [get_text_message("done")],
        ]
    )
    agent = Agent(
        name="worker",
        model=model,
        tools=[gated, ready],
        tool_use_behavior="stop_on_first_tool",
        hooks=AgentHooks(step_policy=decide),
    )
    hooks = EndHooks()
    paused = await run(streamed, agent, hooks=hooks)
    assert effects == ["ready"]
    assert len(steps) == 1
    assert hooks.outputs == []
    state = await RunState.from_string(agent, paused.to_state().to_string())
    state.approve(state.get_interruptions()[0])
    result = await run(streamed, agent, state, hooks=hooks)
    assert result.final_output == "done"
    assert hooks.outputs == ["done"]
    assert effects == ["ready", "gated"]
    assert [(s.turn, s.output) for s in steps] == [(1, "draft"), (2, "first"), (3, "done")]
    batch = steps[1]
    assert (batch.kind, batch.response_id, batch.request_id) == ("tool", "batch", "request")
    assert [item.call_id for item in batch.items] == ["a", "b", "a", "b"]


class Answer(BaseModel):
    count: int


async def test_parsed_candidates_still_require_output_guardrails(streamed):
    seen = []
    checked = []

    def decide(ctx, step):
        seen.append((step.kind, step.output))
        return "finish" if step.output.count else "continue"

    @output_guardrail
    async def block(ctx, agent, output):
        checked.append(output.count)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    model = ScriptedModel([[get_text_message('{"count":0}')], [get_text_message('{"count":1}')]])
    agent = Agent(
        name="count",
        model=model,
        output_type=Answer,
        hooks=AgentHooks(step_policy=decide),
        output_guardrails=[block],
    )
    with pytest.raises(OutputGuardrailTripwireTriggered):
        await run(streamed, agent)
    assert seen == [("structured", Answer(count=0)), ("structured", Answer(count=1))]
    assert checked == [1]


async def test_handoff_and_nonterminal_tools_precede_candidate_policy(streamed):
    steps = []

    @tool
    def lookup() -> str:
        return "value"

    def decide(ctx, step):
        steps.append((step.agent.name, step.turn, step.output))
        return "finish"

    target = Agent(
        name="target",
        model=ScriptedModel(
            [[get_function_tool_call("lookup", "{}", "b")], [get_text_message("done")]]
        ),
        tools=[lookup],
        hooks=AgentHooks(step_policy=decide),
    )
    source = Agent(
        name="source",
        model=ScriptedModel(
            [[get_function_tool_call("lookup", "{}", "a"), get_handoff_tool_call(target)]]
        ),
        tools=[lookup],
        handoffs=[target],
        tool_use_behavior="stop_on_first_tool",
        hooks=AgentHooks(step_policy=decide),
    )
    result = await run(streamed, source)
    assert result.last_agent is target
    assert steps == [("target", 3, "done")]


async def test_empty_text_can_continue_only_within_turn_limit(streamed):
    seen = []

    def decide(ctx, step):
        seen.append((step.turn, step.output))
        return "continue"

    empty = get_text_message("")
    null = ResponseOutputText.model_construct(type="output_text", annotations=[], text=None)
    missing = empty.model_copy(update={"content": [null]})
    agent = Agent(
        name="a",
        model=ScriptedModel(
            [
                [],
                [empty.model_copy(update={"content": []})],
                get_exact_output_stream_step([missing]) if streamed else [missing],
                [empty],
            ]
        ),
        hooks=AgentHooks(step_policy=decide),
    )
    hooks = EndHooks()
    with pytest.raises(MaxTurnsExceeded):
        await run(streamed, agent, max_turns=4, hooks=hooks)
    assert seen == [(4, "")]
    assert hooks.outputs == []


async def test_invalid_candidate_or_decision_cannot_finish(streamed):
    hooks = EndHooks()

    def invalid(ctx, step):
        return None

    model = ScriptedModel([[get_text_message("invalid")], [get_text_message("draft")]])
    agent = Agent(name="a", model=model, output_type=Answer, hooks=AgentHooks(step_policy=invalid))
    with pytest.raises(ModelBehaviorError):
        await run(streamed, agent, hooks=hooks)
    with pytest.raises(UserError, match="step_policy"):
        await run(streamed, agent.clone(output_type=None), hooks=hooks)
    assert hooks.outputs == []


async def test_cancel_discards_late_finish_and_joins_policy(streamed):
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    hooks = EndHooks()

    async def decide(ctx, step):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "finish"
        finally:
            cleaned.set()

    agent = Agent(
        name="a",
        model=ScriptedModel([[get_text_message("draft")]]),
        hooks=AgentHooks(step_policy=decide),
    )
    if streamed:
        result = Runner.run_streamed(agent, "start", hooks=hooks)
        try:
            await asyncio.wait_for(entered.wait(), 3)
        finally:
            result.cancel()
            async for _ in result.stream_events():
                pass
        assert result.final_output is None
    else:
        task = asyncio.create_task(Runner.run(agent, "start", hooks=hooks))
        try:
            await asyncio.wait_for(entered.wait(), 3)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert cleaned.is_set()
    assert hooks.outputs == []


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_policy_failure_after_approval_preserves_tool_effect(streamed, error):
    effects = []
    hooks = EndHooks()

    @tool(needs_approval=True)
    def record() -> str:
        effects.append("record")
        return "recorded"

    async def fail(ctx, step):
        raise error("decision failed")

    agent = Agent(
        name="worker",
        tools=[record],
        model=ScriptedModel([[get_function_tool_call("record", "{}", "r")]]),
        tool_use_behavior="stop_on_first_tool",
        hooks=AgentHooks(step_policy=fail),
    )
    paused = await run(streamed, agent)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    with pytest.raises(error, match="decision failed"):
        await run(streamed, agent, state, hooks=hooks)
    assert hooks.outputs == []
    assert effects == ["record"]
    agent.hooks = None
    result = await run(streamed, agent, state, hooks=hooks)
    assert result.final_output == "recorded"
    assert effects == ["record"]


async def test_empty_refusal_is_never_a_text_candidate(streamed):
    def decide(ctx, step):
        pytest.fail("A refusal cannot be approved")

    agent = Agent(
        name="worker",
        model=ScriptedModel([[get_refusal_message("")]]),
        hooks=AgentHooks(step_policy=decide),
    )
    with pytest.raises(ModelRefusalError):
        await run(streamed, agent)


@pytest.mark.parametrize("synchronous", [False, True])
async def test_policy_local_cancellation_keeps_completed_tool_in_session(streamed, synchronous):
    effects = []

    @tool
    def record() -> str:
        effects.append("record")
        return "recorded"

    def cancel(ctx, step):
        if synchronous:
            raise asyncio.CancelledError("local")
        future = asyncio.get_running_loop().create_future()
        future.cancel("local")
        return future

    hooks = EndHooks()
    agent = Agent(
        name="worker",
        tools=[record],
        model=ScriptedModel([[get_function_tool_call("record", "{}", "r")]]),
        tool_use_behavior="stop_on_first_tool",
        hooks=AgentHooks(step_policy=cancel),
    )
    session = SQLiteSession("effects")
    try:
        with pytest.raises(asyncio.CancelledError, match="local"):
            await run(streamed, agent, hooks=hooks, session=session)
        items = await session.get_items()
    finally:
        session.close()
    assert hooks.outputs == []
    assert effects == ["record"]
    assert [item.get("type") for item in items[1:]] == ["function_call", "function_call_output"]
    assert items[-1].get("output") == "recorded"
