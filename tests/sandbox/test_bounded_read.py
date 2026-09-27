from __future__ import annotations

import asyncio
import base64
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.sandbox.errors import WorkspaceArchiveReadError, WorkspaceReadNotFoundError
from agents.sandbox.session import (
    CallbackSink,
    Instrumentation,
    SandboxSession,
    SandboxSessionEvent,
    SandboxSessionFinishEvent,
)
from agents.sandbox.types import ExecResult


class _Content:
    def __init__(self, data: bytes, *, fail: bool = False) -> None:
        self.data = data
        self.offset = 0
        self.closed = False
        self.fail = fail

    async def chunks(self, chunk_size: int = 3) -> AsyncIterator[bytes]:
        while self.offset < len(self.data):
            if self.fail:
                raise ValueError("synthetic-private-response")
            end = min(self.offset + min(chunk_size, 3), len(self.data))
            chunk = self.data[self.offset : end]
            self.offset = end
            yield chunk

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self.chunks()

    def iter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        return self.chunks(chunk_size)

    def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        return self.chunks(chunk_size)

    def iter_chunked(self, chunk_size: int) -> AsyncIterator[bytes]:
        return self.chunks(chunk_size)

    async def readexactly(self, size: int) -> bytes:
        if self.fail:
            raise ValueError("synthetic-private-response")
        data = self.data[self.offset : self.offset + size]
        self.offset += len(data)
        if len(data) < size:
            raise asyncio.IncompleteReadError(data, size)
        return data

    async def __aenter__(self) -> _Content:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


PROVIDERS = ["e2b", "runloop", "vercel", "daytona", "blaxel", "cloudflare"]
CLASSES = {
    "e2b": "E2BSandboxSession",
    "runloop": "RunloopSandboxSession",
    "vercel": "VercelSandboxSession",
    "daytona": "DaytonaSandboxSession",
    "blaxel": "BlaxelSandboxSession",
    "cloudflare": "CloudflareSandboxSession",
}


def _session(provider: str, content: _Content, *, status: int = 200) -> Any:
    module = pytest.importorskip(f"agents.extensions.sandbox.{provider}.sandbox")
    cls = getattr(module, CLASSES[provider])
    session = object.__new__(cls)
    session._validate_path_access = AsyncMock(return_value=Path("/workspace/out.jsonl"))
    # The same response object supports the documented provider streaming shapes.
    content.content = content  # type: ignore[attr-defined]
    content.status = status  # type: ignore[attr-defined]
    content.status_code = status  # type: ignore[attr-defined]
    if provider == "e2b":
        session._sandbox = SimpleNamespace(
            files=SimpleNamespace(read=AsyncMock(return_value=content))
        )
    elif provider == "runloop":
        session.state = SimpleNamespace(
            devbox_id="test", timeouts=SimpleNamespace(file_download_s=30)
        )
        session._sdk = SimpleNamespace(
            api=SimpleNamespace(
                devboxes=SimpleNamespace(
                    with_streaming_response=SimpleNamespace(
                        download_file=MagicMock(return_value=content)
                    )
                )
            )
        )
    elif provider == "vercel":

        @asynccontextmanager
        async def mount() -> AsyncIterator[None]:
            session.mount_active = True
            try:
                yield
            finally:
                session.mount_active = False

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in content:
                    assert session.mount_active
                    yield chunk
            finally:
                assert session.mount_active
                content.closed = True

        session._s3_mount_operation = mount
        session._ensure_sandbox = AsyncMock(
            return_value=SimpleNamespace(iter_file=AsyncMock(return_value=chunks()))
        )
    elif provider == "daytona":
        session.state = SimpleNamespace(timeouts=SimpleNamespace(file_download_s=30))
        session._sandbox = SimpleNamespace(
            fs=SimpleNamespace(
                _api_client=SimpleNamespace(
                    download_file_without_preload_content=AsyncMock(return_value=content)
                )
            )
        )
    elif provider == "blaxel":
        session._sandbox = SimpleNamespace(
            fs=SimpleNamespace(
                url="https://example.invalid",
                format_path=lambda p: p,
                get_client=lambda: SimpleNamespace(stream=MagicMock(return_value=content)),
            )
        )
    else:
        session._session = lambda: SimpleNamespace(get=MagicMock(return_value=content))
        session._url = lambda p: "https://example.invalid/" + p
        session._request_timeout = lambda: None
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("limit", [1, 5, 100])
async def test_bounded_provider_read_closes_at_prefix(provider: str, limit: int) -> None:
    content = _Content(b"\x00\xffabcdefghijk")
    session = _session(provider, content)
    assert await session.read_bounded(Path("out.jsonl"), max_bytes=limit) == content.data[:limit]
    assert content.closed
    assert content.offset <= max(9, limit + 2)
    if provider == "vercel":
        assert not session.mount_active


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_bounded_provider_read_closes_and_discards_failed_response(provider: str) -> None:
    content = _Content(b"private-response", fail=True)
    session = _session(provider, content)
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await session.read_bounded(Path("out.jsonl"), max_bytes=5)
    assert content.closed
    assert caught.value.__context__ is None
    assert "synthetic-private-response" not in repr(vars(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["daytona", "blaxel", "cloudflare"])
async def test_bounded_http_read_missing_file_closes_without_body(provider: str) -> None:
    content = _Content(b"private-error-body")
    session = _session(provider, content, status=404)
    with pytest.raises(WorkspaceReadNotFoundError):
        await session.read_bounded(Path("out.jsonl"), max_bytes=5)
    assert content.closed
    assert content.offset == 0


@pytest.mark.asyncio
async def test_cloudflare_bounded_read_decodes_existing_sse_format() -> None:
    expected = b"\x00\xffbinary\n"
    wire = (
        b'data: {"type":"metadata","isBinary":true}\n\n'
        b'data: {"type":"chunk","data":"' + base64.b64encode(expected) + b'"}\n\n'
        b'data: {"type":"complete"}\n\n'
    )
    content = _Content(wire)
    session = _session("cloudflare", content)
    assert await session.read_bounded(Path("out.jsonl"), max_bytes=4) == expected[:4]
    assert content.closed


@pytest.mark.asyncio
async def test_cloudflare_bounded_read_limits_encoded_response() -> None:
    content = _Content(b'data: {"type":"chunk","data":"' + b"x" * 100000)
    session = _session("cloudflare", content)
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await session.read_bounded(Path("out.jsonl"), max_bytes=4)
    assert caught.value.context["reason"] == "bounded_read_wire_limit"
    assert caught.value.__context__ is None
    assert content.offset < 66000
    assert content.closed


@pytest.mark.asyncio
async def test_docker_bounded_read_uses_trusted_utilities_without_encoding() -> None:
    pytest.importorskip("docker")
    from agents.sandbox.sandboxes.docker import DockerSandboxSession

    session = object.__new__(DockerSandboxSession)
    session._validate_path_access = AsyncMock(return_value=Path("/workspace/out.jsonl"))
    session.exec = AsyncMock(
        return_value=ExecResult(stdout=b"\x00\xffabc", stderr=b"", exit_code=0)
    )
    assert await session.read_bounded(Path("out.jsonl"), max_bytes=5) == b"\x00\xffabc"
    args = session.exec.call_args.args
    assert args[0] == "/bin/sh"
    assert "PATH=/usr/bin:/bin; export PATH" in args[2]
    assert "base64" not in args[2]
    assert args[-2:] == ("/workspace/out.jsonl", "5")
    assert session.exec.call_args.kwargs == {"shell": False, "timeout": 30.0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_error", [None, RuntimeError("Close failed"), asyncio.TimeoutError()]
)
async def test_modal_bounded_read_closes_provider_descriptor(close_error: Exception | None) -> None:
    pytest.importorskip("modal")
    from agents.extensions.sandbox.modal.sandbox import ModalSandboxSession

    session = object.__new__(ModalSandboxSession)
    session._validate_path_access = AsyncMock(return_value=Path("/workspace/out.jsonl"))
    session._ensure_sandbox = AsyncMock()
    stream = SimpleNamespace(
        read=SimpleNamespace(aio=AsyncMock(return_value=b"abc")),
        close=SimpleNamespace(aio=AsyncMock(side_effect=close_error)),
    )
    session._sandbox = SimpleNamespace(open=SimpleNamespace(aio=AsyncMock(return_value=stream)))
    if close_error is None:
        assert await session.read_bounded(Path("out.jsonl"), max_bytes=3) == b"abc"
    else:
        with pytest.raises(WorkspaceArchiveReadError) as caught:
            await session.read_bounded(Path("out.jsonl"), max_bytes=3)
        assert caught.value.context["reason"] == "bounded_read_failed"
        assert caught.value.__context__ is None
    stream.read.aio.assert_awaited_once_with(3)
    stream.close.aio.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,expected_requests",
    [
        (4 * 1024 * 1024, [4 * 1024 * 1024]),
        (8 * 1024 * 1024 + 1, [8 * 1024 * 1024 + 1, 4 * 1024 * 1024 + 1]),
        (101 * 1024 * 1024, [100 * 1024 * 1024, 97 * 1024 * 1024]),
    ],
)
async def test_modal_bounded_read_uses_large_bounded_requests(
    limit: int, expected_requests: list[int]
) -> None:
    pytest.importorskip("modal")
    from agents.extensions.sandbox.modal.sandbox import ModalSandboxSession

    session = object.__new__(ModalSandboxSession)
    session._validate_path_access = AsyncMock(return_value=Path("/workspace/out.jsonl"))
    session._ensure_sandbox = AsyncMock()
    history = b"x" * (4 * 1024 * 1024)
    remaining = history

    async def read(size: int) -> bytes:
        nonlocal remaining
        chunk, remaining = remaining[:size], remaining[size:]
        return chunk

    stream = SimpleNamespace(
        read=SimpleNamespace(aio=AsyncMock(side_effect=read)),
        close=SimpleNamespace(aio=AsyncMock()),
    )
    session._sandbox = SimpleNamespace(open=SimpleNamespace(aio=AsyncMock(return_value=stream)))
    assert await session.read_bounded(Path("out.jsonl"), max_bytes=limit) == history
    requests = [call.args[0] for call in stream.read.aio.await_args_list]
    assert requests == expected_requests
    assert all(size <= 100 * 1024 * 1024 for size in requests)
    stream.close.aio.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["acquisition", "body"])
async def test_daytona_bounded_read_timeout_preserves_retryability(phase: str) -> None:
    pytest.importorskip("daytona")
    from agents.extensions.sandbox.daytona.sandbox import DaytonaSandboxSessionState
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.snapshot import NoopSnapshot

    class TimeoutContent(_Content):
        async def chunks(self, chunk_size: int = 3) -> AsyncIterator[bytes]:
            yield b"x"
            raise asyncio.TimeoutError("synthetic-private-response")

    content = TimeoutContent(b"fixture")
    inner = _session("daytona", content)
    inner.state = DaytonaSandboxSessionState(
        sandbox_id="test", manifest=Manifest(), snapshot=NoopSnapshot(id="test")
    )
    if phase == "acquisition":
        inner._sandbox.fs._api_client.download_file_without_preload_content.side_effect = (
            asyncio.TimeoutError("synthetic-private-response")
        )
    events: list[SandboxSessionEvent] = []
    wrapper = SandboxSession(
        inner,
        instrumentation=Instrumentation(
            sinks=[CallbackSink(lambda event, _: events.append(event), mode="sync")]
        ),
    )
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await wrapper.read_bounded(Path("out.jsonl"), max_bytes=5)
    assert caught.value.retryable is True
    assert caught.value.cause is caught.value.__cause__ is caught.value.__context__ is None
    assert "synthetic-private-response" not in str(caught.value)
    assert content.closed is (phase == "body")
    finish = next(event for event in events if event.op == "read" and event.phase == "finish")
    assert isinstance(finish, SandboxSessionFinishEvent)
    assert finish.error_retryable is True
    assert "synthetic-private-response" not in finish.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,retryable",
    [
        (provider, retryable)
        for provider in ["cloudflare", "vercel", "runloop", "modal", "daytona", "e2b"]
        for retryable in [True, False]
    ]
    + [("blaxel", True), ("blaxel", None)],
)
async def test_bounded_provider_read_preserves_retry_policy(
    provider: str, retryable: bool | None
) -> None:
    import httpx

    from agents.sandbox.manifest import Manifest
    from agents.sandbox.session.sandbox_session_state import SandboxSessionState
    from agents.sandbox.snapshot import NoopSnapshot

    content = _Content(b"synthetic-private-response")
    close = AsyncMock()
    inner: Any
    if provider == "modal":
        modal = pytest.importorskip("modal")
        from agents.extensions.sandbox.modal.sandbox import ModalSandboxSession

        inner = object.__new__(ModalSandboxSession)
        inner._validate_path_access = AsyncMock(return_value=Path("/workspace/out.jsonl"))
        inner._ensure_sandbox = AsyncMock()
        error_cls = (
            modal.exception.InternalError if retryable else modal.exception.PermissionDeniedError
        )
        stream = SimpleNamespace(
            read=SimpleNamespace(
                aio=AsyncMock(side_effect=error_cls("synthetic-private-response"))
            ),
            close=SimpleNamespace(aio=close),
        )
        inner._sandbox = SimpleNamespace(open=SimpleNamespace(aio=AsyncMock(return_value=stream)))
    else:
        inner = _session(provider, content, status=503 if retryable else 403)
    inner.state = SandboxSessionState(
        type="test", manifest=Manifest(), snapshot=NoopSnapshot(id="test")
    )
    if provider == "e2b":
        from e2b import exceptions as sdk_errors

        error_cls = (
            sdk_errors.RateLimitException if retryable else sdk_errors.AuthenticationException
        )
        inner._sandbox.files.read.side_effect = error_cls("synthetic-private-response")
    elif provider == "daytona":
        from agents.extensions.sandbox.daytona.sandbox import DaytonaSandboxSessionState

        inner.state = DaytonaSandboxSessionState(
            sandbox_id="test", manifest=Manifest(), snapshot=NoopSnapshot(id="test")
        )
    elif provider == "vercel":
        from vercel import sandbox as sdk

        error_cls = sdk.SandboxRateLimitError if retryable else sdk.SandboxPermissionError
        inner._ensure_sandbox.return_value.iter_file.side_effect = error_cls(
            httpx.Response(429 if retryable else 403), "synthetic-private-response"
        )
    elif provider == "runloop":
        import runloop_api_client

        from agents.extensions.sandbox.runloop.sandbox import RunloopSandboxSessionState

        inner.state = RunloopSandboxSessionState(
            devbox_id="test", manifest=Manifest(), snapshot=NoopSnapshot(id="test")
        )
        error_cls = (
            runloop_api_client.RateLimitError
            if retryable
            else runloop_api_client.PermissionDeniedError
        )
        inner._sdk.api.devboxes.with_streaming_response.download_file.side_effect = error_cls(
            "synthetic-private-response",
            response=httpx.Response(
                429 if retryable else 403, request=httpx.Request("GET", "https://example.invalid")
            ),
            body={"detail": "synthetic-private-response"},
        )
    events: list[SandboxSessionEvent] = []
    wrapper = SandboxSession(
        inner,
        instrumentation=Instrumentation(
            sinks=[CallbackSink(lambda event, _: events.append(event), mode="sync")]
        ),
    )
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await wrapper.read_bounded(Path("out.jsonl"), max_bytes=5)
    assert caught.value.retryable is retryable
    assert caught.value.cause is caught.value.__cause__ is caught.value.__context__ is None
    finish = next(event for event in events if event.op == "read" and event.phase == "finish")
    assert isinstance(finish, SandboxSessionFinishEvent)
    assert finish.error_retryable is retryable
    assert "synthetic-private-response" not in finish.model_dump_json()
    if provider in {"cloudflare", "daytona", "blaxel"}:
        assert content.closed
        assert content.offset == 0
    elif provider == "modal":
        close.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_bounded_wrapper_forwards_limit() -> None:
    content = _Content(b"abcdefgh")
    inner = _session("daytona", content)
    # The public wrapper's instrumentation needs a real session state.
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.session.sandbox_session_state import SandboxSessionState
    from agents.sandbox.snapshot import NoopSnapshot

    inner.state = SandboxSessionState(
        type="test", manifest=Manifest(), snapshot=NoopSnapshot(id="test")
    )
    inner.read_bounded = AsyncMock(return_value=b"abc")
    wrapper = SandboxSession(inner)
    assert await wrapper.read_bounded(Path("out.jsonl"), max_bytes=3) == b"abc"
    inner.read_bounded.assert_awaited_once_with(Path("out.jsonl"), max_bytes=3)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="UnixLocal is not available on Windows")
async def test_native_bounded_read_needs_no_process(tmp_path: Path) -> None:
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )
    from agents.sandbox.snapshot import NoopSnapshot

    (tmp_path / "out.jsonl").write_bytes(b"\x00\xffbinary data")
    session = UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(tmp_path)), snapshot=NoopSnapshot(id="test")
        )
    )
    session.exec = AsyncMock(side_effect=AssertionError("No process required"))
    assert await session.read_bounded(Path("out.jsonl"), max_bytes=4) == b"\x00\xffbi"
    with pytest.raises(WorkspaceReadNotFoundError):
        await session.read_bounded(Path("missing"), max_bytes=4)
    with pytest.raises(ValueError):
        await session.read_bounded(Path("out.jsonl"), max_bytes=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_bounded_provider_read_cancellation_closes_response(provider: str) -> None:
    entered = asyncio.Event()

    class PausedContent(_Content):
        async def chunks(self, chunk_size: int = 3) -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def readexactly(self, size: int) -> bytes:
            entered.set()
            await asyncio.Event().wait()
            return b"unreachable"

    content = PausedContent(b"fixture")
    session = _session(provider, content)
    task = asyncio.create_task(session.read_bounded(Path("out.jsonl"), max_bytes=5))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert content.closed
    if provider == "vercel":
        assert not session.mount_active


@pytest.mark.asyncio
async def test_runloop_bounded_read_uses_streaming_sdk_response() -> None:
    import httpx

    pytest.importorskip("runloop_api_client")
    from runloop_api_client import AsyncRunloop

    consumed = 0
    closed = False

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal consumed
            for _ in range(10):
                consumed += 1
                yield b"x" * 65536

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/download_file")
        return httpx.Response(200, stream=Body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        async with AsyncRunloop(bearer_token="synthetic-test-key", http_client=http) as api:
            session = _session("runloop", _Content(b"unused"))
            session._sdk = SimpleNamespace(api=api)
            assert await session.read_bounded(Path("out.jsonl"), max_bytes=5) == b"xxxxx"
    assert consumed == 1
    assert closed


@pytest.mark.asyncio
async def test_e2b_bounded_read_closes_real_sdk_stream() -> None:
    import httpx

    pytest.importorskip("e2b")
    from e2b.sandbox.filesystem.filesystem import AsyncFileStreamReader

    consumed = 0
    closed = False

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal consumed
            for _ in range(10):
                consumed += 1
                yield b"x" * 65536

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    stream = AsyncFileStreamReader(httpx.Response(200, stream=Body()))
    session = _session("e2b", _Content(b"unused"))
    session._sandbox.files.read = AsyncMock(return_value=stream)
    assert await session.read_bounded(Path("out.jsonl"), max_bytes=5) == b"xxxxx"
    assert consumed == 1
    assert closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_error", [None, RuntimeError("Close failed"), asyncio.TimeoutError()]
)
async def test_modal_bounded_read_cancellation_closes_descriptor(
    close_error: Exception | None,
) -> None:
    pytest.importorskip("modal")
    from agents.extensions.sandbox.modal.sandbox import ModalSandboxSession

    entered = asyncio.Event()

    async def read(size: int) -> bytes:
        entered.set()
        await asyncio.Event().wait()
        return b"unreachable"

    session: Any = object.__new__(ModalSandboxSession)
    session._validate_path_access = AsyncMock(return_value=Path("/workspace/out.jsonl"))
    session._ensure_sandbox = AsyncMock()
    close = AsyncMock(side_effect=close_error)
    stream = SimpleNamespace(read=SimpleNamespace(aio=read), close=SimpleNamespace(aio=close))
    session._sandbox = SimpleNamespace(open=SimpleNamespace(aio=AsyncMock(return_value=stream)))
    task = asyncio.create_task(session.read_bounded(Path("out.jsonl"), max_bytes=5))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retryable", [(400, False), (429, True), (500, True), (418, None)])
async def test_daytona_bounded_read_classifies_http_status(
    status: int, retryable: bool | None
) -> None:
    content = _Content(b"synthetic-private-response")
    session = _session("daytona", content, status=status)
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await session.read_bounded(Path("out.jsonl"), max_bytes=5)
    assert caught.value.retryable is retryable
    assert content.closed
    assert content.offset == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancellation"])
async def test_vercel_bounded_read_preserves_primary_failure_during_close(outcome: str) -> None:
    import httpx

    pytest.importorskip("vercel.sandbox")
    from vercel import sandbox as sdk

    session = _session("vercel", _Content(b"fixture"))
    entered = asyncio.Event()
    closed = False

    class Stream:
        def __aiter__(self) -> Stream:
            return self

        async def __anext__(self) -> bytes:
            entered.set()
            if outcome == "failure":
                raise sdk.SandboxPermissionError(httpx.Response(403), "synthetic-private-read")
            if outcome == "cancellation":
                await asyncio.Event().wait()
            return b"fixture"

        async def aclose(self) -> None:
            nonlocal closed
            assert session.mount_active
            closed = True
            raise sdk.SandboxRateLimitError(httpx.Response(429), "synthetic-private-close")

    session._ensure_sandbox.return_value.iter_file.return_value = Stream()
    task = asyncio.create_task(session.read_bounded(Path("out.jsonl"), max_bytes=5))
    await asyncio.wait_for(entered.wait(), timeout=1)
    if outcome == "cancellation":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(WorkspaceArchiveReadError) as caught:
            await task
        assert caught.value.retryable is (outcome == "success")
        assert caught.value.__cause__ is caught.value.__context__ is None
        assert "synthetic-private" not in repr(vars(caught.value))
    assert closed
    assert not session.mount_active


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_name,retryable",
    [("RateLimitException", True), ("TimeoutException", True), ("AuthenticationException", False)],
)
async def test_e2b_bounded_stream_failure_preserves_retryability(
    error_name: str, retryable: bool
) -> None:
    pytest.importorskip("e2b")
    from e2b import exceptions as sdk_errors

    error_cls = getattr(sdk_errors, error_name)

    class FailedContent(_Content):
        async def chunks(self, chunk_size: int = 3) -> AsyncIterator[bytes]:
            yield b"x"
            raise error_cls("synthetic-private-response")

    content = FailedContent(b"fixture")
    session = _session("e2b", content)
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await session.read_bounded(Path("out.jsonl"), max_bytes=5)
    assert caught.value.retryable is retryable
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert "synthetic-private-response" not in repr(vars(caught.value))
    assert content.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retryable", [(429, None), (500, True), (502, True), (504, True)])
async def test_blaxel_bounded_read_preserves_existing_status_policy(
    status: int, retryable: bool | None
) -> None:
    content = _Content(b"synthetic-private-response")
    session = _session("blaxel", content, status=status)
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await session.read_bounded(Path("out.jsonl"), max_bytes=5)
    assert caught.value.retryable is retryable
    assert content.closed
    assert content.offset == 0
