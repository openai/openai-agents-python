import asyncio
import json
import traceback
from typing import Any, get_type_hints

import pytest
from openai.types.responses import (
    ResponseApplyPatchToolCall,
    ResponseCustomToolCall,
    ResponseFunctionWebSearch,
)
from pydantic import BaseModel, Field, Json, SkipValidation, TypeAdapter, field_validator

from agents import (
    Agent,
    ApplyPatchTool,
    ComputerTool,
    ModelBehaviorError,
    RunConfig,
    RunHooks,
    Runner,
    RunState,
    ShellTool,
    SQLiteSession,
    ToolCallHookContext,
    ToolExecutionConfig,
    ToolGuardrailFunctionOutput,
    UserError,
    function_tool,
)
from agents.decorators import tool, tool_input_guardrail, tool_output_guardrail
from agents.testing import ScriptedModel, assistant_message, function_call

from .test_apply_patch_tool import RecordingEditor
from .test_computer_action import BatchedClick, LoggingComputer, ResponseComputerToolCall
from .test_run_state_pending_input import _make_after_turn_state, _message_text
from .test_shell_tool import _shell_call


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_effective_arguments_and_results_reach_executor_history_and_model(streamed):
    effects = []
    model = ScriptedModel(
        [
            [function_call("publish", {"text": "proposal"}, call_id="req")],
            [assistant_message("done")],
        ]
    )

    @tool_input_guardrail
    def check_input(data):
        effects.append(("input guardrail", json.loads(data.context.tool_arguments)["text"]))
        return ToolGuardrailFunctionOutput.allow()

    @tool_output_guardrail
    def check_output(data):
        effects.append(("output guardrail", data.output))
        return ToolGuardrailFunctionOutput.allow()

    @tool(tool_input_guardrails=[check_input], tool_output_guardrails=[check_output])
    async def publish(text: str) -> str:
        effects.append(("execute", text))
        return "private receipt"

    async def before(call: ToolCallHookContext[str]) -> dict[str, Any]:
        effects.append(("before", call.call_id, call.tool.name, call.arguments["text"]))
        call.arguments["text"] = call.run_context.context
        return call.arguments

    async def after(call: ToolCallHookContext[str], result: Any) -> str:
        effects.append(("after", call.arguments["text"], result))
        return "public receipt"

    class Observers(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            effects.append(("start", json.loads(context.tool_arguments)["text"]))

        async def on_tool_end(self, context, agent, tool, result):
            effects.append(("end", result))

    config = RunConfig(
        tool_execution=ToolExecutionConfig(prepare_tool_input=before, prepare_tool_output=after)
    )
    agent = Agent(name="test", model=model, tools=[publish])
    session = SQLiteSession("hooks")
    if streamed:
        result = Runner.run_streamed(
            agent, "go", context="accepted", run_config=config, hooks=Observers(), session=session
        )
        events = [event async for event in result.stream_events()]
        original = next(
            e.data.item
            for e in events
            if e.type == "raw_response_event"
            and e.data.type == "response.output_item.done"
            and e.data.item.type == "function_call"
        )
        assert json.loads(original.arguments) == {"text": "proposal"}
    else:
        result = await Runner.run(
            agent, "go", context="accepted", run_config=config, hooks=Observers(), session=session
        )
    assert effects == [
        ("before", "req", "publish", "proposal"),
        ("input guardrail", "accepted"),
        ("start", "accepted"),
        ("execute", "accepted"),
        ("after", "accepted", "private receipt"),
        ("output guardrail", "public receipt"),
        ("end", "public receipt"),
    ]
    assert result.final_output == "done"
    for history in (model.calls[1].input, await session.get_items(), result.to_input_list()):
        outgoing = next(item for item in history if item.get("type") == "function_call_output")
        assert outgoing["call_id"] == "req" and outgoing["output"] == "public receipt"
        assert "private receipt" not in str(history)
    session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_patch_editor_gets_rewritten_path_and_model_gets_replacement(legacy):
    operation = {"type": "delete_file", "path": "proposal.txt"}
    raw = (
        function_call("apply_patch", operation, call_id="edit")
        if legacy
        else ResponseApplyPatchToolCall.model_validate(
            {
                "type": "apply_patch_call",
                "id": "edit",
                "call_id": "edit",
                "operation": operation,
                "status": "in_progress",
            }
        )
    )
    model = ScriptedModel([[raw], [assistant_message("done")]])
    editor = RecordingEditor()

    async def before(call):
        call.arguments["operation"]["path"] = "approved.txt"
        return call.arguments

    async def after(call, output):
        assert call.arguments["operation"]["path"] == "approved.txt"
        return "edit complete"

    await Runner.run(
        Agent(name="editor", model=model, tools=[ApplyPatchTool(editor=editor)]),
        "edit",
        run_config=RunConfig(
            tool_execution=ToolExecutionConfig(prepare_tool_input=before, prepare_tool_output=after)
        ),
    )
    assert [op.path for op in editor.operations] == ["approved.txt"]
    item = next(
        item for item in model.calls[1].input if item.get("type") == "apply_patch_call_output"
    )
    assert item["output"] == "edit complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["allow", "fail", "timeout"])
async def test_approval_binds_rewrite_across_serialized_resume(policy):
    effects = []
    model = ScriptedModel([[function_call("publish", {"text": "proposed"}, call_id="req")]])

    async def requires_review(ctx, args, call_id):
        effects.append(("approval", args["text"]))
        return args["text"] == "review this"

    @tool(needs_approval=requires_review)
    async def publish(text: str) -> str:
        effects.append(("execute", text))
        return text

    async def before(call):
        effects.append(("rewrite", call.arguments["text"]))
        return {"text": "review this"}

    agent = Agent(name="test", model=model, tools=[publish], tool_use_behavior="stop_on_first_tool")
    config = RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_input=before))
    first = await Runner.run(agent, "go", run_config=config)
    assert json.loads(first.interruptions[0].raw_item.arguments) == {"text": "review this"}
    assert effects == [("rewrite", "proposed"), ("approval", "review this")]
    snapshot = first.to_state().to_json()
    for old_version in ("1.14", "1.22"):
        with pytest.raises(UserError, match="schema 1.23"):
            await RunState.from_json(agent, {**snapshot, "$schemaVersion": old_version})
    state = await RunState.from_json(agent, snapshot)
    state.approve(state.get_interruptions()[0])
    if policy != "allow":

        class Unavailable(RunHooks):
            async def on_tool_start(self, context, agent, tool):
                if policy == "timeout":
                    await asyncio.wait_for(asyncio.Event().wait(), timeout=0)
                raise RuntimeError("Policy unavailable")

        with pytest.raises(UserError) as error:
            await Runner.run(agent, state, run_config=config, hooks=Unavailable())
        expected = asyncio.TimeoutError if policy == "timeout" else RuntimeError
        assert isinstance(error.value.__cause__, expected)
        assert effects == [("rewrite", "proposed"), ("approval", "review this")]
        return
    resumed = await Runner.run(agent, state, run_config=config)
    assert resumed.final_output == "review this"
    assert effects == [
        ("rewrite", "proposed"),
        ("approval", "review this"),
        ("execute", "review this"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after"])
async def test_hook_failure_withholds_tool_call_or_output(phase):
    effects = []
    model = ScriptedModel([[function_call("publish", {}, call_id="req")]])

    @tool
    async def publish() -> str:
        effects.append("executed")
        return "private"

    async def failing(*args):
        raise UserError("policy denied")

    config = RunConfig(
        tool_execution=ToolExecutionConfig(
            prepare_tool_input=failing if phase == "before" else None,
            prepare_tool_output=failing if phase == "after" else None,
        )
    )
    session = SQLiteSession("failure")
    with pytest.raises(UserError, match="policy denied"):
        await Runner.run(
            Agent(name="test", model=model, tools=[publish]),
            "go",
            run_config=config,
            session=session,
        )
    assert effects == ([] if phase == "before" else ["executed"])
    assert len(model.calls) == 1
    assert all(item.get("type") != "function_call_output" for item in await session.get_items())
    session.close()


@pytest.mark.asyncio
async def test_legacy_patch_metadata_cannot_bypass_admission():
    item = ResponseCustomToolCall.model_validate(
        {
            "type": "custom_tool_call",
            "name": "apply_patch",
            "call_id": "outer",
            "input": json.dumps(
                {
                    "call_id": "inner",
                    "type": "function_call",
                    "caller": {"type": "direct"},
                    "operation": {"type": "delete_file", "path": "proposed.txt"},
                }
            ),
        }
    )
    editor = RecordingEditor()
    observed = []

    async def deny(call):
        observed.append((call.call_id, call.arguments))
        raise UserError("edit denied")

    with pytest.raises(UserError, match="edit denied"):
        await Runner.run(
            Agent(
                name="editor", model=ScriptedModel([[item]]), tools=[ApplyPatchTool(editor=editor)]
            ),
            "go",
            run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_input=deny)),
        )
    assert observed == [("outer", {"operation": {"type": "delete_file", "path": "proposed.txt"}})]
    assert editor.operations == []


@pytest.mark.asyncio
async def test_cancelled_input_hook_never_executes_or_resends_acknowledged_input():
    model, agent, state, executed = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("late message")
    model.enqueue([function_call("record_destination", {"destination": "Rome"}, call_id="new")])
    started = asyncio.Event()
    release = asyncio.Event()
    returned = asyncio.Event()

    async def before(call):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass
        returned.set()
        return call.arguments

    config = RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_input=before))
    attempt = asyncio.create_task(Runner.run(agent, state, run_config=config))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        await asyncio.wait_for(returned.wait(), timeout=2)
    finally:
        release.set()
        attempt.cancel()
        await asyncio.gather(attempt, return_exceptions=True)
    assert state.pending_input == []
    snapshot = await RunState.from_json(agent, state.to_json())
    with pytest.raises(UserError, match="accepted model response could not be processed"):
        await Runner.run(agent, snapshot, run_config=config)
    assert executed == ["Paris"]
    assert sum(_message_text(item) == "late message" for c in model.calls for item in c.input) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_replay_uses_original_decision_after_serialized_resume(changed):
    decisions = []
    executed = []
    model = ScriptedModel([[function_call("save", {"n": 0}, call_id="repeat")]])

    @tool
    async def save(n: int) -> str:
        executed.append(n)
        return "saved"

    async def before(call):
        decisions.append(call.arguments["n"])
        return {"n": len(decisions)}

    agent = Agent(name="test", model=model, tools=[save])
    config = RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_input=before))
    stream = Runner.run_streamed(agent, "go", run_config=config)
    async for event in stream.stream_events():
        if event.type == "run_item_stream_event" and event.name == "tool_output":
            stream.cancel(mode="after_turn")
    snapshot = stream.to_state().to_json()
    restored = await RunState.from_json(agent, snapshot)
    model.enqueue(
        [
            function_call("save", {"n": 8 if changed else 0}, call_id="repeat"),
            assistant_message("done"),
        ]
    )
    if changed:
        with pytest.raises(ModelBehaviorError, match="different invocation"):
            await Runner.run(agent, restored, run_config=config)
    else:
        assert (await Runner.run(agent, restored, run_config=config)).final_output == "done"
    assert decisions == [0]
    assert executed == [1]


@pytest.mark.asyncio
async def test_output_only_hook_can_replace_malformed_function_error():
    @tool
    async def change(n: int) -> str:
        pytest.fail("malformed input must never execute")

    raw = function_call("change", {}, call_id="bad")
    raw.arguments = "{invalid json"
    model = ScriptedModel([[raw], [assistant_message("corrected")]])

    async def after(call, output):
        assert call.arguments == {} and call.run_context.tool_arguments == "{invalid json"
        assert isinstance(output, str)
        return "use a JSON object"

    await Runner.run(
        Agent(name="test", model=model, tools=[change]),
        "go",
        run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_output=after)),
    )
    assert (
        next(x for x in model.calls[1].input if x.get("type") == "function_call_output")["output"]
        == "use a JSON object"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_replacement_of_generated_error_uses_declared_output_schema(valid):
    class Receipt(BaseModel):
        value: int

    @function_tool(output_type=Receipt)
    async def lookup() -> Receipt:
        raise ValueError("handler failed")

    model = ScriptedModel(
        [[function_call("lookup", {}, call_id="lookup")], [assistant_message("done")]]
    )

    async def after(call, output):
        assert isinstance(output, str)
        return {"value": 7} if valid else {"wrong": 7}

    run = Runner.run(
        Agent(name="test", model=model, tools=[lookup]),
        "go",
        run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_output=after)),
    )
    if valid:
        await run
        out = next(x for x in model.calls[1].input if x.get("type") == "function_call_output")
        assert json.loads(out["output"]) == {"value": 7}
    else:
        with pytest.raises(UserError, match="declared output schema"):
            await run
        assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_native_output_filter_settles_after_parallel_tool_fails():
    hook_started = asyncio.Event()
    side_effects = []

    @tool(failure_error_function=None)
    async def fail() -> str:
        await hook_started.wait()
        raise ValueError("sibling failed")

    def shell(request):
        side_effects.append("shell executed")
        return "internal"

    async def after(call, output):
        hook_started.set()
        await asyncio.sleep(0.02)
        return "public"

    model = ScriptedModel([[function_call("fail", {}, call_id="bad"), _shell_call("ok")]])
    session = SQLiteSession("sibling")
    try:
        with pytest.raises(UserError, match="sibling failed"):
            await Runner.run(
                Agent(name="test", model=model, tools=[fail, ShellTool(executor=shell)]),
                "go",
                session=session,
                run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_output=after)),
            )
        history = await session.get_items()
        assert side_effects == ["shell executed"]
        accepted = next(x for x in history if x.get("type") == "shell_call_output")
        assert "public" in str(accepted) and "internal" not in str(history)
    finally:
        session.close()


@pytest.mark.asyncio
async def test_ga_batched_computer_actions_can_be_rewritten():
    computer = LoggingComputer()
    call = ResponseComputerToolCall(
        id="item",
        type="computer_call",
        call_id="batch",
        pending_safety_checks=[],
        status="completed",
        actions=[BatchedClick(type="click", x=1, y=2, button="left")],
    )
    model = ScriptedModel([[call], [assistant_message("done")]])

    async def before(ctx):
        ctx.arguments["actions"][0]["x"] = 8
        return ctx.arguments

    async def after(ctx, screenshot):
        assert ctx.arguments["actions"][0]["x"] == 8
        return "cHVibGlj"

    await Runner.run(
        Agent(name="test", model=model, tools=[ComputerTool(computer=computer)]),
        "go",
        run_config=RunConfig(
            tool_execution=ToolExecutionConfig(prepare_tool_input=before, prepare_tool_output=after)
        ),
    )
    assert computer.calls[0] == ("click", (8, 2, "left"))
    output = next(x for x in model.calls[1].input if x.get("type") == "computer_call_output")
    assert output["output"]["image_url"] == "data:image/png;base64,cHVibGlj"


@pytest.mark.asyncio
async def test_reordering_tool_arguments_reaches_python_executor():
    @tool(strict_mode=False)
    async def ordered(items: dict[str, str]) -> str:
        return ",".join(items)

    async def before(ctx):
        return {"items": dict(reversed(ctx.arguments["items"].items()))}

    model = ScriptedModel(
        [[function_call("ordered", {"items": {"a": "a", "b": "b"}}, call_id="map")]]
    )
    agent = Agent(name="test", model=model, tools=[ordered], tool_use_behavior="stop_on_first_tool")
    result = await Runner.run(
        agent,
        "go",
        run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_input=before)),
    )
    assert result.final_output == "b,a"


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["private", "public"])
async def test_duplicate_proposals_execute_exactly_the_input_seen_by_the_hook(first):
    observed = []
    executed = []

    @tool(strict_mode=False)
    async def choose(options: dict[str, str]) -> str:
        selected = next(iter(options))
        executed.append(selected)
        return selected

    async def admit(call):
        selected = next(iter(call.arguments["options"]))
        observed.append(selected)
        if selected == "private":
            raise UserError("private choice denied")
        return call.arguments

    options = dict.fromkeys([first, "private" if first == "public" else "public"], "v")
    model = ScriptedModel(
        [
            [
                function_call("choose", {"options": options}, call_id="same"),
                function_call(
                    "choose", {"options": dict(reversed(options.items()))}, call_id="same"
                ),
            ]
        ]
    )
    run = Runner.run(
        Agent(name="test", model=model, tools=[choose], tool_use_behavior="stop_on_first_tool"),
        "choose",
        run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_input=admit)),
    )
    if first == "private":
        with pytest.raises(UserError, match="choice denied"):
            await run
        assert executed == []
    else:
        assert (await run).final_output == "public"
        assert executed == ["public"]
    assert observed == [first]


@pytest.mark.asyncio
async def test_concurrent_runs_capture_separate_hook_decisions_and_results():
    class Receipt(BaseModel):
        destination: str

    shared_input = {}
    shared_output = Receipt(destination="")
    executed = {}

    @function_tool(output_type=Receipt)
    async def dispatch(destination: str) -> Receipt:
        return Receipt(destination=destination)

    async def before(call):
        shared_input["destination"] = call.run_context.context
        return shared_input

    async def after(call, receipt):
        executed[call.call_id] = receipt.destination
        shared_output.destination = receipt.destination
        return shared_output

    config = RunConfig(
        tool_execution=ToolExecutionConfig(prepare_tool_input=before, prepare_tool_output=after)
    )

    async def run(destination):
        model = ScriptedModel(
            [[function_call("dispatch", {"destination": "proposal"}, call_id=destination)]]
        )
        result = await Runner.run(
            Agent(
                name=destination,
                model=model,
                tools=[dispatch],
                tool_use_behavior="stop_on_first_tool",
            ),
            "go",
            context=destination,
            run_config=config,
        )
        item = next(i for i in result.new_items if i.type == "tool_call_output_item")
        return json.loads(item.raw_item["output"])["destination"]

    assert await asyncio.gather(run("first"), run("second")) == ["first", "second"]
    assert executed == {"first": "first", "second": "second"}


@pytest.mark.asyncio
@pytest.mark.parametrize("rewrite", [False, True])
async def test_stream_consumers_cannot_modify_a_call_during_admission(rewrite):
    started = asyncio.Event()
    release = asyncio.Event()
    executed = []

    @tool
    async def dispatch(destination: str) -> str:
        executed.append(destination)
        return destination

    @tool
    async def other(destination: str) -> str:
        pytest.fail("A stream consumer cannot select another tool after admission.")

    async def before(call):
        assert call.tool.name == "dispatch" and call.arguments == {"destination": "reviewed"}
        started.set()
        await release.wait()
        return {"destination": "accepted"} if rewrite else call.arguments

    model = ScriptedModel([[function_call("dispatch", {"destination": "reviewed"}, call_id="req")]])
    stream = Runner.run_streamed(
        Agent(
            name="agent",
            model=model,
            tools=[dispatch, other],
            tool_use_behavior="stop_on_first_tool",
        ),
        "go",
        run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_input=before)),
    )
    try:
        async for event in stream.stream_events():
            if event.type == "raw_response_event" and event.data.type == "response.completed":
                await asyncio.wait_for(started.wait(), timeout=2)
                event.data.response.output[0].name = "other"
                event.data.response.output[0].arguments = '{"destination":"unreviewed"}'
                release.set()
    finally:
        release.set()
    assert executed == ["accepted" if rewrite else "reviewed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_source", ["raw", "tool_called"])
async def test_output_only_hook_redacts_using_the_arguments_the_tool_executed(event_source):
    started = asyncio.Event()
    event_edited = asyncio.Event()

    @tool(failure_error_function=None)
    async def fetch(scope: str) -> str:
        started.set()
        await asyncio.wait_for(event_edited.wait(), timeout=5)
        return f"{scope} record"

    async def redact(call, result):
        assert result == "private record"
        return "redacted" if call.arguments["scope"] == "private" else result

    model = ScriptedModel(
        [[function_call("fetch", {"scope": "private"}, call_id="req")], [assistant_message("done")]]
    )
    stream = Runner.run_streamed(
        Agent(name="agent", model=model, tools=[fetch]),
        "go",
        run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_output=redact)),
    )
    try:
        async for event in stream.stream_events():
            if (
                event_source == "raw"
                and event.type == "raw_response_event"
                and event.data.type == "response.completed"
            ):
                call = event.data.response.output[0]
            elif (
                event_source == "tool_called"
                and event.type == "run_item_stream_event"
                and event.name == "tool_called"
            ):
                call = event.item.raw_item
            else:
                continue
            if call.type == "function_call":
                await asyncio.wait_for(started.wait(), timeout=2)
                call.arguments = '{"scope":"public"}'
                event_edited.set()
    finally:
        event_edited.set()

    output = next(
        item for item in model.calls[1].input if item.get("type") == "function_call_output"
    )
    assert output["output"] == "redacted"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize(
    ("redacted", "streamed"),
    [(False, False), (True, False), (True, True)],
    ids=["diagnostics", "redacted", "redacted-stream"],
)
async def test_rejected_hook_payloads_respect_diagnostic_redaction(
    phase, redacted, streamed, monkeypatch
):
    from agents import _debug

    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)
    private_value = "diagnostic-payload-" + phase

    class Receipt(BaseModel):
        amount: int

    @function_tool(output_type=Receipt, failure_error_function=None)
    async def lookup() -> Receipt:
        return Receipt(amount=1)

    async def before(call):
        pytest.fail("Invalid input must fail before the callback.")

    async def after(call, result):
        return {"amount": private_value}

    raw = function_call("lookup", {}, call_id="id")
    if phase == "before":
        raw.arguments = '{"amount":"' + private_value
    agent = Agent(name="test", model=ScriptedModel([[raw]]), tools=[lookup])
    config = RunConfig(
        tracing_disabled=True,
        tool_execution=ToolExecutionConfig(
            prepare_tool_input=before if phase == "before" else None,
            prepare_tool_output=after if phase == "after" else None,
        ),
    )
    with pytest.raises(ModelBehaviorError if phase == "before" else UserError) as caught:
        if streamed:
            async for _ in Runner.run_streamed(agent, "go", run_config=config).stream_events():
                pass
        else:
            await Runner.run(agent, "go", run_config=config)
    formatted = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert (private_value in formatted) is (not redacted)
    if redacted:
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert getattr(caught.value, "doc", None) is None


@pytest.mark.asyncio
async def test_hosted_call_with_extra_metadata_cannot_impersonate_local_call():
    hosted = ResponseFunctionWebSearch.model_validate(
        {
            "id": "hosted",
            "type": "web_search_call",
            "status": "completed",
            "action": {"type": "search", "query": "example"},
            "call_id": "local",
            "namespace": "hosted",
        }
    )
    seen = []

    @tool
    async def publish(note: str) -> str:
        return note

    async def before(ctx):
        seen.append((ctx.tool.name, ctx.tool_namespace))
        return {"note": "public"}

    model = ScriptedModel(
        [
            [hosted, function_call("publish", {"note": "original"}, call_id="local")],
            [assistant_message("done")],
        ]
    )
    result = await Runner.run(
        Agent(name="test", model=model, tools=[publish]),
        "go",
        run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_input=before)),
    )
    assert seen == [("publish", None)]
    hosted_item = next(i for i in result.to_input_list() if i.get("type") == "web_search_call")
    assert "note" not in hosted_item


@pytest.mark.asyncio
async def test_identity_hook_preserves_already_validated_fields_and_json_values():
    class Receipt(BaseModel):
        amount: int = Field(serialization_alias="amountCents")
        items: Json[list[int]]

        @field_validator("amount")
        @classmethod
        def cents(cls, amount: int) -> int:
            return amount * 100

    @function_tool(output_type=Receipt)
    async def lookup() -> Receipt:
        return Receipt(amount=2, items="[1,2]")

    observed = []

    async def after(call, value):
        observed.append((value.amount, value.items))
        return value

    model = ScriptedModel(
        [[function_call("lookup", {}, call_id="typed")], [assistant_message("done")]]
    )
    result = await Runner.run(
        Agent(name="test", model=model, tools=[lookup]),
        "go",
        run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_output=after)),
    )
    accepted = next(i for i in result.new_items if i.type == "tool_call_output_item")
    wire = next(i for i in model.calls[1].input if i.get("type") == "function_call_output")
    assert observed == [(200, [1, 2])]
    assert accepted.output.amount == 200 and accepted.output.items == [1, 2]
    assert json.loads(wire["output"]) == {"amountCents": 200, "items": [1, 2]}


@pytest.mark.asyncio
async def test_output_hook_preserves_serializer_warnings():
    class Measurement(BaseModel):
        reading: SkipValidation[int]

    @tool(output_type=Measurement)
    async def measure() -> Measurement:
        return Measurement.model_validate({"reading": "pending"})

    async def pass_through(call, result):
        return result

    model = ScriptedModel(
        [[function_call("measure", {}, call_id="measurement")], [assistant_message("done")]]
    )
    with pytest.warns(UserWarning, match="serializer"):
        await Runner.run(
            Agent(name="test", model=model, tools=[measure]),
            "go",
            run_config=RunConfig(
                tool_execution=ToolExecutionConfig(prepare_tool_output=pass_through)
            ),
        )
    output = next(i for i in model.calls[1].input if i.get("type") == "function_call_output")
    assert json.loads(output["output"]) == {"reading": "pending"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("point", "fallback"),
    [
        ("response.output_item.done", True),
        ("response.completed", False),
        ("response.completed", True),
    ],
)
async def test_declaration_preserves_streamed_call_before_admission(point, fallback):
    displayed = asyncio.Event()

    @function_tool
    def report(color: str):
        return color

    async def allow(ctx, declaration):
        return declaration.description

    class PausedModel(ScriptedModel):
        async def stream_response(self, *args, **kwargs):
            async for event in super().stream_response(*args, **kwargs):
                if event.type == "response.completed" and fallback:
                    event.response.output = []
                yield event
                if event.type == point:
                    await asyncio.wait_for(displayed.wait(), timeout=5)

    model = PausedModel([[function_call("report", {"color": "Blue"}, call_id="one")]])
    result = Runner.run_streamed(
        Agent(name="host", model=model, tools=[report], tool_use_behavior="stop_on_first_tool"),
        "go",
        run_config=RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_declaration=allow)),
    )
    async for event in result.stream_events():
        if event.type == "raw_response_event" and event.data.type == point:
            item = (
                event.data.item
                if point == "response.output_item.done"
                else event.data.response.output[0]
            )
            item.arguments = '{"color":"blue"}'
            displayed.set()
    assert result.final_output == "Blue"


def test_tool_execution_annotations_resolve_for_settings_adapters():
    assert "prepare_tool_declaration" in get_type_hints(ToolExecutionConfig)
    config = TypeAdapter(ToolExecutionConfig).validate_python({"max_function_tool_concurrency": 2})
    assert config.max_function_tool_concurrency == 2
    assert config.prepare_tool_declaration is None
