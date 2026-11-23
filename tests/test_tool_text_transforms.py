import asyncio
import json
import logging
from dataclasses import replace

import pytest
from mcp.types import CallToolResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, Field, TypeAdapter, field_validator

from agents import (
    Agent,
    RunConfig,
    RunHooks,
    Runner,
    RunState,
    SQLiteSession,
    ToolExecutionConfig,
    ToolGuardrailFunctionOutput,
    UserError,
    _debug,
    function_tool,
    tool_namespace,
    tool_output_guardrail,
)
from agents.extensions.tracing import OpenTelemetryTracingProcessor
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tracing import get_current_span, set_trace_processors

from .mcp.helpers import FakeMCPServer
from .testing_processor import SPAN_PROCESSOR_TESTING, fetch_ordered_spans

RAW = "synthetic-private-draft"
SAFE = "approved-preview"
IMAGE = {"type": "image", "mimeType": "image/png", "data": "AQID"}


@pytest.fixture
def exporter():
    provider = TracerProvider()
    recorder = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(recorder))
    processor = OpenTelemetryTracingProcessor(tracer_provider=provider)
    set_trace_processors([SPAN_PROCESSOR_TESTING, processor])
    try:
        yield recorder
    finally:
        set_trace_processors([SPAN_PROCESSOR_TESTING])
        processor.shutdown()
        provider.shutdown()


class Source(FakeMCPServer):
    def __init__(self, server_name="Reports", **kwargs):
        super().__init__(server_name=server_name, **kwargs)
        self.add_tool("read.report", {})
        self.use_structured_content = True
        self.result = {
            "content": [
                {"type": "text", "text": RAW + " fallback"},
                IMAGE,
                {"type": "text", "text": RAW + " suffix"},
            ],
            "structuredContent": {"draft": RAW},
            "_meta": {"private": RAW},
        }

    async def call_tool(self, tool_name, arguments, meta=None):
        self.tool_calls.append(tool_name)
        return CallToolResult.model_validate(self.result)


def model_for(name):
    return ScriptedModel([[function_call(name, {}, call_id="one")], [assistant_message("done")]])


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [None, "content"])
@pytest.mark.parametrize("route", ["function", "mcp"])
@pytest.mark.parametrize("outcome", ["edit", "throw", "cancel", "invalid"])
async def test_selected_output_before_publication(
    detail, route, outcome, exporter, caplog, monkeypatch
):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    caplog.set_level(logging.DEBUG, logger="openai.agents")
    observed = []
    edits = []
    effects = []

    @tool_output_guardrail
    def guard(data):
        observed.append(str(data.output))
        return ToolGuardrailFunctionOutput.allow()

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            observed.append(str(result))

    @function_tool(tool_output_guardrails=[guard])
    async def report():
        effects.append("local")
        return [
            {"type": "text", "text": RAW},
            {"type": "image", "image_url": "data:image/png;base64,AQID"},
            {"type": "file", "file_id": "example-file"},
        ]

    def extract(ctx):
        observed.append(str((ctx.tool_output, ctx.result_meta, ctx.structured_content)))
        return {"copy": observed[-1]}

    source = Source(custom_data_extractor=extract, tool_output_guardrails=[guard])
    local = route == "function"
    target = report if local else (source, "read.report")
    model = model_for("report" if local else "mcp_Reports__read_report")
    agent = Agent(
        name="reports",
        tools=[report],
        mcp_servers=[source],
        mcp_config={"include_server_in_tool_names": True},
        model=model,
    )

    async def edit(call, texts):
        span = get_current_span()
        assert span is not None
        edits.append((call.call_id, call.mcp_target, texts, str(span.export())))
        if outcome == "throw":
            raise ValueError(RAW)
        if outcome == "cancel":
            raise asyncio.CancelledError(RAW)
        if outcome == "invalid":
            return (RAW, 42)
        return tuple(SAFE if t else "" for t in texts)

    async def legacy(*args):
        pytest.fail("selected call reached the generic editor")

    config = RunConfig(
        tool_execution=ToolExecutionConfig(
            tool_text_transforms=[(target, edit)], prepare_tool_output=legacy
        ),
        tracing={"operation_detail": detail} if detail else {},
        trace_include_sensitive_data=True,
    )
    session = SQLiteSession("local")
    try:
        run = Runner.run_streamed(agent, "go", run_config=config, hooks=Hooks(), session=session)
        events = []

        async def collect():
            async for event in run.stream_events():
                if (
                    event.type == "run_item_stream_event"
                    and event.item.type == "tool_call_output_item"
                ):
                    events.append(event.item)

        if outcome == "edit":
            await collect()
            (item,) = events
            assert item.raw_item["call_id"] == "one"
            blocks = item.raw_item["output"]
            assert blocks[0] == {"type": "input_text", "text": SAFE}
            assert blocks[1] == {"type": "input_image", "image_url": "data:image/png;base64,AQID"}
            assert blocks[2] == (
                {"type": "input_file", "file_id": "example-file"}
                if local
                else {"type": "input_text", "text": ""}
            )
            assert len(observed) == (2 if local else 3)
            views = [
                *observed,
                str(item.output),
                str(model.calls[1].input),
                str(await session.get_items()),
                str(run.to_input_list()),
                run.to_state().to_string(),
            ]
            if not local:
                views.append(str(item.custom_data))
            assert all(SAFE in view and RAW not in view for view in views)
        else:
            with pytest.raises(asyncio.CancelledError if outcome == "cancel" else UserError) as exc:
                await collect()
            error: BaseException | None = exc.value
            seen = set()
            while error is not None:
                assert id(error) not in seen
                seen.add(id(error))
                assert error.__cause__ is None
                assert RAW not in str((error, vars(error)))
                if outcome == "cancel":
                    # Python 3.10 chains empty cancellations across task boundaries.
                    assert type(error) is asyncio.CancelledError and not error.args
                else:
                    assert error.__context__ is None
                error = error.__context__
            assert RAW not in str(run.to_input_list())
            assert RAW not in run.to_state().to_string()
            assert not observed and not events and len(model.calls) == 1
            assert RAW not in str(await session.get_items())
        assert effects + source.tool_calls == (["local"] if local else ["read.report"])
        [(call_id, mcp_target, slots, live)] = edits
        assert call_id == "one" and mcp_target == (None if local else target)
        assert slots == ((RAW,) if local else (json.dumps({"draft": RAW}), ""))
        spans = [span.export() for span in fetch_ordered_spans()]
        otel = [span.to_json() for span in exporter.get_finished_spans()]
        assert spans and otel
        assert RAW not in str((spans, otel, live, caplog.text))
        if outcome == "edit":
            assert SAFE in str(spans)
            if detail:
                assert SAFE in str(otel)
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [None, "content"])
@pytest.mark.parametrize("failure", ["peer", "transport", "function", "unsupported"])
async def test_selected_failures_never_become_success(
    failure, detail, exporter, monkeypatch, caplog
):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    caplog.set_level(logging.DEBUG, logger="openai.agents")

    unexpected = []

    def forbidden(*args):
        unexpected.append(args)

    @function_tool(failure_error_function=forbidden)
    async def report():
        raise ValueError(RAW)

    class Peer(Source):
        async def call_tool(self, tool_name, arguments, meta=None):
            if failure == "transport":
                raise RuntimeError(RAW)
            return await super().call_tool(tool_name, arguments, meta)

    source = Peer(custom_data_extractor=forbidden, failure_error_function=forbidden)
    if failure == "peer":
        source.result["isError"] = True
    if failure == "unsupported":
        source.result["content"].append({"type": "audio", "mimeType": "audio/wav", "data": "AQID"})
    local = failure == "function"
    model = model_for("report" if local else "read.report")
    agent = Agent(name="reports", tools=[report], mcp_servers=[source], model=model)
    config = RunConfig(
        tracing={"operation_detail": detail} if detail else {},
        tool_execution=ToolExecutionConfig(
            tool_text_transforms=[(report if local else (source, "read.report"), forbidden)],
            prepare_tool_output=forbidden,
        ),
    )
    with pytest.raises(UserError) as exc:
        await Runner.run(agent, "go", run_config=config)
    assert not unexpected
    if failure == "peer":
        assert "MCP tool reported an error" in str(exc.value)
    assert len(model.calls) == 1 and exc.value.__context__ is None and exc.value.__cause__ is None
    spans = [s.export() for s in fetch_ordered_spans()]
    otel = [s.to_json() for s in exporter.get_finished_spans()]
    assert spans and otel and RAW not in str((spans, otel, exc.value, caplog.text))


class Preview(BaseModel):
    text: str


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_typed_result_has_one_validated_value_for_caller_and_model(valid):
    @function_tool(output_type=Preview)
    def report():
        return Preview(text=RAW)

    async def edit(call, slots):
        assert json.loads(slots[0]) == {"text": RAW}
        return (json.dumps({"text": SAFE if valid else 7, "ignored": RAW}),)

    model = model_for("report")
    agent = Agent(name="reports", tools=[report], model=model)
    config = RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=[(report, edit)]))
    if not valid:
        with pytest.raises(UserError):
            await Runner.run(agent, "go", run_config=config)
        assert len(model.calls) == 1
        return
    result = await Runner.run(agent, "go", run_config=config)
    item = next(item for item in result.new_items if item.type == "tool_call_output_item")
    assert item.output == {"text": SAFE}
    assert SAFE in str(model.calls[1].input)
    assert RAW not in result.to_state().to_string()


@pytest.mark.asyncio
async def test_exact_selection_across_parallel_calls_and_approval_resume():
    completed = asyncio.Event()
    seen = []

    @function_tool(needs_approval=True)
    async def report(index: int):
        return RAW + str(index)

    async def prepare(call):
        return {"index": call.arguments["index"] + 1}

    async def edit(call, texts):
        index = call.arguments.pop("index")
        assert call.tool is report and texts == (RAW + str(index),)
        if index == 1:
            await asyncio.wait_for(completed.wait(), timeout=5)
        else:
            completed.set()
        seen.append((call.call_id, index))
        return (SAFE + str(index),)

    model = ScriptedModel(
        [
            [function_call("report", {"index": i}, call_id=str(i)) for i in range(2)],
            [assistant_message("done")],
        ]
    )
    agent = Agent(name="reports", tools=[report], model=model)
    config = RunConfig(
        tool_execution=ToolExecutionConfig(
            prepare_tool_input=prepare, tool_text_transforms=[(report, edit)]
        )
    )
    paused = await Runner.run(agent, "go", run_config=config)
    state = await RunState.from_string(agent, paused.to_state().to_string())
    for approval in state.get_interruptions():
        state.approve(approval)
    result = await Runner.run(agent, state, run_config=config)
    assert seen == [("1", 2), ("0", 1)]
    outputs = {
        i.raw_item["call_id"]: i.output
        for i in result.new_items
        if i.type == "tool_call_output_item"
    }
    assert outputs == {"0": SAFE + "1", "1": SAFE + "2"}
    assert RAW not in result.to_state().to_string()


@pytest.mark.asyncio
async def test_unselected_peer_with_same_tool_name_keeps_legacy_behavior():
    selected, other = Source("Reports.A"), Source("Reports_A")
    seen = []

    async def legacy(call, output):
        seen.append(output)
        return "legacy"

    async def edit(*args):
        pytest.fail("matched a different server")

    model = model_for("read.report")
    agent = Agent(name="reports", mcp_servers=[other], model=model)
    config = RunConfig(
        tool_execution=ToolExecutionConfig(
            tool_text_transforms=[((selected, "read.report"), edit)], prepare_tool_output=legacy
        )
    )
    await Runner.run(agent, "go", run_config=config)
    assert seen == [json.dumps({"draft": RAW})]
    assert "legacy" in str(model.calls[1].input)
    assert not selected.tool_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_selected_image_only_result_keeps_media_position(structured):
    peer = Source()
    peer.use_structured_content = structured
    peer.result["content"] = [IMAGE]
    slots = []

    async def edit(call, texts):
        slots.append(texts)
        return (SAFE,) if texts else ()

    model = model_for("read.report")
    agent = Agent(name="reports", mcp_servers=[peer], model=model)
    config = RunConfig(
        tool_execution=ToolExecutionConfig(tool_text_transforms=[((peer, "read.report"), edit)])
    )
    result = await Runner.run(agent, "go", run_config=config)
    item = next(item for item in result.new_items if item.type == "tool_call_output_item")
    assert slots == ([(json.dumps({"draft": RAW}),)] if structured else [()])
    assert item.raw_item["output"][0]["image_url"] == "data:image/png;base64,AQID"
    assert RAW not in result.to_state().to_string()


def test_invalid_or_duplicate_registration():
    peer = Source()

    async def edit(call, texts):
        return texts

    settings = TypeAdapter(ToolExecutionConfig).validate_python(
        {"tool_text_transforms": [((peer, "read.report"), edit)]}
    )
    assert settings.tool_text_transforms[0][0][0] is peer
    for entries in [[("read.report", edit)], [((peer, "read.report"), edit)] * 2]:
        with pytest.raises(ValueError, match="tool_text_transforms"):
            ToolExecutionConfig(tool_text_transforms=entries)


@pytest.mark.asyncio
async def test_cancelled_run_discards_a_late_edit(exporter):
    started, finished = asyncio.Event(), asyncio.Event()

    async def edit(call, slots):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            finished.set()
            return slots

    source = Source()
    model = model_for("read.report")
    agent = Agent(name="reports", model=model, mcp_servers=[source])
    config = RunConfig(
        tracing={"operation_detail": "content"},
        tool_execution=ToolExecutionConfig(tool_text_transforms=[((source, "read.report"), edit)]),
    )
    attempt = asyncio.create_task(Runner.run(agent, "go", run_config=config))
    await asyncio.wait_for(started.wait(), timeout=5)
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    await asyncio.wait_for(finished.wait(), timeout=5)
    assert source.tool_calls == ["read.report"] and len(model.calls) == 1
    assert RAW not in str([s.export() for s in fetch_ordered_spans()])
    assert RAW not in str([s.to_json() for s in exporter.get_finished_spans()])


@pytest.mark.asyncio
async def test_timeout_does_not_recover_a_selected_tool():
    unexpected = []

    def forbidden(*args):
        unexpected.append(args)

    @function_tool(timeout=0.01, timeout_error_function=forbidden)
    async def report():
        await asyncio.Event().wait()

    model = model_for("report")
    agent = Agent(name="reports", tools=[report], model=model)
    config = RunConfig(
        tool_execution=ToolExecutionConfig(tool_text_transforms=[(report, forbidden)])
    )
    with pytest.raises(UserError):
        await Runner.run(agent, "go", run_config=config)
    assert len(model.calls) == 1
    assert not unexpected


@pytest.mark.asyncio
async def test_namespace_copies_select_the_registered_function():
    @function_tool
    async def report():
        return "status"

    (selected,) = tool_namespace(name="internal", description="Reports", tools=[report])
    (other,) = tool_namespace(name="public", description="Reports", tools=[report])

    async def edit(call, texts):
        assert call.tool is selected and call.tool_namespace == "internal"
        return (SAFE,)

    model = ScriptedModel(
        [
            [function_call("report", {}, namespace=n, call_id=n) for n in ("internal", "public")],
            [assistant_message("done")],
        ]
    )
    agent = Agent(name="reports", tools=[selected, other], model=model)
    config = RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=[(selected, edit)]))
    result = await Runner.run(agent, "go", run_config=config)
    outputs = {
        i.raw_item["call_id"]: i.output
        for i in result.new_items
        if i.type == "tool_call_output_item"
    }
    assert outputs == {"internal": SAFE, "public": "status"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change, key", [(False, "amount"), (False, "cents"), (True, "amount")])
async def test_typed_edit_preserves_converted_values_or_stops(change, key):
    conversions = []

    class Price(BaseModel):
        amount: int = Field(serialization_alias=key)
        label: str
        internal: str = Field(default=RAW, exclude=True)

        @field_validator("amount")
        @classmethod
        def cents(cls, amount: int) -> int:
            conversions.append(amount)
            return amount * 100

    @function_tool(output_type=Price)
    def quote():
        return Price(amount=2, label="old")

    async def edit(call, texts):
        assert json.loads(texts[0]) == {key: 200, "label": "old"}
        return (texts[0].replace("old", "new"),) if change else texts

    model = model_for("quote")
    config = RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=[(quote, edit)]))
    agent = Agent(name="reports", model=model, tools=[quote])
    if change:
        with pytest.raises(UserError):
            await Runner.run(agent, "go", run_config=config)
        assert len(model.calls) == 1
        return
    result = await Runner.run(agent, "go", run_config=config)
    output = next(i for i in result.new_items if i.type == "tool_call_output_item")
    assert conversions == [2]
    assert output.output == json.loads(output.raw_item["output"]) == {key: 200, "label": "old"}
    assert RAW not in result.to_state().to_string()


@pytest.mark.asyncio
async def test_empty_mcp_object_withholds_fallback_text():
    server = Source()
    server.result["structuredContent"] = {}

    async def edit(call, texts):
        assert texts == ("{}", "")
        return texts

    model = model_for("read.report")
    config = RunConfig(
        tool_execution=ToolExecutionConfig(tool_text_transforms=[((server, "read.report"), edit)])
    )
    result = await Runner.run(
        Agent(name="reports", model=model, mcp_servers=[server]), "go", run_config=config
    )
    assert RAW not in result.to_state().to_string()
    assert IMAGE["data"] in str(model.calls[1].input)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("point", "fallback"),
    [
        ("response.output_item.done", True),
        ("response.completed", False),
        ("response.completed", True),
    ],
)
async def test_stream_views_cannot_change_selected_arguments(point, fallback):
    displayed = asyncio.Event()
    classified = asyncio.Event()

    class Paused(ScriptedModel):
        async def stream_response(self, *args, **kwargs):
            async for event in super().stream_response(*args, **kwargs):
                if event.type == "response.completed" and fallback:
                    event.response.output = []
                yield event
                if event.type == point:
                    await asyncio.wait_for(displayed.wait(), 5)

    @function_tool
    async def report(color: str):
        return color

    async def edit(call, texts):
        await asyncio.wait_for(classified.wait(), 5)
        assert call.arguments == {"color": "Blue"}
        return texts

    model = Paused([[function_call("report", {"color": "Blue"}, call_id="color")]])
    config = RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=[(report, edit)]))
    result = Runner.run_streamed(
        Agent(name="reports", model=model, tools=[report], tool_use_behavior="stop_on_first_tool"),
        "go",
        run_config=config,
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
        if event.type == "run_item_stream_event" and event.name == "tool_called":
            event.item.raw_item.arguments = '{"color":"green"}'
            classified.set()
    assert result.final_output == "Blue"


def _check_selected_exit(route):
    import traceback

    class Peer(Source):
        async def call_tool(self, tool_name, arguments, meta=None):
            if route == "peer":
                raise SystemExit(RAW)
            return await super().call_tool(tool_name, arguments, meta)

    @function_tool(timeout=5 if route == "timed" else None)
    async def report():
        if route == "timed":
            raise SystemExit(7)
        return RAW

    async def edit(call, texts):
        if route == "interrupt":
            raise KeyboardInterrupt(texts[0])
        raise SystemExit(texts[0])

    peer = Peer()
    remote = route.startswith("peer")
    model = model_for("read.report" if remote else "report")
    agent = Agent(name="reports", model=model, tools=[report], mcp_servers=[peer])
    config = RunConfig(
        tracing_disabled=True,
        tool_execution=ToolExecutionConfig(
            tool_text_transforms=[((peer, "read.report") if remote else report, edit)]
        ),
    )
    loop = asyncio.new_event_loop()
    task = loop.create_task(Runner.run(agent, "go", run_config=config))
    expected = KeyboardInterrupt if route == "interrupt" else SystemExit
    try:
        # Each awaiting task can propagate the exit to the loop before Runner finishes.
        while not task.done():
            with pytest.raises(expected) as exc:
                loop.run_until_complete(task)
            error = exc.value
            if isinstance(error, SystemExit):
                assert error.code == (7 if route == "timed" else 1)
            assert error.__cause__ is None and error.__context__ is None
            assert RAW not in str((error, vars(error)))
            for frame, _ in traceback.walk_tb(error.__traceback__):
                if frame.f_globals.get("__name__", "").startswith("agents."):
                    assert RAW not in str(frame.f_locals)
        assert isinstance(task.exception(), expected)
        assert len(model.calls) == 1
    finally:
        loop.close()


def test_selected_exit_is_redacted_before_reaching_the_event_loop():
    import subprocess
    import sys

    program = """
from tests.test_tool_text_transforms import _check_selected_exit
for route in ("edit", "timed", "peer", "peer_edit", "interrupt"):
    _check_selected_exit(route)
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert RAW not in result.stdout + result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["status", "structured"])
async def test_invalid_peer_result_stops_before_editing(invalid):
    from agents.mcp._compat import MCP_V2

    class Invalid(Source):
        async def call_tool(self, *args, **kwargs):
            result = await super().call_tool(*args, **kwargs)
            status = "is_error" if MCP_V2 else "isError"
            structured = "structured_content" if MCP_V2 else "structuredContent"
            return result.model_copy(
                update={status: 0} if invalid == "status" else {structured: []}
            )

    async def edit(call, texts):
        pytest.fail("invalid peer result reached the editor")

    server = Invalid()
    model = model_for("read.report")
    config = RunConfig(
        tool_execution=ToolExecutionConfig(tool_text_transforms=[((server, "read.report"), edit)])
    )
    with pytest.raises(UserError):
        await Runner.run(
            Agent(name="reports", model=model, mcp_servers=[server]), "go", run_config=config
        )
    assert len(model.calls) == len(server.tool_calls) == 1


@pytest.mark.asyncio
async def test_model_adapter_retaining_a_call_cannot_change_accepted_arguments():
    class Model(ScriptedModel):
        async def get_response(self, *args, **kwargs):
            response = await super().get_response(*args, **kwargs)
            self.saved = response.output[0]
            return response

    class Hooks(RunHooks):
        async def on_llm_end(self, context, agent, response):
            model.saved.arguments = '{"color":"blue"}'

    @function_tool
    async def report(color: str):
        return color

    async def edit(call, texts):
        assert call.arguments == {"color": "Blue"}
        return texts

    model = Model([[function_call("report", {"color": "Blue"}, call_id="color")]])
    config = RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=[(report, edit)]))
    result = await Runner.run(
        Agent(name="reports", model=model, tools=[report], tool_use_behavior="stop_on_first_tool"),
        "go",
        run_config=config,
        hooks=Hooks(),
    )
    assert result.final_output == "Blue"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("resume", [False, True])
async def test_second_wrapper_requires_exact_editor_before_effect(streamed, resume):
    effects = []

    def report():
        effects.append(True)
        return RAW

    async def edit(call, texts):
        return (SAFE,)

    selected = function_tool(report, needs_approval=resume)
    replacement = function_tool(report, needs_approval=resume)
    model = model_for("report")
    agent = Agent(name="reports", tools=[selected if resume else replacement], model=model)
    config = RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=[(selected, edit)]))
    request: str | RunState = "go"
    if resume:
        paused = await Runner.run(agent, request, run_config=config)
        snapshot = paused.to_state().to_string()
        agent.tools = [replacement]
        request = await RunState.from_string(agent, snapshot)
        request.approve(request.get_interruptions()[0])

    with pytest.raises(UserError, match="exact tool_text_transforms target"):
        if streamed:
            stream = Runner.run_streamed(agent, request, run_config=config)
            async for _ in stream.stream_events():
                pass
        else:
            await Runner.run(agent, request, run_config=config)
    assert effects == [] and len(model.calls) == 1


@pytest.mark.asyncio
async def test_same_name_on_distinct_agents_uses_only_exact_editors():
    effects = []

    def make_report(name="report"):
        @function_tool(name_override=name)
        def report():
            effects.append(name)
            return "original"

        return report

    first, second = make_report(), make_report()
    other = make_report("other")

    async def edit(call, texts):
        return (SAFE,)

    config = RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=[(first, edit)]))
    with pytest.raises(UserError, match="exact tool_text_transforms target"):
        await Runner.run(
            Agent(name="second", tools=[second], model=model_for("report")), "go", run_config=config
        )
    assert not effects
    config.tool_execution = ToolExecutionConfig(
        tool_text_transforms=[(first, edit), (second, edit)]
    )
    for tool, expected in [(first, SAFE), (second, SAFE), (other, "original")]:
        agent = Agent(name="reports", tools=[tool], model=model_for(tool.name))
        result = await Runner.run(agent, "go", run_config=config)
        assert (
            next(i.output for i in result.new_items if i.type == "tool_call_output_item")
            == expected
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("selection", ["stale", "missing", "current"])
async def test_pending_mcp_edit_requires_current_exact_selector(restored, selection):
    previous = Source(require_approval="always")
    current = Source(require_approval="always")

    async def edit(call, texts):
        assert call.mcp_target == (current, "read.report")
        return tuple(SAFE for _ in texts)

    def config(server):
        return RunConfig(
            tool_execution=ToolExecutionConfig(
                tool_text_transforms=[((server, "read.report"), edit)]
            )
        )

    model = model_for("read.report")
    agent = Agent(name="reports", mcp_servers=[previous], model=model)
    paused = await Runner.run(agent, "go", run_config=config(previous))
    state = paused.to_state()
    snapshot = state.to_string()
    agent.mcp_servers = [current]
    if restored:
        state = await RunState.from_string(agent, snapshot)
    state.approve(state.get_interruptions()[0])
    if selection == "current":
        result = await Runner.run(agent, state, run_config=config(current))
        assert current.tool_calls == ["read.report"]
        assert SAFE in str(model.calls[-1].input) and RAW not in result.to_state().to_string()
    else:
        settings = config(previous) if selection == "stale" else RunConfig()
        with pytest.raises(UserError, match="exact tool_text_transforms target"):
            await Runner.run(agent, state, run_config=settings)
        assert not current.tool_calls and len(model.calls) == 1
    assert not previous.tool_calls


@pytest.mark.asyncio
async def test_pending_text_requirement_uses_current_checkpoint_schema():
    @function_tool(needs_approval=True)
    def report():
        return RAW

    async def edit(call, texts):
        return (SAFE,)

    agent = Agent(name="reports", tools=[report], model=model_for("report"))
    config = RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=[(report, edit)]))
    paused = await Runner.run(agent, "go", run_config=config)
    snapshot = paused.to_state().to_json()
    snapshot["$schemaVersion"] = "1.22"
    with pytest.raises(UserError, match="require RunState schema"):
        await RunState.from_json(agent, snapshot)
    del snapshot["context"]["tool_invocations"]["one"]["requires_text_transform"]
    state = await RunState.from_json(agent, snapshot)
    state.approve(state.get_interruptions()[0])
    result = await Runner.run(agent, state, run_config=config)
    assert RAW not in result.to_state().to_string()


@pytest.mark.asyncio
async def test_admitted_call_cannot_lose_editor_before_invocation():
    @function_tool
    def report():
        pytest.fail("executed after the editor was removed")

    async def edit(call, texts):
        return (SAFE,)

    settings = ToolExecutionConfig(tool_text_transforms=[(report, edit)])

    class Hooks(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            settings.tool_text_transforms = ()

    agent = Agent(name="reports", tools=[report], model=model_for("report"))
    with pytest.raises(UserError, match="exact tool_text_transforms target"):
        await Runner.run(agent, "go", run_config=RunConfig(tool_execution=settings), hooks=Hooks())


@pytest.mark.asyncio
@pytest.mark.parametrize("rotate", [False, True])
async def test_reconnected_server_with_same_name_needs_its_own_editor(rotate):
    stale, current = Source(), Source()

    async def edit(call, texts):
        assert call.mcp_target == (current, "read.report")
        return tuple(SAFE for _ in texts)

    selected = ((stale, "read.report"), edit)
    agent = Agent(name="reports", mcp_servers=[current], model=model_for("read.report"))
    config = RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=[selected]))

    class Hooks(RunHooks):
        async def on_llm_start(self, *args):
            if rotate:
                agent.mcp_servers = [stale]

    with pytest.raises(UserError, match="exact tool_text_transforms target"):
        await Runner.run(agent, "go", run_config=config, hooks=Hooks())
    assert not current.tool_calls
    agent.mcp_servers = [current]
    agent.model = model_for("read.report")
    config.tool_execution = ToolExecutionConfig(
        tool_text_transforms=[selected, ((current, "read.report"), edit)]
    )
    result = await Runner.run(agent, "go", run_config=config)
    assert RAW not in result.to_state().to_string()
    assert current.tool_calls == ["read.report"] and not stale.tool_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "route", ["function", "mcp", "child", "namespaced", "copied", "reject_child"]
)
async def test_missing_editor_leaves_session_paused_for_corrected_retry(streamed, route):
    from agents import ResumableSession

    from .memory.test_resumable_session import MemoryStore

    effects = []

    @function_tool(needs_approval=True)
    def report():
        effects.append(True)
        return RAW

    async def edit(call, texts):
        return tuple(SAFE for _ in texts)

    store = MemoryStore()
    session = await ResumableSession.create(store)
    peer = Source(require_approval="always")
    mcp = route == "mcp"
    model = model_for("read.report" if mcp else "report")
    worker = Agent(name="reports", tools=[report], mcp_servers=[peer], model=model)

    def config(target):
        return RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=[(target, edit)]))

    correct = config((peer, "read.report") if mcp else report)
    namespace = "team" if route == "namespaced" else None
    parent = Agent(
        name="parent",
        model=ScriptedModel(
            [
                [function_call("delegate", {"input": "go"}, namespace=namespace, call_id="parent")],
                [assistant_message("done")],
            ]
        ),
    )

    def configure(settings):
        if route in ("child", "reject_child", "namespaced", "copied"):
            delegate = worker.as_tool(
                "delegate", "Delegate", run_config=settings, failure_error_function=None
            )
            if namespace:
                (delegate,) = tool_namespace(name=namespace, description="Team", tools=[delegate])
            if route == "copied":
                delegate = replace(delegate)
            parent.tools = [delegate]
            return parent, correct if route == "copied" else RunConfig()
        return worker, settings

    agent, settings = configure(correct)
    await Runner.run(agent, "go", session=session, run_config=settings)
    saved = store.records[session.session_id]

    async def resume(settings, reject=False):
        agent, settings = configure(settings)
        restored = await ResumableSession.resume(store, session.session_id)
        state = await restored.load_state(agent)
        pending = state.get_interruptions()[0]
        if reject:
            state.reject(pending)
        else:
            state.approve(pending)
        if streamed:
            stream = Runner.run_streamed(agent, state, run_config=settings)
            async for _ in stream.stream_events():
                pass
            return stream
        return await Runner.run(agent, state, run_config=settings)

    stale = config((Source(), "read.report")) if mcp else RunConfig()
    with pytest.raises(UserError, match="exact tool_text_transforms target"):
        await resume(stale)
    assert store.records[session.session_id] == saved
    assert not effects and not peer.tool_calls and len(model.calls) == 1
    if route == "reject_child":
        result = await resume(stale, reject=True)
        assert result.final_output == "done" and not effects
        return
    result = await resume(correct)
    assert result.final_output == "done" and RAW not in result.to_state().to_string()
    assert bool(peer.tool_calls) == mcp and bool(effects) != mcp


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("selector", ["read_report", "mcp_Reports__read_report"])
async def test_mcp_alias_cannot_silently_skip_an_editor(streamed, selector):
    peer = Source()
    prefixed = selector.startswith("mcp_")
    model = model_for(selector if prefixed else "read.report")
    agent = Agent(
        name="reports",
        model=model,
        mcp_servers=[peer],
        mcp_config={"include_server_in_tool_names": prefixed},
    )

    async def forbidden(call, texts):
        pytest.fail("An alias must never bind an editor")

    config = RunConfig(
        tool_execution=ToolExecutionConfig(tool_text_transforms=[((peer, selector), forbidden)])
    )
    with pytest.raises(UserError, match="original tool name"):
        if streamed:
            result = Runner.run_streamed(agent, "go", run_config=config)
            async for event in result.stream_events():
                assert RAW not in repr(event)
        else:
            await Runner.run(agent, "go", run_config=config)
    assert not peer.tool_calls and len(model.calls) == 1


@pytest.mark.asyncio
async def test_mcp_alias_added_on_resume_leaves_approval_retryable():
    from agents import ResumableSession

    from .memory.test_resumable_session import MemoryStore

    peer = Source(require_approval="always")
    model = model_for("read.report")
    agent = Agent(name="reports", model=model, mcp_servers=[peer])
    store = MemoryStore()
    session = await ResumableSession.create(store)
    await Runner.run(agent, "go", session=session)
    saved = store.records[session.session_id]
    resumed = await ResumableSession.resume(store, session.session_id)
    state = await resumed.load_state(agent)
    state.approve(state.get_interruptions()[0])

    async def edit(call, texts):
        return tuple(SAFE for _ in texts)

    def config(name):
        return RunConfig(
            tool_execution=ToolExecutionConfig(tool_text_transforms=[((peer, name), edit)])
        )

    with pytest.raises(UserError, match="original tool name"):
        await Runner.run(agent, state, run_config=config("read_report"))
    assert store.records[session.session_id] == saved and not peer.tool_calls
    result = await Runner.run(agent, state, run_config=config("read.report"))
    assert peer.tool_calls == ["read.report"] and result.final_output == "done"
    assert RAW not in store.records[session.session_id] + repr(model.calls[-1].input)


@pytest.mark.asyncio
async def test_mcp_raw_names_win_over_alias_collisions():
    peer = Source()
    peer.add_tool("read_report", {})

    async def edit(call, texts):
        return tuple("edited " + call.mcp_target[1] for _ in texts)

    names = ("read_report", "read.report")
    config = RunConfig(
        tool_execution=ToolExecutionConfig(
            tool_text_transforms=[((peer, name), edit) for name in names]
        )
    )
    for name in names:
        model = model_for(name)
        agent = Agent(name="reports", model=model, mcp_servers=[peer])
        result = await Runner.run(agent, "go", run_config=config)
        assert RAW not in repr(model.calls[-1].input)
        assert "edited " + name in repr(result.to_input_list())
    assert peer.tool_calls == list(names)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("transport", ["http", "sse"])
@pytest.mark.parametrize("selector", ["read.report", "read_report"])
async def test_rotated_mcp_url_requires_current_server(streamed, transport, selector, monkeypatch):
    from agents.mcp import MCPServerSse, MCPServerStreamableHttp

    server_cls = MCPServerSse if transport == "sse" else MCPServerStreamableHttp
    previous, current = (
        server_cls(params={"url": f"https://user:{rev}@example.invalid/tools?q={rev}#{rev}"})
        for rev in ("old", "new")
    )
    source = Source()
    monkeypatch.setattr(current, "list_tools", source.list_tools)
    monkeypatch.setattr(current, "call_tool", source.call_tool)

    async def edit(call, texts):
        assert call.mcp_target == (current, "read.report")
        return tuple(SAFE for _ in texts)

    stale = ((previous, selector), edit)
    model = model_for("read.report")
    agent = Agent(name="reports", model=model, mcp_servers=[current])

    async def run(entries):
        settings = RunConfig(tool_execution=ToolExecutionConfig(tool_text_transforms=entries))
        if streamed:
            stream = Runner.run_streamed(agent, "go", run_config=settings)
            async for event in stream.stream_events():
                assert RAW not in repr(event)
            return stream
        return await Runner.run(agent, "go", run_config=settings)

    with pytest.raises(UserError, match="exact tool_text_transforms target"):
        await run([stale])
    assert not source.tool_calls and len(model.calls) == 1
    agent.model = model = model_for("read.report")
    result = await run([stale, ((current, "read.report"), edit)])
    assert source.tool_calls == ["read.report"]
    assert SAFE in repr(model.calls[-1].input)
    assert RAW not in repr(model.calls[-1].input) + result.to_state().to_string()
