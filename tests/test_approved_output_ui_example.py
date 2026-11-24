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
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from examples.basic.approved_output_ui import View, display

from .model_test_helpers import get_response_obj


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_ui_withholds_secret_split_across_raw_deltas():
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


@pytest.mark.asyncio
async def test_ui_publishes_only_approved_complete_final():
    view = await display(
        Agent(name="demo", model=ScriptedModel([[assistant_message("public answer")]])),
        "Synthetic prompt",
        allow_final=lambda text: text == "public answer",
    )
    assert view.output == "public answer" and view.states[-1] == "complete"


@pytest.mark.asyncio
async def test_ui_rejected_final_and_sensitive_model_error():
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
    for view in [rejected, failed]:
        assert view.states[-1] == "failed" and view.output is None
        assert "secret" not in repr(view)


@pytest.mark.asyncio
async def test_ui_reports_approval_pause_without_tool_execution():
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


@pytest.mark.asyncio
async def test_ui_cancellation_joins_producer_and_keeps_status():
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


@pytest.mark.asyncio
async def test_trace_summary_distinguishes_requests_attempts_owner_parent_and_terminal():
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
    view = await display(
        Agent(name="demo", model=model, tools=[lookup]),
        "Synthetic prompt",
        allow_final=lambda text: text == "public answer",
        run_config=RunConfig(
            model_settings=ModelSettings(
                retry=ModelRetrySettings(
                    max_retries=1, backoff={"initial_delay": 0}, policy=lambda ctx: True
                )
            )
        ),
    )
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
