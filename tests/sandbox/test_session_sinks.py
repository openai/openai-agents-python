from __future__ import annotations

import asyncio
import io
import json
import tarfile
import traceback
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from inline_snapshot import snapshot

from agents import _debug
from agents.sandbox.entries import Dir, File, InContainerMountStrategy, RcloneMountPattern, S3Mount
from agents.sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    OpName,
    WorkspaceArchiveReadError,
    WorkspaceReadNotFoundError,
)
from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxSession,
    UnixLocalSandboxSessionState,
)
from agents.sandbox.session import (
    CallbackSink,
    ChainedSink,
    EventPayloadPolicy,
    HttpProxySink,
    Instrumentation,
    JsonlOutboxSink,
    SandboxSession,
    SandboxSessionEvent,
    SandboxSessionFinishEvent,
    SandboxSessionStartEvent,
    WorkspaceJsonlSink,
)
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sandbox_session import _read_with_expected_span_errors
from agents.sandbox.session.sinks import OnErrorPolicy
from agents.sandbox.session.utils import event_to_json_line
from agents.sandbox.snapshot import LocalSnapshot
from agents.sandbox.types import ExecResult
from agents.tracing import custom_span, trace
from tests.sandbox._filesystem_test_session import FilesystemTestSandboxSession
from tests.testing_processor import fetch_normalized_spans, fetch_ordered_spans


class _BoundedReadSession(FilesystemTestSandboxSession):
    """Record bounded API usage without exposing a whole-file read API."""

    def __init__(self, state: UnixLocalSandboxSessionState) -> None:
        super().__init__(state)
        self.requests: list[tuple[Path, int]] = []

    async def _read_bounded(self, path: Path, *, max_bytes: int) -> bytes:
        self.requests.append((path, max_bytes))
        return await super()._read_bounded(path, max_bytes=max_bytes)


def _build_bounded_read_session(tmp_path: Path) -> _BoundedReadSession:
    return _BoundedReadSession(_build_filesystem_test_session(tmp_path).state)


def _outbox_event(inner: BaseSandboxSession, *, op: OpName = "write") -> SandboxSessionStartEvent:
    return SandboxSessionStartEvent(
        session_id=inner.state.session_id, seq=1, op=op, span_id="test-span"
    )


class _LegacyReadSession(FilesystemTestSandboxSession):
    # Model a custom backend that implements only the released read/write APIs.
    _read_bounded = BaseSandboxSession._read_bounded


class _ShortReadStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        return super().read(min(size, 3))


@pytest.mark.asyncio
@pytest.mark.parametrize("history_kind", ["missing", "binary", "text", "short"])
async def test_workspace_jsonl_sink_legacy_backend_delivers(
    tmp_path: Path, history_kind: str
) -> None:
    inner = _LegacyReadSession(_build_filesystem_test_session(tmp_path).state)
    sink = WorkspaceJsonlSink(workspace_relpath=Path("out.jsonl"), mode="sync", on_error="raise")
    instrumentation = Instrumentation(sinks=[sink])
    SandboxSession(inner, instrumentation=instrumentation)
    old = "日本語\n".encode() if history_kind == "text" else b"\x00\xff\n"
    stream: io.IOBase
    if history_kind == "text":
        stream = io.StringIO(old.decode())
    elif history_kind == "short":
        old += b"short reads must preserve all history\n"
        stream = _ShortReadStream(old)
    else:
        stream = io.BytesIO(old)
    async with inner:
        if history_kind == "missing":
            old = b""
            await instrumentation.emit(_outbox_event(inner))
            stream.close()
        else:
            await inner.write(Path("out.jsonl"), io.BytesIO(old))
            with patch.object(inner, "read", return_value=stream):
                await instrumentation.emit(_outbox_event(inner))
        content = (Path(inner.state.manifest.root) / "out.jsonl").read_bytes()
    assert stream.closed
    assert content.startswith(old)
    assert json.loads(content[len(old) :])["seq"] == 1
    assert not sink._buf


@pytest.mark.asyncio
@pytest.mark.parametrize("text_stream", [False, True])
async def test_workspace_jsonl_sink_legacy_backend_stops_at_limit(
    tmp_path: Path, text_stream: bool
) -> None:
    inner = _LegacyReadSession(_build_filesystem_test_session(tmp_path).state)
    sink = WorkspaceJsonlSink(workspace_relpath=Path("out.jsonl"), max_bytes=1024)
    sink.bind(inner)
    old = "日本語\n" * 1024
    stream = io.StringIO(old) if text_stream else io.BytesIO(old.encode())
    async with inner:
        await inner.write(Path("out.jsonl"), io.BytesIO(old.encode()))
        with patch.object(inner, "read", return_value=stream):
            with pytest.raises(RuntimeError, match="delivery stopped"):
                await sink.handle(_outbox_event(inner))
    assert stream.closed
    assert (Path(inner.state.manifest.root) / "out.jsonl").read_bytes() == old.encode()
    assert not sink._buf


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_legacy_backend_read_failure_closes_stream(
    tmp_path: Path,
) -> None:
    inner = _LegacyReadSession(_build_filesystem_test_session(tmp_path).state)
    sink = WorkspaceJsonlSink(workspace_relpath=Path("out.jsonl"), mode="sync", on_error="raise")
    instrumentation = Instrumentation(sinks=[sink])
    SandboxSession(inner, instrumentation=instrumentation)
    stream = io.BytesIO(b"original\n")
    async with inner:
        await inner.write(Path("out.jsonl"), io.BytesIO(b"original\n"))
        with (
            patch.object(inner, "read", return_value=stream),
            patch.object(stream, "read", side_effect=OSError("synthetic-private-payload")),
        ):
            with pytest.raises(RuntimeError, match="sandbox event sink failed") as caught:
                await instrumentation.emit(_outbox_event(inner))
    assert stream.closed
    assert (Path(inner.state.manifest.root) / "out.jsonl").read_bytes() == b"original\n"
    assert sink._buf
    error = caught.value.__context__
    assert isinstance(error, WorkspaceArchiveReadError)
    assert error.__context__ is None
    assert "synthetic-private-payload" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("slack", [-1, 0, 1])
async def test_workspace_jsonl_sink_replacement_budget(tmp_path: Path, slack: int) -> None:
    inner = _build_bounded_read_session(tmp_path)
    event = _outbox_event(inner)
    old = b'{"old":true}\n'
    budget = len(old) + len(event_to_json_line(event).encode()) + slack
    sink = WorkspaceJsonlSink(workspace_relpath=Path("out.jsonl"), max_bytes=budget)
    sink.bind(inner)
    async with inner:
        await inner.write(Path("out.jsonl"), io.BytesIO(old))
        with patch.object(inner, "read", side_effect=AssertionError("Unbounded download")):
            if slack < 0:
                with pytest.raises(RuntimeError, match="delivery stopped"):
                    await sink.handle(event)
            else:
                await sink.handle(event)
        content = (Path(inner.state.manifest.root) / "out.jsonl").read_bytes()
    assert content.startswith(old)
    if slack < 0:
        assert content == old
    else:
        assert json.loads(content[len(old) :])["seq"] == 1
    assert not sink._buf


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["raise", "log", "ignore"])
async def test_workspace_jsonl_sink_exhaustion_stops_buffering(
    tmp_path: Path, policy: OnErrorPolicy, caplog: pytest.LogCaptureFixture
) -> None:
    inner = _build_bounded_read_session(tmp_path)
    sink = WorkspaceJsonlSink(workspace_relpath=Path("out.jsonl"), max_bytes=1024)
    sink.mode = "sync"
    sink.on_error = policy
    instrumentation = Instrumentation(sinks=[sink])
    SandboxSession(inner, instrumentation=instrumentation)
    old = b"synthetic-history\n" * 100
    async with inner:
        await inner.write(Path("out.jsonl"), io.BytesIO(old))
        if policy == "raise":
            with pytest.raises(RuntimeError, match="sandbox event sink failed"):
                await instrumentation.emit(_outbox_event(inner))
        else:
            await instrumentation.emit(_outbox_event(inner))
        for _ in range(20):
            await instrumentation.emit(_outbox_event(inner))
        sink.bind(inner)
        await instrumentation.emit(_outbox_event(inner))
    assert len(inner.requests) == 1
    assert not sink._buf
    assert (Path(inner.state.manifest.root) / "out.jsonl").read_bytes() == old
    assert len(caplog.records) == (1 if policy == "log" else 0)
    assert "synthetic-history" not in caplog.text


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_pending_budget_before_flush(tmp_path: Path) -> None:
    inner = _build_bounded_read_session(tmp_path)
    event = _outbox_event(inner)
    sink = WorkspaceJsonlSink(max_bytes=len(event_to_json_line(event).encode()), flush_every=100)
    sink.bind(inner)
    async with inner:
        await sink.handle(event)
        with pytest.raises(RuntimeError, match="max_bytes"):
            await sink.handle(event)
        await sink.handle(event)
    assert not inner.requests
    assert not sink._buf


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_retries_delivery_and_flushes_lifecycle(tmp_path: Path) -> None:
    inner = _build_bounded_read_session(tmp_path)
    sink = WorkspaceJsonlSink(workspace_relpath=Path("out.jsonl"), flush_every=100, ephemeral=True)
    sink.bind(inner)
    async with inner:
        await sink.handle(_outbox_event(inner))
        with patch.object(inner, "write", side_effect=OSError("temporary failure")):
            with pytest.raises(OSError):
                await sink.handle(_outbox_event(inner, op="persist_workspace"))
        assert sink._buf
        await sink.handle(_outbox_event(inner, op="stop"))
    content = (Path(inner.state.manifest.root) / "out.jsonl").read_text()
    assert [json.loads(line)["op"] for line in content.splitlines()] == [
        "write",
        "persist_workspace",
        "stop",
    ]
    assert not sink._buf
    assert inner._persist_workspace_skip_relpaths() == {Path("out.jsonl")}


@pytest.mark.asyncio
@pytest.mark.parametrize("root_mount", [False, True])
async def test_workspace_jsonl_sink_preserves_mounted_write_and_rebind(
    tmp_path: Path, root_mount: bool
) -> None:
    inner = _build_bounded_read_session(tmp_path)
    inner.state.manifest.entries["storage"] = S3Mount(
        bucket="test-bucket",
        mount_path=inner.state.manifest.root if root_mount else "logs",
        mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
    )
    original_exclusions = inner._persist_workspace_skip_relpaths()
    sink = WorkspaceJsonlSink(workspace_relpath=Path("logs/out.jsonl"))
    sink.bind(inner)
    assert inner._persist_workspace_skip_relpaths() == original_exclusions
    async with inner:
        await sink.handle(_outbox_event(inner))
        sink = WorkspaceJsonlSink(workspace_relpath=Path("logs/out.jsonl"))
        sink.bind(inner)
        await sink.handle(_outbox_event(inner))
    content = (Path(inner.state.manifest.root) / "logs/out.jsonl").read_text()
    assert len(content.splitlines()) == 2
    assert all(json.loads(line)["seq"] == 1 for line in content.splitlines())


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_bounds_read_before_transfer(tmp_path: Path) -> None:
    inner = _build_bounded_read_session(tmp_path)
    sink = WorkspaceJsonlSink(workspace_relpath=Path("out.jsonl"), max_bytes=1024)
    sink.bind(inner)
    async with inner:
        old = b"ordinary fixture\n" * 1024
        await inner.write(Path("out.jsonl"), io.BytesIO(old))
        with patch.object(inner, "read", side_effect=AssertionError("Unbounded download")):
            assert len(await sink._read_existing_outbox(Path("out.jsonl"))) == 1025
            with pytest.raises(RuntimeError, match="delivery stopped"):
                await sink.handle(_outbox_event(inner))
    assert (Path(inner.state.manifest.root) / "out.jsonl").read_bytes() == old


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_preserves_bytes_and_reports_failure(
    tmp_path: Path,
) -> None:
    inner = _build_bounded_read_session(tmp_path)
    sink = WorkspaceJsonlSink(workspace_relpath=Path("out.jsonl"))
    sink.bind(inner)
    async with inner:
        await sink.handle(_outbox_event(inner))
        old = b"\x00\xff\n" + "日本語\n".encode() + b"x" * 20000 + b"\n"
        await inner.write(Path("out.jsonl"), io.BytesIO(old))
        await sink.handle(_outbox_event(inner))
        content = (Path(inner.state.manifest.root) / "out.jsonl").read_bytes()
        assert content.startswith(old)
        assert json.loads(content[len(old) :])["seq"] == 1
        # Read failures must not cause a replacement write.
        await inner.mkdir(Path("directory"))
        sink = WorkspaceJsonlSink(workspace_relpath=Path("directory"))
        sink.bind(inner)
        with pytest.raises(WorkspaceArchiveReadError):
            await sink.handle(_outbox_event(inner))


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_failed_read_without_writing(tmp_path: Path) -> None:
    inner = _build_bounded_read_session(tmp_path)
    sink = WorkspaceJsonlSink()
    sink.bind(inner)
    async with inner:
        with (
            patch.object(inner, "_read_bounded", side_effect=ValueError("synthetic-private-value")),
            patch.object(inner, "write", new_callable=AsyncMock) as write,
        ):
            with pytest.raises(WorkspaceArchiveReadError) as caught:
                await sink.handle(_outbox_event(inner))
    write.assert_not_called()
    assert "synthetic-private-value" not in str(caught.value)
    assert caught.value.__context__ is None
    assert sink._buf


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_error", [False, True])
@pytest.mark.parametrize("policy", ["raise", "log"])
@pytest.mark.parametrize("redact", [False, True])
async def test_workspace_jsonl_sink_errors_have_no_pending_payload(
    tmp_path: Path,
    timeout_error: bool,
    policy: OnErrorPolicy,
    redact: bool,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = _build_bounded_read_session(tmp_path)
    sink = WorkspaceJsonlSink(mode="sync", on_error=policy)
    instrumentation = Instrumentation(sinks=[sink])
    SandboxSession(inner, instrumentation=instrumentation)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redact)
    event = _outbox_event(inner).model_copy(update={"data": {"secret": "synthetic-private-value"}})

    async def fail(path: Path, *, max_bytes: int) -> bytes:
        command = ("provider-file-read", str(path))
        if timeout_error:
            raise ExecTimeoutError(command=command, timeout_s=30.0)
        raise ExecTransportError(command=command)

    error: BaseException | None = None
    async with inner:
        with patch.object(inner, "_read_bounded", side_effect=fail):
            if policy == "raise":
                with pytest.raises(RuntimeError, match="sandbox event sink failed") as caught:
                    await instrumentation.emit(event)
                error = caught.value
            else:
                await instrumentation.emit(event)
    for record in caplog.records:
        assert "synthetic-private-value" not in repr(vars(record))
        if redact:
            assert record.exc_info is None
        elif record.exc_info:
            error = record.exc_info[1]
    while error is not None:
        assert "synthetic-private-value" not in repr(vars(error))
        assert "synthetic-private-value" not in "".join(traceback.format_exception(error))
        error = error.__context__
    assert sink._buf


def test_workspace_jsonl_sink_requires_positive_budget() -> None:
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        WorkspaceJsonlSink(max_bytes=0)


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


def _build_filesystem_test_session(
    tmp_path: Path,
    *,
    manifest: Manifest | None = None,
) -> FilesystemTestSandboxSession:
    workspace = tmp_path / "workspace"
    session_manifest = (
        manifest.model_copy(update={"root": str(workspace)}, deep=True)
        if manifest is not None
        else Manifest(root=str(workspace))
    )
    state = UnixLocalSandboxSessionState(
        manifest=session_manifest,
        snapshot=LocalSnapshot(id=str(uuid.uuid4()), base_path=tmp_path),
    )
    return FilesystemTestSandboxSession(state=state)


@pytest.mark.asyncio
async def test_filesystem_test_session_rejects_process_backed_operations(tmp_path: Path) -> None:
    session = _build_filesystem_test_session(tmp_path)

    assert session.supports_pty() is False
    with pytest.raises(NotImplementedError, match="PTY execution is not supported"):
        await session.pty_exec_start("echo hi")
    with pytest.raises(AssertionError, match="user-scoped filesystem operations"):
        await session.write(Path("x.txt"), io.BytesIO(b"hello"), user="sandbox-user")


@pytest.mark.asyncio
@pytest.mark.requires_native_macos_sandbox
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

    inner = _build_filesystem_test_session(tmp_path)
    async with SandboxSession(inner, instrumentation=instrumentation) as session:
        await session.write(Path("x.txt"), io.BytesIO(b"hello"))

    write_start = [event for event in events if event.op == "write" and event.phase == "start"][0]
    assert "bytes" not in write_start.data


@pytest.mark.asyncio
async def test_sandbox_session_apply_manifest_preserves_write_instrumentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[SandboxSessionEvent] = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda e, _sess: events.append(e), mode="sync")],
    )
    inner = _build_unix_local_session(
        tmp_path,
        manifest=Manifest(entries={"materialized.txt": File(content=b"hello")}),
    )

    async def successful_exec(*_command: str | Path, timeout: float | None = None) -> ExecResult:
        _ = timeout
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    monkeypatch.setattr(inner, "_exec_internal", successful_exec)
    session = SandboxSession(inner, instrumentation=instrumentation)

    await session.apply_manifest()

    write_events = [event for event in events if event.op == "write"]
    assert [event.phase for event in write_events] == ["start", "finish"]


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
@pytest.mark.requires_native_macos_sandbox
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
@pytest.mark.requires_native_macos_sandbox
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
    inner = _build_bounded_read_session(tmp_path)
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
    inner = _build_bounded_read_session(tmp_path)
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
async def test_workspace_jsonl_sink_clears_flushed_buffer(tmp_path: Path) -> None:
    inner = _build_bounded_read_session(tmp_path)
    relpath = Path(f"logs/events-{inner.state.session_id}.jsonl")

    async with inner:
        sink = WorkspaceJsonlSink(mode="sync", on_error="raise", ephemeral=False, flush_every=1)
        sink.bind(inner)

        for seq in (1, 2):
            await sink.handle(
                SandboxSessionStartEvent(
                    session_id=inner.state.session_id,
                    seq=seq,
                    op="write",
                    span_id=str(uuid.uuid4()),
                )
            )
            assert sink._buf == bytearray()

        outbox_stream = await inner.read(relpath)
        lines = outbox_stream.read().decode("utf-8").splitlines()

    assert [json.loads(line)["seq"] for line in lines] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.requires_native_macos_sandbox
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
@pytest.mark.requires_native_macos_sandbox
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
@pytest.mark.requires_native_macos_sandbox
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
async def test_http_proxy_sink_spools_direct_timeout(tmp_path: Path) -> None:
    spool_path = tmp_path / "events.jsonl"
    sink = HttpProxySink(
        "http://127.0.0.1:9/events",
        mode="sync",
        on_error="raise",
        spool_path=spool_path,
    )
    event = SandboxSessionStartEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="write",
        span_id=str(uuid.uuid4()),
    )

    with patch("agents.sandbox.session.sinks.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(RuntimeError, match="http proxy sink POST failed"):
            await sink.handle(event)

    lines = spool_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["seq"] == 1


def test_http_proxy_sink_snapshots_headers() -> None:
    headers = {"authorization": "Bearer original"}
    sink = HttpProxySink("https://example.test/events", headers=headers)
    headers["authorization"] = "Bearer changed"
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b""

    with patch("agents.sandbox.session.sinks.urlopen", return_value=response) as urlopen:
        sink._post(b"{}", None)

    request = urlopen.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer original"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retryable", "reason", "expected_reason"),
    [
        (True, "provider_failure", "bounded_read_failed"),
        (False, "bounded_read_wire_limit", "bounded_read_wire_limit"),
        (None, "provider_failure", "bounded_read_failed"),
    ],
)
async def test_bounded_read_preserves_retryability_without_provider_diagnostics(
    tmp_path: Path, retryable: bool | None, reason: str, expected_reason: str
) -> None:
    events: list[SandboxSessionEvent] = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda event, _: events.append(event), mode="sync")]
    )
    inner = _build_bounded_read_session(tmp_path)
    failure = WorkspaceArchiveReadError(
        path=Path("out.jsonl"),
        context={"reason": reason, "response": "synthetic-private-payload"},
        cause=OSError("synthetic-private-payload"),
        retryable=retryable,
    )
    async with SandboxSession(inner, instrumentation=instrumentation) as session:
        with patch.object(inner, "_read_bounded", side_effect=failure):
            with pytest.raises(WorkspaceArchiveReadError) as caught:
                await session.read_bounded(Path("out.jsonl"), max_bytes=100)

    error = caught.value
    assert error is not failure
    assert error.retryable is retryable
    assert error.context == {"path": "out.jsonl", "reason": expected_reason}
    assert error.cause is error.__cause__ is error.__context__ is None
    finish = next(event for event in events if event.op == "read" and event.phase == "finish")
    assert isinstance(finish, SandboxSessionFinishEvent)
    assert finish.error_retryable is retryable
    assert "synthetic-private-payload" not in finish.model_dump_json()


@pytest.mark.asyncio
async def test_bounded_read_preserves_classified_transport_retryability(tmp_path: Path) -> None:
    inner = _build_bounded_read_session(tmp_path)
    failure = ExecTransportError(
        command=("read-helper", "synthetic-private-payload"), retryable=True
    )
    with patch.object(inner, "_read_bounded", side_effect=failure):
        with pytest.raises(WorkspaceArchiveReadError) as caught:
            await inner.read_bounded(Path("out.jsonl"), max_bytes=100)
    assert caught.value.retryable is True
    assert caught.value.cause is caught.value.__cause__ is caught.value.__context__ is None
    assert "synthetic-private-payload" not in str(caught.value)


@pytest.mark.asyncio
async def test_sandbox_session_error_events_and_traces_include_retryability(
    tmp_path: Path,
) -> None:
    events: list[SandboxSessionEvent] = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda e, _sess: events.append(e), mode="sync")]
    )
    inner = _build_filesystem_test_session(tmp_path)

    with trace("sandbox_retryability_test"):
        async with SandboxSession(inner, instrumentation=instrumentation) as session:
            with pytest.raises(WorkspaceReadNotFoundError):
                await session.read(Path("missing.txt"))

    read_finish = [event for event in events if event.op == "read" and event.phase == "finish"][0]
    assert isinstance(read_finish, SandboxSessionFinishEvent)
    assert read_finish.error_retryable is False

    spans = fetch_normalized_spans()
    read_span = next(
        child for child in spans[0]["children"] if child["data"]["name"] == "sandbox.read"
    )
    span_data = read_span["data"]
    assert isinstance(span_data, dict)
    span_payload = span_data["data"]
    assert isinstance(span_payload, dict)
    assert span_payload["error_retryable"] is False

    raw_read_span = next(
        span for span in fetch_ordered_spans() if span.span_data.export()["name"] == "sandbox.read"
    )
    span_error = raw_read_span.error
    assert span_error is not None
    error_payload = span_error["data"]
    assert isinstance(error_payload, dict)
    assert error_payload["error_retryable"] is False


@pytest.mark.asyncio
async def test_expected_read_span_error_is_call_scoped_and_preserves_audit_failures(
    tmp_path: Path,
) -> None:
    events: list[SandboxSessionEvent] = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda e, _sess: events.append(e), mode="sync")]
    )
    inner = _build_filesystem_test_session(tmp_path)
    expected_path = Path("expected-missing.txt")
    ordinary_path = Path("ordinary-missing.txt")

    with trace("sandbox_expected_read_error_test"):
        async with SandboxSession(inner, instrumentation=instrumentation) as session:
            results = await asyncio.gather(
                _read_with_expected_span_errors(
                    session,
                    expected_path,
                    expected_span_errors=(WorkspaceReadNotFoundError,),
                ),
                session.read(ordinary_path),
                return_exceptions=True,
            )

    assert all(isinstance(result, WorkspaceReadNotFoundError) for result in results)

    read_starts = [
        event
        for event in events
        if isinstance(event, SandboxSessionStartEvent) and event.op == "read"
    ]
    path_by_span_id = {event.span_id: event.data["path"] for event in read_starts}
    read_spans = [
        span
        for span in fetch_ordered_spans()
        if span.span_data.export().get("name") == "sandbox.read"
    ]
    error_by_path = {path_by_span_id[span.span_id]: span.error for span in read_spans}
    assert error_by_path[str(expected_path)] is None
    assert error_by_path[str(ordinary_path)] is not None

    read_finishes = [
        event
        for event in events
        if isinstance(event, SandboxSessionFinishEvent) and event.op == "read"
    ]
    assert len(read_finishes) == 2
    for event in read_finishes:
        assert event.ok is False
        assert event.error_type == "WorkspaceReadNotFoundError"
        assert event.error_code == "workspace_read_not_found"
        assert event.error_retryable is False


@pytest.mark.asyncio
async def test_expected_read_span_records_finish_sink_failure(tmp_path: Path) -> None:
    def fail_read_finish(event: SandboxSessionEvent, _session: BaseSandboxSession) -> None:
        if isinstance(event, SandboxSessionFinishEvent) and event.op == "read":
            raise ValueError("simulated sink failure")

    instrumentation = Instrumentation(
        sinks=[CallbackSink(fail_read_finish, mode="sync", on_error="raise")]
    )
    inner = _build_filesystem_test_session(tmp_path)

    with trace("sandbox_expected_read_sink_failure_test"):
        async with SandboxSession(inner, instrumentation=instrumentation) as session:
            with pytest.raises(RuntimeError, match="sandbox event sink failed"):
                await _read_with_expected_span_errors(
                    session,
                    Path("expected-missing.txt"),
                    expected_span_errors=(WorkspaceReadNotFoundError,),
                )

    read_span = next(
        span
        for span in fetch_ordered_spans()
        if span.span_data.export().get("name") == "sandbox.read"
    )
    assert read_span.error is not None
    assert read_span.error["message"] == "RuntimeError"
    assert read_span.span_data.data["error_type"] == "RuntimeError"


@pytest.mark.asyncio
@pytest.mark.requires_native_macos_sandbox
async def test_exec_span_records_cancellation_during_finish_sink_delivery(tmp_path: Path) -> None:
    finish_delivery_started = asyncio.Event()
    completed_exit_codes: list[int] = []

    async def block_exec_finish(event: SandboxSessionEvent, _session: BaseSandboxSession) -> None:
        if isinstance(event, SandboxSessionFinishEvent) and event.op == "exec":
            exit_code = event.data["exit_code"]
            assert isinstance(exit_code, int)
            completed_exit_codes.append(exit_code)
            finish_delivery_started.set()
            await asyncio.Event().wait()

    instrumentation = Instrumentation(
        sinks=[CallbackSink(block_exec_finish, mode="sync", on_error="raise")]
    )
    inner = _build_unix_local_session(tmp_path)

    with trace("sandbox_exec_finish_cancellation_test"):
        async with SandboxSession(inner, instrumentation=instrumentation) as session:
            exec_task = asyncio.create_task(session.exec("exit 7"))
            await finish_delivery_started.wait()
            exec_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await exec_task

    exec_span = next(
        span
        for span in fetch_ordered_spans()
        if span.span_data.export().get("name") == "sandbox.exec"
    )
    assert exec_span.error is not None
    assert exec_span.error["message"] == "CancelledError"
    assert exec_span.span_data.data["error_type"] == "CancelledError"
    assert completed_exit_codes
    assert exec_span.span_data.data["exit_code"] == completed_exit_codes[0]
    assert exec_span.span_data.data["process.exit.code"] == completed_exit_codes[0]


@pytest.mark.asyncio
@pytest.mark.requires_native_macos_sandbox
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
@pytest.mark.requires_native_macos_sandbox
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
    inner = _build_filesystem_test_session(tmp_path)
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


@pytest.mark.asyncio
async def test_workspace_jsonl_sink_wire_budget_stops_delivery(tmp_path: Path) -> None:
    inner = _build_bounded_read_session(tmp_path)
    sink = WorkspaceJsonlSink()
    sink.bind(inner)
    async with inner:
        with (
            patch.object(
                inner,
                "_read_bounded",
                side_effect=WorkspaceArchiveReadError(
                    path=Path("out.jsonl"), context={"reason": "bounded_read_wire_limit"}
                ),
            ) as read,
            patch.object(inner, "write", new_callable=AsyncMock) as write,
        ):
            with pytest.raises(RuntimeError, match="delivery stopped"):
                await sink.handle(_outbox_event(inner))
            await sink.handle(_outbox_event(inner))
    read.assert_awaited_once()
    write.assert_not_called()
    assert not sink._buf


@pytest.mark.asyncio
async def test_workspace_sink_waits_for_backend_read_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner = _build_bounded_read_session(tmp_path)
    sink = WorkspaceJsonlSink(workspace_relpath=Path("out.jsonl"), on_error="raise")
    sink.bind(inner)
    inner.running = AsyncMock(return_value=True)  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = 0.0
    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
    cleaned = False

    async def read(path: Path, *, max_bytes: int) -> bytes:
        nonlocal offset, cleaned
        # Advance beyond the former sink deadline without waiting in real time.
        # The backend still owns its deadline and must finish cleanup first.
        offset = 31.0
        for _ in range(4):
            await asyncio.sleep(0)
        cleaned = True
        raise WorkspaceArchiveReadError(path=path, retryable=True)

    inner._read_bounded = read  # type: ignore[method-assign]
    inner.write = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(WorkspaceArchiveReadError):
        await sink.handle(_outbox_event(inner))
    assert cleaned
    inner.write.assert_not_awaited()
    # A failed read retains the event for delivery once the backend recovers.
    inner._read_bounded = AsyncMock(return_value=b"")  # type: ignore[method-assign]
    await sink.handle(_outbox_event(inner, op="stop"))
    inner.write.assert_awaited_once()
    written = inner.write.call_args.args[1].getvalue().splitlines()
    assert [json.loads(line)["op"] for line in written] == ["write", "stop"]
