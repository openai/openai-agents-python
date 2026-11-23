"""Caller-visible boundaries of the optional Runner text redactor."""

import asyncio
import json
import logging

import pytest

from agents import Agent, ResumableSession, RunConfig, Runner, UserError
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call

from .memory.test_native_approval_recovery import setup_native

SECRET = "private-example"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("source", ["executor", "redactor", "provider", "instructions"])
async def test_grouped_failures_preserve_control_flow_without_private_diagnostics(streamed, source):
    from agents.exceptions import BaseExceptionGroup
    from agents.testing import ModelStep

    original = BaseExceptionGroup(
        SECRET,
        [
            RuntimeError(SECRET),
            BaseExceptionGroup(
                SECRET,
                [
                    asyncio.CancelledError(SECRET),
                    SystemExit(SECRET),
                    KeyboardInterrupt(SECRET),
                    GeneratorExit(SECRET),
                    BaseException(SECRET),
                ],
            ),
        ],
    )
    original.__cause__ = ValueError(SECRET)

    @tool(failure_error_function=None)
    async def lookup() -> str:
        raise original

    def grouped_policy(value):
        if value == "trigger" and source == "redactor":
            raise original
        return scrub(value)

    def fail(_context, _agent=None):
        raise original

    step = (
        ModelStep.respond(fail)
        if source == "provider"
        else [function_call("lookup", {}, call_id="call")]
    )
    agent = Agent(
        name="worker",
        instructions=fail if source == "instructions" else None,
        tools=[lookup],
        model=ScriptedModel([step]),
    )
    with pytest.raises(BaseExceptionGroup) as caught:
        config = RunConfig(redact_text=grouped_policy)
        if streamed:
            async for _ in Runner.run_streamed(agent, "trigger", run_config=config).stream_events():
                pass
        else:
            await Runner.run(agent, "trigger", run_config=config)

    safe = caught.value
    assert not isinstance(safe, Exception)
    assert safe is not original
    assert isinstance(safe.exceptions[0], Exception)
    nested = safe.exceptions[1]
    assert isinstance(nested, BaseExceptionGroup)
    assert isinstance(nested.exceptions[0], asyncio.CancelledError)
    assert isinstance(nested.exceptions[1], SystemExit)
    assert nested.exceptions[1].code == 1
    assert isinstance(nested.exceptions[2], KeyboardInterrupt)
    assert isinstance(nested.exceptions[3], GeneratorExit)
    assert not isinstance(nested.exceptions[4], Exception)
    pending = [safe]
    while pending:
        error = pending.pop()
        assert SECRET not in str(error)
        assert error.__cause__ is error.__context__ is None
        assert SECRET not in repr(vars(error))
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        if error is not safe:
            assert error.__traceback__ is None
    assert original.__cause__.args == (SECRET,)
    assert original.exceptions[0].args == (SECRET,)


@pytest.mark.asyncio
async def test_grouped_tool_failure_without_redaction_retains_original_diagnostics():
    from agents.exceptions import BaseExceptionGroup

    original = BaseExceptionGroup(SECRET, [RuntimeError(SECRET), asyncio.CancelledError(SECRET)])

    @tool(failure_error_function=None)
    async def lookup() -> str:
        raise original

    agent = Agent(
        name="worker",
        tools=[lookup],
        model=ScriptedModel([[function_call("lookup", {}, call_id="call")]]),
    )
    with pytest.raises(BaseExceptionGroup) as caught:
        await Runner.run(agent, "go")
    assert caught.value is original


def scrub(value: str) -> str:
    return value.replace(SECRET, "[removed]")


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("include", [False, True])
@pytest.mark.parametrize("kind", ["max_turns", "model_refusal", "invalid_final_output"])
async def test_handler_results_are_scrubbed_independently_of_history(streamed, include, kind):
    from pydantic import BaseModel

    from agents import RunErrorHandlerResult, RunHooks

    from .test_responses import get_refusal_message

    class Reply(BaseModel):
        text: str

    observed = []

    class Hooks(RunHooks):
        async def on_agent_end(self, context, agent, output):
            observed.append(output)

    structured = kind == "invalid_final_output"
    step = get_refusal_message("no") if kind == "model_refusal" else assistant_message("oops")
    agent = Agent(
        name="worker", model=ScriptedModel([[step]]), output_type=Reply if structured else str
    )

    def handler(data):
        text = data.context.context
        return RunErrorHandlerResult(Reply(text=text) if structured else text, include)

    options = {
        "context": SECRET,
        "hooks": Hooks(),
        "run_config": RunConfig(redact_text=scrub),
        "error_handlers": {kind: handler},
        "max_turns": 0 if kind == "max_turns" else 1,
    }
    if streamed:
        result = Runner.run_streamed(agent, "go", **options)
        async for event in result.stream_events():
            assert SECRET not in repr(event)
    else:
        result = await Runner.run(agent, "go", **options)
    expected = Reply(text="[removed]") if structured else "[removed]"
    assert result.final_output == expected
    assert observed == [expected]
    assert ("[removed]" in repr(result.to_input_list())) is include


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_private_fallback_cannot_complete_a_secret_outside_history(streamed):
    agent = Agent(name="worker", model=ScriptedModel([]))
    options = {
        "run_config": RunConfig(redact_text=scrub),
        "error_handlers": {
            "max_turns": lambda _: {"final_output": "example", "include_in_history": False}
        },
        "max_turns": 0,
    }
    with pytest.raises(UserError, match="redact_text"):
        if streamed:
            async for _ in Runner.run_streamed(agent, "private-", **options).stream_events():
                pass
        else:
            await Runner.run(agent, "private-", **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_custom_tool_final_output_is_scrubbed(streamed):
    from agents import ToolsToFinalOutputResult

    @tool
    def lookup() -> str:
        return "ok"

    def finish(context, results):
        return ToolsToFinalOutputResult(is_final_output=True, final_output=context.context)

    agent = Agent(
        name="worker",
        tools=[lookup],
        tool_use_behavior=finish,
        model=ScriptedModel([[function_call("lookup", {}, call_id="one")]]),
    )
    config = RunConfig(redact_text=scrub)
    if streamed:
        result = Runner.run_streamed(agent, "go", context=SECRET, run_config=config)
        async for event in result.stream_events():
            assert SECRET not in repr(event)
    else:
        result = await Runner.run(agent, "go", context=SECRET, run_config=config)
    assert result.final_output == "[removed]"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_parsed_final_output_does_not_duplicate_a_message(streamed):
    from pydantic import BaseModel

    class Reply(BaseModel):
        text: str

    agent = Agent(
        name="worker",
        model=ScriptedModel([[assistant_message('{"text":"half"}')]]),
        output_type=Reply,
    )
    config = RunConfig(redact_text=lambda text: text.replace("halfhalf", "[removed]"))
    if streamed:
        result = Runner.run_streamed(agent, "go", run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "go", run_config=config)
    assert result.final_output == Reply(text="half")


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("stage", ["schema", "guardrail"])
@pytest.mark.parametrize("source", ["model", "saved_fallback", "omitted_fallback"])
async def test_final_output_is_checked_after_validation_and_guardrails(streamed, stage, source):
    from pydantic import BaseModel, field_validator

    from agents import GuardrailFunctionOutput, output_guardrail

    class Reply(BaseModel):
        text: str

        @field_validator("text")
        @classmethod
        def validate_text(cls, value):
            return "example" if stage == "schema" else value

    @output_guardrail
    def inspect_output(ctx, agent, output):
        assert output.text == "ok"
        output.text = "example"
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    agent = Agent(
        name="worker",
        model=ScriptedModel([[assistant_message('{"text":"ok"}')]]),
        output_type=Reply,
        output_guardrails=[inspect_output],
    )
    config = RunConfig(redact_text=scrub)
    options = {}
    if source != "model":
        options = {
            "max_turns": 0,
            "error_handlers": {
                "max_turns": lambda _: {
                    "final_output": {"text": "ok"},
                    "include_in_history": source == "saved_fallback",
                },
            },
        }
    with pytest.raises(UserError, match="redact_text"):
        if streamed:
            result = Runner.run_streamed(agent, "private-", run_config=config, **options)
            async for event in result.stream_events():
                if event.type == "run_item_stream_event":
                    assert "example" not in repr(event.item.raw_item)
        else:
            await Runner.run(agent, "private-", run_config=config, **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "source", ["instructions", "filter", "parallel", "blocking", "output", "interrupt"]
)
async def test_callback_errors_release_private_context(streamed, source):
    import traceback

    from agents import input_guardrail, output_guardrail

    error_type = BaseException if source == "interrupt" else RuntimeError

    def fail(ctx, *_):
        raise error_type(ctx.context) from ValueError(ctx.context)

    agent = Agent(name="worker", model=ScriptedModel([[assistant_message("ok")]]))
    config = RunConfig(redact_text=scrub)
    if source in ("instructions", "interrupt"):
        agent.instructions = fail
    elif source == "filter":
        config.call_model_input_filter = fail
    elif source == "output":
        agent.output_guardrails = [output_guardrail(fail)]
    else:
        agent.input_guardrails = [input_guardrail(run_in_parallel=source == "parallel")(fail)]

    result = None
    with pytest.raises(error_type) as caught:
        if streamed:
            result = Runner.run_streamed(agent, "go", context=SECRET, run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go", context=SECRET, run_config=config)
    assert type(caught.value) is error_type
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is caught.value.__context__ is None
    for frame, _ in traceback.walk_tb(caught.tb):
        if frame.f_globals.get("__name__", "").startswith("agents."):
            assert SECRET not in repr(frame.f_locals)
    if result is not None and result.run_loop_exception is not None:
        assert SECRET not in str(result.run_loop_exception)


@pytest.mark.asyncio
async def test_unprotected_callback_error_keeps_original_diagnostics():
    def fail(ctx, agent):
        raise ValueError(ctx.context) from LookupError(ctx.context)

    agent = Agent(name="worker", instructions=fail, model=ScriptedModel([]))
    with pytest.raises(ValueError, match=SECRET) as caught:
        await Runner.run(agent, "go", context=SECRET)
    assert isinstance(caught.value.__cause__, LookupError)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("filtered", [False, True])
async def test_effective_instructions_are_scrubbed_before_model_call(streamed, filtered):
    def update(call):
        call.model_data.instructions += " " + SECRET
        return call.model_data

    model = ScriptedModel([[assistant_message("ok")]])
    agent = Agent(name="worker", instructions=lambda ctx, agent: ctx.context, model=model)
    config = RunConfig(redact_text=scrub, call_model_input_filter=update if filtered else None)
    if streamed:
        result = Runner.run_streamed(agent, "go", context=SECRET, run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, "go", context=SECRET, run_config=config)
    assert model.calls[0].system_instructions == (
        "[removed] [removed]" if filtered else "[removed]"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", [False, True])
async def test_static_private_instructions_never_reach_the_agent_on_a_stream_event(handoff):
    model = ScriptedModel([[assistant_message("ok")]])
    agent = Agent(name="worker", instructions=SECRET, model=model)
    if handoff:
        agent = Agent(name="coordinator", handoffs=[agent], model=model)
    with pytest.raises(UserError, match="redact_text"):
        result = Runner.run_streamed(agent, "go", run_config=RunConfig(redact_text=scrub))
        async for event in result.stream_events():
            assert SECRET not in repr(event)
    assert not model.calls


@pytest.mark.asyncio
async def test_policy_follows_nested_runs_but_not_concurrent_callers(caplog):
    caplog.set_level(logging.DEBUG, logger="openai.agents")

    @tool
    async def lookup(value: str) -> str:
        assert value == "[removed]"
        agent = Agent(name="inner", model=ScriptedModel([[assistant_message(SECRET)]]))
        result = await Runner.run(agent, SECRET)
        assert result.input == result.final_output == "[removed]"
        logging.getLogger("openai.agents").debug("private %s", SECRET)
        return value

    # The string in the JSON argument contains an escaped character on the wire.
    model = ScriptedModel(
        [
            [function_call("lookup", r'{"value":"private-\u0065xample"}', call_id="call")],
            [assistant_message(SECRET)],
        ]
    )
    protected = Agent(name="protected", model=model, tools=[lookup])
    ordinary = Agent(name="ordinary", model=ScriptedModel([[assistant_message(SECRET)]]))
    config = RunConfig(redact_text=scrub, tracing={"operation_detail": "content"})
    first, second = await asyncio.gather(
        Runner.run(protected, SECRET, run_config=config), Runner.run(ordinary, SECRET)
    )
    assert first.final_output == "[removed]"
    assert second.final_output == SECRET
    assert config.tracing == {"operation_detail": "content"}
    assert not any(record.msg == "private %s" for record in caplog.records)
    logging.getLogger("openai.agents").debug("visible after run")
    assert "visible after run" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "invalid", "unstable"])
async def test_broken_policy_never_publishes_the_item_or_its_exception(failure):
    def broken(value):
        if SECRET not in value:
            return value
        if failure == "raises":
            raise ValueError(SECRET)
        return None if failure == "invalid" else value + "!"

    agent = Agent(name="worker", model=ScriptedModel([[assistant_message(SECRET)]]))
    result = Runner.run_streamed(agent, "question", run_config=RunConfig(redact_text=broken))
    with pytest.raises(UserError, match="redact_text") as error:
        async for event in result.stream_events():
            assert event.type == "agent_updated_stream_event"
    assert SECRET not in str(error.value)
    assert error.value.__context__ is error.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        [assistant_message("private-"), assistant_message("example", item_id="next")],
        [assistant_message("safe", item_id=SECRET)],
        [function_call("lookup", r'{"value":"private-\u0065xample","value":"safe"}', call_id="a")],
    ],
    ids=["joined-messages", "identifier", "ambiguous-json"],
)
async def test_refuses_uneditable_items_before_streaming(reply):
    agent = Agent(name="worker", model=ScriptedModel([reply]))
    result = Runner.run_streamed(agent, "question", run_config=RunConfig(redact_text=scrub))
    with pytest.raises(UserError, match="redact_text"):
        async for event in result.stream_events():
            assert event.type == "agent_updated_stream_event"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_hosted_approval_never_shows_different_arguments_from_the_server(streamed):
    from openai.types.responses.response_output_item import McpApprovalRequest

    from agents import HostedMCPTool

    from .model_test_helpers import get_exact_output_stream_step

    approvals = []

    async def approve(request):
        approvals.append(request)
        return {"approve": True}

    hosted = HostedMCPTool(
        tool_config={"type": "mcp", "server_label": "files", "server_url": "https://example.org"},
        on_approval_request=approve,
    )
    proposal = McpApprovalRequest(
        id="permission",
        type="mcp_approval_request",
        server_label="files",
        name="write",
        arguments=json.dumps({"value": SECRET}),
    )
    step = get_exact_output_stream_step([proposal]) if streamed else [proposal]
    model = ScriptedModel([step, [assistant_message("done")]])
    agent = Agent(name="worker", model=model, tools=[hosted])
    config = RunConfig(redact_text=scrub)
    with pytest.raises(UserError, match="redact_text"):
        if streamed:
            result = Runner.run_streamed(
                agent, "go", run_config=config, auto_previous_response_id=True
            )
            async for event in result.stream_events():
                assert event.type == "agent_updated_stream_event"
        else:
            await Runner.run(agent, "go", run_config=config, auto_previous_response_id=True)
    assert not approvals
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_never_rewrites_or_publishes_a_private_native_approval(tmp_path, monkeypatch):
    agent, store, session, config, pending, calls, _ = await setup_native(tmp_path, monkeypatch)
    pending.checkpoint["request"]["reason"] = SECRET
    config.redact_text = scrub
    with pytest.raises(UserError, match="redact_text"):
        await Runner.run(agent, "question", session=session, run_config=config)
    assert not calls
    assert SECRET not in store.records[session.session_id]
    assert not session.is_paused


@pytest.mark.asyncio
async def test_restored_native_output_is_scrubbed_without_changing_approval(tmp_path, monkeypatch):
    agent, store, session, config, pending, calls, requests = await setup_native(
        tmp_path, monkeypatch
    )
    config.redact_text = lambda text: text.replace("native accepted", "[removed]")
    await Runner.run(agent, "question", session=session, run_config=config)
    restored = await ResumableSession.resume(store, session.session_id)
    state = await restored.load_state(agent)
    state.approve(state.get_interruptions()[0])
    result = await Runner.run(agent, state, run_config=config)
    assert calls == [pending.checkpoint]
    assert result.final_output == "finished"
    assert "[removed]" in json.dumps(requests[-1])
    assert "native accepted" not in store.records[session.session_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("field", ["name", "description", "schema", "preloaded", "error", "safe"])
async def test_mcp_discovery_checks_definitions_before_export_or_dispatch(
    streamed, field, monkeypatch
):
    from agents.mcp import MCPUtil
    from tests.mcp.helpers import FakeMCPServer
    from tests.mcp.model_compat import Tool
    from tests.testing_processor import fetch_ordered_spans

    definition = Tool(
        name=SECRET if field == "name" else "lookup",
        description=SECRET if field in ("description", "preloaded") else "Look up a record",
        inputSchema={"type": "object", "properties": {SECRET: {"type": "string"}}}
        if field == "schema"
        else {"type": "object"},
    )
    model = ScriptedModel([[assistant_message("ok")]])
    server = FakeMCPServer(tools=[definition])
    if field == "error":

        async def fail(*args):
            raise RuntimeError(SECRET)

        monkeypatch.setattr(server, "list_tools", fail)
    agent = Agent(name="worker", model=model, mcp_servers=[server])
    if field == "preloaded":
        agent = agent.clone(
            tools=[MCPUtil.to_function_tool(definition, server, False)], mcp_servers=[]
        )
    config = RunConfig(redact_text=scrub)

    async def run():
        if streamed:
            result = Runner.run_streamed(agent, "go", run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go", run_config=config)

    if field == "safe":
        await run()
        assert len(model.calls) == 1
    else:
        with pytest.raises(UserError, match="redact_text") as caught:
            await run()
        assert SECRET not in str(caught.value)
        assert caught.value.__context__ is None
        assert not model.calls
    spans = [span.export() for span in fetch_ordered_spans()]
    assert spans or field == "preloaded"
    assert SECRET not in repr(spans)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_protected_restore_defers_mcp_discovery_until_policy_is_available(changed):
    from agents import RunState, trace
    from tests.mcp.helpers import FakeMCPServer
    from tests.testing_processor import fetch_ordered_spans

    config = RunConfig(redact_text=scrub)
    server = FakeMCPServer(require_approval="always")
    server.add_tool("lookup", {})
    model = ScriptedModel([[function_call("lookup", {}, call_id="id")], [assistant_message("ok")]])
    agent = Agent(name="worker", model=model, mcp_servers=[server])
    paused = await Runner.run(agent, "go", run_config=config)
    saved = paused.to_state().to_json()
    if changed:
        server.add_tool(SECRET, {})
    with trace("restore"):
        restored = await RunState.from_json(agent, saved)
    assert SECRET not in repr([span.export() for span in fetch_ordered_spans()])
    restored.approve(restored.get_interruptions()[0])
    if changed:
        with pytest.raises(UserError, match="redact_text"):
            await Runner.run(agent, restored, run_config=config)
        assert not server.tool_calls
    else:
        result = await Runner.run(agent, restored, run_config=config)
        assert result.final_output == "ok"
        assert server.tool_calls == ["lookup"]


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("field", ["model", "usage"])
async def test_untrusted_endpoint_metadata_stays_out_of_automatic_spans(field, monkeypatch):
    from agents.models.openai_responses import OpenAIResponsesModel
    from tests.model_test_helpers import get_response_obj
    from tests.testing_processor import fetch_ordered_spans

    response = get_response_obj([assistant_message("answer")])
    if field == "model":
        response.model = SECRET
    else:
        assert response.usage is not None
        response.usage.input_tokens_details.gateway_note = SECRET

    async def fetch(*args, **kwargs):
        return response

    model = OpenAIResponsesModel(model="fixture", openai_client=None)
    monkeypatch.setattr(model, "_fetch_response", fetch)
    agent = Agent(name="worker", model=model)
    with pytest.raises(UserError, match="redact_text"):
        await Runner.run(agent, "question", run_config=RunConfig(redact_text=scrub))
    spans = [span for span in fetch_ordered_spans() if span.span_data.type == "response"]
    assert spans
    for span in spans:
        assert SECRET not in repr(span.span_data.usage)
        assert SECRET not in repr(span.span_data.operation)


@pytest.mark.asyncio
async def test_streamed_cleanup_respects_the_policy(caplog, monkeypatch):
    from agents import _debug
    from agents.models.multi_provider import MultiProvider

    closed = []

    async def fail_close(self):
        closed.append(True)
        raise RuntimeError(SECRET)

    monkeypatch.setattr(MultiProvider, "aclose", fail_close)
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    agent = Agent(name="worker", model=ScriptedModel([[assistant_message("safe")]]))
    result = Runner.run_streamed(agent, "go", run_config={"redact_text": scrub})
    async for _ in result.stream_events():
        pass
    assert closed == [True]
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_streamed_sandbox_cleanup_respects_the_policy(caplog, monkeypatch):
    from agents import _debug
    from agents.sandbox import SandboxAgent, SandboxRunConfig

    from .sandbox._filesystem_test_session import (
        FilesystemTestSandboxClient,
        FilesystemTestSandboxSession,
    )

    stops = []

    async def fail_stop(session):
        stops.append(True)
        raise RuntimeError(SECRET)

    monkeypatch.setattr(FilesystemTestSandboxSession, "stop", fail_stop)
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    config = RunConfig(
        redact_text=scrub, sandbox=SandboxRunConfig(client=FilesystemTestSandboxClient())
    )
    agent = SandboxAgent(name="worker", model=ScriptedModel([[assistant_message("safe")]]))
    result = Runner.run_streamed(agent, "go", run_config=config)
    async for _ in result.stream_events():
        pass
    assert result.final_output == "safe"
    assert stops == [True]
    assert SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_policy_cancellation_does_not_disclose_private_input(streamed):
    import traceback

    def cancel(text):
        if text == SECRET:
            raise asyncio.CancelledError(text)
        return text

    agent = Agent(name="worker", model=ScriptedModel([]))
    config = RunConfig(redact_text=cancel)
    with pytest.raises(asyncio.CancelledError) as caught:
        if streamed:
            Runner.run_streamed(agent, SECRET, run_config=config)
        else:
            await Runner.run(agent, SECRET, run_config=config)
    assert not caught.value.args
    assert caught.value.__context__ is caught.value.__cause__ is None
    for frame, _ in traceback.walk_tb(caught.tb):
        if frame.f_globals.get("__name__", "").startswith("agents."):
            assert SECRET not in repr(frame.f_locals)


@pytest.mark.asyncio
async def test_context_serializer_cancellation_keeps_private_context_out_of_diagnostics():
    def serialize(context):
        raise asyncio.CancelledError(context) from ValueError(context)

    agent = Agent(name="worker", model=ScriptedModel([[assistant_message("ok")]]))
    result = await Runner.run(agent, "go", context=SECRET, run_config=RunConfig(redact_text=scrub))
    with pytest.raises(asyncio.CancelledError) as caught:
        result.to_state().to_json(context_serializer=serialize)
    assert not caught.value.args
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_provider_cancellation_keeps_response_diagnostics_private(monkeypatch):
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError(SECRET) from ValueError(SECRET)

    model = ScriptedModel([])
    monkeypatch.setattr(model, "get_response", cancel)
    with pytest.raises(asyncio.CancelledError) as caught:
        await Runner.run(
            Agent(name="worker", model=model), "go", run_config=RunConfig(redact_text=scrub)
        )
    assert not caught.value.args
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_stream_start_failure_releases_rejected_prompt():
    import traceback

    def broken(text):
        if text == SECRET:
            raise ValueError(text)
        return text

    agent = Agent(name="worker", model=ScriptedModel([]))
    with pytest.raises(UserError, match="redact_text") as caught:
        Runner.run_streamed(agent, SECRET, run_config=RunConfig(redact_text=broken))
    for frame, _ in traceback.walk_tb(caught.tb):
        if frame.f_globals.get("__name__", "").startswith("agents."):
            assert SECRET not in repr(frame.f_locals)


@pytest.mark.asyncio
async def test_joined_refusals_are_checked_before_refusal_handling():
    from openai.types.responses import ResponseOutputRefusal

    message = assistant_message("")
    message.content = [
        ResponseOutputRefusal(type="refusal", refusal=part) for part in ("private-", "example")
    ]
    agent = Agent(name="worker", model=ScriptedModel([[message]]))
    with pytest.raises(UserError, match="redact_text"):
        await Runner.run(agent, "go", run_config=RunConfig(redact_text=scrub))


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "source",
    [
        "prompt",
        "prompt_json",
        "tool",
        "mixed",
        "strings",
        "nested_strings",
        "mapping",
        "nested_mapping",
        "keys",
        "key_value",
        "tagged",
        "column",
        "encoded_column",
        "encoded_keys",
    ],
)
async def test_split_structured_text_never_reaches_model_session_or_events(streamed, source):
    from agents import SQLiteSession, ToolOutputText

    parts = ["private-", "example"]

    @tool
    async def lookup():
        if source == "strings":
            return parts
        if source == "nested_strings":
            return [[parts[0]], [parts[1]]]
        if source == "mapping":
            return {"left": parts[0], "right": parts[1]}
        if source == "nested_mapping":
            return [{"side": {"left": parts[0]}}, {"right": [parts[1]]}]
        if source == "keys":
            return dict.fromkeys(parts)
        if source == "key_value":
            return {parts[0]: parts[1]}
        if source == "tagged":
            return [{"type": "row", "value": part} for part in parts]
        if source == "column":
            return [{"label": "chunk", "value": part} for part in parts]
        if source == "encoded_column":
            return [json.dumps({"value": json.dumps(part)}) for part in parts]
        if source == "encoded_keys":
            return [json.dumps({part: None}) for part in parts]
        if source == "mixed":
            return [
                ToolOutputText(text=parts[0]),
                {"type": "image", "image_url": "https://example.org/figure.png"},
                ToolOutputText(text=parts[1]),
            ]
        return [ToolOutputText(text=part) for part in parts]

    text = [json.dumps({"v": p}) for p in parts] if source == "prompt_json" else parts
    prompt = [{"role": "user", "content": [{"type": "input_text", "text": p} for p in text]}]
    is_prompt = source in ("prompt", "prompt_json")
    model = ScriptedModel([[function_call("lookup", {}, call_id="id")], [assistant_message("ok")]])
    agent = Agent(name="worker", model=model, tools=[lookup])
    session = SQLiteSession("test")
    config = RunConfig(redact_text=scrub)
    try:
        with pytest.raises(UserError):
            if streamed:
                result = Runner.run_streamed(
                    agent,
                    prompt if is_prompt else "go",
                    run_config=config,
                    session=session,
                )
                async for event in result.stream_events():
                    assert getattr(event, "name", None) != "tool_output"
            else:
                await Runner.run(
                    agent,
                    prompt if is_prompt else "go",
                    run_config=config,
                    session=session,
                )
        assert len(model.calls) == (0 if is_prompt else 1)
        assert parts[0] not in repr(await session.get_items())
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    ("source", "history"),
    [
        (source, history)
        for source in ("model", "tool", "resume", "mixed", "user", "arguments")
        for history in ("session", "auto_previous_response_id", "local")
    ]
    + [("next_prompt", "session")],
)
async def test_later_turn_cannot_complete_secret_in_history_or_events(streamed, source, history):
    from agents import RunState, SQLiteSession

    executed = []

    @tool(needs_approval=source == "resume")
    def lookup(i: int, query: str = "") -> str:
        executed.append(i)
        if source == "tool":
            return "private-" if not i else "example"
        if source in {"mixed", "user", "arguments"}:
            return "example"
        return "ok"

    first = function_call("lookup", {"i": 0}, call_id="one")
    if source == "tool":
        script = [
            [first],
            [function_call("lookup", {"i": 1}, call_id="two")],
            [assistant_message("ok")],
        ]
    elif source == "next_prompt":
        script = [[assistant_message("private-")], [assistant_message("example")]]
    elif source == "mixed":
        script = [[assistant_message("private-"), first], [assistant_message("ok")]]
    elif source in {"user", "arguments"}:
        if source == "arguments":
            first = function_call("lookup", {"i": 0, "query": "private-"}, call_id="one")
        script = [[first], [assistant_message("ok")]]
    elif source == "model":
        script = [
            [assistant_message("private-"), first],
            [assistant_message("example"), function_call("lookup", {"i": 1}, call_id="two")],
        ]
    else:
        script = [[assistant_message("private-"), first], [assistant_message("example")]]
    model = ScriptedModel(script)
    agent = Agent(name="worker", model=model, tools=[lookup])
    session = SQLiteSession("test") if history == "session" else None
    options = {} if history == "local" else {history: session or True}
    config = RunConfig(redact_text=scrub)
    published = []

    async def invoke(value):
        if streamed:
            result = Runner.run_streamed(agent, value, run_config=config, **options)
            async for event in result.stream_events():
                if event.type == "run_item_stream_event":
                    published.append(str(event.item.raw_item))
            return result
        return await Runner.run(agent, value, run_config=config, **options)

    try:
        value: str | RunState = "private-" if source == "user" else "go"
        if source == "resume":
            paused = await invoke(value)
            value = await RunState.from_json(agent, paused.to_state().to_json())
            value.approve(value.get_interruptions()[0])
        elif source == "next_prompt":
            await invoke(value)
            value = "continue"
        with pytest.raises(UserError, match="redact_text"):
            await invoke(value)
        assert len(model.calls) == (1 if source in {"mixed", "user", "arguments"} else 2)
        if source == "model":
            assert executed == [0]
        assert "example" not in str(published)
        if session is not None:
            assert "example" not in str(await session.get_items())
    finally:
        if session is not None:
            session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("key", ["conversation_id", "previous_response_id"])
async def test_private_continuation_rejects_unavailable_history(streamed, restored, key):
    from agents import RunState

    executions = []

    @tool(needs_approval=True)
    def lookup() -> str:
        executions.append(True)
        return "example"

    first = (
        [function_call("lookup", {}, call_id="one")]
        if restored
        else [assistant_message("private-")]
    )
    model = ScriptedModel([first, [assistant_message("example")]])
    agent = Agent(name="worker", model=model, tools=[lookup])
    options = {key: "remote-id"}
    result = await Runner.run(agent, "go", **options)
    value: str | RunState = "continue"
    if restored:
        value = await RunState.from_json(agent, result.to_state().to_json())
        value.approve(value.get_interruptions()[0])
        options = {}
    elif key == "previous_response_id":
        options[key] = result.last_response_id
    config = RunConfig(redact_text=scrub)
    with pytest.raises(UserError, match="local history"):
        if streamed:
            async for _ in Runner.run_streamed(
                agent, value, run_config=config, **options
            ).stream_events():
                pytest.fail("Continuation must reject before publishing events.")
        else:
            await Runner.run(agent, value, run_config=config, **options)
    assert not executions
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_checks_tool_output_after_schema_serialization():
    from pydantic import BaseModel, field_serializer

    class Result(BaseModel):
        value: str

        @field_serializer("value")
        def serialize(self, value: str) -> str:
            return "private-" + value

    @tool(output_type=Result)
    async def lookup() -> Result:
        return Result(value="example")

    agent = Agent(
        name="worker",
        tools=[lookup],
        tool_use_behavior="stop_on_first_tool",
        model=ScriptedModel([[function_call("lookup", {}, call_id="id")]]),
    )
    with pytest.raises(UserError, match="redact_text"):
        await Runner.run(agent, "go", run_config=RunConfig(redact_text=scrub))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["tool", "hook", "extractor", "custom_data"])
async def test_tool_errors_do_not_disclose_rejected_payload(monkeypatch, failure):
    from agents import ToolExecutionConfig, _debug

    def extract(call):
        if failure == "extractor":
            raise ValueError(SECRET)
        return {"value": SECRET, "unsupported": {1}}

    @tool(failure_error_function=None, custom_data_extractor=extract)
    async def lookup() -> str:
        if failure == "tool":
            raise ValueError(SECRET)
        return SECRET

    async def fail(call, result):
        if failure == "hook":
            raise RuntimeError(result)
        return result

    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    config = RunConfig(
        redact_text=scrub, tool_execution=ToolExecutionConfig(prepare_tool_output=fail)
    )
    agent = Agent(
        name="worker",
        tools=[lookup],
        model=ScriptedModel([[function_call("lookup", {}, call_id="id")]]),
    )
    with pytest.raises(UserError) as caught:
        await Runner.run(agent, "go", run_config=config)
    assert SECRET not in str(caught.value)
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("kind", ["local_shell_error", "local_shell_cancel", "custom_cancel"])
async def test_executor_failures_keep_diagnostics_private(streamed, kind):
    from openai.types.responses import ResponseCustomToolCall
    from openai.types.responses.response_output_item import LocalShellCall, LocalShellCallAction

    from agents import CustomTool, LocalShellTool

    from .model_test_helpers import get_exact_output_stream_step

    cancelled = kind.endswith("_cancel")

    async def fail(*args):
        if cancelled:
            raise asyncio.CancelledError(SECRET) from ValueError(SECRET)
        raise RuntimeError(SECRET)

    if kind == "custom_cancel":
        executor = CustomTool(
            name="edit", description="Edit", on_invoke_tool=fail, format={"type": "text"}
        )
        call = ResponseCustomToolCall(
            type="custom_tool_call", name="edit", call_id="c", input="safe"
        )
    else:
        executor = LocalShellTool(executor=fail)
        call = LocalShellCall(
            id="shell",
            action=LocalShellCallAction(command=["echo", "safe"], env={}, type="exec"),
            call_id="call_shell",
            status="completed",
            type="local_shell_call",
        )
    step = get_exact_output_stream_step([call]) if streamed else [call]
    agent = Agent(name="worker", model=ScriptedModel([step]), tools=[executor])
    config = RunConfig(redact_text=scrub)
    with pytest.raises(asyncio.CancelledError if cancelled else UserError) as caught:
        if streamed:
            async for _ in Runner.run_streamed(agent, "go", run_config=config).stream_events():
                pass
        else:
            await Runner.run(agent, "go", run_config=config)
    assert SECRET not in str(caught.value)
    assert caught.value.__context__ is caught.value.__cause__ is None
    if cancelled:
        assert not caught.value.args
    else:
        assert "Tool execution failed" in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["call", "stream", "event", "eof"])
async def test_provider_errors_do_not_echo_private_response_bodies(source):
    from openai.types.responses import ResponseCreatedEvent, ResponseErrorEvent

    from agents import ModelBehaviorError
    from agents.testing import ModelStep
    from tests.model_test_helpers import get_response_obj

    if source == "event":
        event = ResponseErrorEvent(
            type="error", code="server_error", message=SECRET, param=None, sequence_number=0
        )
        model = ScriptedModel([ModelStep.stream([event])])
    elif source == "eof":
        response = get_response_obj([])
        response.id = SECRET
        created = ResponseCreatedEvent(
            type="response.created", response=response, sequence_number=0
        )
        model = ScriptedModel([ModelStep.stream([created])])
    else:
        error = ModelBehaviorError(SECRET)
        error.__cause__ = ValueError(SECRET)
        model = ScriptedModel([error])
    agent = Agent(name="worker", model=model)
    config = RunConfig(redact_text=scrub)
    with pytest.raises(ModelBehaviorError) as caught:
        if source != "call":
            async for _ in Runner.run_streamed(agent, "go", run_config=config).stream_events():
                pass
        else:
            await Runner.run(agent, "go", run_config=config)
    assert SECRET not in str(caught.value)
    assert caught.value.__context__ is caught.value.__cause__ is None
