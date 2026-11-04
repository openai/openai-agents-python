"""Host scope and lifetime contracts, isolated from process-native telemetry.

These tests mock the native ownership boundary. Collector tests separately
establish that the real bridge creates and exports native spans.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from agents import telemetry


class NativeTracing:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.ends: list[tuple[str, dict[str, Any]]] = []
        self.contexts: dict[str, dict[str, Any]] = {}
        self.leases: dict[str, str] = {}
        self.released: list[str] = []
        self.routes: list[dict[str, Any]] = []
        self.acks: list[tuple[int, int]] = []
        self.batches: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.flushes: list[int] = []
        self.credential_headers: list[dict[str, str]] = []
        self.credential_releases: list[str] = []
        self.sequence = 0

    def start_trace_span(self, encoded: str) -> str:
        options = json.loads(encoded)
        self.starts.append(options)
        handle = f"span-{len(self.starts)}"
        owners = [
            self.contexts[self.leases.get(parent, parent)]
            for parent in [
                options.get("parent"),
                options.get("restrictions_parent"),
                *options.get("restrictions_parents", []),
            ]
            if parent is not None
        ]
        data = {
            "handle": handle,
            "trace_id": "a" * 32,
            "span_id": f"{len(self.starts):016x}",
            "parent_span_id": None,
            "disabled": bool(options.get("disabled")) or any(owner["disabled"] for owner in owners),
            "include_sensitive_data": options.get("include_sensitive_data") or False,
        }
        self.contexts[handle] = data
        return json.dumps(data)

    def trace_span_context(self, handle: str) -> str:
        return json.dumps(self.contexts[self.leases.get(handle, handle)])

    def end_trace_span(self, handle: str, encoded: str) -> None:
        self.ends.append((handle, json.loads(encoded)))

    def release_trace_span(self, handle: str) -> None:
        self.released.append(handle)

    def retain_trace_context(self, handle: str) -> str:
        lease = f"lease-{len(self.leases)}"
        self.leases[lease] = self.leases.get(handle, handle)
        return lease

    def release_trace_context(self, context: str) -> None:
        self.released.append(context)

    def set_trace_routes(self, encoded: str) -> int:
        self.routes.append(json.loads(encoded))
        return self.sequence

    async def drain_trace_events(self, count: int) -> str:
        assert count == 64
        await asyncio.sleep(0)
        try:
            batch = self.batches.get_nowait()
        except asyncio.QueueEmpty:
            batch = {"events": [], "through_sequence": 0}
        return json.dumps(batch)

    def ack_trace_events(self, sequence: int, failures: int) -> None:
        self.acks.append((sequence, failures))

    async def force_flush_telemetry(self, timeout: int) -> str:
        self.flushes.append(timeout)
        return json.dumps(
            {"status": "partial", "dropped_records": 2, "failed_deliveries": 1, "barrier": 7}
        )

    def register_trace_credentials(self, encoded: str) -> str:
        self.credential_headers.append(json.loads(encoded))
        return "private-credential-ref"

    def release_trace_credentials(self, reference: str) -> None:
        self.credential_releases.append(reference)


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> NativeTracing:
    value = NativeTracing()
    monkeypatch.setattr(telemetry, "_native_tracing", lambda: value)
    monkeypatch.setattr(telemetry, "_processors", {})
    monkeypatch.setattr(telemetry, "_processor_task", None)
    monkeypatch.setattr(telemetry, "_processor_serial", 0)
    monkeypatch.setattr(telemetry, "_active_processor_ids", set())
    monkeypatch.setattr(telemetry, "_retired_routes", {})
    monkeypatch.setattr(telemetry, "_processor_acknowledged", 0)
    monkeypatch.setattr(telemetry, "_engine_options", "{}")
    return value


def test_nested_scopes_restore_context_and_end_without_exception_payload(
    native: NativeTracing,
) -> None:
    assert telemetry.current_context() is None
    with telemetry.trace("workflow") as workflow:
        assert telemetry.current_context() == workflow.context
        with pytest.raises(ValueError, match="private-error"):
            with telemetry.span("child") as child:
                assert telemetry.current_context() == child.context
                raise ValueError("private-error")
        assert telemetry.current_context() == workflow.context
    assert telemetry.current_context() is None
    assert native.starts[1]["parent"] == workflow.context._handle
    assert native.starts[0]["include_sensitive_data"] is None
    assert native.ends[0][1]["status"] == "error"
    assert "private-error" not in json.dumps(native.ends)
    assert len(native.ends) == 2
    assert len(native.released) == 2


def test_explicit_and_detached_parent_keep_ambient_restrictions(native: NativeTracing) -> None:
    with telemetry.trace("explicit") as explicit:
        with telemetry.trace(
            "restricted", options=telemetry.TraceOptions(disabled=True)
        ) as ambient:
            with telemetry.span("child", options=telemetry.TraceOptions(parent=explicit.context)):
                assert native.starts[-1]["parent"] == explicit.context._handle
                assert native.starts[-1]["restrictions_parent"] == ambient.context._handle
            with telemetry.span("detached", options={"detached": True}):
                assert native.starts[-1]["parent"] == ambient.context._handle
                assert native.starts[-1]["detached"] is True


async def test_async_scopes_isolate_and_captured_work_outlives_parent(
    native: NativeTracing,
) -> None:
    ready = asyncio.Event()
    entered = 0

    async def run(name: str) -> tuple[str, str]:
        nonlocal entered
        with telemetry.trace(name) as scope:
            captured = telemetry._capture()
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            await asyncio.sleep(0)
            assert telemetry.current_context() == scope.context
        assert captured.context is not None
        assert captured.context not in native.released
        identity = json.loads(native.trace_span_context(captured.context))["span_id"]
        captured.close()
        captured.close()
        return scope.context.span_id, identity

    result = await asyncio.gather(run("one"), run("two"))
    assert result[0][0] != result[1][0]
    assert all(parent == captured for parent, captured in result)
    assert len([value for value in native.released if value.startswith("lease")]) == 2
    assert telemetry.current_context() is None


def test_options_capture_restores_context_and_session_defaults(native: NativeTracing) -> None:
    with telemetry.trace("session"):
        session = telemetry._capture()
    assert telemetry.current_context() is None
    operation = telemetry._capture(telemetry.TraceOptions(disabled=True), session.context)
    assert telemetry.current_context() is None
    assert native.starts[-1]["parent"] == session.context
    assert native.starts[-1]["disabled"] is True
    operation.close()
    session.close()


def test_credentials_are_copied_redacted_and_released_after_admission(
    native: NativeTracing,
) -> None:
    headers = {"authorization": "synthetic-secret"}
    credential = telemetry.TraceExportCredentials(headers=headers)
    headers["authorization"] = "changed"
    assert "synthetic-secret" not in repr(credential)
    with pytest.raises(TypeError):
        credential.headers["authorization"] = "changed"  # type: ignore[index]
    with telemetry.trace(
        "credential", options=telemetry.TraceOptions(export_credentials=credential)
    ):
        assert native.credential_headers == [{"authorization": "synthetic-secret"}]
        assert native.credential_releases == ["private-credential-ref"]
        assert "synthetic-secret" not in json.dumps(native.starts)


def test_invalid_json_rejected_before_credential_registration(native: NativeTracing) -> None:
    with pytest.raises(ValueError):
        with telemetry.trace(
            "invalid",
            options=telemetry.TraceOptions(
                metadata={"invalid": float("nan")},
                export_credentials=telemetry.TraceExportCredentials(
                    headers={"authorization": "synthetic"}
                ),
            ),
        ):
            pass
    assert native.starts == []
    assert native.credential_headers == []


async def test_callback_worker_inherits_context_and_error_is_classified(
    native: NativeTracing,
) -> None:
    from agents.session import _call_callback

    def callback() -> str:
        context = telemetry.current_context()
        assert context is not None
        return context.span_id

    with telemetry.trace("workflow"):
        result = await _call_callback(callback, lambda: True)
        assert result == f"{len(native.starts):016x}"
        with pytest.raises(ValueError):
            await _call_callback(lambda: (_ for _ in ()).throw(ValueError("private")), lambda: True)
    assert native.ends[-2][1]["category"] == "callback_error"
    assert "private" not in json.dumps(native.ends)


async def test_processors_get_ordered_lifecycle_and_failure_is_acknowledged(
    native: NativeTracing,
) -> None:
    received: list[str] = []

    class Processor:
        async def on_trace_event(self, event: Mapping[str, Any]) -> None:
            received.append(event["lifecycle"])
            assert event["dropped_attributes_count"] == 3
            assert isinstance(event["dropped_attributes_count"], int)

    class Broken:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            raise RuntimeError("private processor exception")

    telemetry.add_trace_processor(Processor())
    telemetry.add_trace_processor(Broken())
    routes = [route["processor_ids"][0] for route in native.routes]
    await native.batches.put(
        {
            "events": [
                {"lifecycle": "start", "processor_ids": routes, "dropped_attributes_count": 3},
                {"lifecycle": "end", "processor_ids": routes, "dropped_attributes_count": 3},
            ],
            "dropped_records": 0,
            "failed_deliveries": 0,
            "through_sequence": 2,
        }
    )
    try:
        for _ in range(200):
            if native.acks:
                break
            await asyncio.sleep(0.01)
        assert received == ["start", "end"]
        assert native.acks == [(2, 2)]
        telemetry.set_trace_processors([])
        assert native.routes[-1] == {"processor_ids": [], "replace": True}
    finally:
        assert telemetry._processor_task is not None
        telemetry._processor_task.cancel()
        await asyncio.gather(telemetry._processor_task, return_exceptions=True)
        for route in telemetry._processors.values():
            route.close()


async def test_flush_preserves_partial_result_and_budget(native: NativeTracing) -> None:
    result = await telemetry.force_flush(timeout_ms=123)
    assert result == telemetry.FlushResult(
        status="partial", dropped_records=2, failed_deliveries=1, barrier=7
    )
    assert native.flushes == [123]
    with pytest.raises(ValueError):
        await telemetry.force_flush(timeout_ms=0)


async def test_session_open_captures_before_await_and_releases_after_cleanup(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents import Agent
    from agents.session import Session

    session = SimpleNamespace(_trace_context=None, _environment_lease=None)
    cleanup_leases: list[str] = []

    async def close() -> None:
        assert session._trace_context not in native.released
        cleanup_leases.append(session._trace_context)

    session._exit_scope = close

    async def open(*args: Any, **kwargs: Any) -> Any:
        assert native.leases
        assert kwargs["_tracing_context"] in native.leases
        await asyncio.sleep(0)
        return session

    monkeypatch.setattr(Session, "_open", open)
    with telemetry.trace("workflow"):
        async with Agent(instructions="test").session() as actual:
            assert actual._trace_context is not None
    assert cleanup_leases[0] in native.released


async def test_public_submit_keeps_queued_context_until_cancel(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents import Agent
    from agents.schema import _prepare_spec
    from agents.session import Session, TurnStoppedError, _ToolCatalog

    async def read_events(self: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Session, "_read_events", read_events)
    monkeypatch.setattr(Session, "_promote", lambda self: None)
    session = Session(
        None,
        SimpleNamespace(id="session"),
        None,
        _ToolCatalog(0, ()),
        (),
        None,
        _prepare_spec(str),
        "test",
        _start_readers=False,
    )
    session._opening_agent = Agent(instructions="test")
    try:
        with telemetry.trace("queued workflow"):
            turn = await session.submit("input", if_busy="queue")
            lease = next(iter(native.leases))
        assert lease not in native.released
        await turn.cancel()
        assert lease in native.released
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
    finally:
        session._reader.cancel()
        await asyncio.gather(session._reader, return_exceptions=True)


@pytest.mark.parametrize("policy", [{"disabled": True}, {"include_sensitive_data": False}])
def test_session_policy_also_constrains_changed_ambient_and_explicit_parent(
    native: NativeTracing,
    policy: dict[str, bool],
) -> None:
    session = telemetry._capture(telemetry.TraceOptions(**policy))
    try:
        with telemetry.trace("different ambient") as ambient:
            operation = telemetry._capture(fallback=session.context)
            assert native.starts[-1]["parent"] == ambient.context._handle
            assert native.starts[-1]["restrictions_parents"] == [session.context]
            operation.close()
            with telemetry.trace("explicit parent") as explicit:
                operation = telemetry._capture(
                    telemetry.TraceOptions(
                        parent=ambient.context,
                        disabled=False,
                        include_sensitive_data=True,
                    ),
                    session.context,
                )
                assert native.starts[-1]["parent"] == ambient.context._handle
                assert native.starts[-1]["restrictions_parent"] == explicit.context._handle
                assert native.starts[-1]["restrictions_parents"] == [session.context]
                operation.close()
    finally:
        session.close()
    assert telemetry.current_context() is None


async def _wait_for_ack(native: NativeTracing, sequence: int) -> None:
    async def wait() -> None:
        while not any(ack[0] == sequence for ack in native.acks):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=3)


async def _stop_processor_pump() -> None:
    if telemetry._processor_task is not None:
        telemetry._processor_task.cancel()
        await asyncio.gather(telemetry._processor_task, return_exceptions=True)
    for route in telemetry._processors.values():
        route.close()


async def test_replacement_drains_old_events_then_uses_new_route_for_end(
    native: NativeTracing,
) -> None:
    old: list[str] = []
    new: list[str] = []

    class Processor:
        def __init__(self, events: list[str]):
            self.events = events

        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            self.events.append(event["lifecycle"])
            with pytest.raises(TypeError):
                event["attributes"]["values"][0] = "mutation"

    telemetry.add_trace_processor(Processor(old))
    old_id = native.routes[-1]["processor_ids"][0]
    native.sequence = 1
    telemetry.set_trace_processors([Processor(new)])
    new_id = native.routes[-1]["processor_ids"][0]
    assert old_id in telemetry._processors
    try:
        await native.batches.put(
            {
                "events": [
                    {
                        "lifecycle": "start",
                        "processor_ids": [old_id],
                        "attributes": {"values": ["original"]},
                    }
                ],
                "through_sequence": 1,
            }
        )
        await _wait_for_ack(native, 1)
        assert old_id not in telemetry._processors
        await native.batches.put(
            {
                "events": [
                    {
                        "lifecycle": "end",
                        "processor_ids": [new_id],
                        "attributes": {"values": ["original"]},
                    }
                ],
                "through_sequence": 2,
            }
        )
        await _wait_for_ack(native, 2)
        assert old == ["start"]
        assert new == ["end"]
        assert native.acks == [(1, 0), (2, 0)]
    finally:
        await _stop_processor_pump()


async def test_stalled_processor_does_not_stop_healthy_route_or_exceed_worker_budget(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy: list[str] = []
    blocked: list[asyncio.Event] = []
    monkeypatch.setattr(telemetry, "_MAX_PROCESSOR_WORKERS", 1)

    class Stalled:
        async def on_trace_event(self, event: Mapping[str, Any]) -> None:
            gate = asyncio.Event()
            blocked.append(gate)
            await gate.wait()

    class Healthy:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            healthy.append(event["lifecycle"])

    # Wait for earlier tests' cooperatively closed workers to retire.
    async def idle() -> None:
        while telemetry._worker_count:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(idle(), timeout=1)
    telemetry.add_trace_processor(Stalled())
    old_id = native.routes[-1]["processor_ids"][0]
    try:
        native.sequence = 4
        await native.batches.put(
            {
                "events": [{"lifecycle": "start", "processor_ids": [old_id]} for _ in range(4)],
                "through_sequence": 4,
            }
        )
        await _wait_for_ack(native, 4)
        old_route = telemetry._processors[old_id]
        for sequence in range(5, 8):
            telemetry.set_trace_processors([Healthy()])
            identifier = native.routes[-1]["processor_ids"][0]
            native.sequence = sequence
            await native.batches.put(
                {
                    "events": [{"lifecycle": "end", "processor_ids": [identifier]}],
                    "through_sequence": sequence,
                }
            )
            await _wait_for_ack(native, sequence)
            assert telemetry._worker_count == 1
        assert healthy == []
        assert native.acks == [(4, 4), (5, 1), (6, 1), (7, 1)]
        assert len(telemetry._processors) == 1
        assert old_route.loop is not None
        old_route.loop.call_soon_threadsafe(blocked[0].set)
        await asyncio.wait_for(idle(), timeout=1)
        telemetry.set_trace_processors([Healthy()])
        identifier = native.routes[-1]["processor_ids"][0]
        await native.batches.put(
            {"events": [{"lifecycle": "end", "processor_ids": [identifier]}], "through_sequence": 8}
        )
        await _wait_for_ack(native, 8)
        assert healthy == ["end"]
        assert native.acks[-1] == (8, 0)
        assert len(blocked) == 1
    finally:
        await _stop_processor_pump()


def test_route_capacity_failure_does_not_mutate_native_routes(native: NativeTracing) -> None:
    class Processor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            pass

    processors = [Processor() for _ in range(65)]
    with pytest.raises(RuntimeError, match="16 active"):
        telemetry.set_trace_processors(processors)
    assert native.routes == []


def test_tracing_exports_are_available_on_telemetry_namespace() -> None:
    for name in (
        "TraceOptions",
        "TraceExportCredentials",
        "SpanContext",
        "TraceProcessor",
        "TraceLifecycleEvent",
        "trace",
        "span",
        "current_context",
        "force_flush",
        "FlushResult",
        "add_trace_processor",
        "set_trace_processors",
    ):
        assert name in telemetry.__all__
        assert getattr(telemetry, name) is not None


def test_explicit_start_end_restores_parent_and_ends_once(native: NativeTracing) -> None:
    with telemetry.trace("parent") as parent:
        child = telemetry.span("explicit child")
        assert child.start() is child
        assert telemetry.current_context() == child.context
        child.set_outcome(status="error", category="tool_error")
        child.end()
        child.end()
        assert telemetry.current_context() == parent.context
        assert native.ends == [
            (
                child.context._handle,
                {
                    "status": "error",
                    "category": "tool_error",
                    "data": {},
                },
            )
        ]
        with pytest.raises(RuntimeError, match="only be entered once"):
            child.start()
    assert len(native.ends) == 2
    assert telemetry.current_context() is None


@pytest.mark.parametrize("scoped", [False, True])
async def test_manual_restore_reply_forwards_fresh_context_and_joins_admission(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
    scoped: bool,
) -> None:
    from contextlib import nullcontext

    from pydantic import BaseModel

    from agents import FunctionTool
    from agents.schema import _prepare_spec
    from agents.session import Completed, Session, _Submission, _ToolCatalog
    from agents.turn import _root_turn

    class Args(BaseModel):
        value: str

    tool = FunctionTool(name="manual", description="synthetic", input_type=Args, output_type=str)
    entry = tool._capture(None)
    admitted = asyncio.Event()
    resume = asyncio.Event()
    calls: list[tuple[str, ...]] = []

    class Thread:
        id = "restored-session"

        def _check_manual_tool(self, grant: Any) -> bool:
            return True

        def _respond_manual_tool(self, grant: Any, output: str) -> bool:
            return True

        async def _continue_manual(self, *args: str) -> str:
            calls.append(args)
            admitted.set()
            await resume.wait()
            return json.dumps({"status": "started", "turn_id": "original-turn"})

        def _select_steps(self, selected: bool) -> None:
            pass

    async def read_events(self: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Session, "_read_events", read_events)
    session = Session(
        None,
        Thread(),
        None,
        _ToolCatalog(0, (entry,)),
        (),
        None,
        _prepare_spec(str),
        "test",
        _start_readers=False,
    )
    try:
        with telemetry.trace("historical") as historical:
            old = historical.context.span_id
        scope = telemetry.trace("fresh restore") if scoped else nullcontext()
        with scope:
            captured = telemetry._capture()
            state = _Submission(_prepare_spec(str), restored_pending=True, tracing=captured)
            handle = _root_turn(session, "original-turn", "fresh-submission", state.completion)
            state.handle = handle
            state.accepted.set_result(handle)
            session._active = state
            request = entry.request(
                object(), 0, 0, session, handle, "manual-call", Args(value="synthetic"), None
            )
            pending = asyncio.create_task(request.respond("accepted"))
            await admitted.wait()
            context = captured.context
            assert calls == [(context,) if context is not None else ()]
            if scoped:
                assert context is not None
                assert json.loads(native.trace_span_context(context))["span_id"] != old
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            if context is not None:
                assert context not in native.released
            resume.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not state.restored_pending
            if context is not None:
                assert context not in native.released
            # The owning restored submission releases only at its terminal
            # settlement; cancellation of the passive respond waiter does not.
            session._settle_submission(state, True, None, Completed(handle, "done"))
            if context is not None:
                assert context in native.released
    finally:
        session._reader.cancel()
        await asyncio.gather(session._reader, return_exceptions=True)


@pytest.mark.parametrize("timeout", [60_001, 100_000, 600_000])
async def test_flush_rejects_budgets_above_native_contract(
    native: NativeTracing,
    timeout: int,
) -> None:
    with pytest.raises(ValueError, match="60000"):
        await telemetry.force_flush(timeout_ms=timeout)
    assert native.flushes == []


async def test_failed_restore_opening_releases_unstarted_submission_after_native_cleanup(
    native: NativeTracing,
) -> None:
    from agents.schema import _prepare_spec
    from agents.session import _OpeningOwner, _Submission

    with telemetry.trace("fresh restore"):
        captured = telemetry._capture()
    context = captured.context
    assert context is not None
    state = _Submission(_prepare_spec(str), tracing=captured, restored_pending=True)
    state.task = asyncio.create_task(asyncio.Event().wait())
    session = SimpleNamespace(_active=state, _reader=None, _mcp_reader=None, _interception=None)
    opening = _OpeningOwner()
    cleaned: list[bool] = []

    async def close_native() -> None:
        assert state.task is not None and state.task.cancelled()
        assert context not in native.released
        cleaned.append(True)

    opening.lease = SimpleNamespace(orphan=lambda: None, close_orphan=close_native)
    await opening.fail(session, None, None)
    assert cleaned == [True]
    assert context in native.released
    assert state.tracing is None


async def test_detached_explicit_parent_keeps_disabled_restriction(native: NativeTracing) -> None:
    ready: asyncio.Future[telemetry.SpanContext] = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def disabled_owner() -> None:
        with telemetry.trace(
            "disabled parent", options=telemetry.TraceOptions(disabled=True)
        ) as parent:
            ready.set_result(parent.context)
            await release.wait()

    owner = asyncio.create_task(disabled_owner())
    parent_context = await ready
    try:
        with telemetry.trace("enabled ambient") as ambient:
            assert ambient.context.disabled is False
            with telemetry.span(
                "detached child",
                options=telemetry.TraceOptions(
                    parent=parent_context,
                    detached=True,
                    disabled=False,
                ),
            ) as child:
                assert native.starts[-1]["parent"] == parent_context._handle
                assert native.starts[-1]["restrictions_parent"] == ambient.context._handle
                assert native.starts[-1]["detached"] is True
                assert child.context.disabled is True
                assert child.context.parent_span_id is None
    finally:
        release.set()
        await owner


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_fast_burst_progresses_on_worker_without_reader_round_trip(
    native: NativeTracing,
    asynchronous: bool,
) -> None:
    import threading

    entered = threading.Event()
    finished = threading.Event()
    seen: list[int] = []

    def consume(event: Mapping[str, Any]) -> None:
        seen.append(event["sequence"])
        entered.set()
        if len(seen) == 64:
            finished.set()

    class SyncProcessor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            consume(event)

    class AsyncProcessor:
        async def on_trace_event(self, event: Mapping[str, Any]) -> None:
            await asyncio.sleep(0)
            consume(event)

    telemetry.add_trace_processor(AsyncProcessor() if asynchronous else SyncProcessor())
    identifier = native.routes[-1]["processor_ids"][0]
    await native.batches.put(
        {
            "events": [
                {"sequence": sequence, "processor_ids": [identifier]} for sequence in range(64)
            ],
            "through_sequence": 64,
        }
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        # Deliberately pause the reader loop: the fast private worker must
        # finish its admitted group without asking this loop to dispatch again.
        assert finished.wait(0.5)
        await _wait_for_ack(native, 64)
        assert seen == list(range(64))
        assert native.acks == [(64, 0)]
    finally:
        await _stop_processor_pump()


async def test_callback_failure_counts_one_record_and_continues_batch(
    native: NativeTracing,
) -> None:
    seen: list[int] = []

    class Processor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            seen.append(event["sequence"])
            if event["sequence"] in (1, 4):
                raise ValueError("synthetic callback failure")

    telemetry.add_trace_processor(Processor())
    identifier = native.routes[-1]["processor_ids"][0]
    await native.batches.put(
        {
            "events": [
                {"sequence": sequence, "processor_ids": [identifier]} for sequence in range(6)
            ],
            "through_sequence": 6,
        }
    )
    try:
        await _wait_for_ack(native, 6)
        assert seen == list(range(6))
        assert native.acks == [(6, 2)]
    finally:
        await _stop_processor_pump()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_progressing_batch_keeps_each_records_own_deadline(
    native: NativeTracing,
    asynchronous: bool,
) -> None:
    import time

    seen: list[int] = []

    class SyncProcessor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            time.sleep(0.4)
            seen.append(event["sequence"])

    class AsyncProcessor:
        async def on_trace_event(self, event: Mapping[str, Any]) -> None:
            await asyncio.sleep(0.4)
            seen.append(event["sequence"])

    telemetry.add_trace_processor(AsyncProcessor() if asynchronous else SyncProcessor())
    identifier = native.routes[-1]["processor_ids"][0]
    await native.batches.put(
        {
            "events": [
                {"sequence": sequence, "processor_ids": [identifier]} for sequence in range(3)
            ],
            "through_sequence": 3,
        }
    )
    try:
        await _wait_for_ack(native, 3)
        assert seen == [0, 1, 2]
        assert native.acks == [(3, 0)]
    finally:
        await _stop_processor_pump()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_blocked_batch_fences_late_records_and_drains_healthy_replacement(
    native: NativeTracing,
    asynchronous: bool,
) -> None:
    import threading

    entered = threading.Event()
    returned = threading.Event()
    release = threading.Event()
    async_release: list[asyncio.Event] = []
    seen: list[int] = []
    healthy: list[int] = []

    class SyncProcessor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            seen.append(event["sequence"])
            entered.set()
            release.wait()
            returned.set()

    class AsyncProcessor:
        async def on_trace_event(self, event: Mapping[str, Any]) -> None:
            seen.append(event["sequence"])
            gate = asyncio.Event()
            async_release.append(gate)
            entered.set()
            await gate.wait()
            returned.set()

    class Healthy:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            healthy.append(event["sequence"])

    telemetry.add_trace_processor(AsyncProcessor() if asynchronous else SyncProcessor())
    identifier = native.routes[-1]["processor_ids"][0]
    old_route = telemetry._processors[identifier]
    native.sequence = 4
    await native.batches.put(
        {
            "events": [
                {"sequence": sequence, "processor_ids": [identifier]} for sequence in range(4)
            ],
            "through_sequence": 4,
        }
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        telemetry.set_trace_processors([Healthy()])
        replacement = native.routes[-1]["processor_ids"][0]
        await _wait_for_ack(native, 4)
        assert native.acks == [(4, 4)]
        assert seen == [0]
        assert not returned.is_set()
        assert identifier not in telemetry._processors
        await native.batches.put(
            {"events": [{"sequence": 5, "processor_ids": [replacement]}], "through_sequence": 5}
        )
        await _wait_for_ack(native, 5)
        assert healthy == [5]
        assert native.acks[-1] == (5, 0)
        if asynchronous:
            assert old_route.loop is not None
            old_route.loop.call_soon_threadsafe(async_release[0].set)
        else:
            release.set()
        assert await asyncio.to_thread(returned.wait, 1)
        completion = old_route.pending.completion
        assert completion is not None
        await asyncio.wait_for(asyncio.wrap_future(completion), timeout=1)
        assert seen == [0]
    finally:
        release.set()
        if (
            asynchronous
            and async_release
            and old_route.loop is not None
            and not old_route.loop.is_closed()
        ):
            old_route.loop.call_soon_threadsafe(async_release[0].set)
        await _stop_processor_pump()


async def test_cancelled_pump_fences_unstarted_records_until_worker_returns(
    native: NativeTracing,
) -> None:
    import threading

    entered = threading.Event()
    release = threading.Event()
    seen: list[int] = []

    class Processor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            seen.append(event["sequence"])
            entered.set()
            release.wait()

    telemetry.add_trace_processor(Processor())
    identifier = native.routes[-1]["processor_ids"][0]
    route = telemetry._processors[identifier]
    await native.batches.put(
        {
            "events": [
                {"sequence": sequence, "processor_ids": [identifier]} for sequence in range(4)
            ],
            "through_sequence": 4,
        }
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await _stop_processor_pump()
        assert native.acks == [(4, 4)]
        release.set()
        assert route.pending is not None and route.pending.completion is not None
        await asyncio.wait_for(asyncio.wrap_future(route.pending.completion), timeout=1)
        assert seen == [0]
    finally:
        release.set()
        await _stop_processor_pump()


async def test_shutdown_keeps_pump_live_then_fences_unstarted_batch_records(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    entered = threading.Event()
    release = threading.Event()
    seen: list[int] = []
    budgets: list[int | None] = []

    class Processor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            seen.append(event["sequence"])
            entered.set()
            release.wait()

    async def shutdown_native(timeout_ms: int | None = None) -> None:
        budgets.append(timeout_ms)
        assert telemetry._processor_task is not None and not telemetry._processor_task.done()
        await asyncio.sleep(0)

    original_import = telemetry.importlib.import_module

    def import_module(name: str, package: str | None = None) -> Any:
        return native if name == "agents._native" else original_import(name, package)

    monkeypatch.setattr(telemetry.importlib, "import_module", import_module)
    monkeypatch.setattr(native, "shutdown_telemetry", shutdown_native, raising=False)
    telemetry.add_trace_processor(Processor())
    identifier = native.routes[-1]["processor_ids"][0]
    route = telemetry._processors[identifier]
    await native.batches.put(
        {
            "events": [
                {"sequence": sequence, "processor_ids": [identifier]} for sequence in range(4)
            ],
            "through_sequence": 4,
        }
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(telemetry.shutdown(timeout_ms=20), timeout=1)
        assert budgets == [20]
        assert native.acks == [(4, 4)]
        assert telemetry._processors == {}
        assert seen == [0]
        release.set()
        assert route.pending is not None and route.pending.completion is not None
        await asyncio.wait_for(asyncio.wrap_future(route.pending.completion), timeout=1)
        assert seen == [0]
    finally:
        release.set()
        await _stop_processor_pump()


async def test_pump_cancellation_preserves_completed_outcomes_and_counts_later_routes(
    native: NativeTracing,
) -> None:
    import threading

    entered = threading.Event()
    release = threading.Event()
    first: list[int] = []
    later: list[int] = []

    class First:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            first.append(event["sequence"])
            if event["sequence"] == 2:
                entered.set()
                release.wait()

    class Later:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            later.append(event["sequence"])

    telemetry.add_trace_processor(First())
    telemetry.add_trace_processor(Later())
    identifiers = [route["processor_ids"][0] for route in native.routes]
    route = telemetry._processors[identifiers[0]]
    await native.batches.put(
        {
            "events": [{"sequence": sequence, "processor_ids": identifiers} for sequence in (1, 2)],
            "through_sequence": 2,
        }
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await _stop_processor_pump()
        assert first == [1, 2]
        assert later == []
        # One completed delivery survives; the current callback and both
        # not-yet-submitted route deliveries cannot claim completion.
        assert native.acks == [(2, 3)]
        release.set()
        assert route.pending is not None and route.pending.completion is not None
        await asyncio.wait_for(asyncio.wrap_future(route.pending.completion), timeout=1)
        assert later == []
    finally:
        release.set()
        await _stop_processor_pump()


@pytest.mark.parametrize("completed_before_resume", [False, True])
async def test_cancelled_drain_accounts_result_before_restart_without_new_events(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
    completed_before_resume: bool,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    seen: list[int] = []
    read_cancelled: list[bool] = []

    class Processor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            seen.append(event["sequence"])

    async def drain(count: int) -> str:
        raw = json.dumps(await native.batches.get())
        entered.set()
        if completed_before_resume:
            assert telemetry._processor_task is not None
            asyncio.get_running_loop().call_soon(telemetry._processor_task.cancel)
        else:
            try:
                await release.wait()
            except asyncio.CancelledError:
                read_cancelled.append(True)
                raise
        return raw

    monkeypatch.setattr(native, "drain_trace_events", drain)
    telemetry.add_trace_processor(Processor())
    identifier = native.routes[-1]["processor_ids"][0]
    await native.batches.put(
        {
            "events": [
                {"sequence": sequence, "processor_ids": [identifier]} for sequence in (1, 2)
            ],
            "through_sequence": 2,
        }
    )
    assert telemetry._processor_task is not None
    pump = telemetry._processor_task
    await entered.wait()
    if not completed_before_resume:
        pump.cancel()
        await asyncio.sleep(0)
        # Model asyncio.run cancelling every child Task as well as its pump.
        owner = next(
            task for task in asyncio.all_tasks() if isinstance(task, telemetry._ProcessorDrain)
        )
        assert owner.cancel() is False
        pump.cancel()
        await asyncio.sleep(0)
        assert native.acks == []
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pump
    assert read_cancelled == []
    assert seen == []
    assert native.acks == [(2, 2)]
    assert telemetry._processor_acknowledged == 2
    for route in telemetry._processors.values():
        route.close()


@pytest.mark.parametrize("exact", [False, True])
async def test_steering_retains_admitted_context_instead_of_callers_ambient(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
    exact: bool,
) -> None:
    from agents.schema import _prepare_spec
    from agents.session import Session, _Submission, _ToolCatalog
    from agents.turn import _root_turn

    with telemetry.trace("admitting workflow") as owner:
        captured = telemetry._capture()
        admitted_span = owner.context._handle
    owner_token = captured.context
    sent: list[dict[str, Any]] = []

    class Thread:
        id = "session"

        async def _submit(self, encoded: str) -> str:
            request = json.loads(encoded)
            token = request["tracing_context"]
            assert token != owner_token
            assert native.leases[token] == admitted_span
            captured.close()
            await asyncio.sleep(0)
            assert token not in native.released
            sent.append(request)
            return json.dumps({"status": "steered", "turn_id": "turn", "input_id": "input"})

    async def read_events(self: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Session, "_read_events", read_events)
    session = Session(
        None,
        Thread(),
        None,
        _ToolCatalog(0, ()),
        (),
        None,
        _prepare_spec(str),
        "test",
        _start_readers=False,
    )
    state = _Submission(_prepare_spec(str), tracing=captured)
    handle = _root_turn(session, "turn", "submission", state.completion)
    state.handle = handle
    state.accepted.set_result(handle)
    session._active = state
    session._last_turn = handle
    try:
        with telemetry.trace("unrelated caller") as ambient:
            if exact:
                receipt = await handle.steer_exact("follow up", expected_turn_id="turn")
                assert receipt.input_id == "input"
            else:
                await handle.steer("follow up")
            assert telemetry.current_context() == ambient.context
        assert sent[0]["tracing_context"] in native.released
        assert sent[0].get("exact_steering", False) is exact
    finally:
        captured.close()
        session._reader.cancel()
        await asyncio.gather(session._reader, return_exceptions=True)
