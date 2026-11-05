"""Host scope and lifetime contracts, isolated from process-native telemetry.

These tests mock the native ownership boundary. Collector tests separately
establish that the real bridge creates and exports native spans.
"""

from __future__ import annotations

import asyncio
import gc
import json
import weakref
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import pytest

from agents import telemetry
from tests._asyncio import timeout


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
    assert set(native.leases).issubset(native.released)
    assert telemetry.current_context() is None


def test_options_capture_restores_context_and_session_defaults(native: NativeTracing) -> None:
    with telemetry.trace("session"):
        session = telemetry._capture()
    assert telemetry.current_context() is None
    operation = telemetry._capture(telemetry.TraceOptions(disabled=True), session.context)
    assert telemetry.current_context() is None
    assert native.leases[native.starts[-1]["parent"]] == native.leases[session.context]
    assert native.starts[-1]["disabled"] is True
    operation.close()
    session.close()


def test_credentials_are_copied_redacted_and_released_after_admission(
    native: NativeTracing,
) -> None:
    headers = {"authorization": "synthetic-secret"}
    credential = telemetry.TraceExportCredentials(headers=headers)
    assert credential == telemetry.TraceExportCredentials(headers=dict(headers))
    with pytest.raises(FrozenInstanceError):
        credential.headers = {}  # type: ignore[misc]
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


@pytest.mark.parametrize("failure", ["key", "value", "iteration"])
def test_rejected_credentials_leave_no_private_mapping_in_sdk_tracebacks(failure: str) -> None:
    secret = "synthetic-private-credential"

    class InterruptedMapping(Mapping[str, str]):
        def __iter__(self) -> Iterator[str]:
            yield "authorization"
            raise RuntimeError("mapping interrupted")

        def __getitem__(self, key: str) -> str:
            return secret

        def __len__(self) -> int:
            return 2

    headers: Any = (
        InterruptedMapping()
        if failure == "iteration"
        else {"authorization": secret, 1: "invalid"}
        if failure == "key"
        else {"authorization": secret, "invalid": 1}
    )
    with pytest.raises((TypeError, RuntimeError)) as raised:
        telemetry.TraceExportCredentials(headers=headers)
    pending = [raised.value]
    seen: set[int] = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        pending.extend(link for link in (error.__context__, error.__cause__) if link is not None)
        traceback = error.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if (
                Path(frame.f_code.co_filename) == Path(telemetry.__file__)
                or frame.f_code.co_filename == "<string>"
            ):
                for name, value in frame.f_locals.items():
                    retained = getattr(value, "headers", None) if name == "self" else value
                    assert not isinstance(retained, Mapping) or secret not in retained.values(), (
                        frame.f_code.co_name,
                        name,
                    )
            traceback = traceback.tb_next
    assert headers["authorization"] == secret


def test_credentials_preserve_empty_runtime_mapping() -> None:
    assert dict(telemetry.TraceExportCredentials(headers=None).headers) == {}  # type: ignore[arg-type]


async def test_mcp_notice_restores_its_exact_owner_over_an_unrelated_scope(
    native: NativeTracing,
) -> None:
    from agents.session import Session

    with telemetry.trace(
        "native MCP owner", options=telemetry.TraceOptions(disabled=True)
    ) as owner:
        retained = native.retain_trace_context(owner.context._handle)
    observed: list[telemetry.SpanContext] = []

    async def transform(state: Any, notice: Any) -> None:
        current = telemetry.current_context()
        assert current is not None and current.disabled
        observed.append(current)
        await asyncio.sleep(0)
        assert telemetry.current_context() is current

    state = SimpleNamespace(failure=asyncio.get_running_loop().create_future())
    session: Any = SimpleNamespace(_invoke_mcp_policy_body=transform)
    try:
        with telemetry.trace("unrelated dispatcher") as ambient:
            await Session._invoke_mcp_policy(session, state, {"tracing_context": retained})
            assert telemetry.current_context() is ambient.context
            assert not ambient.context.disabled
        assert len(observed) == 1
        assert observed[0].span_id != owner.context.span_id
        assert native.leases[native.starts[-1]["parent"]] == native.leases[retained]
        assert telemetry.current_context() is None
    finally:
        native.release_trace_context(retained)


async def test_copied_context_and_explicit_parent_outlive_recording_scope(
    native: NativeTracing,
) -> None:
    entered = asyncio.Event()

    async def delayed() -> telemetry.SpanContext:
        await entered.wait()
        with telemetry.span("late child") as child:
            assert child.context.disabled
            return child.context

    with telemetry.trace("parent", options=telemetry.TraceOptions(disabled=True)) as scope:
        parent = scope.context
        task = asyncio.create_task(delayed())
    assert parent._handle not in native.released
    entered.set()
    await task
    assert native.starts[-1]["parent"] == parent._handle
    with telemetry.span("explicit", options=telemetry.TraceOptions(parent=parent)):
        pass
    token = parent._handle
    reference = weakref.ref(parent)
    del scope, parent, task
    await asyncio.sleep(0)
    gc.collect()
    assert reference() is None
    assert token in native.released


def test_borrowed_activation_context_owns_a_distinct_lease(native: NativeTracing) -> None:
    with telemetry.trace("parent") as owner:
        borrowed = native.retain_trace_context(owner.context._handle)
    with telemetry._activate(borrowed):
        context = telemetry.current_context()
        assert context is not None and context._handle != borrowed
    owned = context._handle
    native.release_trace_context(borrowed)
    with telemetry.span("after native callback", options=telemetry.TraceOptions(parent=context)):
        pass
    assert owned not in native.released
    reference = weakref.ref(context)
    del context
    gc.collect()
    assert reference() is None and owned in native.released


@pytest.mark.parametrize("category", ["rate-limit", "é", 1, True, "x" * 257])
def test_invalid_outcome_category_preserves_the_previous_outcome(
    native: NativeTracing, category: Any
) -> None:
    with telemetry.trace("outcome") as scope:
        scope.set_outcome(status="error", category="")
        with pytest.raises(ValueError, match="category"):
            scope.set_outcome(status="ok", category=category)
    assert native.ends[-1][1]["status"] == "error"
    assert native.ends[-1][1]["category"] == ""


@pytest.mark.parametrize("boundary", ["credentials", "span"])
@pytest.mark.parametrize("entry", ["with", "start", "capture", "session"])
async def test_admission_errors_drop_private_credentials_from_sdk_frames(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, boundary: str, entry: str
) -> None:
    from agents import Agent

    secret = "synthetic-private-admission"
    credentials = telemetry.TraceExportCredentials(headers={"authorization": secret})
    options = telemetry.TraceOptions(export_credentials=credentials)

    def fail(*args: Any) -> Any:
        raise RuntimeError("synthetic admission failure")

    monkeypatch.setattr(
        native,
        "register_trace_credentials" if boundary == "credentials" else "start_trace_span",
        fail,
    )

    def retains(value: Any) -> bool:
        if isinstance(value, telemetry.TraceScope):
            value = value._options
        if isinstance(value, telemetry.TraceOptions):
            value = value.export_credentials
        if isinstance(value, telemetry.TraceExportCredentials):
            value = value.headers
        return isinstance(value, Mapping) and secret in value.values()

    with pytest.raises(RuntimeError, match="synthetic admission failure") as caught:
        if entry == "capture":
            telemetry._capture(options)
        elif entry == "session":
            async with Agent(instructions="test").session(trace_options=options):
                pytest.fail("Failed tracing admission opened a Session")
        elif entry == "start":
            telemetry.trace("test", options=options).start()
        else:
            with telemetry.trace("test", options=options):
                pytest.fail("Failed tracing admission entered its scope")
    error: BaseException | None = caught.value
    while error is not None:
        traceback = error.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if Path(frame.f_code.co_filename).is_relative_to(Path(telemetry.__file__).parent):
                assert not any(retains(value) for value in frame.f_locals.values()), (
                    frame.f_code.co_name
                )
            traceback = traceback.tb_next
        error = error.__cause__ or error.__context__
    assert credentials.headers["authorization"] == secret
    assert options.export_credentials is credentials
    assert telemetry._processor_task is None


async def test_processor_invocation_suppresses_detached_scopes_and_bare_operations(
    native: NativeTracing,
) -> None:
    class Processor:
        async def on_trace_event(self, event: Any) -> None:
            await asyncio.sleep(0)
            with telemetry.trace(
                "processor instrumentation",
                options=telemetry.TraceOptions(disabled=False, detached=True),
            ) as scope:
                assert scope.context.disabled
            operation = telemetry._capture()
            assert operation.scope is not None and operation.scope.context.disabled
            operation.close()
            assert telemetry._processor_task is None

    route = telemetry._ProcessorRoute(Processor())
    await route._invoke({})  # type: ignore[typeddict-item]
    with telemetry.trace("application") as scope:
        assert not scope.context.disabled


@pytest.mark.parametrize(
    "entry", ["trace", "span", "capture", "session", "run", "run_sync", "submit"]
)
@pytest.mark.parametrize("invalid", ["parent", "unknown"])
async def test_invalid_options_clear_all_sdk_forwarding_frames(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, entry: str, invalid: str
) -> None:
    from agents import Agent
    from agents.schema import _prepare_spec
    from agents.session import Session, _ToolCatalog

    secret = "synthetic-private-invalid-options"
    options: Any = {
        "export_credentials": telemetry.TraceExportCredentials(headers={"authorization": secret}),
        invalid: "invalid",
    }
    agent = Agent(instructions="test")
    session = None

    async def read_events(self: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Session, "_read_events", read_events)

    def retains(value: Any) -> bool:
        if isinstance(value, telemetry.TraceScope):
            value = getattr(value, "_options", None)
        if isinstance(value, telemetry.TraceOptions):
            value = value.export_credentials
        if isinstance(value, telemetry.TraceExportCredentials):
            return secret in value.headers.values()
        return isinstance(value, dict) and any(retains(item) for item in value.values())

    try:
        with pytest.raises((TypeError, ValueError)) as caught:
            if entry in ("trace", "span"):
                getattr(telemetry, entry)("invalid", options=options)
            elif entry == "capture":
                telemetry._capture(options)
            elif entry == "session":
                async with agent.session(trace_options=options):
                    pytest.fail("Invalid options opened a Session")
            elif entry == "run":
                await agent.run("test", trace_options=options)
            elif entry == "run_sync":
                await asyncio.to_thread(agent.run_sync, "test", trace_options=options)
            else:
                session = Session(
                    None,
                    SimpleNamespace(id="test"),
                    None,
                    _ToolCatalog(0, ()),
                    (),
                    None,
                    _prepare_spec(str),
                    "test",
                    _start_readers=False,
                )
                session._opening_agent = agent
                await session.submit("test", trace_options=options)
        pending = [caught.value]
        seen: set[int] = set()
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            pending.extend(
                link for link in (error.__context__, error.__cause__) if link is not None
            )
            traceback = error.__traceback__
            while traceback is not None:
                frame = traceback.tb_frame
                if Path(frame.f_code.co_filename).is_relative_to(Path(telemetry.__file__).parent):
                    assert not any(retains(value) for value in frame.f_locals.values()), (
                        frame.f_code.co_name
                    )
                traceback = traceback.tb_next
        assert options["export_credentials"].headers["authorization"] == secret
        assert not native.starts
    finally:
        if session is not None:
            session._reader.cancel()
            await asyncio.gather(session._reader, return_exceptions=True)


def test_rejected_pump_factory_unwinds_admission_and_closes_coroutine(
    native: NativeTracing,
) -> None:
    import inspect

    async def exercise() -> None:
        class Processor:
            def on_trace_event(self, event: Any) -> None:
                pass

        work: list[Any] = []
        failure = RuntimeError("factory rejected")

        def reject(loop: Any, coroutine: Any, **kwargs: Any) -> Any:
            work.append(coroutine)
            raise failure

        with telemetry.trace("parent") as parent:
            telemetry._processors["registered"] = telemetry._ProcessorRoute(Processor())
            loop = asyncio.get_running_loop()
            original = loop.get_task_factory()
            loop.set_task_factory(reject)
            try:
                with pytest.raises(RuntimeError, match="factory rejected") as caught:
                    telemetry.trace("unpublished").start()
                assert caught.value is failure
                assert telemetry.current_context() is parent.context
                assert native.ends[-1][1]["status"] == "error"
                assert native.ends[-1][0] in native.released
                assert telemetry._processor_task is None
                assert len(work) == 1 and inspect.getcoroutinestate(work[0]) == inspect.CORO_CLOSED
            finally:
                loop.set_task_factory(original)

    asyncio.run(exercise())


def test_falsey_exceptions_record_scope_and_capture_errors(native: NativeTracing) -> None:
    class FalseyError(Exception):
        def __bool__(self) -> bool:
            return False

    with pytest.raises(FalseyError):
        with telemetry.trace("falsey"):
            raise FalseyError()
    captured = telemetry._capture(telemetry.TraceOptions())
    captured.close(FalseyError())
    assert [outcome["status"] for _, outcome in native.ends] == ["error", "error"]


async def test_shutdown_rejects_processor_reentrancy_without_mutation(
    native: NativeTracing,
) -> None:
    token = telemetry._in_processor.set(True)
    try:
        with pytest.raises(RuntimeError, match="inside a trace processor"):
            await telemetry.shutdown(timeout_ms=20)
        assert not native.acks and telemetry._processor_task is None
    finally:
        telemetry._in_processor.reset(token)


async def test_shutdown_joins_a_live_foreign_loop_pump(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    loop = asyncio.new_event_loop()
    running = threading.Event()

    def serve() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(running.set)
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=serve)
    thread.start()
    assert await asyncio.to_thread(running.wait, 2)

    async def start() -> asyncio.Task[Any]:
        return asyncio.create_task(asyncio.Event().wait())

    task = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(start(), loop))
    telemetry._processor_task = task
    calls: list[int] = []

    async def shutdown(budget: int) -> None:
        calls.append(budget)

    original = telemetry.importlib.import_module
    monkeypatch.setattr(native, "shutdown_telemetry", shutdown, raising=False)
    monkeypatch.setattr(
        telemetry.importlib,
        "import_module",
        lambda name, package=None: native if name == "agents._native" else original(name, package),
    )
    try:
        await telemetry.shutdown(timeout_ms=20)
        assert calls == [20] and task.cancelled()
        assert telemetry._processor_task is None and not telemetry._processors
    finally:
        loop.call_soon_threadsafe(loop.stop)
        await asyncio.to_thread(thread.join, 2)
        assert not thread.is_alive()


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


@pytest.mark.parametrize("budget", [0, True, "10", 1.5, 60_001])
@pytest.mark.parametrize("previous_loop", [False, True])
def test_invalid_flush_preserves_queued_records_and_processor_owner(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, budget: Any, previous_loop: bool
) -> None:
    received: list[Any] = []
    drains: list[int] = []

    class Processor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            received.append(event)

    telemetry.add_trace_processor(Processor())
    if previous_loop:

        async def prior() -> None:
            telemetry._ensure_processor_pump()

        asyncio.run(prior())
    original = telemetry._processor_task
    identifier = next(iter(telemetry._processors))
    native.batches.put_nowait(
        {"events": [{"processor_ids": [identifier], "sequence": 1}], "through_sequence": 1}
    )
    drain = native.drain_trace_events

    async def observe(count: int) -> str:
        drains.append(count)
        return await drain(count)

    monkeypatch.setattr(native, "drain_trace_events", observe)

    async def reject() -> None:
        try:
            with pytest.raises(ValueError, match="timeout_ms"):
                await telemetry.force_flush(timeout_ms=budget)
            for _ in range(5):
                await asyncio.sleep(0)
            assert telemetry._processor_task is original
            assert native.batches.qsize() == 1
            assert not drains and not received and not native.flushes and not native.acks
        finally:
            if original is not None and telemetry._processor_task is original:
                assert original.done()
                telemetry._processor_task = None
            await _stop_processor_pump()

    asyncio.run(reject())


@pytest.mark.parametrize("with_instruction_files", [False, True])
async def test_session_open_captures_before_await_and_releases_after_cleanup(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    with_instruction_files: bool,
) -> None:
    from agents import Agent
    from agents.session import Session

    session = SimpleNamespace(_trace_context=None, _environment_lease=None)
    cleanup_leases: list[str] = []
    files = [tmp_path / "instructions.md"] if with_instruction_files else []

    async def close() -> None:
        assert session._trace_context not in native.released
        cleanup_leases.append(session._trace_context)

    session._exit_scope = close

    async def open(*args: Any, **kwargs: Any) -> Any:
        assert native.leases
        assert kwargs["_tracing_context"] in native.leases
        assert kwargs["instruction_files"] == files
        policy = json.loads(native.trace_span_context(kwargs["_tracing_context"]))
        assert policy["disabled"] is with_instruction_files
        await asyncio.sleep(0)
        return session

    monkeypatch.setattr(Session, "_open", open)
    with telemetry.trace("workflow"):
        async with Agent(instructions="test").session(
            instruction_files=files,
            trace_options=telemetry.TraceOptions(disabled=True) if with_instruction_files else None,
        ) as actual:
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
            pending, _ = session._pending[0]
            assert pending.tracing is not None
            lease = pending.tracing.context
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
                native.leases[child.context._handle],
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
            assert native.leases[token] == native.leases[admitted_span]
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


@pytest.mark.parametrize(("cancel_count", "fails"), [(0, True), (2, False), (2, True)])
async def test_shutdown_retires_processors_before_primary_outcome(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
    cancel_count: int,
    fails: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("synthetic native failure")

    class Processor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            pass

    async def shutdown_native(timeout_ms: int | None = None) -> None:
        entered.set()
        await release.wait()
        if fails:
            raise failure

    original_import = telemetry.importlib.import_module
    monkeypatch.setattr(
        telemetry.importlib,
        "import_module",
        lambda name, package=None: (
            native if name == "agents._native" else original_import(name, package)
        ),
    )
    monkeypatch.setattr(native, "shutdown_telemetry", shutdown_native, raising=False)
    telemetry.add_trace_processor(Processor())
    identifier = native.routes[-1]["processor_ids"][0]
    route = telemetry._processors[identifier]
    await native.batches.put(
        {
            "events": [{"sequence": 1, "processor_ids": [identifier]}],
            "through_sequence": 1,
        }
    )
    async with timeout(2):
        while not native.acks:
            await asyncio.sleep(0)
    pump = telemetry._processor_task
    pending = asyncio.create_task(telemetry.shutdown(timeout_ms=20))
    try:
        await entered.wait()
        for _ in range(cancel_count):
            pending.cancel()
            await asyncio.sleep(0)
        release.set()
        try:
            outcome = await pending
        except BaseException as error:
            outcome = error
        if cancel_count:
            assert isinstance(outcome, asyncio.CancelledError)
            if fails:
                error = outcome.__context__
                while error is not None and error is not failure:
                    error = error.__context__
                assert error is failure
        else:
            assert outcome is failure
        assert pump is not None and pump.done()
        assert telemetry._processor_task is None
        assert telemetry._processors == {}
        assert telemetry._active_processor_ids == set()
        assert telemetry._retired_routes == {}
        async with timeout(2):
            while route.loop is not None and not route.loop.is_closed():
                await asyncio.sleep(0.01)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await _stop_processor_pump()
        route.close()


@pytest.mark.parametrize("budget", [0, True, 60_001])
async def test_shutdown_rejects_invalid_budget_before_native_or_processor_mutation(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
) -> None:
    class Processor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            pass

    async def unexpected_shutdown(*args: Any) -> None:
        pytest.fail("Invalid budget reached native shutdown")

    original_import = telemetry.importlib.import_module
    monkeypatch.setattr(
        telemetry.importlib,
        "import_module",
        lambda name, package=None: (
            native if name == "agents._native" else original_import(name, package)
        ),
    )
    monkeypatch.setattr(native, "shutdown_telemetry", unexpected_shutdown, raising=False)
    telemetry.add_trace_processor(Processor())
    pump = telemetry._processor_task
    routes = dict(telemetry._processors)
    try:
        with pytest.raises(ValueError, match="timeout_ms"):
            await telemetry.shutdown(timeout_ms=budget)
        assert telemetry._processor_task is pump
        assert telemetry._processors == routes
    finally:
        await _stop_processor_pump()


async def test_shutdown_joins_cancelled_drain_and_accounts_undelivered_records(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reading, release, native_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen: list[int] = []
    identifier = ""

    class Processor:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            seen.append(event["sequence"])

    async def drain(count: int) -> str:
        reading.set()
        await release.wait()
        return json.dumps(
            {
                "events": [{"sequence": 7, "processor_ids": [identifier]}],
                "through_sequence": 7,
            }
        )

    async def shutdown_native(*args: Any) -> None:
        await reading.wait()
        native_done.set()

    original_import = telemetry.importlib.import_module
    monkeypatch.setattr(
        telemetry.importlib,
        "import_module",
        lambda name, package=None: (
            native if name == "agents._native" else original_import(name, package)
        ),
    )
    monkeypatch.setattr(native, "shutdown_telemetry", shutdown_native, raising=False)
    monkeypatch.setattr(native, "drain_trace_events", drain)
    telemetry.add_trace_processor(Processor())
    identifier = native.routes[-1]["processor_ids"][0]
    pump = telemetry._processor_task
    pending = asyncio.create_task(telemetry.shutdown(timeout_ms=20))
    try:
        async with timeout(2):
            await native_done.wait()
            assert pump is not None and not pump.done()
            for _ in range(2):
                pending.cancel()
                await asyncio.sleep(0)
            assert not pending.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert native.acks == [(7, 1)]
        assert seen == []
        assert telemetry._processor_task is None
        assert telemetry._processors == {}
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await _stop_processor_pump()


@pytest.mark.parametrize("completed", [False, True])
async def test_batch_completion_cannot_swallow_waiter_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    completed: bool,
) -> None:
    from time import monotonic

    record = telemetry._ProcessorDelivery({"sequence": 1})
    batch = telemetry._ProcessorBatch((record,))
    wrap_future = asyncio.wrap_future
    pending: asyncio.Task[int]

    def wrap(completion: Any) -> Any:
        future = wrap_future(completion)
        if completed:
            # Cancel at completion, before the host resumes with that result.
            future.add_done_callback(lambda _: pending.cancel())
        return future

    monkeypatch.setattr(asyncio, "wrap_future", wrap)
    pending = asyncio.create_task(batch.wait())
    await asyncio.sleep(0)
    if completed:
        record.result.set_result((True, monotonic()))
    else:
        pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert batch.stopped.is_set()
    assert not record.result.cancelled()
    if not completed:
        record.result.set_result((True, monotonic()))
    assert record.result.result()[0] is True


@pytest.mark.parametrize("roots_kind", ["inherit", "clear", "replace"])
@pytest.mark.parametrize("refuse", [False, True])
async def test_write_policy_and_trace_snapshots_survive_queue_and_continuation(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    roots_kind: str,
    refuse: bool,
) -> None:
    from agents import Agent, Environment, NativeError
    from agents.schema import _prepare_spec
    from agents.session import Session, _ToolCatalog

    requests: list[dict[str, Any]] = []
    trace_owner: str | None = None

    class Thread:
        id = "session"

        def _select_steps(self, *args: Any) -> None:
            pass

        async def _submit(self, encoded: str) -> str:
            request = json.loads(encoded)
            assert request["tracing_context"] == trace_owner
            assert trace_owner not in native.released
            requests.append(request)
            if refuse:
                return json.dumps(
                    {
                        "status": "not_submitted",
                        "reason": {
                            "type": "writable_roots_refused",
                            "message": "synthetic policy refusal",
                        },
                    }
                )
            state = session._active
            assert state is not None
            if len(requests) == 1:
                state.unsampled_steering = True
            else:
                assert state.writable_roots is None
                assert session._writable_roots == applied
            state.terminal.set_result({"type": "turn_complete", "last_agent_message": "done"})
            return json.dumps({"status": "started", "turn_id": f"turn-{len(requests)}"})

        async def call(self, operation: str) -> str:
            assert operation == "token_usage_info"
            assert trace_owner not in native.released
            await asyncio.sleep(0)
            return "null"

    async def read_events(self: Any) -> None:
        await asyncio.Event().wait()

    promote = Session._promote
    monkeypatch.setattr(Session, "_read_events", read_events)
    monkeypatch.setattr(Session, "_promote", lambda self: None)
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
    session._opening_agent = Agent(instructions="test")
    session._executor = Environment.local(cwd=tmp_path)
    opening = ((tmp_path / "opening").as_uri(),)
    session._writable_roots = opening
    roots = (
        None if roots_kind == "inherit" else [] if roots_kind == "clear" else [tmp_path / "first"]
    )
    expected = None if roots is None else tuple(unquote(path.as_uri()) for path in roots)
    applied = opening
    try:
        with telemetry.trace("admitting workflow"):
            turn = await session.submit(
                "input",
                if_busy="queue",
                writable_roots=roots,
                trace_options=telemetry.TraceOptions(disabled=True),
            )
            state, _ = session._pending[0]
            assert state.tracing is not None
            trace_owner = state.tracing.context
            captured_roots = state.writable_roots
            assert (
                None if captured_roots is None else tuple(unquote(uri) for uri in captured_roots)
            ) == expected
            if captured_roots is not None:
                applied = captured_roots
        if roots is not None:
            roots.append(tmp_path / "later mutation")
        with telemetry.trace("unrelated caller"):
            promote(session)
            async with timeout(3):
                if refuse:
                    with pytest.raises(NativeError, match="synthetic policy refusal") as caught:
                        await turn.get_result()
                    assert caught.value._effects == "not_started"
                else:
                    assert (await turn.get_result()).final_output == "done"
        assert requests[0]["mode"] == "start_if_idle"
        if expected is None:
            assert "writable_roots" not in requests[0]
        else:
            assert captured_roots is not None
            assert requests[0]["writable_roots"] == list(captured_roots)
        if not refuse:
            assert len(requests) == 2
            assert requests[1]["mode"] == "continue_if_idle"
            assert "writable_roots" not in requests[1]
            assert requests[1]["expected_previous_turn_id"] == "turn-1"
        assert session._writable_roots == (opening if refuse else applied)
        assert trace_owner in native.released
        assert not session._broken
    finally:
        session._reader.cancel()
        await asyncio.gather(session._reader, return_exceptions=True)


@pytest.mark.parametrize("settings", ["trace", "roots", "both"])
async def test_steering_rejects_trace_and_write_policy_overrides(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
    settings: str,
) -> None:
    from agents.schema import _prepare_spec
    from agents.session import Session, _ToolCatalog

    async def read_events(self: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Session, "_read_events", read_events)
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
    kwargs: dict[str, Any] = {}
    if settings != "roots":
        kwargs["trace_options"] = telemetry.TraceOptions(disabled=True)
    if settings != "trace":
        kwargs["writable_roots"] = []
    try:
        with pytest.raises(TypeError, match="cannot override"):
            await session.submit("steer", if_busy="steer", **kwargs)
        assert not session._pending
        assert native.starts == []
    finally:
        session._reader.cancel()
        await asyncio.gather(session._reader, return_exceptions=True)


async def test_question_context_retires_registration_but_survives_queued_callback(
    native: NativeTracing,
) -> None:
    from agents.manual import _question
    from agents.session import Session

    class Issuer:
        id = "session"

    issuer = Issuer()
    with telemetry.trace("origin", options=telemetry.TraceOptions(disabled=True)) as origin:
        message = {
            "type": "exec_approval_request",
            "turn_id": "turn",
            "command": ["synthetic"],
            "tracing_context": origin.context._handle,
        }
        question = _question(issuer, message, SimpleNamespace(turn_id="turn", submission_id="s"))
    owner = question._tracing
    assert owner is not None
    owned_token = owner._handle
    reference = weakref.ref(owner)
    entered = []

    async def callback(context: telemetry.SpanContext) -> None:
        with telemetry._owned_span("queued callback", "callback", context._handle):
            current = telemetry.current_context()
            assert current is not None and current.disabled
            entered.append(current.trace_id)

    task = asyncio.create_task(callback(owner))
    del owner
    state = SimpleNamespace(questions={id(question): question})
    Session._retire_questions(state)
    assert state.questions == {}
    assert question._tracing is None
    assert "tracing_context" not in question._message
    assert message["tracing_context"] == origin.context._handle
    assert reference() is not None and owned_token not in native.released
    await task
    gc.collect()
    assert entered == [origin.context.trace_id]
    assert reference() is None and owned_token in native.released
    # Retaining the resolved public question must not retain its credential lease.
    assert question.turn_id == "turn"


@pytest.mark.parametrize(("pending", "unsupported"), [(False, False), (True, False), (True, True)])
async def test_child_question_context_failure_discards_only_expired_grant(
    monkeypatch: pytest.MonkeyPatch, pending: bool, unsupported: bool
) -> None:
    import agents.session as sessions

    loop = asyncio.get_running_loop()
    state = SimpleNamespace(
        completion=loop.create_future(),
        observed=loop.create_future(),
        terminal=loop.create_future(),
        failure=loop.create_future(),
        stopping=False,
        handle=SimpleNamespace(submission_id="submission"),
    )
    failure = (
        sessions.InteractionRequired("unsupported question")
        if unsupported
        else RuntimeError("original context is unavailable")
    )

    def capture(*args: Any) -> None:
        raise failure

    async def is_pending(question_id: str) -> bool:
        assert question_id == "exact-original-grant"
        return pending

    monkeypatch.setattr(sessions, "_question", capture)
    issuer = SimpleNamespace(
        _closing=False,
        _fail_unhandled_questions=False,
        _child_callback_state=lambda *args: state,
        _observe_mcp=lambda *args: None,
        _observe_progress=lambda *args: None,
        _native=SimpleNamespace(_child_question_pending=is_pending),
    )
    event = {
        "subagent_id": "child",
        "subagent_incarnation": 1,
        "subagent_instance_id": 1,
        "msg": {
            "type": "exec_approval_request",
            "turn_id": "turn",
            "_sdk_question_id": "exact-original-grant",
        },
    }
    if unsupported:
        await sessions.Session._read_child_event(issuer, event)
        assert state.failure.result() is failure
        return
    if pending:
        with pytest.raises(RuntimeError) as raised:
            await sessions.Session._read_child_event(issuer, event)
        assert raised.value is failure
    else:
        await sessions.Session._read_child_event(issuer, event)
    assert not state.failure.done() and not state.terminal.done()


@pytest.mark.parametrize("replace", [False, True])
async def test_processor_factory_rejection_preserves_effective_routes(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    old = telemetry._ProcessorRoute(SimpleNamespace(on_trace_event=lambda event: None))
    monkeypatch.setattr(telemetry, "_processors", {"existing": old})
    monkeypatch.setattr(telemetry, "_active_processor_ids", {"existing"})
    monkeypatch.setattr(telemetry, "_processor_serial", 7)
    native.set_trace_routes(json.dumps({"processor_ids": ["existing"], "replace": False}))
    before = list(native.routes)
    loop = asyncio.get_running_loop()
    original = loop.get_task_factory()

    def reject(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("pump factory rejected")

    loop.set_task_factory(reject)
    try:
        with pytest.raises(RuntimeError, match="pump factory rejected"):
            processor = SimpleNamespace(on_trace_event=lambda event: None)
            if replace:
                telemetry.set_trace_processors([processor])
            else:
                telemetry.add_trace_processor(processor)
    finally:
        loop.set_task_factory(original)
    assert native.routes == before  # Replacement never removed the native exporter.
    assert telemetry._processors == {"existing": old}
    assert telemetry._active_processor_ids == {"existing"}
    assert telemetry._retired_routes == {} and telemetry._processor_serial == 7
    assert telemetry._processor_task is None


@pytest.mark.parametrize("eager", [False, True])
async def test_processor_native_rejection_retires_only_provisional_pump(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, eager: bool
) -> None:
    if eager and not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("Eager task factories require Python 3.12")
    old = telemetry._ProcessorRoute(SimpleNamespace(on_trace_event=lambda event: None))
    monkeypatch.setattr(telemetry, "_processors", {"existing": old})
    monkeypatch.setattr(telemetry, "_active_processor_ids", {"existing"})
    created = []
    drained = []
    loop = asyncio.get_running_loop()
    original = loop.get_task_factory()

    def factory(loop: Any, work: Any, **kwargs: Any) -> Any:
        task = (
            asyncio.eager_task_factory(loop, work, **kwargs)
            if eager
            else asyncio.Task(work, loop=loop, **kwargs)
        )
        created.append(task)
        return task

    async def drain(count: int) -> str:
        drained.append(count)
        raise AssertionError("Rejected registration started a native drain")

    def reject(encoded: str) -> int:
        assert created and not created[0].done()
        assert telemetry._processors == {"existing": old}
        raise RuntimeError("native registration rejected")

    monkeypatch.setattr(native, "set_trace_routes", reject)
    monkeypatch.setattr(native, "drain_trace_events", drain)
    loop.set_task_factory(factory)
    try:
        with pytest.raises(RuntimeError, match="native registration rejected"):
            telemetry.set_trace_processors([SimpleNamespace(on_trace_event=lambda event: None)])
    finally:
        loop.set_task_factory(original)
    await asyncio.gather(*created, return_exceptions=True)
    assert created[0].cancelled() and drained == []
    assert telemetry._processor_task is None
    assert telemetry._processors == {"existing": old}
    assert telemetry._active_processor_ids == {"existing"}
    assert telemetry._retired_routes == {} and telemetry._processor_serial == 0


async def test_eager_processor_pump_waits_for_committed_routes(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("Eager task factories require Python 3.12")
    seen = []
    old = telemetry._ProcessorRoute(SimpleNamespace(on_trace_event=lambda event: None))
    monkeypatch.setattr(telemetry, "_processors", {"existing": old})
    monkeypatch.setattr(telemetry, "_active_processor_ids", {"existing"})
    native.sequence = 1
    native.batches.put_nowait(
        {"events": [{"sequence": 1, "processor_ids": ["python-0"]}], "through_sequence": 1}
    )
    original_set = native.set_trace_routes

    def publish(encoded: str) -> int:
        assert seen == [] and native.acks == []
        assert telemetry._processors == {"existing": old}
        return original_set(encoded)

    monkeypatch.setattr(native, "set_trace_routes", publish)
    loop = asyncio.get_running_loop()
    original = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        telemetry.set_trace_processors(
            [SimpleNamespace(on_trace_event=lambda event: seen.append(event["sequence"]))]
        )
        assert seen == [] and native.acks == []
    finally:
        loop.set_task_factory(original)
    try:
        await _wait_for_ack(native, 1)
        assert seen == [1] and native.acks == [(1, 0)]
        assert telemetry._active_processor_ids == {"python-0"}
        assert set(telemetry._processors) == {"python-0"}
    finally:
        await _stop_processor_pump()
