from __future__ import annotations

import asyncio
import base64
import io
import json
import tarfile
import uuid
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest

from agents.extensions.sandbox.cloudflare import (
    CloudflareBucketMountStrategy,
    CloudflareSandboxClient,
    CloudflareSandboxClientOptions,
    CloudflareSandboxSession,
    CloudflareSandboxSessionState,
)
from agents.extensions.sandbox.cloudflare.sandbox import _CloudflarePtyProcessEntry
from agents.sandbox.entries import Dir, GCSMount, R2Mount, S3Mount
from agents.sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecTimeoutError,
    ExecTransportError,
    InvalidManifestPathError,
    MountConfigError,
    PtySessionNotFoundError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceWriteTypeError,
)
from agents.sandbox.manifest import Environment, Manifest
from agents.sandbox.session.dependencies import Dependencies
from agents.sandbox.session.pty_types import PTY_PROCESSES_MAX, allocate_pty_process_id
from agents.sandbox.snapshot import NoopSnapshot, SnapshotBase
from agents.sandbox.types import ExecResult
from agents.sandbox.workspace_paths import SandboxPathGrant

_WORKER_URL = "https://sandbox-cf.example.workers.dev"


class _FakeResponse:
    def __init__(self, status: int = 200, json_body: Any = None, raw_body: bytes = b"") -> None:
        self.status = status
        self._json_body = json_body
        self._raw_body = raw_body

    async def json(self, *, content_type: str | None = None) -> Any:
        _ = content_type
        if self._json_body is not None:
            return self._json_body
        return json.loads(self._raw_body)

    async def read(self) -> bytes:
        if self._json_body is not None:
            return json.dumps(self._json_body).encode()
        return self._raw_body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        _ = args


class _FakeStreamContent:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def iter_any(self) -> Any:
        yield self._data


class _FakeSSEResponse:
    def __init__(self, status: int, sse_body: bytes) -> None:
        self.status = status
        self.content = _FakeStreamContent(sse_body)

    async def json(self, *, content_type: str | None = None) -> Any:
        _ = content_type
        return {}

    async def __aenter__(self) -> _FakeSSEResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        _ = args


class _FakeHttp:
    def __init__(
        self, responses: dict[str, _FakeResponse | _FakeSSEResponse] | None = None
    ) -> None:
        self._responses: dict[tuple[str, str], _FakeResponse | _FakeSSEResponse] = {}
        self.default_response: _FakeResponse | _FakeSSEResponse = _FakeResponse(
            status=200, json_body={"ok": True}
        )
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.ws_connect_calls: list[dict[str, Any]] = []
        self.fake_ws: _FakeWebSocket | None = None
        if responses:
            for key, val in responses.items():
                method, _, suffix = key.partition(" ")
                self._responses[(method.upper(), suffix)] = val

    def _match(self, method: str, url: str) -> _FakeResponse | _FakeSSEResponse:
        for (m, suffix), resp in self._responses.items():
            if m == method and suffix in url:
                return resp
        return self.default_response

    def _record(self, method: str, url: str, **kwargs: Any) -> _FakeResponse | _FakeSSEResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._match(method, url)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse | _FakeSSEResponse:
        return self._record("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> _FakeResponse | _FakeSSEResponse:
        return self._record("GET", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> _FakeResponse | _FakeSSEResponse:
        return self._record("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> _FakeResponse | _FakeSSEResponse:
        return self._record("DELETE", url, **kwargs)

    async def ws_connect(self, url: str, **kwargs: Any) -> _FakeWebSocket:
        self.ws_connect_calls.append({"url": url, **kwargs})
        if self.fake_ws is None:
            raise RuntimeError("fake_ws must be set before ws_connect")
        return self.fake_ws

    async def close(self) -> None:
        self.closed = True


class _FakeWebSocket:
    def __init__(self, frames: list[aiohttp.WSMessage] | None = None) -> None:
        self.frames = list(frames or [])
        self.sent_bytes: list[bytes] = []
        self.closed = False

    async def receive(self) -> aiohttp.WSMessage:
        if self.frames:
            return self.frames.pop(0)
        return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self) -> None:
        self.closed = True


class _BlockingFakeWebSocket(_FakeWebSocket):
    async def receive(self) -> aiohttp.WSMessage:
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(60.0)
        return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)


def _valid_tar_bytes() -> bytes:
    """Return a minimal valid tar archive for hydrate tests."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="hello.txt")
        data = b"hello"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _RestorableSnapshot(SnapshotBase):
    type: str = "test_restorable_snapshot"
    payload: bytes = b""

    def __init__(self, **kwargs: object) -> None:
        if "payload" not in kwargs:
            kwargs["payload"] = _valid_tar_bytes()
        super().__init__(**kwargs)  # type: ignore[arg-type]

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = (data, dependencies)
        return None

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        return io.BytesIO(self.payload)

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return True


def _make_state(
    *,
    worker_url: str = _WORKER_URL,
    sandbox_id: str = "abc123",
    manifest: Manifest | None = None,
) -> CloudflareSandboxSessionState:
    return CloudflareSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=manifest or Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        worker_url=worker_url,
        sandbox_id=sandbox_id,
    )


def _make_session(
    *,
    state: CloudflareSandboxSessionState | None = None,
    fake_http: _FakeHttp | None = None,
    exec_timeout_s: float | None = None,
    request_timeout_s: float | None = None,
) -> CloudflareSandboxSession:
    sess = CloudflareSandboxSession(
        state=state or _make_state(),
        http=cast(Any, fake_http),
        exec_timeout_s=exec_timeout_s,
        request_timeout_s=request_timeout_s,
    )

    # Override remote path normalization so tests do not need a live exec endpoint
    # for the runtime helper script.  Dedicated tests verify the override is wired in.
    async def _sync_normalize(path: Path | str, *, for_write: bool = False) -> Path:
        return sess.normalize_path(path, for_write=for_write)

    sess._validate_path_access = _sync_normalize  # type: ignore[method-assign]
    return sess


def _build_sse_body(stdout: str = "", stderr: str = "", exit_code: int = 0) -> bytes:
    parts: list[str] = []
    if stdout:
        parts.append(f"event: stdout\ndata: {base64.b64encode(stdout.encode()).decode()}\n\n")
    if stderr:
        parts.append(f"event: stderr\ndata: {base64.b64encode(stderr.encode()).decode()}\n\n")
    parts.append(f'event: exit\ndata: {{"exit_code": {exit_code}}}\n\n')
    return "".join(parts).encode("utf-8")


def _exec_ok_response(stdout: str = "", stderr: str = "", exit_code: int = 0) -> _FakeSSEResponse:
    return _FakeSSEResponse(
        status=200,
        sse_body=_build_sse_body(stdout=stdout, stderr=stderr, exit_code=exit_code),
    )


def _streamed_payload_response(*, payload: bytes, is_binary: bool) -> _FakeResponse:
    chunk = base64.b64encode(payload).decode() if is_binary else payload.decode()
    body = (
        f'data: {{"type":"metadata","isBinary":{str(is_binary).lower()}}}\n\n'
        f'data: {{"type":"chunk","data":"{chunk}"}}\n\n'
        'data: {"type":"complete"}\n\n'
    ).encode()
    return _FakeResponse(status=200, raw_body=body)


def _truncated_streamed_payload_response(*, payload: bytes, is_binary: bool) -> _FakeResponse:
    chunk = base64.b64encode(payload).decode() if is_binary else payload.decode()
    body = (
        f'data: {{"type":"metadata","isBinary":{str(is_binary).lower()}}}\n\n'
        f'data: {{"type":"chunk","data":"{chunk}"}}\n\n'
    ).encode()
    return _FakeResponse(status=200, raw_body=body)


def _ws_text_frame(payload: dict[str, object]) -> aiohttp.WSMessage:
    return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(payload), None)


def _ws_binary_frame(payload: bytes) -> aiohttp.WSMessage:
    return aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, payload, None)


async def _register_pty_entry(
    session: CloudflareSandboxSession,
    *,
    ws: _FakeWebSocket,
    tty: bool,
    last_used: float = 0.0,
) -> int:
    pty_entry = _CloudflarePtyProcessEntry(ws=cast(Any, ws), tty=tty, last_used=last_used)
    async with session._pty_lock:
        process_id = allocate_pty_process_id(session._reserved_pty_process_ids)
        session._reserved_pty_process_ids.add(process_id)
        session._pty_processes[process_id] = pty_entry
    return process_id


def test_cloudflare_bucket_mount_strategy_round_trips_through_manifest_parse() -> None:
    manifest = Manifest.model_validate(
        {
            "entries": {
                "remote": {
                    "type": "s3_mount",
                    "bucket": "bucket",
                    "mount_strategy": {"type": "cloudflare_bucket_mount"},
                }
            }
        }
    )

    mount = manifest.entries["remote"]

    assert isinstance(mount, S3Mount)
    assert isinstance(mount.mount_strategy, CloudflareBucketMountStrategy)


def test_cloudflare_bucket_mount_strategy_builds_s3_config() -> None:
    strategy = CloudflareBucketMountStrategy()
    mount = S3Mount(
        bucket="bucket",
        access_key_id="access-key",
        secret_access_key="secret-key",
        prefix="nested/prefix/",
        mount_strategy=strategy,
        read_only=False,
    )

    config = strategy._build_cloudflare_bucket_mount_config(mount)  # noqa: SLF001

    assert config.bucket_name == "bucket"
    assert config.bucket_endpoint_url == "https://s3.amazonaws.com"
    assert config.provider == "s3"
    assert config.key_prefix == "/nested/prefix/"
    assert config.credentials == {
        "access_key_id": "access-key",
        "secret_access_key": "secret-key",
    }
    assert config.read_only is False


def test_cloudflare_bucket_mount_strategy_builds_r2_config() -> None:
    strategy = CloudflareBucketMountStrategy()
    mount = R2Mount(
        bucket="bucket",
        account_id="abc123accountid",
        access_key_id="access-key",
        secret_access_key="secret-key",
        mount_strategy=strategy,
    )

    config = strategy._build_cloudflare_bucket_mount_config(mount)  # noqa: SLF001

    assert config.bucket_name == "bucket"
    assert config.bucket_endpoint_url == "https://abc123accountid.r2.cloudflarestorage.com"
    assert config.provider == "r2"
    assert config.key_prefix is None
    assert config.credentials == {
        "access_key_id": "access-key",
        "secret_access_key": "secret-key",
    }
    assert config.read_only is True


def test_cloudflare_bucket_mount_strategy_builds_gcs_hmac_config() -> None:
    strategy = CloudflareBucketMountStrategy()
    mount = GCSMount(
        bucket="bucket",
        access_id="access-id",
        secret_access_key="secret-key",
        prefix="nested/prefix/",
        mount_strategy=strategy,
        read_only=False,
    )

    config = strategy._build_cloudflare_bucket_mount_config(mount)  # noqa: SLF001

    assert config.bucket_name == "bucket"
    assert config.bucket_endpoint_url == "https://storage.googleapis.com"
    assert config.provider == "gcs"
    assert config.key_prefix == "/nested/prefix/"
    assert config.credentials == {
        "access_key_id": "access-id",
        "secret_access_key": "secret-key",
    }
    assert config.read_only is False


def test_cloudflare_bucket_mount_strategy_rejects_gcs_native_auth() -> None:
    with pytest.raises(
        MountConfigError,
        match="gcs cloudflare bucket mounts require access_id and secret_access_key",
    ):
        GCSMount(
            bucket="bucket",
            service_account_file="/data/config/gcs.json",
            mount_strategy=CloudflareBucketMountStrategy(),
        )


def test_cloudflare_bucket_mount_strategy_rejects_s3_session_token() -> None:
    with pytest.raises(
        MountConfigError,
        match="cloudflare bucket mounts do not support s3 session_token credentials",
    ):
        S3Mount(
            bucket="bucket",
            access_key_id="access-key",
            secret_access_key="secret-key",
            session_token="session-token",
            mount_strategy=CloudflareBucketMountStrategy(),
        )


@pytest.mark.asyncio
async def test_cloudflare_create_uses_client_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_request_sandbox_id(
        self: CloudflareSandboxClient, worker_url: str, api_key: str | None, **kwargs: object
    ) -> str:
        return "mfrggzdfmy2tqnrzgezdgnbv"

    monkeypatch.setattr(CloudflareSandboxClient, "_request_sandbox_id", _fake_request_sandbox_id)

    client = CloudflareSandboxClient(exec_timeout_s=10.0, request_timeout_s=60.0)
    session = await client.create(
        options=CloudflareSandboxClientOptions(
            worker_url=_WORKER_URL,
        ),
        snapshot=None,
    )
    state = cast(CloudflareSandboxSessionState, session.state)
    assert state.worker_url == _WORKER_URL
    assert state.sandbox_id == "mfrggzdfmy2tqnrzgezdgnbv"
    # Timeouts should NOT be persisted in state.
    assert not hasattr(state, "exec_timeout_s")
    assert not hasattr(state, "request_timeout_s")
    # But the session instance should have them from the client, not from options.
    inner = cast(CloudflareSandboxSession, session._inner)
    assert inner._exec_timeout_s == 10.0
    assert inner._request_timeout_s == 60.0


@pytest.mark.asyncio
async def test_cloudflare_create_uses_injected_api_key_for_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_headers: list[dict[str, str]] = []

    async def _fake_request_sandbox_id(
        self: CloudflareSandboxClient, worker_url: str, api_key: str | None, **kwargs: object
    ) -> str:
        return "mfrggzdfmy2tqnrzgezdgnbv"

    monkeypatch.setattr(CloudflareSandboxClient, "_request_sandbox_id", _fake_request_sandbox_id)

    class _RecordingClientSession:
        def __init__(self, *, headers: dict[str, str] | None = None) -> None:
            self.headers = headers or {}
            self.closed = False
            created_headers.append(self.headers)

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setenv("CLOUDFLARE_SANDBOX_API_KEY", "env-token")
    monkeypatch.setattr(aiohttp, "ClientSession", _RecordingClientSession)

    client = CloudflareSandboxClient()
    session = await client.create(
        options=CloudflareSandboxClientOptions(
            worker_url=_WORKER_URL,
            api_key="injected-token",
        ),
        snapshot=None,
    )
    inner = cast(CloudflareSandboxSession, session._inner)
    inner._session()

    assert created_headers == [{"Authorization": "Bearer injected-token"}]
    await inner._close_http()


@pytest.mark.asyncio
async def test_cloudflare_create_rejects_non_workspace_root() -> None:
    client = CloudflareSandboxClient()
    with pytest.raises(ConfigurationError) as exc_info:
        await client.create(
            options=CloudflareSandboxClientOptions(worker_url=_WORKER_URL),
            manifest=Manifest(root="/tmp/app"),
            snapshot=None,
        )
    assert exc_info.value.error_code is ErrorCode.SANDBOX_CONFIG_INVALID
    assert exc_info.value.context["manifest_root"] == "/tmp/app"


@pytest.mark.asyncio
async def test_cloudflare_create_calls_post_sandbox_for_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that create() calls POST /sandbox and uses the returned ID."""
    requested_urls: list[str] = []

    async def _fake_request_sandbox_id(
        self: CloudflareSandboxClient, worker_url: str, api_key: str | None, **kwargs: object
    ) -> str:
        requested_urls.append(worker_url)
        return "server2generated3id4base32"

    monkeypatch.setattr(CloudflareSandboxClient, "_request_sandbox_id", _fake_request_sandbox_id)

    client = CloudflareSandboxClient()
    session = await client.create(
        options=CloudflareSandboxClientOptions(worker_url=_WORKER_URL),
        snapshot=None,
    )
    state = cast(CloudflareSandboxSessionState, session.state)
    assert state.sandbox_id == "server2generated3id4base32"
    assert requested_urls == [_WORKER_URL]


@pytest.mark.asyncio
async def test_cloudflare_create_raises_on_post_sandbox_failure() -> None:
    """Verify that create() raises ConfigurationError when POST /sandbox fails."""
    client = CloudflareSandboxClient()
    with pytest.raises(ConfigurationError) as exc_info:
        await client.create(
            options=CloudflareSandboxClientOptions(
                worker_url="https://unreachable.invalid",
            ),
            snapshot=None,
        )
    assert exc_info.value.error_code is ErrorCode.SANDBOX_CONFIG_INVALID


@pytest.mark.asyncio
async def test_cloudflare_resume_uses_client_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _running(self: CloudflareSandboxSession) -> bool:
        _ = self
        return False

    monkeypatch.setattr(CloudflareSandboxSession, "running", _running)

    client = CloudflareSandboxClient(exec_timeout_s=11.0, request_timeout_s=77.0)
    state = _make_state()
    session = await client.resume(state)
    inner = cast(CloudflareSandboxSession, session._inner)
    assert session.state is state
    # Timeouts come from the client, not from state.
    assert inner._exec_timeout_s == 11.0
    assert inner._request_timeout_s == 77.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_running", "workspace_root_ready", "workspace_preserved", "workspace_reusable"),
    [
        (False, False, False, False),
        (False, True, False, False),
        (True, False, True, False),
        (True, True, True, True),
    ],
)
async def test_cloudflare_resume_sets_preserved_state_from_running(
    monkeypatch: pytest.MonkeyPatch,
    is_running: bool,
    workspace_root_ready: bool,
    workspace_preserved: bool,
    workspace_reusable: bool,
) -> None:
    running_calls: list[str] = []

    async def _running(self: CloudflareSandboxSession) -> bool:
        running_calls.append(self.state.sandbox_id)
        return is_running

    monkeypatch.setattr(CloudflareSandboxSession, "running", _running)

    client = CloudflareSandboxClient()
    state = _make_state()
    state.workspace_root_ready = workspace_root_ready

    session = await client.resume(state)

    inner = cast(CloudflareSandboxSession, session._inner)
    assert running_calls == ["abc123"]
    assert inner._workspace_state_preserved_on_start() is workspace_preserved  # noqa: SLF001
    assert inner._system_state_preserved_on_start() is workspace_preserved  # noqa: SLF001
    assert inner._can_reuse_preserved_workspace_on_resume() is workspace_reusable  # noqa: SLF001
    assert state.workspace_root_ready is (workspace_root_ready and is_running)


@pytest.mark.asyncio
async def test_cloudflare_exec_decodes_sse_output() -> None:
    sess = _make_session(
        fake_http=_FakeHttp({"POST /exec": _exec_ok_response(stdout="hello\n", stderr="warn")})
    )
    result = await sess._exec_internal("echo", "hello", timeout=5.0)
    assert result.stdout == b"hello\n"
    assert result.stderr == b"warn"
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_cloudflare_exec_applies_manifest_environment() -> None:
    fake_http = _FakeHttp({"POST /exec": _exec_ok_response(stdout="hello")})
    sess = _make_session(
        state=_make_state(manifest=Manifest(environment=Environment(value={"A": "1", "B": "two"}))),
        fake_http=fake_http,
    )

    result = await sess._exec_internal("printenv", "A", timeout=5.0)

    assert result.exit_code == 0
    exec_calls = [call for call in fake_http.calls if call["method"] == "POST"]
    assert exec_calls[0]["json"]["argv"] == ["env", "A=1", "B=two", "printenv", "A"]


@pytest.mark.asyncio
async def test_cloudflare_exec_timeout_raises_exec_timeout_error() -> None:
    class _TimeoutHttp(_FakeHttp):
        def post(self, url: str, **kwargs: Any) -> Any:
            self._record("POST", url, **kwargs)
            raise asyncio.TimeoutError()

    with pytest.raises(ExecTimeoutError):
        await _make_session(fake_http=_TimeoutHttp())._exec_internal("sleep", "999", timeout=1.0)


@pytest.mark.asyncio
async def test_cloudflare_exec_stream_without_exit_raises_transport_error() -> None:
    sess = _make_session(
        fake_http=_FakeHttp(
            {
                "POST /exec": _FakeSSEResponse(
                    status=200, sse_body=b"event: stdout\ndata: aGVsbG8=\n\n"
                )
            }
        )
    )
    with pytest.raises(ExecTransportError):
        await sess._exec_internal("echo", "hello", timeout=5.0)


@pytest.mark.asyncio
async def test_cloudflare_read_and_write_use_file_endpoints() -> None:
    fake_http = _FakeHttp(
        {
            "GET /file/": _FakeResponse(status=200, raw_body=b"file-content"),
            "PUT /file/": _FakeResponse(status=200, json_body={"ok": True}),
        }
    )
    sess = _make_session(fake_http=fake_http)
    result = await sess.read(Path("/workspace/test.txt"))
    assert result.read() == b"file-content"
    await sess.write(Path("/workspace/out.txt"), io.BytesIO(b"data"))
    get_calls = [c for c in fake_http.calls if c["method"] == "GET"]
    put_calls = [c for c in fake_http.calls if c["method"] == "PUT"]
    assert "/file/workspace/test.txt" in get_calls[0]["url"]
    assert "/file/workspace/out.txt" in put_calls[0]["url"]


@pytest.mark.asyncio
async def test_cloudflare_mount_and_unmount_bucket_use_http_endpoints() -> None:
    fake_http = _FakeHttp(
        {
            "POST /mount": _FakeResponse(status=200, json_body={"ok": True}),
            "POST /unmount": _FakeResponse(status=200, json_body={"ok": True}),
        }
    )
    sess = _make_session(fake_http=fake_http)

    await sess.mount_bucket(
        bucket="my-bucket",
        mount_path=Path("/workspace/data"),
        options={
            "endpoint": "https://s3.amazonaws.com",
            "readOnly": True,
        },
    )
    await sess.unmount_bucket(Path("/workspace/data"))

    mount_call = next(c for c in fake_http.calls if "/mount" in c["url"])
    unmount_call = next(c for c in fake_http.calls if "/unmount" in c["url"])
    assert mount_call["json"] == {
        "bucket": "my-bucket",
        "mountPath": "/workspace/data",
        "options": {
            "endpoint": "https://s3.amazonaws.com",
            "readOnly": True,
        },
    }
    assert unmount_call["json"] == {"mountPath": "/workspace/data"}


@pytest.mark.asyncio
async def test_cloudflare_mount_and_unmount_validate_path_access_for_write() -> None:
    fake_http = _FakeHttp(
        {
            "POST /mount": _FakeResponse(status=200, json_body={"ok": True}),
            "POST /unmount": _FakeResponse(status=200, json_body={"ok": True}),
        }
    )
    sess = _make_session(fake_http=fake_http)
    calls: list[tuple[str, bool]] = []

    async def _tracking_normalize(path: Path | str, *, for_write: bool = False) -> Path:
        calls.append((Path(path).as_posix(), for_write))
        return sess.normalize_path(path, for_write=for_write)

    sess._validate_path_access = _tracking_normalize  # type: ignore[method-assign]

    await sess.mount_bucket(
        bucket="my-bucket",
        mount_path=Path("/workspace/data"),
        options={
            "endpoint": "https://s3.amazonaws.com",
            "readOnly": True,
        },
    )
    await sess.unmount_bucket(Path("/workspace/data"))

    assert calls == [
        ("/workspace/data", True),
        ("/workspace/data", True),
    ]


@pytest.mark.asyncio
async def test_cloudflare_mount_rejects_read_only_extra_path_grant() -> None:
    fake_http = _FakeHttp({"POST /mount": _FakeResponse(status=200, json_body={"ok": True})})
    sess = _make_session(
        state=_make_state(
            manifest=Manifest(
                extra_path_grants=(SandboxPathGrant(path="/tmp/protected", read_only=True),)
            )
        ),
        fake_http=fake_http,
    )

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        await sess.mount_bucket(
            bucket="my-bucket",
            mount_path=Path("/tmp/protected/data"),
            options={
                "endpoint": "https://s3.amazonaws.com",
                "readOnly": True,
            },
        )

    assert fake_http.calls == []
    assert str(exc_info.value) == "failed to write archive for path: /tmp/protected/data"
    assert exc_info.value.context == {
        "path": "/tmp/protected/data",
        "reason": "read_only_extra_path_grant",
        "grant_path": "/tmp/protected",
    }


async def test_cloudflare_read_decodes_streamed_file_payload() -> None:
    sess = _make_session(
        fake_http=_FakeHttp(
            {"GET /file/": _streamed_payload_response(payload=b"file-content", is_binary=False)}
        )
    )
    result = await sess.read(Path("/workspace/test.txt"))
    assert result.read() == b"file-content"


@pytest.mark.asyncio
async def test_cloudflare_read_leaves_raw_data_prefix_payload_unchanged() -> None:
    raw_payload = b'data: this is a normal file, not an SSE payload\n{"ok": false}\n'
    sess = _make_session(
        fake_http=_FakeHttp({"GET /file/": _FakeResponse(status=200, raw_body=raw_payload)})
    )
    result = await sess.read(Path("/workspace/test.txt"))
    assert result.read() == raw_payload


@pytest.mark.asyncio
async def test_cloudflare_read_rejects_truncated_streamed_file_payload() -> None:
    sess = _make_session(
        fake_http=_FakeHttp(
            {
                "GET /file/": _truncated_streamed_payload_response(
                    payload=b"file-content",
                    is_binary=False,
                )
            }
        )
    )
    with pytest.raises(WorkspaceArchiveReadError):
        await sess.read(Path("/workspace/test.txt"))


@pytest.mark.asyncio
async def test_cloudflare_read_404_and_write_non_bytes_raise_structured_errors() -> None:
    fake_http = _FakeHttp(
        {"GET /file/": _FakeResponse(status=404, json_body={"error": "not found"})}
    )
    sess = _make_session(fake_http=fake_http)
    with pytest.raises(WorkspaceReadNotFoundError):
        await sess.read(Path("/workspace/missing.txt"))

    class _BadIO(io.IOBase):
        def read(self, *args: Any) -> int:
            _ = args
            return 42

    with pytest.raises(WorkspaceWriteTypeError):
        await sess.write(Path("/workspace/out.txt"), _BadIO())


@pytest.mark.asyncio
async def test_cloudflare_read_and_write_normalize_workspace_paths() -> None:
    fake_http = _FakeHttp()
    sess = _make_session(fake_http=fake_http)

    with pytest.raises(InvalidManifestPathError):
        await sess.read(Path("../secret.txt"))
    with pytest.raises(InvalidManifestPathError):
        await sess.write(Path("/workspace/../secret.txt"), io.BytesIO(b"data"))

    assert fake_http.calls == []


@pytest.mark.asyncio
async def test_cloudflare_persist_and_hydrate_use_http_endpoints() -> None:
    fake_http = _FakeHttp(
        {
            "POST /persist": _FakeResponse(status=200, raw_body=b"fake-tar"),
            "POST /hydrate": _FakeResponse(status=200, json_body={"ok": True}),
        }
    )
    manifest = Manifest(entries={Path("cache"): Dir(ephemeral=True)})
    sess = _make_session(state=_make_state(manifest=manifest), fake_http=fake_http)
    sess.register_persist_workspace_skip_path("generated/runtime")
    persisted = await sess.persist_workspace()
    assert persisted.read() == b"fake-tar"
    await sess.hydrate_workspace(io.BytesIO(_valid_tar_bytes()))
    persist_calls = [c for c in fake_http.calls if c["method"] == "POST" and "/persist" in c["url"]]
    hydrate_calls = [c for c in fake_http.calls if c["method"] == "POST" and "/hydrate" in c["url"]]
    assert "root" not in persist_calls[0]["params"]
    assert "cache" in persist_calls[0]["params"]["excludes"]
    assert "generated/runtime" in persist_calls[0]["params"]["excludes"]
    assert "root" not in hydrate_calls[0].get("params", {})


@pytest.mark.asyncio
async def test_cloudflare_persist_unmounts_and_remounts_ephemeral_bucket_mounts() -> None:
    fake_http = _FakeHttp(
        {
            "POST /mount": _FakeResponse(status=200, json_body={"ok": True}),
            "POST /unmount": _FakeResponse(status=200, json_body={"ok": True}),
            "POST /persist": _FakeResponse(status=200, raw_body=b"fake-tar"),
        }
    )
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                prefix="nested/prefix/",
                mount_strategy=CloudflareBucketMountStrategy(),
            )
        }
    )
    sess = _make_session(state=_make_state(manifest=manifest), fake_http=fake_http)

    persisted = await sess.persist_workspace()

    assert persisted.read() == b"fake-tar"
    assert [call["url"].split("/")[-1] for call in fake_http.calls] == [
        "unmount",
        "persist",
        "mount",
    ]


@pytest.mark.asyncio
async def test_cloudflare_hydrate_unmounts_and_remounts_ephemeral_bucket_mounts() -> None:
    fake_http = _FakeHttp(
        {
            "POST /mount": _FakeResponse(status=200, json_body={"ok": True}),
            "POST /unmount": _FakeResponse(status=200, json_body={"ok": True}),
            "POST /hydrate": _FakeResponse(status=200, json_body={"ok": True}),
        }
    )
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                prefix="nested/prefix/",
                mount_strategy=CloudflareBucketMountStrategy(),
            )
        }
    )
    sess = _make_session(state=_make_state(manifest=manifest), fake_http=fake_http)

    await sess.hydrate_workspace(io.BytesIO(_valid_tar_bytes()))

    assert [call["url"].split("/")[-1] for call in fake_http.calls] == [
        "unmount",
        "hydrate",
        "mount",
    ]


@pytest.mark.asyncio
async def test_cloudflare_resume_start_hydrates_without_preemptive_unmount() -> None:
    fake_http = _FakeHttp({"POST /hydrate": _FakeResponse(status=200, json_body={"ok": True})})
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                prefix="nested/prefix/",
                mount_strategy=CloudflareBucketMountStrategy(),
            )
        }
    )
    sess = _make_session(state=_make_state(manifest=manifest), fake_http=fake_http)
    sess.state.snapshot = _RestorableSnapshot(id="snapshot")
    sess.state.workspace_root_ready = True
    sess._start_workspace_root_ready = True  # noqa: SLF001
    sess._set_start_state_preserved(True)  # noqa: SLF001

    async def _exec_internal(*command: str | Path, timeout: float | None = None) -> ExecResult:
        _ = (command, timeout)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    sess._exec_internal = _exec_internal  # type: ignore[method-assign]

    await sess.start()

    assert [call["url"].split("/")[-1] for call in fake_http.calls] == [
        "running",
        "hydrate",
        "mount",
    ]


@pytest.mark.asyncio
async def test_cloudflare_resume_start_skips_hydrate_when_shared_resume_gate_matches() -> None:
    fake_http = _FakeHttp({"GET /running": _FakeResponse(status=200, json_body={"running": True})})
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                prefix="nested/prefix/",
                mount_strategy=CloudflareBucketMountStrategy(),
            )
        }
    )
    sess = _make_session(state=_make_state(manifest=manifest), fake_http=fake_http)
    sess.state.snapshot = _RestorableSnapshot(id="snapshot")
    sess.state.workspace_root_ready = True
    sess._start_workspace_root_ready = True  # noqa: SLF001
    sess._set_start_state_preserved(True)  # noqa: SLF001

    async def _exec_internal(*command: str | Path, timeout: float | None = None) -> ExecResult:
        _ = (command, timeout)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def _gate(*, is_running: bool) -> bool:
        assert is_running is True
        return True

    sess._exec_internal = _exec_internal  # type: ignore[method-assign]
    sess._can_skip_snapshot_restore_on_resume = _gate  # type: ignore[method-assign]

    await sess.start()

    assert [call["url"].split("/")[-1] for call in fake_http.calls] == [
        "running",
        "mount",
    ]


@pytest.mark.asyncio
async def test_cloudflare_resume_start_unmounts_before_hydrate_when_sandbox_is_running() -> None:
    fake_http = _FakeHttp(
        {
            "GET /running": _FakeResponse(status=200, json_body={"running": True}),
            "POST /unmount": _FakeResponse(status=200, json_body={"ok": True}),
            "POST /hydrate": _FakeResponse(status=200, json_body={"ok": True}),
        }
    )
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                prefix="nested/prefix/",
                mount_strategy=CloudflareBucketMountStrategy(),
            )
        }
    )
    sess = _make_session(state=_make_state(manifest=manifest), fake_http=fake_http)
    sess.state.snapshot = _RestorableSnapshot(id="snapshot")
    sess.state.workspace_root_ready = True
    sess._start_workspace_root_ready = True  # noqa: SLF001
    sess._set_start_state_preserved(True)  # noqa: SLF001

    async def _exec_internal(*command: str | Path, timeout: float | None = None) -> ExecResult:
        _ = (command, timeout)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    sess._exec_internal = _exec_internal  # type: ignore[method-assign]

    await sess.start()

    assert [call["url"].split("/")[-1] for call in fake_http.calls] == [
        "running",
        "unmount",
        "hydrate",
        "mount",
    ]


@pytest.mark.asyncio
async def test_cloudflare_persist_preserves_hidden_exclude_paths() -> None:
    fake_http = _FakeHttp({"POST /persist": _FakeResponse(status=200, raw_body=b"fake-tar")})
    sess = _make_session(fake_http=fake_http)
    sess.register_persist_workspace_skip_path(".sandbox-blobfuse-config/session")
    sess.register_persist_workspace_skip_path("./generated/runtime")

    await sess.persist_workspace()

    persist_calls = [c for c in fake_http.calls if c["method"] == "POST" and "/persist" in c["url"]]
    assert persist_calls[0]["params"]["excludes"].split(",") == [
        ".sandbox-blobfuse-config/session",
        "generated/runtime",
    ]


@pytest.mark.asyncio
async def test_cloudflare_persist_decodes_streamed_archive_payload() -> None:
    fake_http = _FakeHttp(
        {"POST /persist": _streamed_payload_response(payload=b"fake-tar", is_binary=True)}
    )
    sess = _make_session(fake_http=fake_http)
    persisted = await sess.persist_workspace()
    assert persisted.read() == b"fake-tar"


@pytest.mark.asyncio
async def test_cloudflare_persist_leaves_raw_data_prefix_archive_unchanged() -> None:
    raw_payload = b"data: raw tar bytes that happen to share the prefix"
    fake_http = _FakeHttp({"POST /persist": _FakeResponse(status=200, raw_body=raw_payload)})
    sess = _make_session(fake_http=fake_http)
    persisted = await sess.persist_workspace()
    assert persisted.read() == raw_payload


@pytest.mark.asyncio
async def test_cloudflare_persist_rejects_truncated_streamed_archive_payload() -> None:
    fake_http = _FakeHttp(
        {"POST /persist": _truncated_streamed_payload_response(payload=b"fake-tar", is_binary=True)}
    )
    sess = _make_session(fake_http=fake_http)
    with pytest.raises(WorkspaceArchiveReadError):
        await sess.persist_workspace()


@pytest.mark.asyncio
async def test_cloudflare_delete_calls_shutdown() -> None:
    fake_http = _FakeHttp()
    inner = _make_session(state=_make_state(), fake_http=fake_http)
    client = CloudflareSandboxClient()
    session = client._wrap_session(inner)
    await client.delete(session)
    delete_calls = [c for c in fake_http.calls if c["method"] == "DELETE"]
    assert len(delete_calls) == 1


@pytest.mark.asyncio
async def test_cloudflare_supports_pty() -> None:
    sess = _make_session()
    assert sess.supports_pty() is True


@pytest.mark.asyncio
async def test_cloudflare_pty_exec_start_opens_websocket_and_sends_command() -> None:
    fake_http = _FakeHttp()
    fake_http.fake_ws = _FakeWebSocket(
        frames=[
            _ws_text_frame({"type": "ready"}),
            _ws_binary_frame(b">>> "),
            _ws_text_frame({"type": "exit", "code": 0}),
        ]
    )
    sess = _make_session(fake_http=fake_http)

    started = await sess.pty_exec_start("python3", shell=False, tty=True, yield_time_s=0.05)

    assert started.process_id is None
    assert started.exit_code == 0
    assert started.output == b">>> "
    assert fake_http.ws_connect_calls == [
        {"url": "wss://sandbox-cf.example.workers.dev/v1/sandbox/abc123/pty?cols=80&rows=24"}
    ]
    assert fake_http.fake_ws.sent_bytes == [b"python3\n"]
    assert fake_http.fake_ws.closed is True


@pytest.mark.asyncio
async def test_cloudflare_pty_write_stdin_sends_input_and_collects_output() -> None:
    fake_ws = _FakeWebSocket()
    sess = _make_session(fake_http=_FakeHttp())
    process_id = await _register_pty_entry(sess, ws=fake_ws, tty=True)
    entry = sess._pty_processes[process_id]

    async with entry.output_lock:
        entry.output_chunks.append(b"10\n")
    entry.output_notify.set()

    updated = await sess.pty_write_stdin(
        session_id=process_id,
        chars="5 + 5\n",
        yield_time_s=0.05,
    )

    assert updated.process_id == process_id
    assert updated.exit_code is None
    assert updated.output == b"10\n"
    assert fake_ws.sent_bytes == [b"5 + 5\n"]


@pytest.mark.asyncio
async def test_cloudflare_pty_write_stdin_rejects_unknown_session() -> None:
    sess = _make_session(fake_http=_FakeHttp())

    with pytest.raises(PtySessionNotFoundError):
        await sess.pty_write_stdin(session_id=999_999, chars="")


@pytest.mark.asyncio
async def test_cloudflare_pty_write_stdin_rejects_non_tty_input() -> None:
    fake_ws = _FakeWebSocket()
    sess = _make_session(fake_http=_FakeHttp())
    process_id = await _register_pty_entry(sess, ws=fake_ws, tty=False)

    with pytest.raises(RuntimeError, match="stdin is not available for this process"):
        await sess.pty_write_stdin(session_id=process_id, chars="hello")


@pytest.mark.asyncio
async def test_cloudflare_pty_terminate_all_closes_websockets() -> None:
    sess = _make_session(fake_http=_FakeHttp())
    fake_ws_1 = _FakeWebSocket()
    fake_ws_2 = _FakeWebSocket()
    await _register_pty_entry(sess, ws=fake_ws_1, tty=True)
    await _register_pty_entry(sess, ws=fake_ws_2, tty=True)

    await sess.pty_terminate_all()

    assert sess._pty_processes == {}
    assert sess._reserved_pty_process_ids == set()
    assert fake_ws_1.closed is True
    assert fake_ws_2.closed is True


@pytest.mark.asyncio
async def test_cloudflare_pty_exec_start_prunes_oldest_session() -> None:
    fake_http = _FakeHttp()
    sess = _make_session(fake_http=fake_http)
    oldest_ws = _FakeWebSocket()
    await _register_pty_entry(sess, ws=oldest_ws, tty=True, last_used=0.0)
    for index in range(1, PTY_PROCESSES_MAX):
        await _register_pty_entry(
            sess,
            ws=_FakeWebSocket(),
            tty=True,
            last_used=float(index),
        )

    fake_http.fake_ws = _BlockingFakeWebSocket(frames=[_ws_text_frame({"type": "ready"})])

    started = await sess.pty_exec_start("python3", shell=False, tty=True, yield_time_s=0.05)

    assert started.process_id is not None
    assert oldest_ws.closed is True
    assert len(sess._pty_processes) == PTY_PROCESSES_MAX


@pytest.mark.asyncio
async def test_cloudflare_pty_exec_start_wraps_websocket_connect_failures() -> None:
    class _FailingHttp(_FakeHttp):
        async def ws_connect(self, url: str, **kwargs: Any) -> _FakeWebSocket:
            _ = (url, kwargs)
            raise aiohttp.ClientError("connect failed")

    sess = _make_session(fake_http=_FailingHttp())

    with pytest.raises(ExecTransportError) as exc_info:
        await sess.pty_exec_start("python3", shell=False, tty=True)

    assert isinstance(exc_info.value.__cause__, aiohttp.ClientError)
    assert str(exc_info.value.__cause__) == "connect failed"


@pytest.mark.asyncio
async def test_cloudflare_pty_exec_start_wraps_ready_timeout() -> None:
    class _NeverReadyWebSocket(_FakeWebSocket):
        async def receive(self) -> aiohttp.WSMessage:
            raise asyncio.TimeoutError()

    fake_http = _FakeHttp()
    fake_http.fake_ws = _NeverReadyWebSocket()
    sess = _make_session(fake_http=fake_http)

    with pytest.raises(ExecTimeoutError):
        await sess.pty_exec_start("python3", shell=False, tty=True)

    assert fake_http.fake_ws.closed is True


@pytest.mark.asyncio
async def test_cloudflare_stop_terminates_active_pty_sessions() -> None:
    fake_http = _FakeHttp({"POST /persist": _FakeResponse(status=200, raw_body=b"fake-tar")})
    sess = _make_session(fake_http=fake_http)
    fake_ws = _FakeWebSocket()
    process_id = await _register_pty_entry(sess, ws=fake_ws, tty=True)

    await sess.stop()

    assert fake_ws.closed is True
    with pytest.raises(PtySessionNotFoundError):
        await sess.pty_write_stdin(session_id=process_id, chars="")


@pytest.mark.asyncio
async def test_cloudflare_hydrate_rejects_unsafe_tar() -> None:
    """Verify that _hydrate_workspace_via_http rejects archives with path-traversal members."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="../../etc/passwd")
        info.size = 5
        tar.addfile(info, io.BytesIO(b"evil\n"))
    buf.seek(0)

    fake_http = _FakeHttp({"POST /hydrate": _FakeResponse(status=200, json_body={"ok": True})})
    sess = _make_session(fake_http=fake_http)

    from agents.sandbox.errors import WorkspaceArchiveWriteError

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        await sess._hydrate_workspace_via_http(buf)

    assert exc_info.value.context.get("reason") == "unsafe_or_invalid_tar"
    assert exc_info.value.context.get("member") is not None
    # The HTTP POST should never have been made.
    assert not any(c["method"] == "POST" and "/hydrate" in c["url"] for c in fake_http.calls)


def test_cloudflare_runtime_helpers_returns_resolve_helper() -> None:
    """Verify that _runtime_helpers() includes the workspace path resolver."""
    from agents.sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER

    sess = _make_session()
    helpers = sess._runtime_helpers()
    assert RESOLVE_WORKSPACE_PATH_HELPER in helpers
    assert sess._current_runtime_helper_cache_key() == sess.state.sandbox_id


@pytest.mark.asyncio
async def test_cloudflare_read_validates_path_access() -> None:
    """Verify that read() routes through _validate_path_access for symlink safety."""
    fake_http = _FakeHttp({"GET /file/": _FakeResponse(status=200, raw_body=b"file-content")})
    sess = _make_session(fake_http=fake_http)

    calls: list[tuple[str, bool]] = []

    async def _tracking_normalize(path: Path | str, *, for_write: bool = False) -> Path:
        calls.append((Path(path).as_posix(), for_write))
        # Fall back to synchronous normalize_path to avoid needing a real remote.
        return sess.normalize_path(path, for_write=for_write)

    sess._validate_path_access = _tracking_normalize  # type: ignore[method-assign]

    await sess.read(Path("/workspace/test.txt"))
    assert calls == [("/workspace/test.txt", False)]


@pytest.mark.asyncio
async def test_cloudflare_write_validates_path_access_for_write() -> None:
    """Verify that write() routes through _validate_path_access(for_write=True)."""
    fake_http = _FakeHttp({"PUT /file/": _FakeResponse(status=200, json_body={"ok": True})})
    sess = _make_session(fake_http=fake_http)

    calls: list[tuple[str, bool]] = []

    async def _tracking_normalize(path: Path | str, *, for_write: bool = False) -> Path:
        calls.append((Path(path).as_posix(), for_write))
        return sess.normalize_path(path, for_write=for_write)

    sess._validate_path_access = _tracking_normalize  # type: ignore[method-assign]

    await sess.write(Path("/workspace/out.txt"), io.BytesIO(b"data"))
    assert calls == [("/workspace/out.txt", True)]


@pytest.mark.asyncio
async def test_cloudflare_shutdown_logs_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that _shutdown_backend logs at DEBUG when the DELETE request fails."""
    import logging

    class _FailingDeleteHttp(_FakeHttp):
        def delete(self, url: str, **kwargs: Any) -> Any:
            raise aiohttp.ClientError("delete failed")

    sess = _make_session(fake_http=_FailingDeleteHttp())
    with caplog.at_level(logging.DEBUG, logger="agents.extensions.sandbox.cloudflare.sandbox"):
        await sess._shutdown_backend()

    assert any("Failed to delete Cloudflare sandbox" in r.message for r in caplog.records)
