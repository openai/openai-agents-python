import asyncio
import sys
from typing import Literal

import pytest
from openai.types.responses import ResponseOutputText
from pydantic import BaseModel

from agents import (
    Agent,
    AgentHooks,
    ContinueWithFeedback,
    GuardrailFunctionOutput,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    ModelRetrySettings,
    ModelSettings,
    OutputGuardrailTripwireTriggered,
    ProposedFinalOutput,
    RetryDecision,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    RunState,
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
        ctx: RunContextWrapper[dict[str, str]], step: ProposedFinalOutput[dict[str, str]]
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
        hooks=AgentHooks(final_output_policy=require_area),
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
        hooks=AgentHooks(final_output_policy=decide),
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
        hooks=AgentHooks(final_output_policy=decide),
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
        hooks=AgentHooks(final_output_policy=decide),
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
        hooks=AgentHooks(final_output_policy=decide),
    )
    source = Agent(
        name="source",
        model=ScriptedModel(
            [[get_function_tool_call("lookup", "{}", "a"), get_handoff_tool_call(target)]]
        ),
        tools=[lookup],
        handoffs=[target],
        tool_use_behavior="stop_on_first_tool",
        hooks=AgentHooks(final_output_policy=decide),
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
        hooks=AgentHooks(final_output_policy=decide),
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
    agent = Agent(
        name="a", model=model, output_type=Answer, hooks=AgentHooks(final_output_policy=invalid)
    )
    with pytest.raises(ModelBehaviorError):
        await run(streamed, agent, hooks=hooks)
    with pytest.raises(UserError, match="final_output_policy"):
        await run(streamed, agent.clone(output_type=None), hooks=hooks)
    assert hooks.outputs == []


@pytest.mark.parametrize("feedback", [False, True])
async def test_cancel_discards_late_decision_and_joins_policy(streamed, feedback):
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    hooks = EndHooks()

    async def decide(ctx, step):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ContinueWithFeedback("Try again.") if feedback else "finish"
        finally:
            cleaned.set()

    agent = Agent(
        name="a",
        model=ScriptedModel([[get_text_message("draft")]]),
        hooks=AgentHooks(final_output_policy=decide),
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
        hooks=AgentHooks(final_output_policy=fail),
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
        hooks=AgentHooks(final_output_policy=decide),
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
        hooks=AgentHooks(final_output_policy=cancel),
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
        hooks=AgentHooks(final_output_policy=decide),
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
    agent = Agent(
        name="worker", model=model, hooks=AgentHooks(final_output_policy=lambda *_: "continue")
    )
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
        hooks=AgentHooks(final_output_policy=fail),
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


async def test_omitted_refusal_reply_is_distinct_from_continued_answer(streamed):
    decisions = []

    def decide(ctx, step):
        decisions.append(step.output)
        return "continue"

    model = ScriptedModel([[get_text_message("half")], [get_refusal_message("")]])
    agent = Agent(name="worker", model=model, hooks=AgentHooks(final_output_policy=decide))
    with pytest.raises(UserError, match="redact_text"):
        await run(
            streamed,
            agent,
            run_config=RunConfig(redact_text=lambda s: s.replace("halfhalf", "-")),
            error_handlers={
                "model_refusal": lambda _: {"final_output": "half", "include_in_history": False}
            },
        )
    assert decisions == ["half"]
    assert len(model.calls) == 2


@pytest.mark.parametrize("stage", ["schema", "policy", "guardrail"])
async def test_continued_reply_is_clean_at_each_callback_boundary(streamed, stage):
    from pydantic import field_validator

    class Reply(BaseModel):
        text: str

        @field_validator("text")
        @classmethod
        def validate_text(cls, value):
            return "example" if stage == "schema" else value

    seen = []

    def decide(ctx, step):
        seen.append(("policy", step.output.text))
        if stage == "policy":
            step.output.text = "example"
        return "continue"

    @output_guardrail
    def check(ctx, agent, output):
        seen.append(("guardrail", output.text))
        output.text = "example"
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    model = ScriptedModel([[get_text_message('{"text":"ok"}')]])
    agent = Agent(
        name="worker",
        model=model,
        output_type=Reply,
        hooks=AgentHooks(final_output_policy=decide),
        output_guardrails=[check],
    )
    with pytest.raises(UserError, match="redact_text"):
        await run(
            streamed,
            agent,
            "private-",
            max_turns=1,
            run_config=RunConfig(redact_text=lambda s: s.replace("private-example", "-")),
        )
    expected = [] if stage == "schema" else [("policy", "ok")]
    if stage == "guardrail":
        expected.append(("guardrail", "ok"))
    assert seen == expected
    assert len(model.calls) == 1


async def test_feedback_is_one_user_turn_including_retries(streamed):
    from agents.stream_events import RunCompleted

    correction = "Count both groups."
    seen = []

    async def decide(ctx, proposal):
        seen.append(proposal.output.count)
        if proposal.turn == 1:
            return ContinueWithFeedback(correction)
        return "continue" if proposal.turn == 2 else "finish"

    model = ScriptedModel(
        [
            [get_text_message('{"count":1}')],
            ModelStep(error=ConnectionError("retry")),
            [get_text_message('{"count":2}')],
            [get_text_message('{"count":3}')],
        ]
    )
    retry = ModelRetrySettings(max_retries=1, policy=lambda _: RetryDecision(retry=True, delay=0))
    agent = Agent(
        name="worker",
        instructions="Count",
        model=model,
        model_settings=ModelSettings(retry=retry),
        output_type=Answer,
        hooks=AgentHooks(final_output_policy=decide),
    )
    hooks = EndHooks()
    session = SQLiteSession("feedback")
    try:
        if streamed:
            stream = Runner.run_streamed(agent, "start", hooks=hooks, session=session)
            events = [event async for event in stream.subscribe_approved_output()]
            async for _ in stream.stream_events():
                pass
            assert sum(isinstance(event, RunCompleted) for event in events) == 1
            result = stream
        else:
            result = await run(False, agent, hooks=hooks, session=session)
        assert seen == [1, 2, 3]
        assert hooks.outputs == [Answer(count=3)] == [result.final_output]
        assert all(c.system_instructions == "Count" for c in model.calls)
        assert [str(c.input).count(correction) for c in model.calls] == [0, 1, 1, 0]
        assert model.calls[1].input[-1] == {"role": "user", "content": correction}
        assert correction not in str(
            (result.to_input_list(), result.to_state().to_string(), await session.get_items())
        )
    finally:
        session.close()


async def test_feedback_between_two_approvals_is_not_saved(streamed):
    effects = []

    @tool(needs_approval=True)
    def record() -> str:
        effects.append("record")
        return "saved"

    correction = "Record one more entry."

    def decide(ctx, proposal):
        if proposal.turn == 1:
            return ContinueWithFeedback(correction)
        return "continue" if proposal.kind == "tool" else "finish"

    model = ScriptedModel(
        [[get_function_tool_call("record", "{}", str(i))] for i in range(2)]
        + [[get_text_message("done")]]
    )
    agent = Agent(
        name="worker",
        model=model,
        tools=[record],
        tool_use_behavior="stop_on_first_tool",
        hooks=AgentHooks(final_output_policy=decide),
    )
    result = await run(streamed, agent)
    for i in range(2):
        assert len(effects) == i
        saved = result.to_state().to_string()
        assert correction not in saved
        state = await RunState.from_string(agent, saved)
        state.approve(state.get_interruptions()[0])
        result = await run(streamed, agent, state)
    assert result.final_output == "done" and len(effects) == 2
    assert [str(c.input).count(correction) for c in model.calls] == [0, 1, 0]


@pytest.mark.parametrize("protected", [True, False])
async def test_feedback_is_checked_before_filter_and_filter_can_remove_it(streamed, protected):
    correction = "Use SI units."
    observed = []

    def filter_input(data):
        items = data.model_data.input
        if items[-1].get("content") == correction:
            observed.append(items.pop())
        return data.model_data

    model = ScriptedModel([[get_text_message("draft")], [get_text_message("done")]])
    agent = Agent(
        name="worker",
        model=model,
        hooks=AgentHooks(
            final_output_policy=lambda ctx, p: (
                ContinueWithFeedback(correction) if p.turn == 1 else "finish"
            )
        ),
    )
    config = RunConfig(
        call_model_input_filter=filter_input,
        redact_text=(lambda s: s.replace(correction, "-")) if protected else None,
    )
    if protected:
        with pytest.raises(UserError, match="redact_text"):
            await run(streamed, agent, run_config=config)
        assert not observed and len(model.calls) == 1
    else:
        assert (await run(streamed, agent, run_config=config)).final_output == "done"
        assert observed == [{"role": "user", "content": correction}]
        assert correction not in str(model.calls[1].input)


@pytest.mark.parametrize("case", ["blank", "nonstring", "limit", "blocked", "linked"])
async def test_feedback_cannot_bypass_admission(streamed, case):
    @output_guardrail
    def block(ctx, agent, output):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    message = {"blank": "  ", "nonstring": None}.get(case, "Check units.")
    model = ScriptedModel([[get_text_message("draft")]])
    agent = Agent(
        name="worker",
        model=model,
        hooks=AgentHooks(final_output_policy=lambda *_: ContinueWithFeedback(message)),
        output_guardrails=[block] if case == "blocked" else [],
    )
    error = {"limit": MaxTurnsExceeded, "blocked": OutputGuardrailTripwireTriggered}.get(
        case, UserError
    )
    with pytest.raises(error):
        await run(
            streamed,
            agent,
            max_turns=1 if case == "limit" else 2,
            auto_previous_response_id=case == "linked",
        )
    assert len(model.calls) == 1


async def test_concurrent_runs_keep_feedback_local(streamed):
    async def decide(ctx, proposal):
        await asyncio.sleep(0)
        return ContinueWithFeedback(ctx.context) if proposal.turn == 1 else "finish"

    agent = Agent(name="worker", hooks=AgentHooks(final_output_policy=decide))

    async def check(message):
        model = ScriptedModel([[get_text_message("draft")], [get_text_message("done")]])
        result = await run(streamed, agent, context=message, run_config=RunConfig(model=model))
        assert result.final_output == "done"
        assert model.calls[1].input[-1] == {"role": "user", "content": message}

    await asyncio.gather(check("Check length."), check("Check width."))


@pytest.mark.parametrize("mode", ["input", "auto", "previous_response_id"])
async def test_feedback_requires_input_based_compaction(streamed, mode):
    from unittest.mock import MagicMock

    from agents.memory import OpenAIResponsesCompactionSession

    def decide(ctx, proposal):
        return ContinueWithFeedback("Check units.") if proposal.turn == 1 else "finish"

    model = ScriptedModel([[get_text_message("draft")], [get_text_message("done")]])
    agent = Agent(name="worker", model=model, hooks=AgentHooks(final_output_policy=decide))
    backend = SQLiteSession("feedback")
    try:
        session = OpenAIResponsesCompactionSession(
            "feedback",
            backend,
            client=MagicMock(),
            compaction_mode=mode,
            should_trigger_compaction=lambda _: False,
        )
        if mode == "input":
            assert (await run(streamed, agent, session=session)).final_output == "done"
            assert model.calls[1].input[-1] == {"role": "user", "content": "Check units."}
        else:
            with pytest.raises(UserError, match="SDK-managed"):
                await run(streamed, agent, session=session)
            assert len(model.calls) == 1
        assert "Check units." not in str(await session.get_items())
    finally:
        backend.close()


@pytest.mark.parametrize("route", ["direct", "to_policy", "from_policy", "custom"])
async def test_agent_tool_empty_answer_uses_finishing_policy(streamed, route):
    def decide(ctx, proposal):
        return ContinueWithFeedback("Reconsider.") if proposal.output == "draft" else "finish"

    async def extract(result):
        assert result.final_output == ""
        return "custom"

    hooks = AgentHooks(final_output_policy=decide)
    draft = [get_text_message("draft")]
    empty = [get_text_message("")]
    if route == "from_policy":
        target = Agent(name="target", model=ScriptedModel([empty]))
        worker = Agent(
            name="worker",
            model=ScriptedModel([draft, [get_handoff_tool_call(target)]]),
            hooks=hooks,
            handoffs=[target],
        )
    else:
        worker = Agent(name="editor", model=ScriptedModel([draft, empty]), hooks=hooks)
        if route == "to_policy":
            worker = Agent(
                name="worker",
                model=ScriptedModel([[get_handoff_tool_call(worker)]]),
                handoffs=[worker],
            )

    edit = worker.as_tool(
        "edit",
        "Edit",
        on_stream=(lambda _: None) if streamed else None,
        custom_output_extractor=extract if route == "custom" else None,
    )
    model = ScriptedModel(
        [[get_function_tool_call("edit", '{"input":"Review"}', "c")], [get_text_message("done")]]
    )
    await Runner.run(Agent(name="manager", model=model, tools=[edit]), "Review")
    expected = {"custom": "custom", "from_policy": "draft"}.get(route, "")
    assert model.calls[1].input[-1] == {
        "type": "function_call_output",
        "call_id": "c",
        "output": expected,
    }
