import asyncio
import sys
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
    RunConfig,
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
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=output.count == 1)

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
    assert checked == [0, 1]


@pytest.mark.parametrize("kind", ["text", "structured", "tool", "approval"])
@pytest.mark.parametrize(
    "failure", [OutputGuardrailTripwireTriggered, LookupError, UserError, asyncio.CancelledError]
)
async def test_blocked_continue_never_reaches_next_turn_or_session(streamed, kind, failure):
    marker = "867530923"
    decisions = []
    checked = []
    effects = []

    @tool(needs_approval=kind == "approval")
    def note() -> str:
        effects.append("note")
        return marker

    @output_guardrail
    def block(ctx, agent, output):
        checked.append(output)
        if failure is not OutputGuardrailTripwireTriggered:
            raise failure("validation interrupted")
        return GuardrailFunctionOutput(output_info=output, tripwire_triggered=True)

    def decide(ctx, step):
        decisions.append(step.output)
        return "continue"

    tool_step = kind in ("tool", "approval")
    candidate = Answer(count=int(marker)) if kind == "structured" else marker
    item = (
        get_function_tool_call("note", "{}", "call")
        if tool_step
        else get_text_message(
            candidate.model_dump_json() if isinstance(candidate, Answer) else marker
        )
    )
    model = ScriptedModel([[item]])
    agent = Agent(
        name="worker",
        model=model,
        tools=[note],
        tool_use_behavior="stop_on_first_tool",
        output_type=Answer if kind == "structured" else None,
        hooks=AgentHooks(step_policy=decide),
    )
    hooks = EndHooks()
    session = SQLiteSession("blocked")
    config = RunConfig(output_guardrails=[block])
    value: str | RunState = "start"
    try:
        if kind == "approval":
            paused = await run(streamed, agent, session=session, run_config=config, hooks=hooks)
            assert not decisions and not checked and not effects
            value = await RunState.from_string(agent, paused.to_state().to_string())
            value.approve(value.get_interruptions()[0])
        with pytest.raises(failure) as caught:
            if streamed:
                stream = Runner.run_streamed(
                    agent, value, session=session, hooks=hooks, run_config=config
                )
                live_items = stream.new_items
                async for _ in stream.stream_events():
                    pass
            else:
                await run(False, agent, value, session=session, hooks=hooks, run_config=config)
        if streamed:
            assert stream.final_output is None
            assert marker not in str(stream.new_items)
            assert marker not in str(live_items)
        saved = await session.get_items()
        assert marker not in str(saved)
        if tool_step:
            assert sum(item.get("type") == "function_call_output" for item in saved) == 1
        if isinstance(caught.value, UserError):
            assert str(caught.value) == "validation interrupted"
            assert caught.value.run_data is None
        if isinstance(caught.value, OutputGuardrailTripwireTriggered):
            assert marker not in str(caught.value.guardrail_result.output.output_info)
        followup = ScriptedModel([[get_text_message("ok")]])
        await run(streamed, Agent(name="next", model=followup), session=session)
        assert marker not in str(followup.calls[0].input)
    finally:
        session.close()
    assert effects == (["note"] if tool_step else [])
    assert decisions == checked == [candidate]
    assert len(model.calls) == 1
    assert hooks.outputs == []


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
    # Python 3.10 can drop a child task's cancellation message.
    message = "decision failed" if error is RuntimeError or sys.version_info >= (3, 11) else None
    with pytest.raises(error, match=message):
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
        with pytest.raises(
            asyncio.CancelledError, match="local" if sys.version_info >= (3, 11) else None
        ):
            await run(streamed, agent, hooks=hooks, session=session)
        items = await session.get_items()
    finally:
        session.close()
    assert hooks.outputs == []
    assert effects == ["record"]
    assert [item.get("type") for item in items[1:]] == ["function_call", "function_call_output"]
    assert items[-1].get("output") == "recorded"


async def test_redacted_tool_candidate_continues_after_approval(streamed):
    secret = "private-example"
    decisions = []
    checked = []

    @tool(needs_approval=True)
    def lookup() -> str:
        return secret

    def decide(ctx, step):
        decisions.append(step.output)
        return "continue" if step.kind == "tool" else "finish"

    @output_guardrail
    def check(ctx, agent, output):
        checked.append(output)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    model = ScriptedModel(
        [[get_function_tool_call("lookup", "{}", "call")], [get_text_message(f"Done {secret}")]]
    )
    agent = Agent(
        name="worker",
        tools=[lookup],
        model=model,
        tool_use_behavior="stop_on_first_tool",
        hooks=AgentHooks(step_policy=decide),
        output_guardrails=[check],
    )
    config = RunConfig(redact_text=lambda text: text.replace(secret, "[hidden]"))
    session = SQLiteSession("redacted")
    hooks = EndHooks()
    try:
        paused = await run(streamed, agent, session=session, run_config=config, hooks=hooks)
        assert not decisions and not checked
        state = await RunState.from_string(agent, paused.to_state().to_string())
        state.approve(state.get_interruptions()[0])
        if streamed:
            result = Runner.run_streamed(
                agent, state, session=session, run_config=config, hooks=hooks
            )
            events = [event async for event in result.stream_events()]
            assert not any(event.type == "raw_response_event" for event in events)
            assert secret not in str(events)
        else:
            result = await run(False, agent, state, session=session, run_config=config, hooks=hooks)
        assert decisions == checked == ["[hidden]", "Done [hidden]"]
        assert hooks.outputs == [result.final_output] == ["Done [hidden]"]
        assert secret not in str((model.calls[1].input, await session.get_items()))
        assert "[hidden]" in str(model.calls[1].input)
    finally:
        session.close()


async def test_protected_continuation_redacts_guardrail_error(streamed):
    secret = "private-example"

    @output_guardrail
    def fail(ctx, agent, output):
        raise UserError(secret) from ValueError(secret)

    model = ScriptedModel([[get_text_message("draft")]])
    agent = Agent(name="worker", model=model, hooks=AgentHooks(step_policy=lambda *_: "continue"))
    config = RunConfig(
        redact_text=lambda text: text.replace(secret, "[hidden]"), output_guardrails=[fail]
    )
    with pytest.raises(RuntimeError) as caught:
        await run(streamed, agent, run_config=config)
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert getattr(caught.value, "run_data", None) is None
    assert len(model.calls) == 1


@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("error", [UserError, asyncio.CancelledError])
async def test_failed_policy_withholds_guarded_tool_output_but_records_effect(
    streamed, approval, error
):
    marker = "unchecked-result"
    effects = []

    @tool(needs_approval=approval)
    def record() -> str:
        effects.append("record")
        return marker

    @output_guardrail
    def check(ctx, agent, output):
        pytest.fail("The policy failed before output validation")

    def fail(ctx, step):
        assert step.output == marker
        raise error("decision failed")

    model = ScriptedModel([[get_function_tool_call("record", "{}", "r")]])
    agent = Agent(
        name="worker",
        tools=[record],
        model=model,
        tool_use_behavior="stop_on_first_tool",
        hooks=AgentHooks(step_policy=fail),
        output_guardrails=[check],
    )
    hooks = EndHooks()
    session = SQLiteSession("effects")
    value: str | RunState = "start"
    try:
        if approval:
            paused = await run(streamed, agent, session=session)
            value = await RunState.from_string(agent, paused.to_state().to_string())
            value.approve(value.get_interruptions()[0])
        with pytest.raises(error) as caught:
            if streamed:
                stream = Runner.run_streamed(agent, value, session=session, hooks=hooks)
                live_items = stream.new_items
                async for event in stream.stream_events():
                    assert marker not in str(event)
            else:
                await run(False, agent, value, session=session, hooks=hooks)
        if streamed:
            assert stream.final_output is None
            assert marker not in str(stream.new_items)
            assert marker not in str(live_items)
        if isinstance(caught.value, UserError):
            assert caught.value.run_data is None
        saved = await session.get_items()
        assert marker not in str(saved)
        assert [item.get("type") for item in saved[1:]] == ["function_call", "function_call_output"]
        assert saved[1].get("call_id") == saved[2].get("call_id") == "r"
        assert saved[2].get("output") == "Output withheld by an output guardrail."
    finally:
        session.close()
    assert hooks.outputs == []
    assert effects == ["record"]
    assert len(model.calls) == 1
