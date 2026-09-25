from __future__ import annotations

import asyncio
import io
import json
import tarfile
import traceback
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agents import _debug
from agents.sandbox.entries import Dir, File, InContainerMountStrategy, RcloneMountPattern, S3Mount
from agents.sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    OpName,
    WorkspaceArchiveReadError,
)
from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxSessionState,
)
from agents.sandbox.session import (
    CallbackSink,
    Instrumentation,
    SandboxSession,
    SandboxSessionEvent,
    SandboxSessionFinishEvent,
    SandboxSessionStartEvent,
    WorkspaceJsonlSink,
)
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sinks import OnErrorPolicy
from agents.sandbox.session.utils import event_to_json_line
from tests.sandbox._filesystem_test_session import (
    FilesystemTestSandboxSession,
    _build_filesystem_test_session,
    _build_unix_local_session,
)


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
    sink = WorkspaceJsonlSink(
        max_bytes=8 * 1024 * 1024,
        workspace_relpath=Path("out.jsonl"),
        mode="sync",
        on_error="raise",
    )
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
    sink = WorkspaceJsonlSink(
        max_bytes=8 * 1024 * 1024,
        workspace_relpath=Path("out.jsonl"),
        mode="sync",
        on_error="raise",
    )
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
    sink = WorkspaceJsonlSink(
        max_bytes=8 * 1024 * 1024,
    )
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
    sink = WorkspaceJsonlSink(max_bytes=8 * 1024 * 1024, mode="sync", on_error=policy)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_none", [False, True])
async def test_workspace_jsonl_sink_default_keeps_delivering_after_eight_mib(
    tmp_path: Path, explicit_none: bool
) -> None:
    inner = _build_filesystem_test_session(tmp_path)
    relpath = Path("out.jsonl")
    sink = (
        WorkspaceJsonlSink(workspace_relpath=relpath, mode="sync", on_error="raise", max_bytes=None)
        if explicit_none
        else WorkspaceJsonlSink(workspace_relpath=relpath, mode="sync", on_error="raise")
    )
    instrumentation = Instrumentation(sinks=[sink])
    SandboxSession(inner, instrumentation=instrumentation)
    old = b'{"old":"' + b"x" * (8 * 1024 * 1024) + b'"}\n'
    async with inner:
        await inner.write(relpath, io.BytesIO(old))
        await instrumentation.emit(_outbox_event(inner))
        await instrumentation.emit(_outbox_event(inner, op="stop"))
        content = inner.normalize_path(relpath).read_bytes()
    assert content.startswith(old)
    assert [json.loads(line)["op"] for line in content[len(old) :].splitlines()] == [
        "write",
        "stop",
    ]
    assert not sink._buf


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
async def test_workspace_jsonl_sink_wire_budget_stops_delivery(tmp_path: Path) -> None:
    inner = _build_bounded_read_session(tmp_path)
    sink = WorkspaceJsonlSink(
        max_bytes=8 * 1024 * 1024,
    )
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
    sink = WorkspaceJsonlSink(
        max_bytes=8 * 1024 * 1024, workspace_relpath=Path("out.jsonl"), on_error="raise"
    )
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
