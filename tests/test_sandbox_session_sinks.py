from __future__ import annotations

import asyncio
import io
import json
import tarfile
import uuid
from pathlib import Path

import pytest

from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxSession,
    UnixLocalSandboxSessionState,
)
from agents.sandbox.session import (
    CallbackSink,
    ChainedSink,
    EventPayloadPolicy,
    Instrumentation,
    JsonlOutboxSink,
    SandboxSession,
    UCEvent,
    UCFinishEvent,
    UCStartEvent,
    WorkspaceJsonlSink,
)
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.snapshot import LocalSnapshot


def _build_unix_local_session(tmp_path: Path) -> UnixLocalSandboxSession:
    workspace = tmp_path / "workspace"
    snapshot = LocalSnapshot(id=str(uuid.uuid4()), base_path=tmp_path)
    state = UnixLocalSandboxSessionState(
        manifest=Manifest(root=str(workspace)),
        snapshot=snapshot,
    )
    return UnixLocalSandboxSession.from_state(state)


@pytest.mark.asyncio
async def test_sandbox_session_exec_emits_stdout_when_enabled(tmp_path: Path) -> None:
    events: list[UCEvent] = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda e, _sess: events.append(e), mode="sync")],
        payload_policy=EventPayloadPolicy(include_exec_output=True),
    )

    inner = _build_unix_local_session(tmp_path)
    async with SandboxSession(inner, instrumentation=instrumentation) as session:
        result = await session.exec("echo hi")
        assert result.ok()

    exec_finish = [event for event in events if event.op == "exec" and event.phase == "finish"][0]
    assert isinstance(exec_finish, UCFinishEvent)
    assert exec_finish.stdout is not None
    assert "hi" in exec_finish.stdout


@pytest.mark.asyncio
async def test_sandbox_session_write_does_not_include_bytes_when_disabled(
    tmp_path: Path,
) -> None:
    events: list[UCEvent] = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda e, _sess: events.append(e), mode="sync")],
        payload_policy=EventPayloadPolicy(include_write_len=False),
    )

    inner = _build_unix_local_session(tmp_path)
    async with SandboxSession(inner, instrumentation=instrumentation) as session:
        await session.write(Path("x.txt"), io.BytesIO(b"hello"))

    write_start = [event for event in events if event.op == "write" and event.phase == "start"][0]
    assert "bytes" not in write_start.data


@pytest.mark.asyncio
async def test_jsonl_outbox_sink_appends_one_line_per_event(tmp_path: Path) -> None:
    outbox = tmp_path / "events.jsonl"
    sink = JsonlOutboxSink(outbox, mode="sync", on_error="raise")

    start_event = UCStartEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="write",
        span_id=uuid.uuid4(),
    )
    finish_event = UCFinishEvent(
        session_id=start_event.session_id,
        seq=2,
        op="write",
        span_id=start_event.span_id,
        ok=True,
        duration_ms=0.0,
    )

    await sink.handle(start_event)
    await sink.handle(finish_event)

    lines = outbox.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["phase"] == "start"
    assert json.loads(lines[1])["phase"] == "finish"


@pytest.mark.asyncio
async def test_chained_sink_runs_in_order(tmp_path: Path) -> None:
    outbox = tmp_path / "events.jsonl"
    seen: list[int] = []

    def _callback(_event: UCEvent, _session: BaseSandboxSession) -> None:
        seen.append(len(outbox.read_text(encoding="utf-8").splitlines()))

    inner = _build_unix_local_session(tmp_path)
    callback_sink = CallbackSink(_callback, mode="sync")
    callback_sink.bind(inner)

    instrumentation = Instrumentation(
        sinks=[
            ChainedSink(
                JsonlOutboxSink(outbox, mode="sync", on_error="raise"),
                callback_sink,
            )
        ]
    )

    start_event = UCStartEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="write",
        span_id=uuid.uuid4(),
    )
    finish_event = UCFinishEvent(
        session_id=start_event.session_id,
        seq=2,
        op="write",
        span_id=start_event.span_id,
        ok=True,
        duration_ms=0.0,
    )

    await instrumentation.emit(start_event)
    await instrumentation.emit(finish_event)

    assert seen == [1, 2]


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_writes_into_workspace_and_persists(tmp_path: Path) -> None:
    inner = _build_unix_local_session(tmp_path)
    instrumentation = Instrumentation(
        sinks=[WorkspaceJsonlSink(mode="sync", on_error="raise", ephemeral=False)]
    )
    wrapped = SandboxSession(inner, instrumentation=instrumentation)

    async with wrapped as session:
        await session.exec("echo hi")

    outbox_stream = await inner.read(Path(f"logs/events-{inner.state.session_id}.jsonl"))
    lines = outbox_stream.read().decode("utf-8").splitlines()
    assert any(json.loads(line)["op"] == "exec" for line in lines)

    snapshot_path = tmp_path / f"{inner.state.snapshot.id}.tar"
    with tarfile.open(snapshot_path, mode="r:*") as tar:
        names = [member.name for member in tar.getmembers()]
        assert any(f"logs/events-{inner.state.session_id}.jsonl" in name for name in names)


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_supports_session_id_template(tmp_path: Path) -> None:
    inner = _build_unix_local_session(tmp_path)
    relpath = Path("logs/events-{session_id}.jsonl")
    instrumentation = Instrumentation(
        sinks=[
            WorkspaceJsonlSink(
                mode="sync",
                on_error="raise",
                ephemeral=False,
                workspace_relpath=relpath,
            )
        ]
    )
    wrapped = SandboxSession(inner, instrumentation=instrumentation)

    async with wrapped as session:
        await session.exec("echo hi")

    expected_path = Path(f"logs/events-{inner.state.session_id}.jsonl")
    outbox_stream = await inner.read(expected_path)
    lines = outbox_stream.read().decode("utf-8").splitlines()
    assert any(json.loads(line)["op"] == "exec" for line in lines)


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_flushes_on_stop_when_flush_every_gt_one(
    tmp_path: Path,
) -> None:
    inner = _build_unix_local_session(tmp_path)
    instrumentation = Instrumentation(
        sinks=[
            WorkspaceJsonlSink(
                mode="sync",
                on_error="raise",
                ephemeral=False,
                flush_every=10,
            )
        ]
    )
    wrapped = SandboxSession(inner, instrumentation=instrumentation)

    async with wrapped as session:
        await session.exec("echo hi")

    outbox_stream = await inner.read(Path(f"logs/events-{inner.state.session_id}.jsonl"))
    lines = outbox_stream.read().decode("utf-8").splitlines()
    assert lines

    snapshot_path = tmp_path / f"{inner.state.snapshot.id}.tar"
    with tarfile.open(snapshot_path, mode="r:*") as tar:
        names = [member.name for member in tar.getmembers()]
        assert any(f"logs/events-{inner.state.session_id}.jsonl" in name for name in names)


@pytest.mark.asyncio
async def test_callback_sink_receives_bound_inner_session(tmp_path: Path) -> None:
    inner = _build_unix_local_session(tmp_path)
    seen: list[tuple[str, BaseSandboxSession]] = []

    def _callback(event: UCEvent, session: BaseSandboxSession) -> None:
        seen.append((event.op, session))

    instrumentation = Instrumentation(sinks=[CallbackSink(_callback, mode="sync")])
    wrapped = SandboxSession(inner, instrumentation=instrumentation)

    async with wrapped as session:
        await session.exec("echo hi")

    assert seen
    assert all(session is inner for _op, session in seen)


@pytest.mark.asyncio
async def test_sandbox_session_aclose_flushes_best_effort_sink_tasks(tmp_path: Path) -> None:
    inner = _build_unix_local_session(tmp_path)
    seen: list[tuple[str, str]] = []

    async def _callback(event: UCEvent, _session: BaseSandboxSession) -> None:
        await asyncio.sleep(0)
        seen.append((event.op, event.phase))

    instrumentation = Instrumentation(
        sinks=[CallbackSink(_callback, mode="best_effort", on_error="log")]
    )
    wrapped = SandboxSession(inner, instrumentation=instrumentation)

    await wrapped.start()
    await wrapped.aclose()

    assert ("stop", "finish") in seen
    assert ("shutdown", "finish") in seen
