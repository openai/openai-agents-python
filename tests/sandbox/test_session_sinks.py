from __future__ import annotations

import asyncio
import io
import json
import tarfile
import uuid
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from agents.sandbox.entries import Dir, File
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
    SandboxSessionEvent,
    SandboxSessionFinishEvent,
    SandboxSessionStartEvent,
    WorkspaceJsonlSink,
)
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.snapshot import LocalSnapshot
from agents.tracing import custom_span, trace
from tests.testing_processor import fetch_normalized_spans


def _build_unix_local_session(
    tmp_path: Path,
    *,
    manifest: Manifest | None = None,
    exposed_ports: tuple[int, ...] = (),
) -> UnixLocalSandboxSession:
    workspace = tmp_path / "workspace"
    snapshot = LocalSnapshot(id=str(uuid.uuid4()), base_path=tmp_path)
    session_manifest = (
        manifest.model_copy(update={"root": str(workspace)}, deep=True)
        if manifest is not None
        else Manifest(root=str(workspace))
    )
    state = UnixLocalSandboxSessionState(
        manifest=session_manifest,
        snapshot=snapshot,
        exposed_ports=exposed_ports,
    )
    return UnixLocalSandboxSession.from_state(state)


@pytest.mark.asyncio
async def test_sandbox_session_exec_emits_stdout_when_enabled(tmp_path: Path) -> None:
    events: list[SandboxSessionEvent] = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda e, _sess: events.append(e), mode="sync")],
        payload_policy=EventPayloadPolicy(include_exec_output=True),
    )

    inner = _build_unix_local_session(tmp_path)
    async with SandboxSession(inner, instrumentation=instrumentation) as session:
        result = await session.exec("echo hi")
        assert result.ok()

    exec_finish = [event for event in events if event.op == "exec" and event.phase == "finish"][0]
    assert isinstance(exec_finish, SandboxSessionFinishEvent)
    assert exec_finish.stdout is not None
    assert "hi" in exec_finish.stdout
    assert exec_finish.trace_id is None
    assert exec_finish.span_id.startswith("sandbox_op_")


@pytest.mark.asyncio
async def test_sandbox_session_write_does_not_include_bytes_when_disabled(
    tmp_path: Path,
) -> None:
    events: list[SandboxSessionEvent] = []
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

    start_event = SandboxSessionStartEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="write",
        span_id="span_write",
    )
    finish_event = SandboxSessionFinishEvent(
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

    def _callback(_event: SandboxSessionEvent, _session: BaseSandboxSession) -> None:
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

    start_event = SandboxSessionStartEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="write",
        span_id="span_write",
    )
    finish_event = SandboxSessionFinishEvent(
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
async def test_workspace_jsonl_sink_preserves_preexisting_outbox_contents(tmp_path: Path) -> None:
    inner = _build_unix_local_session(tmp_path)
    relpath = Path(f"logs/events-{inner.state.session_id}.jsonl")
    old_line = b'{"old":true}\n'

    async with inner:
        await inner.write(relpath, io.BytesIO(old_line))
        sink = WorkspaceJsonlSink(mode="sync", on_error="raise", ephemeral=False)
        sink.bind(inner)

        start = SandboxSessionStartEvent(
            session_id=inner.state.session_id,
            seq=1,
            op="write",
            span_id=str(uuid.uuid4()),
        )
        finish = SandboxSessionFinishEvent(
            session_id=inner.state.session_id,
            seq=2,
            op="write",
            span_id=start.span_id,
            ok=True,
            duration_ms=0.0,
        )

        await sink.handle(start)
        await sink.handle(finish)

        outbox_stream = await inner.read(relpath)
        lines = outbox_stream.read().decode("utf-8").splitlines()

    assert len(lines) == 3
    assert json.loads(lines[0]) == {"old": True}
    assert json.loads(lines[1])["seq"] == 1
    assert json.loads(lines[2])["seq"] == 2


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_does_not_duplicate_lines_across_flushes(
    tmp_path: Path,
) -> None:
    inner = _build_unix_local_session(tmp_path)
    relpath = Path(f"logs/events-{inner.state.session_id}.jsonl")

    async with inner:
        sink = WorkspaceJsonlSink(mode="sync", on_error="raise", ephemeral=False, flush_every=1)
        sink.bind(inner)

        for seq in (1, 2, 3):
            await sink.handle(
                SandboxSessionStartEvent(
                    session_id=inner.state.session_id,
                    seq=seq,
                    op="write",
                    span_id=str(uuid.uuid4()),
                )
            )

        outbox_stream = await inner.read(relpath)
        lines = outbox_stream.read().decode("utf-8").splitlines()

    assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3]


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_ephemeral_excludes_runtime_outbox_with_existing_parent(
    tmp_path: Path,
) -> None:
    inner = _build_unix_local_session(
        tmp_path,
        manifest=Manifest(
            entries={
                "logs": Dir(
                    children={
                        "keep.txt": File(content=b"keep"),
                    }
                )
            }
        ),
    )
    instrumentation = Instrumentation(
        sinks=[WorkspaceJsonlSink(mode="sync", on_error="raise", ephemeral=True)]
    )
    wrapped = SandboxSession(inner, instrumentation=instrumentation)

    async with wrapped as session:
        await session.exec("echo hi")
        relpath = Path(f"logs/events-{inner.state.session_id}.jsonl")
        outbox_stream = await inner.read(relpath)
        assert outbox_stream.read()

        logs_entry = inner.state.manifest.entries["logs"]
        assert isinstance(logs_entry, Dir)
        assert {str(child) for child in logs_entry.children.keys()} == {"keep.txt"}

    snapshot_path = tmp_path / f"{inner.state.snapshot.id}.tar"
    with tarfile.open(snapshot_path, mode="r:*") as tar:
        names = [member.name for member in tar.getmembers()]
        assert any(name.endswith("logs/keep.txt") for name in names)
        assert not any(f"logs/events-{inner.state.session_id}.jsonl" in name for name in names)


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

    def _callback(event: SandboxSessionEvent, session: BaseSandboxSession) -> None:
        seen.append((event.op, session))

    instrumentation = Instrumentation(sinks=[CallbackSink(_callback, mode="sync")])
    wrapped = SandboxSession(inner, instrumentation=instrumentation)

    async with wrapped as session:
        await session.exec("echo hi")

    assert seen
    assert all(session is inner for _op, session in seen)


@pytest.mark.asyncio
async def test_sandbox_session_ops_nest_under_sdk_trace_and_events_carry_trace_ids(
    tmp_path: Path,
) -> None:
    events: list[SandboxSessionEvent] = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda e, _sess: events.append(e), mode="sync")],
        payload_policy=EventPayloadPolicy(include_exec_output=True),
    )
    inner = _build_unix_local_session(tmp_path, exposed_ports=(8765,))
    written_bytes = b"hello from sandbox tracing test\n"

    with trace("sandbox_test"):
        with custom_span("sandbox_parent"):
            async with SandboxSession(inner, instrumentation=instrumentation) as session:
                running = await session.running()
                assert running

                await session.write(Path("notes.txt"), io.BytesIO(written_bytes))
                read_handle = await session.read(Path("notes.txt"))
                try:
                    assert read_handle.read() == written_bytes
                finally:
                    read_handle.close()

                endpoint = await session.resolve_exposed_port(8765)
                assert (endpoint.host, endpoint.port, endpoint.tls) == ("127.0.0.1", 8765, False)

                persisted_workspace = await session.persist_workspace()
                try:
                    persisted_workspace_bytes = persisted_workspace.read()
                finally:
                    persisted_workspace.close()
                assert persisted_workspace_bytes

                await session.hydrate_workspace(io.BytesIO(persisted_workspace_bytes))

                slow_result = await session.exec("sleep 1 && echo slow span")
                assert slow_result.ok()

                fast_result = await session.exec("echo hi")
                assert fast_result.ok()

                failing_result = await session.exec("echo failing >&2; exit 7")
                assert failing_result.exit_code == 7
                assert failing_result.stderr.strip()

    spans = fetch_normalized_spans()
    assert len(spans) == 1
    parent_span = spans[0]["children"][0]
    sandbox_children = parent_span["children"]

    stable_span_tree = [
        {
            "workflow_name": spans[0]["workflow_name"],
            "children": [
                {
                    "type": parent_span["type"],
                    "data": parent_span["data"],
                    "children": [
                        {
                            "type": child["type"],
                            "data": {
                                "name": child["data"]["name"],
                                "data": {
                                    key: value
                                    for key, value in child["data"]["data"].items()
                                    if key
                                    in {
                                        "alive",
                                        "error.type",
                                        "exit_code",
                                        "process.exit.code",
                                        "sandbox.backend",
                                        "sandbox.operation",
                                        "server.address",
                                        "server.port",
                                    }
                                },
                            },
                            **({"error": child["error"]} if "error" in child else {}),
                        }
                        for child in sandbox_children
                    ],
                }
            ],
        }
    ]

    assert stable_span_tree == snapshot(
        [
            {
                "workflow_name": "sandbox_test",
                "children": [
                    {
                        "type": "custom",
                        "data": {"name": "sandbox_parent", "data": {}},
                        "children": [
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.start",
                                    "data": {
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "start",
                                    },
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.running",
                                    "data": {
                                        "alive": True,
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "running",
                                    },
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.write",
                                    "data": {
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "write",
                                    },
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.read",
                                    "data": {
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "read",
                                    },
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.resolve_exposed_port",
                                    "data": {
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "resolve_exposed_port",
                                        "server.address": "127.0.0.1",
                                        "server.port": 8765,
                                    },
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.persist_workspace",
                                    "data": {
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "persist_workspace",
                                    },
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.hydrate_workspace",
                                    "data": {
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "hydrate_workspace",
                                    },
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.exec",
                                    "data": {
                                        "exit_code": 0,
                                        "process.exit.code": 0,
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "exec",
                                    },
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.exec",
                                    "data": {
                                        "exit_code": 0,
                                        "process.exit.code": 0,
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "exec",
                                    },
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.exec",
                                    "data": {
                                        "error.type": "ExecNonZeroError",
                                        "exit_code": 7,
                                        "process.exit.code": 7,
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "exec",
                                    },
                                },
                                "error": {
                                    "message": "Sandbox operation returned an unsuccessful result.",
                                    "data": {"operation": "exec", "exit_code": 7},
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.stop",
                                    "data": {
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "stop",
                                    },
                                },
                            },
                            {
                                "type": "custom",
                                "data": {
                                    "name": "sandbox.shutdown",
                                    "data": {
                                        "sandbox.backend": "unix_local",
                                        "sandbox.operation": "shutdown",
                                    },
                                },
                            },
                        ],
                    }
                ],
            }
        ]
    )

    session_ids = {child["data"]["data"]["session_id"] for child in sandbox_children}
    sandbox_session_ids = {
        child["data"]["data"]["sandbox.session.id"] for child in sandbox_children
    }
    assert len(session_ids) == 1
    assert len(sandbox_session_ids) == 1
    session_id = session_ids.pop()
    sandbox_session_id = sandbox_session_ids.pop()
    assert isinstance(session_id, str)
    assert isinstance(sandbox_session_id, str)
    assert str(uuid.UUID(session_id)) == session_id
    assert sandbox_session_id == session_id

    exec_spans = [child for child in sandbox_children if child["data"]["name"] == "sandbox.exec"]
    assert len(exec_spans) == 3

    exec_finish = [event for event in events if event.op == "exec" and event.phase == "finish"][0]
    assert isinstance(exec_finish, SandboxSessionFinishEvent)
    assert exec_finish.trace_id is not None
    assert exec_finish.span_id.startswith("span_")
    assert exec_finish.parent_span_id is not None
    assert sum(1 for event in events if event.op == "exec" and event.phase == "finish") == 3


@pytest.mark.asyncio
async def test_sandbox_session_events_fallback_to_audit_ids_under_disabled_parent_span(
    tmp_path: Path,
) -> None:
    events: list[SandboxSessionEvent] = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda e, _sess: events.append(e), mode="sync")],
    )
    inner = _build_unix_local_session(tmp_path)

    with trace("sandbox_disabled_parent_test"):
        with custom_span("disabled_parent", disabled=True):
            async with SandboxSession(inner, instrumentation=instrumentation) as session:
                result = await session.exec("echo hi")
                assert result.ok()

    exec_events = [event for event in events if event.op == "exec"]
    assert len(exec_events) == 2
    start_event, finish_event = exec_events
    assert isinstance(start_event, SandboxSessionStartEvent)
    assert isinstance(finish_event, SandboxSessionFinishEvent)
    assert start_event.trace_id is None
    assert finish_event.trace_id is None
    assert start_event.parent_span_id is None
    assert finish_event.parent_span_id is None
    assert start_event.span_id == finish_event.span_id
    assert start_event.span_id.startswith("sandbox_op_")
    assert start_event.span_id != "no-op"


@pytest.mark.asyncio
async def test_sandbox_session_aclose_flushes_best_effort_sink_tasks(tmp_path: Path) -> None:
    inner = _build_unix_local_session(tmp_path)
    seen: list[tuple[str, str]] = []

    async def _callback(event: SandboxSessionEvent, _session: BaseSandboxSession) -> None:
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
