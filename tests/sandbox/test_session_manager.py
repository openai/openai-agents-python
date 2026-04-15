from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from agents.sandbox.manifest import Manifest
from agents.sandbox.runtime_session_manager import SandboxRuntimeSessionManager
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxSession,
    UnixLocalSandboxSessionState,
)
from agents.sandbox.session import (
    CallbackSink,
    EventPayloadPolicy,
    Instrumentation,
    SandboxSessionEvent,
    SandboxSessionFinishEvent,
)
from agents.sandbox.session.sinks import ChainedSink, EventSink
from agents.sandbox.snapshot import LocalSnapshot, LocalSnapshotSpec, NoopSnapshotSpec


class _EventSink(EventSink):
    def __init__(self, *, mode: str, on_error: str = "raise") -> None:
        self.mode = mode  # type: ignore[assignment]
        self.on_error = on_error  # type: ignore[assignment]
        self.payload_policy = None

    async def handle(self, event: SandboxSessionEvent) -> None:  # pragma: no cover
        _ = event
        raise NotImplementedError


def _build_session(tmp_path: Path) -> UnixLocalSandboxSession:
    state = UnixLocalSandboxSessionState(
        manifest=Manifest(root=str(tmp_path / "workspace")),
        snapshot=LocalSnapshot(id="x", base_path=tmp_path),
    )
    return UnixLocalSandboxSession.from_state(state)


@pytest.mark.asyncio
async def test_instrumentation_per_op_policy_overrides_default(tmp_path: Path) -> None:
    events: list[SandboxSessionEvent] = []
    session = _build_session(tmp_path)
    sink = CallbackSink(lambda event, _session: events.append(event), mode="sync")
    sink.bind(session)
    instrumentation = Instrumentation(
        sinks=[sink],
        payload_policy=EventPayloadPolicy(include_exec_output=False),
        payload_policy_by_op={"exec": EventPayloadPolicy(include_exec_output=True)},
    )

    event = SandboxSessionFinishEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="exec",
        span_id="span_exec",
        ok=True,
        duration_ms=0.0,
    )
    event.stdout_bytes = b"hello"
    event.stderr_bytes = b""

    await instrumentation.emit(event)

    assert isinstance(events[0], SandboxSessionFinishEvent)
    assert events[0].stdout == "hello"


@pytest.mark.asyncio
async def test_instrumentation_per_sink_policy_overrides_per_op(tmp_path: Path) -> None:
    first: list[SandboxSessionEvent] = []
    second: list[SandboxSessionEvent] = []
    session = _build_session(tmp_path)
    sink_a = CallbackSink(lambda event, _session: first.append(event), mode="sync")
    sink_b = CallbackSink(
        lambda event, _session: second.append(event),
        mode="sync",
        payload_policy=EventPayloadPolicy(include_exec_output=True),
    )
    sink_a.bind(session)
    sink_b.bind(session)

    instrumentation = Instrumentation(
        sinks=[sink_a, sink_b],
        payload_policy=EventPayloadPolicy(include_exec_output=False),
        payload_policy_by_op={"exec": EventPayloadPolicy(include_exec_output=False)},
    )

    event = SandboxSessionFinishEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="exec",
        span_id="span_exec",
        ok=True,
        duration_ms=0.0,
    )
    event.stdout_bytes = b"hello"
    event.stderr_bytes = b""

    await instrumentation.emit(event)

    assert isinstance(first[0], SandboxSessionFinishEvent)
    assert isinstance(second[0], SandboxSessionFinishEvent)
    assert first[0].stdout is None
    assert second[0].stdout == "hello"


@pytest.mark.asyncio
async def test_instrumentation_redacts_raw_exec_bytes_when_output_disabled(
    tmp_path: Path,
) -> None:
    events: list[SandboxSessionEvent] = []
    session = _build_session(tmp_path)
    sink = CallbackSink(lambda event, _session: events.append(event), mode="sync")
    sink.bind(session)
    instrumentation = Instrumentation(
        sinks=[sink],
        payload_policy=EventPayloadPolicy(include_exec_output=False),
    )

    event = SandboxSessionFinishEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="exec",
        span_id="span_exec",
        ok=True,
        duration_ms=0.0,
    )
    event.stdout_bytes = b"secret"
    event.stderr_bytes = b"secret2"

    await instrumentation.emit(event)

    assert isinstance(events[0], SandboxSessionFinishEvent)
    assert events[0].stdout_bytes is None
    assert events[0].stderr_bytes is None


@pytest.mark.asyncio
async def test_chained_sink_preserves_completion_order_across_modes() -> None:
    completed = asyncio.Event()

    class SlowBestEffortSink(_EventSink):
        async def handle(self, event: SandboxSessionEvent) -> None:
            _ = event
            await asyncio.sleep(0)
            completed.set()

    class AssertAfterSink(_EventSink):
        async def handle(self, event: SandboxSessionEvent) -> None:
            _ = event
            assert completed.is_set(), "later sink ran before earlier sink completed"

    sink_a = SlowBestEffortSink(mode="best_effort", on_error="raise")
    sink_b = AssertAfterSink(mode="sync", on_error="raise")
    instrumentation = Instrumentation(sinks=[ChainedSink(sink_a, sink_b)])

    event = SandboxSessionFinishEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="running",
        span_id="span_running",
        ok=True,
        duration_ms=0.0,
    )
    await instrumentation.emit(event)


@pytest.mark.asyncio
async def test_async_sink_raise_propagates_to_emit() -> None:
    class _FailingAsyncSink(_EventSink):
        async def handle(self, event: SandboxSessionEvent) -> None:
            _ = event
            await asyncio.sleep(0)
            raise RuntimeError("boom")

    instrumentation = Instrumentation(sinks=[_FailingAsyncSink(mode="async", on_error="raise")])
    event = SandboxSessionFinishEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="running",
        span_id="span_running",
        ok=True,
        duration_ms=0.0,
    )

    with pytest.raises(RuntimeError, match="boom"):
        await instrumentation.emit(event)


def test_session_manager_uses_custom_snapshot_spec_without_resolving_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def _unexpected_default_resolution() -> LocalSnapshotSpec:
        nonlocal called
        called = True
        raise AssertionError("default snapshot resolution should not run")

    monkeypatch.setattr(
        "agents.sandbox.runtime_session_manager.resolve_default_local_snapshot_spec",
        _unexpected_default_resolution,
    )

    custom = LocalSnapshotSpec(base_path=Path("/tmp/custom-sandbox-snapshots"))
    resolved = SandboxRuntimeSessionManager._resolve_snapshot_spec(custom)

    assert resolved is custom
    assert called is False


def test_session_manager_falls_back_to_noop_when_default_snapshot_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_os_error() -> LocalSnapshotSpec:
        raise OSError("read-only home")

    monkeypatch.setattr(
        "agents.sandbox.runtime_session_manager.resolve_default_local_snapshot_spec",
        _raise_os_error,
    )

    resolved = SandboxRuntimeSessionManager._resolve_snapshot_spec(None)

    assert isinstance(resolved, NoopSnapshotSpec)
