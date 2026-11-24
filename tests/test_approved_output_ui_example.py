import asyncio

import pytest
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseTextDeltaEvent,
)

from agents import Agent, GuardrailFunctionOutput, RunConfig
from agents.decorators import output_guardrail, tool
from agents.models.multi_provider import MultiProvider
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from examples.basic.approved_output_ui import View, display

from .model_test_helpers import get_response_obj


@pytest.fixture
def closed_providers(monkeypatch):
    closed = []
    close = MultiProvider.aclose

    async def record_close(provider):
        await close(provider)
        closed.append(provider)

    monkeypatch.setattr(MultiProvider, "aclose", record_close)
    return closed


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_ui_withholds_secret_split_across_raw_deltas(closed_providers):
    item = assistant_message("private-marker")
    events = [
        ResponseTextDeltaEvent(
            type="response.output_text.delta",
            item_id=item.id,
            output_index=0,
            content_index=0,
            delta=part,
            sequence_number=i,
            logprobs=[],
        )
        for i, part in enumerate(["pri", "vate-", "marker"])
    ]
    events.insert(
        0,
        ResponseCreatedEvent(
            type="response.created", response=get_response_obj([]), sequence_number=0
        ),
    )
    events.insert(
        1,
        ResponseOutputItemAddedEvent(
            type="response.output_item.added", item=item, output_index=0, sequence_number=0
        ),
    )
    events.extend(
        [
            ResponseOutputItemDoneEvent(
                type="response.output_item.done", item=item, output_index=0, sequence_number=3
            ),
            ResponseCompletedEvent(
                type="response.completed", response=get_response_obj([item]), sequence_number=4
            ),
        ]
    )
    events = [event.model_copy(update={"sequence_number": i}) for i, event in enumerate(events)]
    view = await display(
        Agent(name="demo", model=ScriptedModel([ModelStep.stream(events)])),
        "Synthetic prompt",
        allow_final=lambda text: False,
    )
    assert view.states == ["running", "producer_complete", "rejected"]
    assert "private-marker" not in repr(view)
    assert view.output is None and view.native_transport_attempts is None
    assert len(closed_providers) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer", ["public answer", "é" * 32769], ids=["short", "over-preview-limit"]
)
async def test_ui_publishes_only_approved_complete_final(answer, closed_providers):
    closed_at_approval = []

    def allow_final(text):
        closed_at_approval.append(len(closed_providers))
        return text == answer

    view = await display(
        Agent(name="demo", model=ScriptedModel([[assistant_message(answer)]])),
        "Synthetic prompt",
        allow_final=allow_final,
    )
    assert closed_at_approval == [1]
    assert view.output == answer
    assert view.states == ["running", "producer_complete", "complete"]
    assert len(closed_providers) == 1


@pytest.mark.asyncio
async def test_ui_rejected_final_and_sensitive_model_error(closed_providers):
    @output_guardrail
    def refuse(context, agent, output):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    rejected = await display(
        Agent(
            name="demo",
            model=ScriptedModel([[assistant_message("secret candidate")]]),
            output_guardrails=[refuse],
        ),
        "Synthetic prompt",
        allow_final=lambda text: True,
    )
    failed = await display(
        Agent(
            name="demo",
            model=ScriptedModel([ModelStep.raise_error(RuntimeError("credential-secret"))]),
        ),
        "Synthetic prompt",
        allow_final=lambda text: True,
    )
    assert len(closed_providers) == 2
    for view in [rejected, failed]:
        assert view.states[-1] == "failed" and view.output is None
        assert "secret" not in repr(view)


@pytest.mark.asyncio
async def test_ui_reports_approval_pause_without_tool_execution(closed_providers):
    writes = []

    @tool(needs_approval=True)
    def write() -> str:
        """Write a synthetic record."""
        writes.append("write")
        return "written"

    view = await display(
        Agent(
            name="demo",
            tools=[write],
            model=ScriptedModel([[function_call("write", {}, call_id="write-one")]]),
        ),
        "Synthetic prompt",
        allow_final=lambda text: True,
    )
    assert view.states[-1] == "paused" and view.output is None and writes == []
    assert len(closed_providers) == 1


@pytest.mark.asyncio
async def test_ui_cancellation_joins_producer_and_keeps_status(closed_providers):
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed(call):
        entered.set()
        try:
            await release.wait()
            return [assistant_message("never displayed")]
        finally:
            cleaned.set()

    view = View()
    pending = asyncio.create_task(
        display(
            Agent(name="demo", model=ScriptedModel([ModelStep.respond(delayed)])),
            "Synthetic prompt",
            allow_final=lambda text: True,
            view=view,
        )
    )
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert view.states[-1] == "cancelled" and view.output is None
    assert cleaned.is_set()
    assert len(closed_providers) == 1


@pytest.mark.asyncio
async def test_trace_summary_distinguishes_requests_attempts_owner_parent_and_terminal(
    closed_providers,
):
    from agents import ModelRetrySettings, ModelSettings
    from agents.tracing import set_trace_processors
    from examples.basic.approved_output_ui import MetadataCounter

    counter = MetadataCounter()
    set_trace_processors([counter])

    @tool
    def lookup() -> str:
        """Return a synthetic public tool result."""
        return "public tool result"

    model = ScriptedModel(
        [
            RuntimeError("private transient error"),
            [function_call("lookup", {}, call_id="public-call")],
            [assistant_message("public answer")],
        ],
        emit_traces=True,
    )
    config = RunConfig(
        model_settings=ModelSettings(
            retry=ModelRetrySettings(
                max_retries=1, backoff={"initial_delay": 0}, policy=lambda ctx: True
            )
        )
    )
    try:
        view = await display(
            Agent(name="demo", model=model, tools=[lookup]),
            "Synthetic prompt",
            allow_final=lambda text: text == "public answer",
            run_config=config,
        )
        assert closed_providers == []
    finally:
        await config.model_provider.aclose()
    assert closed_providers == [config.model_provider]
    rows = counter.observations
    models = [row for row in rows if row["kind"] == "model"]
    tools = [row for row in rows if row["kind"] == "tool"]
    assert view.outer_requests == 2 and len(model.calls) == 3
    assert models and all(row["observed_adapter_attempts"] is None for row in models)
    assert view.tool_owners == ["sdk"]
    assert tools and all(row["tool_owner"] == "unknown" for row in tools)
    assert all(row["parent"] is not None for row in models + tools)
    assert all(row["transport_attempts"] is None for row in rows)
    assert view.states[-1] == "complete"
    assert "private transient error" not in str(rows)


@pytest.fixture(autouse=True)
def restore_example_processors():
    from agents.tracing import set_trace_processors

    from .testing_processor import SPAN_PROCESSOR_TESTING

    yield
    set_trace_processors([SPAN_PROCESSOR_TESTING])


@pytest.mark.asyncio
async def test_ui_mcp_executor_is_external():
    from .mcp.helpers import FakeMCPServer

    server = FakeMCPServer()
    server.add_tool("remote_read", {"type": "object", "properties": {}})
    model = ScriptedModel(
        [
            [function_call("remote_read", {}, call_id="remote")],
            [assistant_message("public")],
        ]
    )
    view = await display(
        Agent(name="demo", model=model, mcp_servers=[server]),
        "Synthetic prompt",
        allow_final=lambda text: text == "public",
    )
    assert server.tool_calls == ["remote_read"]
    assert view.states[-1] == "complete" and view.output == "public"
    assert view.tool_owners == ["external"]


def test_trace_summary_excludes_hostile_names_metadata_payloads_and_errors():
    import json

    from agents.tracing import function_span, response_span, set_trace_processors, trace
    from examples.basic.approved_output_ui import MetadataCounter

    counter = MetadataCounter()
    set_trace_processors([counter])
    marker = "private-marker"
    with trace(marker, metadata={"payload": marker}):
        with response_span() as request:
            request.span_data.operation.api_type = "responses"
            request.span_data.operation.retry_timestamps.extend([marker, marker])
            with function_span(marker, input=marker, output=marker) as child:
                child.span_data.operation.outcome = "failed"
                child.set_error({"message": marker, "data": {"payload": marker}})
    rows = counter.observations
    assert marker not in json.dumps(rows)
    models = [row for row in rows if row["kind"] == "model"]
    tools = [row for row in rows if row["kind"] == "tool"]
    assert len(models) == len(tools) == 1
    assert tools[0]["parent"] == models[0]["id"]
    assert tools[0]["status"] == "failed" and tools[0]["tool_owner"] == "unknown"
    assert models[0]["observed_adapter_attempts"] == 3
    assert all(row["transport_attempts"] is None for row in rows)


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(asyncio, "eager_task_factory"), reason="Requires Python 3.12+")
@pytest.mark.parametrize("cancel_during_close", [False, True])
async def test_ui_joins_eager_run_when_subscription_fails(
    monkeypatch, closed_providers, cancel_during_close
):
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closing, allow_close = asyncio.Event(), asyncio.Event()
    close = MultiProvider.aclose

    async def slow_close(provider):
        closing.set()
        await allow_close.wait()
        await close(provider)

    monkeypatch.setattr(MultiProvider, "aclose", slow_close)

    async def respond(call):
        entered.set()
        try:
            await release.wait()
            return [assistant_message("private")]
        finally:
            settled.set()

    view = View()
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        task = asyncio.create_task(
            display(
                Agent(name="demo", model=ScriptedModel([ModelStep.respond(respond)])),
                "Synthetic prompt",
                allow_final=lambda text: pytest.fail("No output should reach approval"),
                view=view,
            )
        )
    finally:
        loop.set_task_factory(previous)
    try:
        assert not task.done()
        await asyncio.wait_for(closing.wait(), timeout=5)
        assert not entered.is_set() or settled.is_set()
        if cancel_during_close:
            task.cancel("first")
            await asyncio.sleep(0)
            task.cancel("second")
            await asyncio.sleep(0)
            assert not task.done()
        allow_close.set()
        if cancel_during_close:
            with pytest.raises(asyncio.CancelledError, match="first"):
                await task
            assert view.states == ["running", "cancelled"]
        else:
            await task
            assert view.states == ["running", "failed"]
        assert view.output is None
        assert len(closed_providers) == 1
    finally:
        release.set()
        allow_close.set()
        await asyncio.gather(task, return_exceptions=True)
