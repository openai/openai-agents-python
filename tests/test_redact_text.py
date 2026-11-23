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


def scrub(value: str) -> str:
    return value.replace(SECRET, "[removed]")


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
    from agents.sandbox import Manifest, SandboxAgent

    from .sandbox.test_runtime import _FakeClient, _FakeSession, _sandbox_run_config

    class FailingSession(_FakeSession):
        async def stop(self):
            await super().stop()
            raise RuntimeError(SECRET)

    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    session = FailingSession(Manifest())
    config = _sandbox_run_config(_FakeClient(session))
    config.redact_text = scrub
    agent = SandboxAgent(name="worker", model=ScriptedModel([[assistant_message("safe")]]))
    result = Runner.run_streamed(agent, "go", run_config=config)
    async for _ in result.stream_events():
        pass
    assert result.final_output == "safe"
    assert session.stop_calls == 1
    assert SECRET not in caplog.text


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
async def test_local_shell_failure_keeps_executor_diagnostics_private(streamed):
    from openai.types.responses.response_output_item import LocalShellCall, LocalShellCallAction

    from agents import LocalShellTool

    from .model_test_helpers import get_exact_output_stream_step

    async def fail(request):
        raise RuntimeError(SECRET)

    call = LocalShellCall(
        id="shell",
        action=LocalShellCallAction(command=["echo", "safe"], env={}, type="exec"),
        call_id="call_shell",
        status="completed",
        type="local_shell_call",
    )
    step = get_exact_output_stream_step([call]) if streamed else [call]
    agent = Agent(name="worker", model=ScriptedModel([step]), tools=[LocalShellTool(executor=fail)])
    config = RunConfig(redact_text=scrub)
    with pytest.raises(UserError, match="Tool execution failed") as caught:
        if streamed:
            async for _ in Runner.run_streamed(agent, "go", run_config=config).stream_events():
                pass
        else:
            await Runner.run(agent, "go", run_config=config)
    assert SECRET not in str(caught.value)
    assert caught.value.__context__ is caught.value.__cause__ is None


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
