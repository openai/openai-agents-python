"""Explicit native trace parenting, proved against flushed OTLP graphs."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import AsyncExitStack
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    CustomStorage,
    FunctionTool,
    SQLiteSessionStore,
    ToolContext,
    TraceContext,
    function_tool,
    telemetry,
)
from agents._trace_context import _native_trace_context, _trace_context
from agents.testing import ScriptedResponses, text, tool_call

ROOT = Path(__file__).parents[1]
PARENT = "00-11111111111111111111111111111111-2222222222222222-01"
OVERRIDE = "00-33333333333333333333333333333333-4444444444444444-01"
WORKER = "00-55555555555555555555555555555555-6666666666666666-01"
STATE = "vendor=synthetic,tenant@abcdefghijklm=value," + ",".join(
    f"v{i}=" + "x" * 128 for i in range(4)
)


def test_carrier_exports_immutable_headers_and_hides_values() -> None:
    assert telemetry.TraceContext is TraceContext
    carrier = TraceContext(traceparent=PARENT, tracestate=" ,vendor=synthetic, \tother=value, ")
    assert carrier.to_headers() == {
        "traceparent": PARENT,
        "tracestate": "vendor=synthetic,other=value",
    }
    carrier.to_headers()["traceparent"] = "changed"
    assert carrier.traceparent == PARENT
    assert PARENT not in repr(carrier) and "synthetic" not in repr(carrier)
    with pytest.raises(FrozenInstanceError):
        carrier.traceparent = OVERRIDE  # type: ignore[misc]
    assert TraceContext(traceparent=PARENT, tracestate=" , \t").tracestate is None
    assert _trace_context({"traceparent": PARENT, "tracestate": None}) == TraceContext(
        traceparent=PARENT
    )
    assert TraceContext(traceparent=PARENT[:-2] + "00").traceparent.endswith("00")
    assert len(STATE) > 512
    assert TraceContext(traceparent=PARENT, tracestate=STATE).tracestate == STATE
    assert (
        TraceContext(traceparent="01" + PARENT[2:] + "-future")
        .to_headers()["traceparent"]
        .endswith("-future")
    )


@pytest.mark.parametrize("flags", ["00", "01", "02", "03"])
def test_defined_trace_flags_round_trip(flags: str) -> None:
    parent = PARENT[:-2] + flags
    assert TraceContext(traceparent=parent).to_headers() == {"traceparent": parent}


@pytest.mark.parametrize(
    "parent",
    [
        "synthetic-private-invalid-parent",
        PARENT.replace("1", "A"),
        "00-" + "0" * 32 + PARENT[35:],
        PARENT[:36] + "0" * 16 + PARENT[52:],
        "ff" + PARENT[2:],
        PARENT + "-extra",
        PARENT[:-2] + "04",
        "01" + PARENT[2:] + "invalid",
        PARENT + "\r\nsynthetic-private-header",
        "01" + PARENT[2:] + "-" + "x" * 512,
    ],
)
def test_invalid_parent_has_fixed_error(parent: str) -> None:
    with pytest.raises(ValueError, match="^Invalid W3C traceparent$"):
        TraceContext(traceparent=parent)


@pytest.mark.parametrize(
    "state",
    [
        "vendor=first,vendor=second",
        "1invalid=value",
        "a@1invalid=value",
        "vendor=synthetic-private\nvalue",
        "vendor=synthetic=private",
        "vendor=",
        "vendor=" + "x" * 257,
        ",".join(f"v{i}=x" for i in range(33)),
        "tenant@abcdefghijklmn=value",
    ],
)
def test_invalid_state_has_fixed_error(state: str) -> None:
    with pytest.raises(ValueError, match="^Invalid W3C tracestate$"):
        TraceContext(traceparent=PARENT, tracestate=state)


async def test_carrier_admission_rejects_before_opening() -> None:
    agent = Agent(instructions="Synthetic test")
    for value in ({"traceparent": "invalid"}, {"traceparent": PARENT, "baggage": "private"}):
        with pytest.raises(ValueError):
            async with agent.session(parent_trace=value):
                pytest.fail("Invalid carrier opened a Session")
    with pytest.raises(ValueError):
        async with agent.session(
            parent_trace={"traceparent": PARENT, "tracestate": "tenant@abcdefghijklmn=value"}
        ):
            pytest.fail("Unsupported native tracestate opened a Session")
    with pytest.raises(TypeError, match="parent_trace must be"):
        async with agent.session(parent_trace=object()):  # type: ignore[arg-type]
            pytest.fail("Invalid carrier opened a Session")


async def test_parent_trace_and_instruction_files_keep_admission_order(tmp_path: Path) -> None:
    agent = Agent(instructions="Synthetic test")
    missing = tmp_path / "missing-instructions.txt"
    with pytest.raises(ValueError, match="Invalid W3C traceparent"):
        async with agent.session(
            instruction_files=[missing], parent_trace={"traceparent": "invalid"}
        ):
            pytest.fail("Invalid parent opened a Session")
    with pytest.raises(FileNotFoundError):
        async with agent.session(
            instruction_files=[missing], parent_trace=TraceContext(traceparent=PARENT)
        ):
            pytest.fail("Missing instruction file opened a Session")


@pytest.mark.parametrize("resume", [None, "synthetic-existing-session"])
async def test_custom_storage_trace_admission_precedes_ownership(
    tmp_path: Path, resume: str | None
) -> None:
    backend = SQLiteSessionStore(tmp_path / "history.sqlite")
    storage = CustomStorage(backend)
    agent = Agent(instructions="Synthetic test")
    missing = tmp_path / "missing-instructions.txt"
    with pytest.raises(ValueError, match="Invalid W3C traceparent"):
        async with agent.session(
            storage=storage, resume=resume, parent_trace={"traceparent": "invalid"}
        ):
            pytest.fail("Invalid parent acquired stored state")
    with pytest.raises(FileNotFoundError):
        async with agent.session(
            storage=storage,
            resume=resume,
            instruction_files=[missing],
            parent_trace={"traceparent": PARENT},
        ):
            pytest.fail("Missing instruction file acquired stored state")
    assert not backend._owners


async def test_trace_parent_preserves_reconciliation_validation() -> None:
    agent = Agent(instructions="Synthetic test")
    with pytest.raises(TypeError, match="reconciled_calls must contain"):
        async with agent.session(
            parent_trace=TraceContext(traceparent=PARENT),
            reconciled_calls=[object()],  # type: ignore[list-item]
        ):
            pytest.fail("Invalid reconciliation opened a Session")


def test_unsupported_native_metadata_remains_optional() -> None:
    assert (
        _native_trace_context({"traceparent": PARENT, "tracestate": "vendor=private\nvalue"})
        is None
    )
    assert _native_trace_context({"traceparent": "invalid"}) is None
    assert _native_trace_context({"traceparent": PARENT, "tracestate": STATE}) == TraceContext(
        traceparent=PARENT, tracestate=STATE
    )


def _run_case(case: str, collector: str, output: Path) -> dict[str, Any]:
    environment = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    # PYTHONSAFEPATH can omit cwd in subprocesses; import only this trusted tree.
    environment["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from tests.test_trace_context import _native_case; "
            "asyncio.run(_native_case(*sys.argv[1:]))",
            case,
            collector,
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic-agents-testing-key" not in result.stdout + result.stderr
    return json.loads(output.read_text())


def _spans(collector: Any) -> list[dict[str, Any]]:
    return [
        span
        for batch in collector.snapshot()
        if batch["signal"] == "traces"
        for resource in batch["data"]["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]


def _attributes(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def _dispatch(spans: list[dict[str, Any]], turn: str) -> dict[str, Any]:
    matches = [
        span
        for span in spans
        if _attributes(span).get("submission.id") == turn
        and span["name"].startswith("op.dispatch.")
    ]
    assert len(matches) == 1, matches
    return matches[0]


def _assert_parent(span: dict[str, Any], parent: str) -> None:
    assert span["traceId"] == parent[3:35]
    assert span["parentSpanId"] == parent[36:52]


def test_native_graph_covers_queued_override_callback_and_cancellation(tmp_path: Path) -> None:
    from examples.observability.trace_walkthrough import Collector

    with Collector().serve() as collector:
        receipt = _run_case("graph", collector.url, tmp_path / "graph.json")
    spans = _spans(collector)
    for key, parent in (
        ("first", PARENT),
        ("queued", OVERRIDE),
        ("cancelled", OVERRIDE),
        ("after", PARENT),
        ("parallel", WORKER),
        ("idle_restored", WORKER),
    ):
        _assert_parent(_dispatch(spans, receipt[key]), parent)
    _assert_parent(_dispatch(spans, receipt["remote"]), receipt["callback"]["traceparent"])
    callback_span = next(
        span for span in spans if span["spanId"] == receipt["callback"]["traceparent"][36:52]
    )
    assert callback_span["traceId"] == PARENT[3:35]
    by_id = {span["spanId"]: span for span in spans}
    ancestry = [callback_span]
    while ancestry[-1].get("parentSpanId") in by_id:
        ancestry.append(by_id[ancestry[-1]["parentSpanId"]])
    assert any(_attributes(span).get("call_id") == "outbound-call" for span in ancestry)
    assert any(_attributes(span).get("submission.id") == receipt["first"] for span in ancestry)
    assert any(
        span["name"] == "thread_spawn" and span.get("parentSpanId") == PARENT[36:52]
        for span in spans
    )
    assert receipt["callback"]["tracestate"] == STATE
    assert receipt["cancel_callback"]["traceparent"][3:35] == OVERRIDE[3:35]
    assert "traceparent" not in json.dumps(receipt["state"])
    assert "tracestate" not in json.dumps(receipt["state"])
    assert "synthetic-agents-testing-key" not in json.dumps(collector.snapshot())
    (tmp_path / "graph-export.json").write_text(
        json.dumps({"receipt": receipt, "batches": collector.snapshot()})
    )


@pytest.mark.parametrize("workflow", [False, True])
def test_model_compaction_uses_session_parent_after_overridden_turn(
    tmp_path: Path, workflow: bool
) -> None:
    from examples.observability.trace_walkthrough import Collector

    with Collector().serve() as collector:
        receipt = _run_case(
            "workflow-compact" if workflow else "compact", collector.url, tmp_path / "compact.json"
        )
    spans = _spans(collector)
    _assert_parent(_dispatch(spans, receipt["first"]), OVERRIDE)
    _assert_parent(_dispatch(spans, receipt["compact"]), PARENT)
    _assert_parent(_dispatch(spans, receipt["after"]), PARENT)


@pytest.mark.parametrize("flags", ["02", "03"])
def test_native_defined_flags_preserve_callback_headers(flags: str, tmp_path: Path) -> None:
    from examples.observability.trace_walkthrough import Collector

    with Collector().serve() as collector:
        receipt = _run_case("flags-" + flags, collector.url, tmp_path / "flags.json")
    assert receipt["headers"]["traceparent"][3:35] == PARENT[3:35]
    assert receipt["headers"]["traceparent"].endswith("-" + flags)
    if flags == "03":
        dispatch = _dispatch(_spans(collector), receipt["turn"])
        _assert_parent(dispatch, PARENT[:-2] + flags)


def test_native_manual_request_limit_excludes_trace_metadata(tmp_path: Path) -> None:
    from examples.observability.trace_walkthrough import Collector

    with Collector().serve() as collector:
        receipt = _run_case("manual-limit", collector.url, tmp_path / "limit.json")
    assert receipt == {"lengths": [64_800, 64_800], "outputs": ["done", "done"]}


@pytest.mark.parametrize("workflow", [False, True])
def test_fresh_process_manual_resume_uses_current_worker_parent(
    tmp_path: Path, workflow: bool
) -> None:
    from examples.observability.trace_walkthrough import Collector

    output = tmp_path / "manual.json"
    with Collector().serve() as collector:
        prefix = "workflow-" if workflow else ""
        first = _run_case(prefix + "suspend", collector.url, output)
        resumed = _run_case(prefix + "resume", collector.url, output)
    assert first["turn"] == resumed["turn"]
    assert first["session"] == resumed["session"]
    assert first["pid"] != resumed["pid"]
    assert resumed["result"] == "resumed"
    spans = _spans(collector)
    dispatches = [
        span
        for span in spans
        if _attributes(span).get("submission.id") == first["turn"]
        and span["name"].startswith("op.dispatch.")
    ]
    assert any(
        span["traceId"] == PARENT[3:35] and span.get("parentSpanId") == PARENT[36:52]
        for span in dispatches
    )
    assert any(
        span["traceId"] == WORKER[3:35] and span.get("parentSpanId") == WORKER[36:52]
        for span in dispatches
    )
    assert "traceparent" not in json.dumps(first["state"])
    assert "tracestate" not in json.dumps(first["state"])
    (tmp_path / "manual-export.json").write_text(
        json.dumps({"source": first, "resumed": resumed, "batches": collector.snapshot()})
    )


def test_custom_storage_reopen_uses_current_trace_parent(tmp_path: Path) -> None:
    from examples.observability.trace_walkthrough import Collector

    with Collector().serve() as collector:
        receipt = _run_case("store-reopen", collector.url, tmp_path / "store.json")
    spans = _spans(collector)
    _assert_parent(_dispatch(spans, receipt["first"]), PARENT)
    _assert_parent(_dispatch(spans, receipt["rebound"]), WORKER)
    fresh = _dispatch(spans, receipt["fresh"])
    assert fresh["traceId"] not in (PARENT[3:35], WORKER[3:35])


def test_same_parent_keeps_concurrent_workflow_policies_separate(tmp_path: Path) -> None:
    from examples.observability.trace_walkthrough import Collector

    with Collector().serve() as collector:
        receipt = _run_case("policy-isolation", collector.url, tmp_path / "policy.json")
    spans = _spans(collector)
    _assert_parent(_dispatch(spans, receipt["permissive"]), PARENT)
    _assert_parent(_dispatch(spans, receipt["fresh"]), PARENT)
    assert not any(
        _attributes(span).get("submission.id") == receipt["restrictive"]
        and span["name"].startswith("op.dispatch.")
        for span in spans
    )
    assert receipt["callbacks"]["restrictive"] == {"disabled": True, "sensitive": False}
    assert receipt["callbacks"]["permissive"] == {"disabled": False, "sensitive": True}
    assert receipt["callbacks"]["fresh"]["disabled"] in (None, False)
    assert receipt["callbacks"]["fresh"]["sensitive"] in (None, False)


def test_disabled_tracing_preserves_sync_run_and_callback(tmp_path: Path) -> None:
    assert _run_case("disabled", "", tmp_path / "disabled.json") == {"result": "done"}


def test_unsupported_native_carrier_does_not_fail_the_callback(tmp_path: Path) -> None:
    from examples.observability.trace_walkthrough import Collector

    with Collector().serve() as collector:
        assert _run_case("unsupported", collector.url, tmp_path / "unsupported.json") == {
            "result": "done",
            "callback": True,
        }


class _Lookup(BaseModel):
    key: str


async def _native_case(case: str, collector: str, output: str) -> None:
    if case == "disabled":
        Path(output).write_text(json.dumps(await _disabled_case()))
        return
    await telemetry.configure(
        traces=telemetry.OtlpHttp(endpoint=collector + "/v1/traces", protocol="json")
    )
    workflow = case.startswith("workflow-")
    case = case.removeprefix("workflow-")
    try:

        async def execute() -> dict[str, Any]:
            if case == "graph":
                return await _graph_case()
            elif case == "compact":
                return await _compact_case(workflow=workflow)
            elif case.startswith("flags-"):
                return await _flags_case(case.removeprefix("flags-"))
            elif case == "policy-isolation":
                return await _policy_isolation_case()
            elif case == "store-reopen":
                return await _store_reopen_case(Path(output))
            elif case == "manual-limit":
                return await _manual_limit_case()
            elif case == "unsupported":
                return await _unsupported_case()
            else:
                return await _manual_case(case, Path(output), workflow=workflow)

        if workflow:
            with telemetry.trace("synthetic workflow"):
                receipt = await execute()
                assert telemetry.current_context() is not None
        else:
            receipt = await execute()
    finally:
        await telemetry.shutdown()
    Path(output).write_text(json.dumps(receipt))


async def _policy_isolation_case() -> dict[str, Any]:
    labels = ("restrictive", "permissive")
    entered = {label: asyncio.Event() for label in labels}
    closed = {label: asyncio.Event() for label in labels}
    release = asyncio.Event()
    callbacks: dict[str, Any] = {}

    @function_tool
    async def observe(ctx: ToolContext, key: str) -> str:
        """Observe the current native policy after its caller scope has closed."""
        if key in entered:
            entered[key].set()
            await release.wait()
        current = telemetry.current_context()
        callbacks[key] = {
            "disabled": current.disabled if current is not None else None,
            "sensitive": current.include_sensitive_data if current is not None else None,
        }
        return "accepted"

    async def run_owned(label: str, disabled: bool, sensitive: bool) -> str:
        async with (
            ScriptedResponses(
                [tool_call("observe", {"key": label}, call_id=label)], [text("done")]
            ) as peer,
            AsyncExitStack() as stack,
        ):
            agent = Agent(
                instructions="Observe synthetic policy", tools=[observe], provider=peer.provider
            )
            with telemetry.trace(
                label,
                options=telemetry.TraceOptions(
                    disabled=disabled,
                    include_sensitive_data=sensitive,
                ),
            ):
                session = await stack.enter_async_context(
                    agent.session(parent_trace={"traceparent": PARENT})
                )
                turn = await session.submit(label)
            closed[label].set()
            assert (await turn.get_result()).final_output == "done"
            return turn.turn_id

    restrictive = asyncio.create_task(run_owned("restrictive", True, False))
    permissive = asyncio.create_task(run_owned("permissive", False, True))
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in [*entered.values(), *closed.values()])), 15
        )
    except BaseException:
        release.set()
        restrictive.cancel()
        permissive.cancel()
        await asyncio.gather(restrictive, permissive, return_exceptions=True)
        raise
    else:
        release.set()
    results = await asyncio.gather(restrictive, permissive)
    async with ScriptedResponses(
        [tool_call("observe", {"key": "fresh"}, call_id="fresh")], [text("done")]
    ) as peer:
        result = await Agent(
            instructions="Observe synthetic policy", tools=[observe], provider=peer.provider
        ).run("fresh", parent_trace={"traceparent": PARENT})
    return {
        **dict(zip(labels, results, strict=True)),
        "fresh": result.turn.turn_id,
        "callbacks": callbacks,
    }


async def _store_reopen_case(output: Path) -> dict[str, Any]:
    storage = CustomStorage(SQLiteSessionStore(output.with_suffix(".sqlite")))
    receipt: dict[str, Any] = {}
    session_id = None
    async with ScriptedResponses([text("first")], [text("rebound")], [text("fresh")]) as peer:
        agent = Agent(instructions="Synthetic stored session", provider=peer.provider)
        for label, parent in [("first", PARENT), ("rebound", WORKER), ("fresh", None)]:
            async with agent.session(
                storage=storage,
                resume=session_id,
                parent_trace={"traceparent": parent} if parent is not None else None,
            ) as session:
                if session_id is not None:
                    assert session.id == session_id
                session_id = session.id
                turn = await session.submit(label)
                assert (await turn.get_result()).final_output == label
                receipt[label] = turn.turn_id
    return receipt


async def _flags_case(flags: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    @function_tool
    async def lookup(context: ToolContext) -> str:
        assert context.trace_context is not None
        captured.update(context.trace_context.to_headers())
        return "accepted"

    async with ScriptedResponses(
        [tool_call("lookup", {}, call_id="flag-call")], [text("done")]
    ) as peer:
        result = await Agent(
            instructions="Call lookup", tools=[lookup], provider=peer.provider
        ).run("start", parent_trace={"traceparent": PARENT[:-2] + flags})
        return {"headers": captured, "turn": result.turn.turn_id}


async def _manual_limit_case() -> dict[str, Any]:
    from agents import ActionRequested

    lookup = FunctionTool(name="lookup", description="Read", input_type=_Lookup, output_type=str)
    lengths: list[int] = []
    outputs: list[str] = []

    def answer(request: Any) -> Any:
        assert isinstance(request, ActionRequested)
        lengths.append(len(request.arguments.key))
        return request.reply("accepted")

    state = ",".join(f"v{i}=" + "x" * 128 for i in range(32))
    for carrier in [None, TraceContext(traceparent=PARENT, tracestate=state)]:
        async with ScriptedResponses(
            [tool_call("lookup", {"key": "x" * 64_800}, call_id="limit-call")], [text("done")]
        ) as peer:
            result = await Agent(instructions="Read", tools=[lookup], provider=peer.provider).run(
                "start", parent_trace=carrier, on_interaction=answer
            )
            outputs.append(result.final_output)
    return {"lengths": lengths, "outputs": outputs}


async def _compact_case(*, workflow: bool = False) -> dict[str, Any]:
    async with ScriptedResponses(
        [text("before")], [text("Remember the synthetic label.")], [text("after")]
    ) as peer:
        agent = Agent(instructions="Remember the synthetic label.", provider=peer.provider)
        async with agent.session(parent_trace={"traceparent": PARENT}) as session:
            first = await session.submit("before", parent_trace={"traceparent": OVERRIDE})
            assert (await first.get_result()).final_output == "before"
            assert await session.compact() is None
            if workflow:
                assert telemetry.current_context() is not None
            after = await session.submit("after")
            assert (await after.get_result()).final_output == "after"
            turns = [turn async for turn in session.turns()]
            assert len(turns) == 3
            assert len(peer.requests) == 3
            return {"first": first.turn_id, "compact": turns[1]["id"], "after": after.turn_id}


async def _unsupported_case() -> dict[str, Any]:
    from agents import Session

    original = Session._invoke_tool
    called = False

    async def invoke(self: Any, state: Any, message: dict[str, Any]) -> None:
        # Fault injection represents native settings outside the public profile.
        # Preserve the real native invocation and its admission/response owners.
        assert message["_sdk_trace"]["traceparent"]
        message["_sdk_trace"]["tracestate"] = "vendor=synthetic\nvalue"
        await original(self, state, message)

    @function_tool
    async def lookup(ctx: ToolContext[None]) -> str:
        """Read despite unavailable optional metadata."""
        nonlocal called
        called = True
        assert ctx.trace_context is None
        return "found"

    Session._invoke_tool = invoke
    try:
        async with ScriptedResponses(
            [tool_call("lookup", {}, call_id="unsupported-carrier")], [text("done")]
        ) as peer:
            result = await Agent(
                instructions="Read synthetic record", tools=[lookup], provider=peer.provider
            ).run("Read", parent_trace={"traceparent": PARENT})
            assert peer.tool_output("unsupported-carrier") == "found"
            return {"result": result.final_output, "callback": called}
    finally:
        Session._invoke_tool = original


async def _disabled_case() -> dict[str, Any]:
    @function_tool
    def lookup(ctx: ToolContext[None]) -> str:
        """Read a synthetic record without an exporter."""
        assert ctx.trace_context is None
        return "found"

    async with ScriptedResponses(
        [tool_call("lookup", {}, call_id="untraced")], [text("done")]
    ) as peer:
        agent = Agent(instructions="Read synthetic record", tools=[lookup], provider=peer.provider)
        result = await asyncio.to_thread(
            agent.run_sync, "Read", parent_trace={"traceparent": PARENT}
        )
        assert peer.tool_output("untraced") == "found"
        return {"result": result.final_output}


async def _graph_case() -> dict[str, Any]:
    entered = asyncio.Event()
    release = asyncio.Event()
    cancel_entered = asyncio.Event()
    cancel_exited = asyncio.Event()
    receipt: dict[str, Any] = {}

    @function_tool
    async def outgoing(ctx: ToolContext[None]) -> str:
        """Call a synthetic remote worker."""
        assert ctx.trace_context is not None
        receipt["callback"] = ctx.trace_context.to_headers()
        async with ScriptedResponses([text("remote")]) as peer:
            async with Agent(instructions="Synthetic remote", provider=peer.provider).session(
                parent_trace=ctx.trace_context
            ) as remote:
                turn = await remote.submit("Remote work")
                assert (await turn.get_result()).final_output == "remote"
                receipt["remote"] = turn.turn_id
        entered.set()
        await release.wait()
        return "sent"

    @function_tool
    async def hold(ctx: ToolContext[None]) -> str:
        """Hold a native invocation until cancellation."""
        assert ctx.trace_context is not None
        receipt["cancel_callback"] = ctx.trace_context.to_headers()
        cancel_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancel_exited.set()
        return "unreachable"

    async with (
        ScriptedResponses(
            [tool_call("outgoing", {}, call_id="outbound-call")],
            [text("first")],
            [text("queued")],
            [tool_call("hold", {}, call_id="cancelled-call")],
            [text("after")],
            [text("idle restored")],
        ) as peer,
        ScriptedResponses([text("parallel")]) as other,
    ):
        agent = Agent(instructions="Synthetic root", tools=[outgoing, hold], provider=peer.provider)
        async with agent.session(
            parent_trace={"traceparent": PARENT, "tracestate": STATE}
        ) as session:
            first = await session.submit("First")
            await asyncio.wait_for(entered.wait(), 15)
            with pytest.raises(TypeError, match="cannot override"):
                await session.submit(
                    "Steer", if_busy="steer", parent_trace=TraceContext(traceparent=OVERRIDE)
                )
            queued_carrier = {"traceparent": OVERRIDE}
            queued = await session.submit("Queued", if_busy="queue", parent_trace=queued_carrier)
            queued_carrier["traceparent"] = WORKER
            # A concurrent unrelated session must retain its own parent.
            parallel = await Agent(instructions="Parallel", provider=other.provider).run(
                "Parallel", parent_trace={"traceparent": WORKER}
            )
            receipt["parallel"] = parallel.turn.turn_id
            waiter = asyncio.create_task(first.get_result())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            release.set()
            assert (await first.get_result()).final_output == "first"
            assert (await queued.get_result()).final_output == "queued"
            cancelled = await session.submit("Cancel", parent_trace={"traceparent": OVERRIDE})
            await asyncio.wait_for(cancel_entered.wait(), 15)
            await cancelled.cancel()
            assert cancel_exited.is_set()
            receipt["cancelled"] = cancelled.turn_id
            after = await session.submit("After", parent_trace=None)
            assert (await after.get_result()).final_output == "after"
            saved = await session.save_state()
            receipt.update(
                first=first.turn_id,
                queued=queued.turn_id,
                after=after.turn_id,
                state=json.loads(saved.to_json()),
            )
        async with agent.session(state=saved, parent_trace={"traceparent": WORKER}) as restored:
            turn = await restored.submit("Fresh worker")
            assert (await turn.get_result()).final_output == "idle restored"
            receipt["idle_restored"] = turn.turn_id
    return receipt


async def _manual_case(case: str, output: Path, *, workflow: bool = False) -> dict[str, Any]:
    from agents import ActionRequested, InteractionRequired, SessionState

    lookup = FunctionTool(
        name="lookup", description="Read synthetic key", input_type=_Lookup, output_type=str
    )
    if case == "suspend":
        async with ScriptedResponses(
            [tool_call("lookup", {"key": "A"}, call_id="manual-call")]
        ) as peer:
            agent = Agent(instructions="Read synthetic key", tools=[lookup], provider=peer.provider)
            with pytest.raises(InteractionRequired) as raised:
                await agent.run("Read", parent_trace={"traceparent": PARENT})
            saved = raised.value.state
            assert saved is not None
            return {
                "state": json.loads(saved.to_json()),
                "turn": saved.native_continuation.turn_id,
                "session": saved.source_session_id,
                "pid": os.getpid(),
            }
    previous = json.loads(output.read_text())
    state = SessionState.from_json(json.dumps(previous["state"]))

    def answer(request: Any) -> Any:
        assert isinstance(request, ActionRequested)
        assert request.call_id == "manual-call"
        if workflow:
            assert telemetry.current_context() is not None
        return request.reply("accepted")

    async with ScriptedResponses([text("resumed")]) as peer:
        result = await Agent(
            instructions="Read synthetic key", tools=[lookup], provider=peer.provider
        ).run(None, state=state, on_interaction=answer, parent_trace={"traceparent": WORKER})
        assert peer.tool_output("manual-call") == "accepted"
        return {
            "turn": result.turn.turn_id,
            "session": result.turn.session_id,
            "pid": os.getpid(),
            "result": result.final_output,
        }
