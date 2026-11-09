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

from _agents_codex import telemetry as runtime_telemetry
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
    monkeypatch.setattr(runtime_telemetry, "_native_tracing", lambda: value)
    monkeypatch.setattr(runtime_telemetry, "_processors", {})
    monkeypatch.setattr(runtime_telemetry, "_processor_task", None)
    monkeypatch.setattr(runtime_telemetry, "_processor_starting", False)
    monkeypatch.setattr(runtime_telemetry, "_processor_closing", False)
    monkeypatch.setattr(runtime_telemetry, "_processor_serial", 0)
    monkeypatch.setattr(runtime_telemetry, "_active_processor_ids", set())
    monkeypatch.setattr(runtime_telemetry, "_retired_routes", {})
    monkeypatch.setattr(runtime_telemetry, "_processor_acknowledged", 0)
    monkeypatch.setattr(runtime_telemetry, "_engine_options", "{}")
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
            captured = runtime_telemetry._capture()
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
        session = runtime_telemetry._capture()
    assert telemetry.current_context() is None
    operation = runtime_telemetry._capture(telemetry.TraceOptions(disabled=True), session.context)
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
    retained_frames = 0
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
                Path(frame.f_code.co_filename) == Path(runtime_telemetry.__file__)
                or frame.f_code.co_filename == "<string>"
            ):
                retained_frames += 1
                for name, value in frame.f_locals.items():
                    retained = getattr(value, "headers", None) if name == "self" else value
                    assert not isinstance(retained, Mapping) or secret not in retained.values(), (
                        frame.f_code.co_name,
                        name,
                    )
            traceback = traceback.tb_next
    assert retained_frames > 0
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
    with runtime_telemetry._activate(borrowed):
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
            runtime_telemetry._capture(options)
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
            if Path(frame.f_code.co_filename).is_relative_to(
                Path(runtime_telemetry.__file__).parent
            ):
                assert not any(retains(value) for value in frame.f_locals.values()), (
                    frame.f_code.co_name
                )
            traceback = traceback.tb_next
        error = error.__cause__ or error.__context__
    assert credentials.headers["authorization"] == secret
    assert options.export_credentials is credentials
    assert runtime_telemetry._processor_task is None


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
            operation = runtime_telemetry._capture()
            assert operation.scope is not None and operation.scope.context.disabled
            operation.close()
            assert runtime_telemetry._processor_task is None

    route = runtime_telemetry._ProcessorRoute(Processor())
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
    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _ToolCatalog
    from agents import Agent
    from agents.session import Session

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
                runtime_telemetry._capture(options)
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
                if Path(frame.f_code.co_filename).is_relative_to(
                    Path(runtime_telemetry.__file__).parent
                ):
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
            runtime_telemetry._processors["registered"] = runtime_telemetry._ProcessorRoute(
                Processor()
            )
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
                assert runtime_telemetry._processor_task is None
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
    captured = runtime_telemetry._capture(telemetry.TraceOptions())
    captured.close(FalseyError())
    assert [outcome["status"] for _, outcome in native.ends] == ["error", "error"]


async def test_shutdown_rejects_processor_reentrancy_without_mutation(
    native: NativeTracing,
) -> None:
    token = runtime_telemetry._in_processor.set(True)
    try:
        with pytest.raises(RuntimeError, match="inside a trace processor"):
            await telemetry.shutdown(timeout_ms=20)
        assert not native.acks and runtime_telemetry._processor_task is None
    finally:
        runtime_telemetry._in_processor.reset(token)


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
    runtime_telemetry._processor_task = task
    calls: list[int] = []

    async def shutdown(budget: int) -> None:
        calls.append(budget)

    original = runtime_telemetry.importlib.import_module
    monkeypatch.setattr(native, "shutdown_telemetry", shutdown, raising=False)
    monkeypatch.setattr(
        runtime_telemetry.importlib,
        "import_module",
        lambda name, package=None: native if name == "agents._native" else original(name, package),
    )
    try:
        await telemetry.shutdown(timeout_ms=20)
        assert calls == [20] and task.cancelled()
        assert runtime_telemetry._processor_task is None and not runtime_telemetry._processors
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
    from _agents_codex.session import _call_callback

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
        assert runtime_telemetry._processor_task is not None
        runtime_telemetry._processor_task.cancel()
        await asyncio.gather(runtime_telemetry._processor_task, return_exceptions=True)
        for route in runtime_telemetry._processors.values():
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
            runtime_telemetry._ensure_processor_pump()

        asyncio.run(prior())
    original = runtime_telemetry._processor_task
    identifier = next(iter(runtime_telemetry._processors))
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
            assert runtime_telemetry._processor_task is original
            assert native.batches.qsize() == 1
            assert not drains and not received and not native.flushes and not native.acks
        finally:
            if original is not None and runtime_telemetry._processor_task is original:
                assert original.done()
                runtime_telemetry._processor_task = None
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

    session = object.__new__(Session)
    session._trace_context = None
    session._environment_lease = None
    session._closed = False
    cleanup_leases: list[str] = []
    files = [tmp_path / "instructions.md"] if with_instruction_files else []

    async def close() -> None:
        assert session._trace_context not in native.released
        cleanup_leases.append(session._trace_context)
        session._closed = True

    session.aclose = close

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
    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _ToolCatalog
    from agents import Agent
    from agents.session import Session, TurnStoppedError

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
    session = runtime_telemetry._capture(telemetry.TraceOptions(**policy))
    try:
        with telemetry.trace("different ambient") as ambient:
            operation = runtime_telemetry._capture(fallback=session.context)
            assert native.starts[-1]["parent"] == ambient.context._handle
            assert native.starts[-1]["restrictions_parents"] == [session.context]
            operation.close()
            with telemetry.trace("explicit parent") as explicit:
                operation = runtime_telemetry._capture(
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
    if runtime_telemetry._processor_task is not None:
        runtime_telemetry._processor_task.cancel()
        await asyncio.gather(runtime_telemetry._processor_task, return_exceptions=True)
    for route in runtime_telemetry._processors.values():
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
    assert old_id in runtime_telemetry._processors
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
        assert old_id not in runtime_telemetry._processors
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
    monkeypatch.setattr(runtime_telemetry, "_MAX_PROCESSOR_WORKERS", 1)

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
        while runtime_telemetry._worker_count:
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
        old_route = runtime_telemetry._processors[old_id]
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
            assert runtime_telemetry._worker_count == 1
        assert healthy == []
        assert native.acks == [(4, 4), (5, 1), (6, 1), (7, 1)]
        assert len(runtime_telemetry._processors) == 1
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
@pytest.mark.parametrize("cancel_phase", ["delivery", "continuation"])
async def test_manual_restore_reply_forwards_fresh_context_and_joins_admission(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
    scoped: bool,
    cancel_phase: str,
) -> None:
    from contextlib import nullcontext

    from pydantic import BaseModel

    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _Submission, _ToolCatalog
    from _agents_codex.turn import _root_turn
    from agents import FunctionTool
    from agents.session import Completed, Session

    class Args(BaseModel):
        value: str

    tool = FunctionTool(name="manual", description="synthetic", input_type=Args, output_type=str)
    entry = tool._capture(None)
    admitted = asyncio.Event()
    resume = asyncio.Event()
    delivering, durable = asyncio.Event(), asyncio.Event()
    deliveries = []
    calls: list[tuple[str, str | None]] = []

    class Thread:
        id = "restored-session"

        def _check_manual_tool(self, grant: Any) -> bool:
            return True

        async def _respond_manual_tool(self, grant: Any, output: str) -> bool:
            assert telemetry.current_context() is None
            delivering.set()
            await durable.wait()
            if captured.context is not None:
                assert captured.context not in native.released
            deliveries.append(output)
            return True

        async def _continue_manual(
            self, observation_submission_id: str, *, tracing_context: str | None = None
        ) -> str:
            calls.append((observation_submission_id, tracing_context))
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
            captured = runtime_telemetry._capture()
            state = _Submission(_prepare_spec(str), restored_pending=True, tracing=captured)
            handle = _root_turn(session, "original-turn", "fresh-submission", state.completion)
            state.handle = handle
            state.accepted.set_result(handle)
            session._active = state
            request = entry.request(
                object(), 0, 0, session, handle, "manual-call", Args(value="synthetic"), None
            )
            with telemetry.trace("unrelated response caller"):
                pending = asyncio.create_task(request.respond("accepted"))
            await delivering.wait()
            if cancel_phase == "delivery":
                pending.cancel()
                await asyncio.sleep(0)
                assert not pending.done() and calls == [] and deliveries == []
            durable.set()
            await admitted.wait()
            context = captured.context
            assert calls == [(handle.submission_id, context)]
            if scoped:
                assert context is not None
                assert json.loads(native.trace_span_context(context))["span_id"] != old
            if cancel_phase == "continuation":
                pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            if context is not None:
                assert context not in native.released
            resume.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert len(deliveries) == 1 and "accepted" in deliveries[0]
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


@pytest.mark.parametrize("started", [False, True])
async def test_failed_restore_opening_releases_unstarted_submission_after_native_cleanup(
    native: NativeTracing,
    started: bool,
) -> None:
    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _OpeningOwner, _Submission

    with telemetry.trace("fresh restore"):
        captured = runtime_telemetry._capture()
    context = captured.context
    assert context is not None
    state = _Submission(_prepare_spec(str), tracing=captured, restored_pending=True)
    state.task = asyncio.create_task(asyncio.Event().wait()) if started else None
    session = SimpleNamespace(_active=state, _reader=None, _mcp_reader=None, _interception=None)
    opening = _OpeningOwner()
    cleaned: list[bool] = []

    async def close_native() -> None:
        assert state.task is None or state.task.cancelled()
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
    old_route = runtime_telemetry._processors[identifier]
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
        assert identifier not in runtime_telemetry._processors
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
    route = runtime_telemetry._processors[identifier]
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
        assert (
            runtime_telemetry._processor_task is not None
            and not runtime_telemetry._processor_task.done()
        )
        await asyncio.sleep(0)

    original_import = runtime_telemetry.importlib.import_module

    def import_module(name: str, package: str | None = None) -> Any:
        return native if name == "agents._native" else original_import(name, package)

    monkeypatch.setattr(runtime_telemetry.importlib, "import_module", import_module)
    monkeypatch.setattr(native, "shutdown_telemetry", shutdown_native, raising=False)
    telemetry.add_trace_processor(Processor())
    identifier = native.routes[-1]["processor_ids"][0]
    route = runtime_telemetry._processors[identifier]
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
        assert runtime_telemetry._processors == {}
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
    route = runtime_telemetry._processors[identifiers[0]]
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
            assert runtime_telemetry._processor_task is not None
            asyncio.get_running_loop().call_soon(runtime_telemetry._processor_task.cancel)
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
    assert runtime_telemetry._processor_task is not None
    pump = runtime_telemetry._processor_task
    await entered.wait()
    if not completed_before_resume:
        pump.cancel()
        await asyncio.sleep(0)
        # Model asyncio.run cancelling every child Task as well as its pump.
        owner = next(
            task
            for task in asyncio.all_tasks()
            if isinstance(task, runtime_telemetry._ProcessorDrain)
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
    assert runtime_telemetry._processor_acknowledged == 2
    for route in runtime_telemetry._processors.values():
        route.close()


@pytest.mark.parametrize("exact", [False, True])
async def test_steering_retains_admitted_context_instead_of_callers_ambient(
    native: NativeTracing,
    monkeypatch: pytest.MonkeyPatch,
    exact: bool,
) -> None:
    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _Submission, _ToolCatalog
    from _agents_codex.turn import _root_turn
    from agents.session import Session

    with telemetry.trace("admitting workflow") as owner:
        captured = runtime_telemetry._capture()
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

    original_import = runtime_telemetry.importlib.import_module
    monkeypatch.setattr(
        runtime_telemetry.importlib,
        "import_module",
        lambda name, package=None: (
            native if name == "agents._native" else original_import(name, package)
        ),
    )
    monkeypatch.setattr(native, "shutdown_telemetry", shutdown_native, raising=False)
    telemetry.add_trace_processor(Processor())
    identifier = native.routes[-1]["processor_ids"][0]
    route = runtime_telemetry._processors[identifier]
    await native.batches.put(
        {
            "events": [{"sequence": 1, "processor_ids": [identifier]}],
            "through_sequence": 1,
        }
    )
    async with timeout(2):
        while not native.acks:
            await asyncio.sleep(0)
    pump = runtime_telemetry._processor_task
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
        assert runtime_telemetry._processor_task is None
        assert runtime_telemetry._processors == {}
        assert runtime_telemetry._active_processor_ids == set()
        assert runtime_telemetry._retired_routes == {}
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

    original_import = runtime_telemetry.importlib.import_module
    monkeypatch.setattr(
        runtime_telemetry.importlib,
        "import_module",
        lambda name, package=None: (
            native if name == "agents._native" else original_import(name, package)
        ),
    )
    monkeypatch.setattr(native, "shutdown_telemetry", unexpected_shutdown, raising=False)
    telemetry.add_trace_processor(Processor())
    pump = runtime_telemetry._processor_task
    routes = dict(runtime_telemetry._processors)
    try:
        with pytest.raises(ValueError, match="timeout_ms"):
            await telemetry.shutdown(timeout_ms=budget)
        assert runtime_telemetry._processor_task is pump
        assert runtime_telemetry._processors == routes
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

    original_import = runtime_telemetry.importlib.import_module
    monkeypatch.setattr(
        runtime_telemetry.importlib,
        "import_module",
        lambda name, package=None: (
            native if name == "agents._native" else original_import(name, package)
        ),
    )
    monkeypatch.setattr(native, "shutdown_telemetry", shutdown_native, raising=False)
    monkeypatch.setattr(native, "drain_trace_events", drain)
    telemetry.add_trace_processor(Processor())
    identifier = native.routes[-1]["processor_ids"][0]
    pump = runtime_telemetry._processor_task
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
        assert runtime_telemetry._processor_task is None
        assert runtime_telemetry._processors == {}
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

    record = runtime_telemetry._ProcessorDelivery({"sequence": 1})
    batch = runtime_telemetry._ProcessorBatch((record,))
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
    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _ToolCatalog
    from agents import Agent, Environment, NativeError
    from agents.session import Session

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
        assert requests[0]["observation_submission_id"] == turn.submission_id
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
            continuation_route = requests[1]["observation_submission_id"]
            assert isinstance(continuation_route, str) and continuation_route
            assert continuation_route != turn.submission_id
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
    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _ToolCatalog
    from agents.session import Session

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
    from _agents_codex.manual import _question
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
        with runtime_telemetry._owned_span("queued callback", "callback", context._handle):
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
    import _agents_codex.session as sessions

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
    old = runtime_telemetry._ProcessorRoute(SimpleNamespace(on_trace_event=lambda event: None))
    monkeypatch.setattr(runtime_telemetry, "_processors", {"existing": old})
    monkeypatch.setattr(runtime_telemetry, "_active_processor_ids", {"existing"})
    monkeypatch.setattr(runtime_telemetry, "_processor_serial", 7)
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
    assert runtime_telemetry._processors == {"existing": old}
    assert runtime_telemetry._active_processor_ids == {"existing"}
    assert runtime_telemetry._retired_routes == {} and runtime_telemetry._processor_serial == 7
    assert runtime_telemetry._processor_task is None


@pytest.mark.parametrize("eager", [False, True])
async def test_processor_native_rejection_retires_only_provisional_pump(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, eager: bool
) -> None:
    if eager and not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("Eager task factories require Python 3.12")
    old = runtime_telemetry._ProcessorRoute(SimpleNamespace(on_trace_event=lambda event: None))
    monkeypatch.setattr(runtime_telemetry, "_processors", {"existing": old})
    monkeypatch.setattr(runtime_telemetry, "_active_processor_ids", {"existing"})
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
        assert runtime_telemetry._processors == {"existing": old}
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
    assert runtime_telemetry._processor_task is None
    assert runtime_telemetry._processors == {"existing": old}
    assert runtime_telemetry._active_processor_ids == {"existing"}
    assert runtime_telemetry._retired_routes == {} and runtime_telemetry._processor_serial == 0


async def test_eager_processor_pump_waits_for_committed_routes(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("Eager task factories require Python 3.12")
    seen = []
    old = runtime_telemetry._ProcessorRoute(SimpleNamespace(on_trace_event=lambda event: None))
    monkeypatch.setattr(runtime_telemetry, "_processors", {"existing": old})
    monkeypatch.setattr(runtime_telemetry, "_active_processor_ids", {"existing"})
    native.sequence = 1
    native.batches.put_nowait(
        {"events": [{"sequence": 1, "processor_ids": ["python-0"]}], "through_sequence": 1}
    )
    original_set = native.set_trace_routes

    def publish(encoded: str) -> int:
        assert seen == [] and native.acks == []
        assert runtime_telemetry._processors == {"existing": old}
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
        assert runtime_telemetry._active_processor_ids == {"python-0"}
        assert set(runtime_telemetry._processors) == {"python-0"}
    finally:
        await _stop_processor_pump()


@pytest.mark.parametrize("register", [False, True])
def test_live_loops_share_one_processor_startup(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, register: bool
) -> None:
    import threading
    from time import monotonic

    first_factory = threading.Event()
    contender_reached = threading.Event()
    release_factory = threading.Event()
    stop = threading.Event()
    finished = [threading.Event(), threading.Event()]
    failures = []
    created = []
    owners = []
    seen = []
    lock = threading.RLock()

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "trace-contender":
                contender_reached.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    monkeypatch.setattr(runtime_telemetry, "_processor_lock", ObservedLock())
    processor = SimpleNamespace(on_trace_event=lambda event: seen.append(event["sequence"]))
    telemetry.add_trace_processor(processor)  # Synchronous registration has no pump.
    native.batches.put_nowait(
        {"events": [{"sequence": 1, "processor_ids": ["python-0"]}], "through_sequence": 1}
    )

    def serve(index: int) -> None:
        async def run() -> None:
            loop = asyncio.get_running_loop()

            def factory(loop: Any, work: Any, **kwargs: Any) -> Any:
                if work.cr_code is runtime_telemetry._pump_processors.__code__:
                    created.append(index)
                    if index == 0:
                        first_factory.set()
                        assert release_factory.wait(2)
                    else:
                        contender_reached.set()
                return asyncio.Task(work, loop=loop, **kwargs)

            loop.set_task_factory(factory)
            try:
                if index == 0 and register:
                    telemetry.add_trace_processor(processor)
                else:
                    with telemetry.trace("concurrent scope"):
                        pass
                owners.append(runtime_telemetry._processor_task)
                finished[index].set()
                await asyncio.to_thread(stop.wait, 3)
            finally:
                loop.set_task_factory(None)
                pumps = [
                    task
                    for task in asyncio.all_tasks()
                    if task.get_coro().cr_code is runtime_telemetry._pump_processors.__code__
                ]
                for task in pumps:
                    task.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)

        try:
            asyncio.run(run())
        except BaseException as error:
            failures.append(error)
            finished[index].set()

    threads = [
        threading.Thread(
            target=serve, args=(i,), name="trace-owner" if i == 0 else "trace-contender"
        )
        for i in range(2)
    ]
    try:
        threads[0].start()
        assert first_factory.wait(2)
        threads[1].start()
        assert contender_reached.wait(2)
        release_factory.set()
        assert all(event.wait(2) for event in finished)
        assert failures == []
        assert created == [0] and owners[0] is owners[1]
        # The retained owner remains live while both loops are alive.
        deadline = monotonic() + 2
        while not native.acks and monotonic() < deadline:
            stop.wait(0.005)
        assert native.acks == [(1, 0)] and seen == [1]
    finally:
        release_factory.set()
        stop.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(3)
                assert not thread.is_alive()
        for route in runtime_telemetry._processors.values():
            route.close()
    assert failures == []


async def test_reentrant_factory_reuses_startup_and_rejects_registration(
    native: NativeTracing,
) -> None:
    loop = asyncio.get_running_loop()
    original = loop.get_task_factory()
    created = []

    def factory(loop: Any, work: Any, **kwargs: Any) -> Any:
        if work.cr_code is runtime_telemetry._pump_processors.__code__:
            created.append(work)
            runtime_telemetry._ensure_processor_pump()
            with pytest.raises(RuntimeError, match="startup or shutdown"):
                telemetry.add_trace_processor(SimpleNamespace(on_trace_event=lambda event: None))
        return asyncio.Task(work, loop=loop, **kwargs)

    loop.set_task_factory(factory)
    try:
        telemetry.add_trace_processor(SimpleNamespace(on_trace_event=lambda event: None))
    finally:
        loop.set_task_factory(original)
    try:
        assert len(created) == 1 and len(native.routes) == 1
        assert runtime_telemetry._processor_serial == 1
    finally:
        await _stop_processor_pump()


async def test_shutdown_excludes_restart_while_joined_cleanup_is_pending(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped, release = asyncio.Event(), asyncio.Event()
    telemetry.add_trace_processor(SimpleNamespace(on_trace_event=lambda event: None))
    owner = runtime_telemetry._processor_task
    original_stop = runtime_telemetry._stop_processor_task

    async def stop(task: asyncio.Task[None]) -> None:
        await original_stop(task)
        stopped.set()
        await release.wait()

    async def shutdown(budget: int) -> None:
        pass

    monkeypatch.setattr(runtime_telemetry, "_stop_processor_task", stop)
    original_import = runtime_telemetry.importlib.import_module
    monkeypatch.setattr(native, "shutdown_telemetry", shutdown, raising=False)
    monkeypatch.setattr(
        runtime_telemetry.importlib,
        "import_module",
        lambda name, package=None: (
            native if name == "agents._native" else original_import(name, package)
        ),
    )
    retiring = asyncio.create_task(telemetry.shutdown(timeout_ms=20))
    try:
        await asyncio.wait_for(stopped.wait(), 1)
        assert owner is not None and owner.done()
        runtime_telemetry._ensure_processor_pump()
        assert runtime_telemetry._processor_task is owner
        with pytest.raises(RuntimeError, match="startup or shutdown"):
            telemetry.add_trace_processor(SimpleNamespace(on_trace_event=lambda event: None))
    finally:
        release.set()
        await retiring
    assert runtime_telemetry._processor_task is None and runtime_telemetry._processors == {}


async def test_foreign_pump_waits_for_native_route_publication(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    staged, reader_lookup, release = threading.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original_lock = threading.RLock()
    old = runtime_telemetry._ProcessorRoute(SimpleNamespace(on_trace_event=lambda event: None))
    monkeypatch.setattr(runtime_telemetry, "_processors", {"existing": old})
    monkeypatch.setattr(runtime_telemetry, "_active_processor_ids", {"existing"})
    seen = []
    failures = []

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is threading.main_thread() and staged.is_set():
                reader_lookup.set()
            original_lock.acquire()

        def __exit__(self, *args):
            original_lock.release()

    monkeypatch.setattr(runtime_telemetry, "_processor_lock", ObservedLock())
    runtime_telemetry._ensure_processor_pump()
    original_drain = native.drain_trace_events
    read_count = 0

    async def drain(count: int) -> str:
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            while not staged.is_set():
                await asyncio.sleep(0)
            return json.dumps(
                {"events": [{"sequence": 1, "processor_ids": ["python-0"]}], "through_sequence": 1}
            )
        return await original_drain(count)

    def publish(encoded: str) -> int:
        native.routes.append(json.loads(encoded))
        staged.set()
        # Model the GIL release of a custom/native implementation after route
        # acceptance but before Python has published its matching route object.
        assert release.wait(2)
        return 1

    monkeypatch.setattr(native, "drain_trace_events", drain)
    monkeypatch.setattr(native, "set_trace_routes", publish)

    def register() -> None:
        try:
            telemetry.add_trace_processor(
                SimpleNamespace(on_trace_event=lambda event: seen.append(event["sequence"]))
            )
        except BaseException as error:
            failures.append(error)

    # A coordinator releases native publication only after the existing reader
    # reaches the route lookup. This remains deterministic with the GIL released.
    def coordinate() -> None:
        assert reader_lookup.wait(2)
        release.set()

    writer = threading.Thread(target=register)
    coordinator = threading.Thread(target=coordinate)
    writer.start()
    coordinator.start()
    try:
        await asyncio.wait_for(_wait_for_ack(native, 1), 3)
        assert failures == [] and seen == [1] and native.acks == [(1, 0)]
        assert runtime_telemetry._processor_task is not None
        assert runtime_telemetry._processor_task.get_loop() is loop
    finally:
        release.set()
        await asyncio.to_thread(writer.join, 2)
        await asyncio.to_thread(coordinator.join, 2)
        assert not writer.is_alive() and not coordinator.is_alive()
        await _stop_processor_pump()


@pytest.mark.parametrize(
    ("outcome", "successor"),
    [("error", False), ("timeout", False), ("error", True), ("cancelled", False), ("ok", True)],
)
async def test_retained_cell_executor_span_records_its_own_outcome(
    native: NativeTracing, outcome: str, successor: bool
) -> None:
    from types import MethodType

    from _agents_codex.native_interception import _CellPolicyOwner
    from agents.session import NativeError, Session

    loop = asyncio.get_running_loop()
    original_turn, successor_turn = object(), object()
    original = SimpleNamespace(
        handle=original_turn,
        stopping=False,
        terminal=loop.create_future(),
        failure=loop.create_future(),
        policy_stop=None,
    )
    replacement = SimpleNamespace(
        handle=successor_turn,
        stopping=False,
        terminal=loop.create_future(),
        failure=loop.create_future(),
        policy_stop=None,
    )
    accepted = []
    live = True

    def resolve(nonce: int, encoded: str) -> bool:
        nonlocal live
        assert nonce == 41
        if not live:
            return False
        live = False
        accepted.append(json.loads(encoded))
        return True

    async def transform(context: Any, value: Any) -> str:
        assert context.submission is original_turn
        if successor:
            original.terminal.set_result(None)
            issuer._active = replacement
        if outcome == "ok":
            replacement.failure.set_result(NativeError("other owner's failure"))
            return "accepted result"
        if outcome == "timeout":
            await asyncio.Future()
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        raise ValueError("PRIVATE_CALLBACK_EXCEPTION")

    executor = SimpleNamespace(
        transform_arguments=None, transform_result=transform, policy_timeout=0.01, approval=None
    )
    owner = _CellPolicyOwner(
        1,
        "thread",
        "original-turn",
        "outer",
        "cell",
        original_turn,
        executor,
        None,
        object(),
    )
    issuer = SimpleNamespace(
        _active=original,
        _closing=False,
        _broken=False,
        _interception=SimpleNamespace(owns_cell=lambda item: item is owner),
        _native=SimpleNamespace(
            _check_native_interception=lambda nonce: live,
            _handoff_native_interception=lambda nonce: True,
            _resolve_native_interception=resolve,
            _expire_native_interception_result=lambda nonce: None,
        ),
    )
    issuer._invoke_executor_policy_body = MethodType(Session._invoke_executor_policy_body, issuer)
    with telemetry.trace("retained executor owner") as parent:
        request = {
            "tracing_context": parent.context._handle,
            "thread_id": "thread",
            "turn_id": "original-turn",
            "outer_call_id": "outer",
            "cell_id": "cell",
            "runtime_tool_call_id": "runtime",
            "call_id": None,
            "phase": "after",
            "route": "code_mode",
            "tool_kind": "function_json",
            "tool_name": {"name": "exec_command"},
            "result": "PRIVATE_NATIVE_RESULT",
        }
        pending = asyncio.create_task(
            Session._invoke_executor_policy(issuer, None, 41, request, cell_owner=owner)
        )
        await asyncio.wait_for(pending, 1)
    callback_id = next(
        i + 1 for i, span in enumerate(native.starts) if span["name"] == "executor callback"
    )
    [recorded] = [data for handle, data in native.ends if handle == f"span-{callback_id}"]
    expected = "error" if outcome == "timeout" else outcome
    assert recorded["status"] == expected
    assert recorded["category"] == (
        "callback_error"
        if expected == "error"
        else "cancelled"
        if expected == "cancelled"
        else None
    )
    assert "PRIVATE" not in json.dumps(native.ends)
    if successor:
        assert not original.failure.done()
        assert replacement.failure.done() == (outcome == "ok")
    else:
        assert original.failure.done()
        assert isinstance(
            original.failure.result(),
            asyncio.CancelledError if outcome == "cancelled" else NativeError,
        )
    if outcome == "ok":
        assert accepted == [{"action": "replace", "value": "accepted result"}]
    else:
        assert accepted == [{"action": "deny"}]


async def test_capture_releases_ambient_lease_when_task_factory_rejects(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch
) -> None:
    with telemetry.trace(
        "ambient credential owner",
        options=telemetry.TraceOptions(
            export_credentials=telemetry.TraceExportCredentials(
                headers={"authorization": "private-key"}
            )
        ),
    ) as parent:
        before = set(native.leases)
        monkeypatch.setattr(
            runtime_telemetry,
            "_processors",
            {
                "old": runtime_telemetry._ProcessorRoute(
                    SimpleNamespace(on_trace_event=lambda event: None)
                )
            },
        )
        loop = asyncio.get_running_loop()
        original = loop.get_task_factory()
        failure = RuntimeError("factory rejected capture")

        def reject(*args: Any, **kwargs: Any) -> Any:
            raise failure

        loop.set_task_factory(reject)
        try:
            with pytest.raises(RuntimeError) as raised:
                runtime_telemetry._capture()
        finally:
            loop.set_task_factory(original)
        assert raised.value is failure
        [captured] = set(native.leases) - before
        assert native.released.count(captured) == 1
        assert telemetry.current_context() is parent.context
        assert runtime_telemetry._processor_task is None


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_capture_rollback_closes_operation_and_preserves_primary_error(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, cleanup_error: bool
) -> None:
    primary = RuntimeError("pump startup failed")
    cleanup = RuntimeError("context cleanup failed")
    calls = 0
    retained = []
    original_release = native.release_trace_context

    def ensure() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return  # Operation admission succeeds before capture's pump check.
        retained.append(next(reversed(native.leases)))
        if cleanup_error:

            def release(context: str) -> None:
                original_release(context)
                raise cleanup

            monkeypatch.setattr(native, "release_trace_context", release)
        raise primary

    monkeypatch.setattr(runtime_telemetry, "_ensure_processor_pump", ensure)
    with pytest.raises(RuntimeError) as raised:
        runtime_telemetry._capture(telemetry.TraceOptions())
    assert raised.value is primary
    assert native.released.count(retained[0]) == 1
    assert [handle for handle, _ in native.ends] == ["span-1"]
    assert native.released.count("span-1") == 1
    assert telemetry.current_context() is None
    if cleanup_error:
        from _agents_codex._lifecycle import _error_contexts

        assert cleanup in _error_contexts(primary.__context__)


def test_capture_retain_failure_still_closes_operation_once(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = native.retain_trace_context

    def retain(handle: str) -> str:
        if handle.startswith("lease-"):
            raise RuntimeError("capture retain failed")
        return original(handle)

    monkeypatch.setattr(native, "retain_trace_context", retain)
    with pytest.raises(RuntimeError, match="capture retain failed"):
        runtime_telemetry._capture(telemetry.TraceOptions())
    assert [handle for handle, _ in native.ends] == ["span-1"]
    assert native.released.count("span-1") == 1
    assert telemetry.current_context() is None


@pytest.mark.parametrize("disabled", [False, True])
async def test_restored_manual_validation_uses_fresh_owner_without_saved_carrier(
    native: NativeTracing, disabled: bool
) -> None:
    from pydantic import BaseModel, model_validator

    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _Submission, _ToolCatalog
    from _agents_codex.turn import _root_turn
    from agents import FunctionTool
    from agents.session import Session

    seen = []

    class Arguments(BaseModel):
        value: str

        @model_validator(mode="after")
        def check_context(self) -> Arguments:
            context = telemetry.current_context()
            assert context is not None and context.disabled == disabled
            assert not context.include_sensitive_data
            parent = native.starts[-1]["parent"]
            assert native.leases[parent] == native.leases[captured.context]
            with telemetry.span("validator nested work") as child:
                assert child.context.disabled == disabled
            seen.append(context.span_id)
            return self

    entry = FunctionTool(
        name="manual", description="restore", input_type=Arguments, output_type=str
    )._capture(None)
    message = {
        "msg": {
            "turnId": "restored-turn",
            "callId": "call",
            "tool": "manual",
            "arguments": {"value": "synthetic"},
            "_sdk_python_tool": {"thread": "session", "generation": 0, "slot": 0},
            "_sdk_code_origin": None,
        }
    }

    class Thread:
        id = "session"

        def _pending_manual_tools(self):
            return [(1, object(), json.dumps(message))]

        def _check_manual_tool(self, grant):
            return True

    session = Session(
        None,
        Thread(),
        None,
        _ToolCatalog(0, (entry,)),
        (),
        None,
        _prepare_spec(str),
        "restored",
        _start_readers=False,
    )
    captured = runtime_telemetry._capture(
        telemetry.TraceOptions(disabled=disabled, include_sensitive_data=False)
    )
    state = _Submission(_prepare_spec(str), tracing=captured, restored_pending=True)
    state.handle = _root_turn(session, "restored-turn", "restored-submission", state.completion)
    session._active = state
    try:
        with telemetry.trace(
            "unrelated permissive caller",
            options=telemetry.TraceOptions(include_sensitive_data=True),
        ) as unrelated:
            assert session._validate_pending_manual(state)
            request = session._manual_request(state.handle.submission_id, set())
            assert request is not None and request._tracing_context == captured.context
            assert telemetry.current_context() is unrelated.context
        assert len(seen) == 1  # Opening validation is cached for later cursors.
        assert "tracing_context" not in message["msg"]
    finally:
        captured.close()


@pytest.mark.parametrize("rejected", ["initialize", "pump"])
async def test_configure_factory_rejection_precedes_native_admission_and_drops_credentials(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, rejected: str
) -> None:
    from tests.test_tracing_failure_cleanup import (
        _assert_no_exporter_credentials,
        _private_exporter,
    )

    admissions = []
    original_import = runtime_telemetry.importlib.import_module

    async def configure(encoded: str) -> None:
        admissions.append(True)
        assert len(admissions) == 1  # The rejected attempt left native unclaimed.

    monkeypatch.setattr(native, "configure_telemetry", configure, raising=False)
    monkeypatch.setattr(runtime_telemetry, "_engine_options", None)
    monkeypatch.setattr(
        runtime_telemetry.importlib,
        "import_module",
        lambda name, package=None: (
            native if name == "agents._native" else original_import(name, package)
        ),
    )
    loop = asyncio.get_running_loop()
    old_factory = loop.get_task_factory()
    rejected_work = []
    failure = RuntimeError("configuration task factory rejected")

    def factory(loop: Any, work: Any, **kwargs: Any) -> Any:
        if work.cr_code.co_name == (
            "initialize" if rejected == "initialize" else "_pump_processors"
        ):
            rejected_work.append(work)
            raise failure
        return asyncio.Task(work, loop=loop, **kwargs)

    processor = SimpleNamespace(on_trace_event=lambda event: None)
    exporter = _private_exporter()
    loop.set_task_factory(factory)
    try:
        with pytest.raises(RuntimeError) as raised:
            await telemetry.configure(
                traces=exporter, logs=exporter, metrics=exporter, processors=[processor]
            )
    finally:
        loop.set_task_factory(old_factory)
    assert raised.value is failure
    _assert_no_exporter_credentials(failure)
    assert rejected_work and rejected_work[0].cr_frame is None
    assert admissions == [] and native.routes == []
    assert runtime_telemetry._engine_options is None and runtime_telemetry._processor_task is None
    assert not runtime_telemetry._processor_starting and runtime_telemetry._processors == {}
    try:
        await telemetry.configure(processors=[processor])
        assert admissions == [True] and runtime_telemetry._engine_options is not None
        assert runtime_telemetry._active_processor_ids == {"python-0"}
    finally:
        await _stop_processor_pump()


@pytest.mark.parametrize("eager", [False, True])
async def test_configure_native_failure_cancels_only_its_gated_pump(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, eager: bool
) -> None:
    if eager and not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("Eager task factories require Python 3.12")
    original_import = runtime_telemetry.importlib.import_module
    created = []
    drains = []

    async def configure(encoded: str) -> None:
        assert created and not created[0].done()
        assert runtime_telemetry._processor_task is None and runtime_telemetry._processors == {}
        raise RuntimeError("native configuration rejected")

    async def drain(count: int) -> str:
        drains.append(count)
        raise AssertionError("Unconfigured pump reached native drain")

    monkeypatch.setattr(native, "configure_telemetry", configure, raising=False)
    monkeypatch.setattr(native, "drain_trace_events", drain)
    monkeypatch.setattr(runtime_telemetry, "_engine_options", None)
    monkeypatch.setattr(
        runtime_telemetry.importlib,
        "import_module",
        lambda name, package=None: (
            native if name == "agents._native" else original_import(name, package)
        ),
    )
    loop = asyncio.get_running_loop()
    old_factory = loop.get_task_factory()

    def factory(loop: Any, work: Any, **kwargs: Any) -> Any:
        task = (
            asyncio.eager_task_factory(loop, work, **kwargs)
            if eager
            else asyncio.Task(work, loop=loop, **kwargs)
        )
        if work.cr_code is runtime_telemetry._pump_processors.__code__:
            created.append(task)
        return task

    loop.set_task_factory(factory)
    try:
        with pytest.raises(RuntimeError, match="Native telemetry configuration failed"):
            await telemetry.configure(
                processors=[SimpleNamespace(on_trace_event=lambda event: None)]
            )
    finally:
        loop.set_task_factory(old_factory)
    await asyncio.gather(*created, return_exceptions=True)
    assert created[0].cancelled() and drains == []
    assert runtime_telemetry._engine_options is None and runtime_telemetry._processor_task is None
    assert not runtime_telemetry._processor_starting and runtime_telemetry._processors == {}


async def test_configure_reuses_reservation_through_joined_cancellation(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original_import = runtime_telemetry.importlib.import_module

    async def configure(encoded: str) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(native, "configure_telemetry", configure, raising=False)
    monkeypatch.setattr(runtime_telemetry, "_engine_options", None)
    monkeypatch.setattr(
        runtime_telemetry.importlib,
        "import_module",
        lambda name, package=None: (
            native if name == "agents._native" else original_import(name, package)
        ),
    )
    processor = SimpleNamespace(on_trace_event=lambda event: None)
    pending = asyncio.create_task(telemetry.configure(processors=[processor]))
    await entered.wait()
    assert runtime_telemetry._processor_task is None and runtime_telemetry._processor_starting
    with pytest.raises(RuntimeError, match="startup or shutdown"):
        telemetry.add_trace_processor(processor)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert runtime_telemetry._engine_options is not None
        assert runtime_telemetry._processor_task is not None
        assert len(native.routes) == 1 and not runtime_telemetry._processor_starting
    finally:
        await _stop_processor_pump()


@pytest.mark.parametrize(
    ("mode", "backlog"),
    [
        ("immediate", 1),
        ("cancelled", 1),
        ("compact", 1),
        ("queued", 1),
        ("queued", 1100),
        ("continuation", 1),
    ],
)
async def test_execution_factory_rejection_releases_owner_and_allows_progress(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, mode: str, backlog: int
) -> None:
    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _ToolCatalog
    from agents import Agent
    from agents.session import Session

    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    rejected_ids = set()
    rejected_states = []
    rejected_tokens = []
    rejected_work = []

    class FalseyFailure(RuntimeError):
        def __bool__(self) -> bool:
            return False

    failure = (
        asyncio.CancelledError("factory cancelled")
        if mode == "cancelled"
        else FalseyFailure("factory rejected execute")
    )

    class Thread:
        id = "session"

        def _select_steps(self, *args: Any) -> None:
            pass

        async def _submit(self, encoded: str) -> str:
            requests.append(json.loads(encoded))
            if mode == "queued" and len(requests) == 1:
                entered.set()
                await release.wait()
            state = session._active
            assert state is not None
            if mode == "continuation" and len(requests) == 1:
                state.unsampled_steering = True
            state.terminal.set_result(
                {"type": "turn_complete", "last_agent_message": f"reply-{len(requests)}"}
            )
            return json.dumps({"status": "started", "turn_id": f"turn-{len(requests)}"})

        async def _compact(self, *args: Any) -> str:
            state = session._active
            assert state is not None
            state.terminal.set_result({"type": "turn_complete"})
            return json.dumps({"status": "started", "turn_id": "compact-turn"})

        async def call(self, operation: str) -> str:
            assert operation == "token_usage_info"
            return "null"

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
    session._opening_agent = Agent(instructions="scripted")
    loop = asyncio.get_running_loop()
    original_factory = loop.get_task_factory()

    def factory(loop: Any, work: Any, **kwargs: Any) -> Any:
        if work.cr_code is Session._execute.__code__:
            state = work.cr_frame.f_locals["state"]
            reject = (
                state.issued is not None and state.issued.submission_id in rejected_ids
                if mode == "queued"
                else state.continued_from is not None
                if mode == "continuation"
                else not rejected_states
            )
            if reject:
                rejected_states.append(state)
                assert state.tracing is not None
                rejected_tokens.append(state.tracing.context)
                rejected_work.append(work)
                raise failure
        return asyncio.Task(work, loop=loop, **kwargs)

    try:
        with telemetry.trace("execution lifetime"):
            if mode == "queued":
                first = await session.submit("first")
                await entered.wait()
                failed = [await session.submit("rejected", if_busy="queue") for _ in range(backlog)]
                rejected_ids.update(turn.submission_id for turn in failed)
                later = await session.submit("later", if_busy="queue")
                loop.set_task_factory(factory)
                release.set()
                assert (await asyncio.wait_for(first.get_result(), 5)).final_output == "reply-1"
                for turn in failed:
                    with pytest.raises(FalseyFailure) as raised:
                        await turn.get_result()
                    assert raised.value is failure
                assert (await later.get_result()).final_output == "reply-2"
                assert (await first.get_result()).final_output == "reply-1"
                assert len(requests) == 2
            elif mode == "continuation":
                loop.set_task_factory(factory)
                first = await session.submit("steered task")
                with pytest.raises(FalseyFailure) as raised:
                    await asyncio.wait_for(first.get_result(), 2)
                assert raised.value is failure
                assert len(requests) == 1 and session.active_turn is None
                later = await session.submit("retry")
                assert (await later.get_result()).final_output == "reply-2"
            else:
                loop.set_task_factory(factory)
                with pytest.raises(type(failure)) as raised:
                    if mode == "compact":
                        await session.compact()
                    else:
                        await session.submit("reject first")
                assert raised.value is failure
                assert session.active_turn is None and requests == []
                if mode == "compact":
                    await session.compact()
                later = await session.submit("retry")
                assert (await later.get_result()).final_output == "reply-1"
            assert session.active_turn is None and not session._pending and not session._broken
            assert len(rejected_states) == backlog
            for state, token, work in zip(
                rejected_states, rejected_tokens, rejected_work, strict=True
            ):
                assert state.task is None and state.tracing is None
                assert state.accepted.done() and state.completion.done()
                assert state.completion.exception() is failure
                assert token is not None and native.released.count(token) == 1
                assert work.cr_frame is None
    finally:
        loop.set_task_factory(original_factory)
        release.set()
        session._reader.cancel()
        await asyncio.gather(session._reader, return_exceptions=True)


@pytest.mark.parametrize("route", ["environment", "cell", "mcp", "collaboration", "child"])
@pytest.mark.parametrize("policy", ["disabled", "metadata", "legacy"])
async def test_interception_reader_keeps_native_context_private_through_dispatch(
    native: NativeTracing, tmp_path: Path, route: str, policy: str
) -> None:
    from dataclasses import asdict
    from types import MethodType

    from _agents_codex.native_interception import _CellPolicyOwner, _NativeInterception
    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _ChildCallbacks, _Submission
    from _agents_codex.tools import _ToolCatalog
    from agents import Environment, HttpMCP, NativeAgents
    from agents.native_interception import Allow, NativeToolCall
    from agents.session import Session

    entered, finish, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observations = []
    resolved = []
    release_calls = []
    live = True
    read_count = 0

    async def callback(context: Any, value: Any) -> Any:
        current = telemetry.current_context()
        if policy == "legacy":
            assert current is None
        else:
            assert current is not None and current.disabled == (policy == "disabled")
            assert not current.include_sensitive_data
            with telemetry.span(
                "interception nested work", options=telemetry.TraceOptions(disabled=False)
            ) as nested:
                assert nested.context.disabled == current.disabled
                assert not nested.context.include_sensitive_data
            observations.append((current.trace_id, current.disabled))
        if isinstance(value, NativeToolCall):
            assert "tracing_context" not in asdict(value)
            assert not hasattr(value, "tracing_context")
        entered.set()
        await finish.wait()
        return True if route == "collaboration" else Allow()

    executor = Environment.local(cwd=tmp_path, tools=["exec_command"], transform_arguments=callback)
    turn = SimpleNamespace(turn_id="turn", child_id=None, instance_id=None)
    root = _Submission(_prepare_spec(str))
    root.handle = turn
    root.accepted.set_result(turn)
    child = _ChildCallbacks(SimpleNamespace(turn_id="turn", child_id="child", instance_id=1))
    request = {
        "thread_id": "child" if route == "child" else "session",
        "turn_id": "turn",
        "call_id": "call",
        "tool_name": {"name": "exec_command", "namespace": None},
        "phase": "before",
        "route": "direct",
        "boundary": "pre_dispatch",
        "tool_kind": "function_json",
        "input": "{}",
        "result": None,
        "instance": 1,
        "outer_call_id": None,
        "cell_id": None,
        "runtime_tool_call_id": None,
    }
    if route == "mcp":
        request["tool_name"] = {"name": "search", "namespace": "mcp__records"}
        request["route"] = "mcp"
    elif route == "collaboration":
        request["tool_name"] = {"name": "spawn_agent", "namespace": "collaboration"}
    elif route == "cell":
        request.update(
            route="code_mode",
            outer_call_id="outer",
            cell_id="cell",
            runtime_tool_call_id="nested",
            cell_owner_id=7,
        )
    if policy != "legacy":
        with telemetry.trace(
            "native original owner",
            options=telemetry.TraceOptions(
                disabled=policy == "disabled", include_sensitive_data=False
            ),
        ) as original:
            request["tracing_context"] = original.context._handle

    async def next_notice() -> str:
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            notice = {"nonce": 41, "request": request}
            if route == "mcp":
                notice["generation"] = 0
            return json.dumps(notice)
        await released.wait()
        return "null"

    def resolve(nonce: int, encoded: str) -> bool:
        nonlocal live
        if live:
            resolved.append(json.loads(encoded))
            live = False
            return True
        return False

    def release(nonce: int) -> None:
        release_calls.append(nonce)
        released.set()

    issuer = SimpleNamespace(
        id="session",
        _active=root,
        _context=None,
        _agent=object(),
        _closing=False,
        _broken=False,
        _executor=executor if route in ("environment", "cell", "child") else None,
        _native_agents=NativeAgents(approval=callback)
        if route == "collaboration"
        else NativeAgents(),
        _mcp_tools=(
            HttpMCP(name="records", url="http://unused.invalid", tools=["search"], before=callback),
        )
        if route == "mcp"
        else (),
        _child_callback_state=lambda *args: child,
        _native=SimpleNamespace(
            _next_native_interception=next_notice,
            _check_native_interception=lambda nonce: live,
            _handoff_native_interception=lambda nonce: True,
            _resolve_native_interception=resolve,
            _release_native_interception=release,
            _revoke_native_interception=lambda: None,
            _finish_native_interception_reader=lambda: None,
        ),
    )
    if route == "mcp":
        # Match the captured source carried by actual native MCP notices.
        issuer._profiles = {}
        issuer._opening_agent = issuer._agent
        issuer._profile_catalogs = {(None, 0): _ToolCatalog(0, ())}
        issuer._catalog_bindings = {(None, 0): issuer._mcp_tools}
        for name in (
            "_catalog_for_message",
            "_bindings_for_message",
            "_agent_for_message",
            "_agent_for_profile",
        ):
            setattr(issuer, name, MethodType(getattr(Session, name), issuer))
    issuer._invoke_executor_policy = MethodType(Session._invoke_executor_policy, issuer)
    issuer._invoke_executor_policy_body = MethodType(Session._invoke_executor_policy_body, issuer)
    interception = _NativeInterception(issuer)
    issuer._interception = interception
    if route == "cell":
        interception.cell_owners[7] = _CellPolicyOwner(
            7, "session", "turn", "outer", "cell", turn, executor, None, issuer._agent
        )
    with telemetry.trace(
        "unrelated reader", options=telemetry.TraceOptions(include_sensitive_data=True)
    ) as unrelated:
        before = set(native.leases)
        reader = asyncio.create_task(interception._read())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not released.is_set()
            assert not issuer._broken and not root.failure.done() and not child.failure.done()
            if policy != "legacy":
                held = set(native.leases) - before
                assert any(token not in native.released for token in held)
            finish.set()
            await asyncio.wait_for(reader, 1)
            assert telemetry.current_context() is unrelated.context
        finally:
            finish.set()
            reader.cancel()
            await asyncio.gather(reader, *interception.tasks.values(), return_exceptions=True)
    # Let the loop retire canceled policy timers and their context snapshots.
    await asyncio.sleep(0)
    gc.collect()
    assert resolved == [{"action": "continue"}] and release_calls == [41]
    assert interception.tasks == {} and not issuer._broken
    assert observations == ([] if policy == "legacy" else [("a" * 32, policy == "disabled")])
    assert all(token in native.released for token in set(native.leases) - before), [
        (token, native.leases[token])
        for token in set(native.leases) - before
        if token not in native.released
    ]


@pytest.mark.parametrize(
    "expiry", ["before_ingress", "during_retain", "before_dispatch", "unexpected"]
)
async def test_interception_reader_skips_only_revoked_contexts(
    native: NativeTracing, monkeypatch: pytest.MonkeyPatch, expiry: str
) -> None:
    from _agents_codex.native_interception import _NativeInterception
    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import _Submission
    from agents import NativeAgents

    called = []
    releases = []
    revoked = []
    done = asyncio.Event()
    live = expiry != "before_ingress"
    reads = 0
    retentions = []
    original_retain = native.retain_trace_context
    with telemetry.trace("owned original") as owner:
        original_token = owner.context._handle

    def retain(handle: str) -> str:
        nonlocal live
        retentions.append(handle)
        if expiry in ("during_retain", "unexpected"):
            if expiry == "during_retain":
                live = False
            raise RuntimeError("Native context unavailable")
        return original_retain(handle)

    monkeypatch.setattr(native, "retain_trace_context", retain)
    message = {
        "nonce": 41,
        "request": {
            "thread_id": "session",
            "turn_id": "turn",
            "call_id": "call",
            "tool_name": {"name": "spawn_agent", "namespace": "collaboration"},
            "phase": "before",
            "route": "direct",
            "boundary": "pre_dispatch",
            "tool_kind": "function_json",
            "input": "{}",
            "result": None,
            "tracing_context": original_token,
        },
    }

    async def next_notice() -> str:
        nonlocal reads
        reads += 1
        if reads == 1:
            return json.dumps(message)
        if expiry == "before_dispatch":
            await done.wait()
        return "null"

    def release(nonce: int) -> None:
        releases.append(nonce)
        done.set()

    def create(work: Any) -> asyncio.Task[Any]:
        nonlocal live
        live = False
        return asyncio.create_task(work)

    if expiry == "before_dispatch":
        monkeypatch.setattr(runtime_telemetry, "_create_task", create)
    state = _Submission(_prepare_spec(str))
    state.handle = SimpleNamespace(turn_id="turn")
    state.accepted.set_result(state.handle)
    issuer = SimpleNamespace(
        id="session",
        _active=state,
        _context=None,
        _closing=False,
        _broken=False,
        _executor=None,
        _mcp_tools=(),
        _native_agents=NativeAgents(approval=lambda *args: called.append(True) or True),
        _native=SimpleNamespace(
            _next_native_interception=next_notice,
            _check_native_interception=lambda nonce: live,
            _resolve_native_interception=lambda *args: False,
            _release_native_interception=release,
            _revoke_native_interception=lambda: revoked.append(True),
            _finish_native_interception_reader=lambda: None,
        ),
    )
    interception = _NativeInterception(issuer)
    before = set(native.leases)
    await asyncio.wait_for(interception._read(), 1)
    await asyncio.sleep(0)
    gc.collect()
    assert called == [] and releases == [41]
    assert issuer._broken == (expiry == "unexpected")
    assert state.failure.done() == (expiry == "unexpected")
    assert revoked == ([True] if expiry == "unexpected" else [])
    if expiry == "before_dispatch":
        assert len(retentions) == 2 and retentions[0] == original_token
        assert native.leases[retentions[1]] == native.leases[original_token]
    else:
        assert retentions == ([] if expiry == "before_ingress" else [original_token])
    assert all(token in native.released for token in set(native.leases) - before)
