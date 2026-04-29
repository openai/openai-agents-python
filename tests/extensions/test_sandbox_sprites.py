from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio

pytest.importorskip("agents.extensions.sandbox.sprites.sandbox")

from sprites.exceptions import NotFoundError as _SpritesNotFoundError  # noqa: E402

from agents.extensions.sandbox.sprites import (  # noqa: E402
    SpritesPlatformContext,
    SpritesSandboxClient,
    SpritesSandboxClientOptions,
    SpritesSandboxSession,
    SpritesSandboxSessionState,
    sandbox as sprites_sandbox,  # noqa: E402
)
from agents.sandbox.errors import (  # noqa: E402
    ConfigurationError,
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceWriteTypeError,
)
from agents.sandbox.manifest import Manifest  # noqa: E402
from agents.sandbox.session.sandbox_client import BaseSandboxClientOptions  # noqa: E402
from agents.sandbox.snapshot import NoopSnapshot  # noqa: E402
from agents.sandbox.types import ExecResult, ExposedPortEndpoint, User  # noqa: E402

SPRITE_NAME = "sprite-test-1"
SESSION_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _clear_platform_context_cache() -> Any:
    """Make sure cached platform-context text from one test doesn't leak."""

    from agents.extensions.sandbox.sprites import clear_platform_context_cache

    clear_platform_context_cache()
    yield
    clear_platform_context_cache()


def _attach(inner: SpritesSandboxSession, *, client: Any, sprite: Any = None) -> None:
    """Inject fake client/sprite into a SpritesSandboxSession.

    ``setattr`` is used to sidestep mypy's invariant attribute typing — the fakes
    duck-type the real ``SpritesClient``/``Sprite`` interface only as far as the
    tests exercise.

    Also marks ``_warmth_verified=True`` so I/O paths skip the lazy
    wake-up poll — the test has already set up the fake sprite directly,
    so we can trust it's "warm enough" for the assertions under test.
    Tests that specifically exercise the wait-for-running poll override
    this back to False.
    """

    # ``setattr`` (instead of plain assignment) silences mypy's invariant attribute
    # check; the fakes only duck-type the parts we exercise.
    setattr(inner, "_client", client)  # noqa: B010
    if sprite is not None:
        setattr(inner, "_sprite", sprite)  # noqa: B010
        setattr(inner, "_warmth_verified", True)  # noqa: B010


# ---------- Fakes ----------


class _FakeFileNotFound(Exception):
    """Stands in for ``sprites.exceptions.FileNotFoundError_`` in fake fs ops."""


class _FakeOpConn:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        wait_event: asyncio.Event | None = None,
        start_failure: BaseException | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.exit_code = exit_code
        self._wait_event = wait_event
        self._start_failure = start_failure
        self.signals: list[str] = []
        self.write_calls: list[bytes] = []
        self.closed = False
        self.on_stdout: Any = None
        self.on_stderr: Any = None
        self.on_message: Any = None

    async def wait(self) -> int:
        if self._wait_event is not None:
            await self._wait_event.wait()
        return self.exit_code

    def get_stdout(self) -> bytes:
        return self._stdout

    def get_stderr(self) -> bytes:
        return self._stderr

    def get_exit_code(self) -> int:
        return self.exit_code

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def signal(self, sig: str) -> None:
        self.signals.append(sig)

    async def write(self, data: bytes) -> None:
        self.write_calls.append(data)


class _FakeControlConnection:
    def __init__(self) -> None:
        self.start_op_calls: list[dict[str, Any]] = []
        # Each entry is consumed in FIFO order; if empty, a default zero-exit op is returned.
        self.next_ops: list[_FakeOpConn] = []
        self.start_op_failures: list[BaseException] = []

    async def start_op(
        self,
        op: str,
        cmd: list[str] | None = None,
        env: dict[str, str] | None = None,
        dir: str | None = None,
        tty: bool = False,
        rows: int = 24,
        cols: int = 80,
        stdin: bool = True,
    ) -> _FakeOpConn:
        self.start_op_calls.append(
            {"op": op, "cmd": list(cmd or []), "dir": dir, "tty": tty, "stdin": stdin}
        )
        if self.start_op_failures:
            raise self.start_op_failures.pop(0)
        if self.next_ops:
            return self.next_ops.pop(0)
        return _FakeOpConn()


class _FakeSpritePath:
    def __init__(self, fs: _FakeSpriteFilesystem, path: str) -> None:
        self._fs = fs
        self._path = path

    def read_bytes(self) -> bytes:
        if self._fs.read_failure is not None:
            raise self._fs.read_failure
        if self._path not in self._fs.files:
            raise _FakeFileNotFound(self._path)
        return self._fs.files[self._path]

    def write_bytes(self, data: bytes) -> None:
        if self._fs.write_failure is not None:
            raise self._fs.write_failure
        self._fs.files[self._path] = bytes(data)


class _FakeSpriteFilesystem:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.read_failure: BaseException | None = None
        self.write_failure: BaseException | None = None

    def __truediv__(self, path: str) -> _FakeSpritePath:
        return _FakeSpritePath(self, path)


class _FakeService:
    def __init__(self, *, http_port: int | None) -> None:
        self.http_port = http_port


class _FakeSprite:
    def __init__(
        self,
        *,
        name: str,
        url: str | None = "https://example-sprite-org.sprites.dev",
        status: str = "running",
        services: list[_FakeService] | None = None,
        files: dict[str, bytes] | None = None,
        list_services_failure: BaseException | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.status = status
        self.organization_name = "example-org"
        self.update_url_settings_calls: list[Any] = []
        self.close_control_connection_calls = 0
        self.list_services_failure = list_services_failure
        self._services = services or []
        self._fs = _FakeSpriteFilesystem(files or {})

    def filesystem(self, working_dir: str = "/") -> _FakeSpriteFilesystem:
        return self._fs

    def list_services(self) -> list[_FakeService]:
        if self.list_services_failure is not None:
            raise self.list_services_failure
        return list(self._services)

    def update_url_settings(self, settings: Any) -> None:
        self.update_url_settings_calls.append(settings)

    async def close_control_connection(self) -> None:
        self.close_control_connection_calls += 1


class _FakeSpritesClient:
    def __init__(
        self,
        *,
        token: str = "tok",
        base_url: str = "https://api.sprites.dev",
        control_mode: bool = False,
        sprites_by_name: dict[str, _FakeSprite] | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url
        self.control_mode = control_mode
        self.create_sprite_calls: list[tuple[str, Any]] = []
        self.delete_sprite_calls: list[str] = []
        self.get_sprite_calls: list[str] = []
        self.sprite_handle_calls: list[str] = []
        self.closed = False
        self._sprites_by_name = sprites_by_name or {}
        self.create_failures: list[BaseException] = []
        self.get_failures: list[BaseException] = []

    def create_sprite(self, name: str, config: Any | None = None) -> _FakeSprite:
        self.create_sprite_calls.append((name, config))
        if self.create_failures:
            raise self.create_failures.pop(0)
        sprite = _FakeSprite(name=name)
        self._sprites_by_name[name] = sprite
        return sprite

    def sprite(self, name: str) -> _FakeSprite:
        self.sprite_handle_calls.append(name)
        return self._sprites_by_name.get(name) or _FakeSprite(name=name)

    def get_sprite(self, name: str) -> _FakeSprite:
        self.get_sprite_calls.append(name)
        if self.get_failures:
            raise self.get_failures.pop(0)
        sprite = self._sprites_by_name.get(name)
        if sprite is None:
            raise _SpritesNotFoundError(f"sprite not found: {name}")
        return sprite

    def delete_sprite(self, name: str) -> None:
        self.delete_sprite_calls.append(name)

    def close(self) -> None:
        self.closed = True


# ---------- Helpers ----------


def _make_state(**overrides: object) -> SpritesSandboxSessionState:
    base = {
        "session_id": SESSION_UUID,
        "snapshot": NoopSnapshot(id="snapshot-1"),
        "manifest": Manifest(root="/workspace"),
        "sprite_name": SPRITE_NAME,
        "created_by_us": True,
    }
    base.update(overrides)
    return SpritesSandboxSessionState.model_validate(base)


@pytest_asyncio.fixture
async def patched_sprites(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fake_client = _FakeSpritesClient()
    fake_control = _FakeControlConnection()

    monkeypatch.setattr(sprites_sandbox, "SpritesClient", lambda **kw: fake_client)
    monkeypatch.setattr(sprites_sandbox, "FileNotFoundError_", _FakeFileNotFound)

    async def _get_control(_sprite: Any) -> _FakeControlConnection:
        return fake_control

    def _release_control(_sprite: Any, _cc: Any) -> None:
        return None

    monkeypatch.setattr(sprites_sandbox, "get_control_connection", _get_control)
    monkeypatch.setattr(sprites_sandbox, "release_control_connection", _release_control)
    return {"client": fake_client, "control": fake_control}


# ---------- 1. Options & state roundtrip ----------


def test_options_roundtrip_through_polymorphic_registry() -> None:
    options = SpritesSandboxClientOptions(
        sprite_name="my-sprite",
        url_auth="public",
        ram_mb=512,
        cpus=2,
        region="iad",
        storage_gb=8,
        exposed_ports=(8080,),
        env={"FOO": "BAR"},
        timeout_ms=120_000,
    )
    payload = options.model_dump(mode="json")
    assert payload["type"] == "fly"
    restored = BaseSandboxClientOptions.parse(payload)
    assert isinstance(restored, SpritesSandboxClientOptions)
    assert restored.model_dump(mode="json") == payload


def test_state_roundtrip_does_not_leak_token() -> None:
    state = _make_state(sprite_name="x", created_by_us=False, url_auth="public")
    payload = state.model_dump(mode="json")
    assert "token" not in payload and "base_url" not in payload
    client = SpritesSandboxClient(token="tok-1", base_url="https://example")
    restored = client.deserialize_session_state(payload)
    assert isinstance(restored, SpritesSandboxSessionState)
    assert restored.model_dump(mode="json") == payload


# ---------- 2. Auth resolution ----------


def test_client_resolves_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPRITES_API_TOKEN", "from-env")
    client = SpritesSandboxClient()
    assert client._token == "from-env"
    assert client._base_url == "https://api.sprites.dev"


def test_client_kwarg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPRITES_API_TOKEN", "from-env")
    monkeypatch.setenv("SPRITES_API_URL", "https://env.example")
    client = SpritesSandboxClient(token="kwarg", base_url="https://kwarg.example")
    assert client._token == "kwarg"
    assert client._base_url == "https://kwarg.example"


def test_client_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPRITES_API_TOKEN", raising=False)
    with pytest.raises(ConfigurationError):
        SpritesSandboxClient()


def test_resume_uses_live_client_token_after_env_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPRITES_API_TOKEN", "from-env")
    client = SpritesSandboxClient()
    monkeypatch.delenv("SPRITES_API_TOKEN", raising=False)
    assert client._token == "from-env"


# ---------- 3 & 4. Lifecycle ephemeral & named-attach ----------


@pytest.mark.asyncio
async def test_create_ephemeral_sprite(patched_sprites: dict[str, Any]) -> None:
    fake_client = patched_sprites["client"]
    client = SpritesSandboxClient(token="tok")
    options = SpritesSandboxClientOptions()
    session = await client.create(options=options)
    inner = session._inner
    assert isinstance(inner, SpritesSandboxSession)
    assert inner.state.created_by_us is True
    assert len(fake_client.create_sprite_calls) == 1
    assert fake_client.create_sprite_calls[0][0].startswith("openai-agents-")
    # No eager get_sprite poll — ephemeral path is lazy too. The first I/O
    # operation drives the wait-for-running via ``_ensure_warm``.
    assert fake_client.get_sprite_calls == []
    assert inner._warmth_verified is False
    # delete via client.delete deletes the ephemeral sprite
    await client.delete(session)
    assert fake_client.delete_sprite_calls == [fake_client.create_sprite_calls[0][0]]


@pytest.mark.asyncio
async def test_create_attaches_to_named_sprite(patched_sprites: dict[str, Any]) -> None:
    fake_client = patched_sprites["client"]
    fake_client._sprites_by_name["existing"] = _FakeSprite(name="existing")
    client = SpritesSandboxClient(token="tok")
    options = SpritesSandboxClientOptions(sprite_name="existing")
    session = await client.create(options=options)
    inner = session._inner
    assert isinstance(inner, SpritesSandboxSession)
    assert inner.state.created_by_us is False
    assert fake_client.create_sprite_calls == []
    assert fake_client.sprite_handle_calls == ["existing"]
    await client.delete(session)
    assert fake_client.delete_sprite_calls == []


# ---------- 5. Wait-for-running timeout ----------


@pytest.mark.asyncio
async def test_wait_for_running_raises_workspace_start_error(
    patched_sprites: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sprites_sandbox, "_SPRITE_READY_POLL_INTERVAL_S", 0.0)
    fake_client = patched_sprites["client"]
    fake_client._sprites_by_name[SPRITE_NAME] = _FakeSprite(name=SPRITE_NAME, status="starting")
    state = _make_state(timeout_ms=1)  # 1ms deadline
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client)
    with pytest.raises(WorkspaceStartError) as excinfo:
        await inner._wait_for_sprite_running()
    assert excinfo.value.context.get("reason") == "wait_for_running_timeout"


# ---------- 6. Exec mapping ----------


@pytest.mark.asyncio
async def test_exec_internal_returns_buffered_streams(
    patched_sprites: dict[str, Any],
) -> None:
    fake_control = patched_sprites["control"]
    fake_control.next_ops.append(_FakeOpConn(stdout=b"hi\n", stderr=b"warn\n", exit_code=0))
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    fake_sprite = _FakeSprite(name=SPRITE_NAME)
    _attach(inner, client=patched_sprites["client"], sprite=fake_sprite)
    result = await inner._exec_internal("echo", "hi", timeout=5.0)
    assert isinstance(result, ExecResult)
    assert result.stdout == b"hi\n"
    assert result.stderr == b"warn\n"
    assert result.exit_code == 0
    assert fake_control.start_op_calls == [
        {"op": "exec", "cmd": ["echo", "hi"], "dir": "/workspace", "tty": False, "stdin": False}
    ]


@pytest.mark.asyncio
async def test_exec_internal_timeout_raises_and_signals_kill(
    patched_sprites: dict[str, Any],
) -> None:
    fake_control = patched_sprites["control"]
    never = asyncio.Event()
    op = _FakeOpConn(wait_event=never)
    fake_control.next_ops.append(op)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"], sprite=_FakeSprite(name=SPRITE_NAME))
    with pytest.raises(ExecTimeoutError):
        await inner._exec_internal("sleep", "1000", timeout=0.05)
    assert "KILL" in op.signals


@pytest.mark.asyncio
async def test_exec_internal_start_op_failure_raises_transport_error(
    patched_sprites: dict[str, Any],
) -> None:
    fake_control = patched_sprites["control"]
    fake_control.start_op_failures.append(RuntimeError("ws closed"))
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"], sprite=_FakeSprite(name=SPRITE_NAME))
    with pytest.raises(ExecTransportError):
        await inner._exec_internal("echo", "x", timeout=1.0)


# ---------- 7. PTY ----------


def test_supports_pty_is_true() -> None:
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    assert inner.supports_pty() is True


@pytest.mark.asyncio
async def test_pty_exec_start_registers_callbacks_and_pre_drains(
    patched_sprites: dict[str, Any],
) -> None:
    fake_control = patched_sprites["control"]
    op = _FakeOpConn(stdout=b"pre-drain\n", exit_code=0)
    fake_control.next_ops.append(op)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    sprite = _FakeSprite(name=SPRITE_NAME)
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    update = await inner.pty_exec_start(
        "bash",
        shell=False,
        tty=True,
        yield_time_s=0.001,
    )
    # Either pre-drain (via get_stdout) or callback drain ran; the chunk should
    # appear in the returned output.
    assert b"pre-drain" in update.output
    assert fake_control.start_op_calls[0]["tty"] is True


@pytest.mark.asyncio
async def test_pty_write_stdin_writes_and_returns_buffered_output(
    patched_sprites: dict[str, Any],
) -> None:
    fake_control = patched_sprites["control"]
    op = _FakeOpConn()
    fake_control.next_ops.append(op)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    sprite = _FakeSprite(name=SPRITE_NAME)
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    started = await inner.pty_exec_start("bash", shell=False, tty=True, yield_time_s=0.001)
    session_id = started.process_id
    assert session_id is not None

    # Simulate the server pushing output between writes by delivering a chunk
    # synchronously through the registered on_stdout callback.
    assert op.on_stdout is not None
    op.on_stdout(b"hello\n")
    update = await inner.pty_write_stdin(session_id=session_id, chars="ls\n", yield_time_s=0.001)
    assert op.write_calls == [b"ls\n"]
    assert b"hello" in update.output


@pytest.mark.asyncio
async def test_pty_terminate_all_signals_term_and_kill(
    patched_sprites: dict[str, Any],
) -> None:
    fake_control = patched_sprites["control"]
    op = _FakeOpConn()
    fake_control.next_ops.append(op)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    sprite = _FakeSprite(name=SPRITE_NAME)
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    await inner.pty_exec_start("bash", shell=False, tty=True, yield_time_s=0.001)
    await inner.pty_terminate_all()
    # Live op never closed, so terminate sequences TERM then KILL.
    assert "TERM" in op.signals
    assert "KILL" in op.signals
    assert inner._pty_processes == {}


@pytest.mark.asyncio
async def test_pty_finalize_drops_session_when_op_closed(
    patched_sprites: dict[str, Any],
) -> None:
    fake_control = patched_sprites["control"]
    op = _FakeOpConn(exit_code=0)
    # Mark closed so _entry_exit_code returns 0 immediately.
    op.closed = True
    op.exit_code = 0
    fake_control.next_ops.append(op)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    sprite = _FakeSprite(name=SPRITE_NAME)
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    update = await inner.pty_exec_start("bash", shell=False, tty=True, yield_time_s=0.001)
    assert update.process_id is None
    assert update.exit_code == 0
    assert inner._pty_processes == {}


# ---------- 8. Exposed ports ----------


def test_options_exposed_ports_can_be_empty_or_single() -> None:
    SpritesSandboxClientOptions(exposed_ports=())
    SpritesSandboxClientOptions(exposed_ports=(8080,))


@pytest.mark.asyncio
async def test_validate_exposed_ports_rejects_more_than_one(
    patched_sprites: dict[str, Any],
) -> None:
    client = SpritesSandboxClient(token="tok")
    options = SpritesSandboxClientOptions(exposed_ports=(8080, 9090))
    with pytest.raises(ConfigurationError):
        await client.create(options=options)


@pytest.mark.asyncio
async def test_resolve_exposed_port_happy_path(
    patched_sprites: dict[str, Any],
) -> None:
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(
        name=SPRITE_NAME,
        url="https://example-sprite-example-org.sprites.dev",
        services=[_FakeService(http_port=8080)],
    )
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state(exposed_ports=(8080,))
    inner = SpritesSandboxSession.from_state(state, token="tok")
    # Inject fakes; bypass _ensure_sprite (which would re-create on the fake).
    _attach(inner, client=fake_client, sprite=sprite)
    endpoint = await inner._resolve_exposed_port(8080)
    assert isinstance(endpoint, ExposedPortEndpoint)
    assert endpoint.tls is True
    assert endpoint.host == "example-sprite-example-org.sprites.dev"
    assert endpoint.port == 443


@pytest.mark.asyncio
async def test_resolve_exposed_port_not_configured_when_no_matching_service(
    patched_sprites: dict[str, Any],
) -> None:
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(
        name=SPRITE_NAME,
        url="https://example-sprite-example-org.sprites.dev",
        services=[_FakeService(http_port=3000)],
    )
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state(exposed_ports=(8080,))
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)
    with pytest.raises(ExposedPortUnavailableError) as excinfo:
        await inner._resolve_exposed_port(8080)
    assert excinfo.value.context.get("backend") == "fly"


# ---------- 9. Read / write ----------


@pytest.mark.asyncio
async def test_read_returns_bytesio(patched_sprites: dict[str, Any]) -> None:
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME, files={"/workspace/hi.txt": b"hello"})
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    # Bypass _validate_path_access (uses runtime helper exec) for unit-test isolation.
    async def _validate_passthrough(path: Path | str, *, for_write: bool = False) -> Path:
        return Path(str(path))

    with patch.object(inner, "_validate_path_access", _validate_passthrough):
        stream = await inner.read(Path("/workspace/hi.txt"))
    assert isinstance(stream, io.IOBase)
    assert stream.read() == b"hello"


@pytest.mark.asyncio
async def test_read_missing_file_raises_workspace_read_not_found(
    patched_sprites: dict[str, Any],
) -> None:
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME, files={})
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    async def _validate_passthrough(path: Path | str, *, for_write: bool = False) -> Path:
        return Path(str(path))

    with patch.object(inner, "_validate_path_access", _validate_passthrough):
        with pytest.raises(WorkspaceReadNotFoundError):
            await inner.read(Path("/workspace/missing.txt"))


@pytest.mark.asyncio
async def test_write_rejects_string_payload_with_workspace_write_type_error(
    patched_sprites: dict[str, Any],
) -> None:
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    async def _validate_passthrough(path: Path | str, *, for_write: bool = False) -> Path:
        return Path(str(path))

    class _BadStream(io.IOBase):
        def read(self, *_args: Any) -> Any:
            return 42  # not bytes / str

    with patch.object(inner, "_validate_path_access", _validate_passthrough):
        with pytest.raises(WorkspaceWriteTypeError):
            await inner.write(Path("/workspace/x"), _BadStream())


@pytest.mark.asyncio
async def test_write_propagates_filesystem_failure_as_archive_write_error(
    patched_sprites: dict[str, Any],
) -> None:
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    sprite._fs.write_failure = RuntimeError("disk full")
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    async def _validate_passthrough(path: Path | str, *, for_write: bool = False) -> Path:
        return Path(str(path))

    with patch.object(inner, "_validate_path_access", _validate_passthrough):
        with pytest.raises(WorkspaceArchiveWriteError):
            await inner.write(Path("/workspace/x"), io.BytesIO(b"data"))


@pytest.mark.asyncio
async def test_read_rejects_user_arg(patched_sprites: dict[str, Any]) -> None:
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"])
    with pytest.raises(ConfigurationError):
        await inner.read(Path("/workspace/x"), user="root")


@pytest.mark.asyncio
async def test_write_rejects_user_arg(patched_sprites: dict[str, Any]) -> None:
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"])
    with pytest.raises(ConfigurationError):
        await inner.write(Path("/workspace/x"), io.BytesIO(b"x"), user=User(name="r"))


@pytest.mark.asyncio
async def test_exec_rejects_user_arg(patched_sprites: dict[str, Any]) -> None:
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"])
    with pytest.raises(ConfigurationError):
        await inner.exec("echo", "x", user="root")


# ---------- 11 (subset). Tar-based persistence sanity ----------


@pytest.mark.asyncio
async def test_persist_workspace_uses_tar_via_exec_and_filesystem_read(
    patched_sprites: dict[str, Any],
) -> None:
    import tarfile

    fake_control = patched_sprites["control"]
    # tar cf, rm cleanup
    fake_control.next_ops.append(_FakeOpConn(exit_code=0))
    fake_control.next_ops.append(_FakeOpConn(exit_code=0))

    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    archive_path = f"/tmp/openai-agents-{SESSION_UUID.hex}.tar"
    # Build a minimal valid tar so hydrate could read it back.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="./hello.txt")
        info.size = 5
        tar.addfile(info, io.BytesIO(b"hello"))
    sprite._fs.files[archive_path] = buf.getvalue()
    fake_client._sprites_by_name[SPRITE_NAME] = sprite

    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)
    stream = await inner.persist_workspace()
    assert isinstance(stream, io.IOBase)
    archive_bytes = stream.read()
    # Round-trip: validate the tar produced is parseable
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
        names = tar.getnames()
    assert "./hello.txt" in names
    # First start_op was the tar create (passed shell=False, so verbatim cmd).
    first_cmd = fake_control.start_op_calls[0]["cmd"]
    assert first_cmd[0:3] == ["tar", "cf", archive_path]
    assert "." in first_cmd


# ---------- 14. SpritesPlatformContext capability ----------


@pytest.mark.asyncio
async def test_sprites_platform_context_reads_llm_txt(
    patched_sprites: dict[str, Any],
) -> None:
    fake_control = patched_sprites["control"]
    fake_control.next_ops.append(
        _FakeOpConn(stdout=b"# Sprite Environment\nbe nice\n", exit_code=0)
    )
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    capability = SpritesPlatformContext()
    capability.bind(inner)
    text = await capability.instructions(state.manifest)
    assert text is not None
    assert "<sprites-platform-context>" in text
    assert "be nice" in text
    # First exec call should be the cat — verbatim (shell=False) and absolute path.
    first_cmd = fake_control.start_op_calls[0]["cmd"]
    assert first_cmd[0:3] == ["cat", "--", "/.sprite/llm.txt"]


@pytest.mark.asyncio
async def test_sprites_platform_context_caches_after_first_read(
    patched_sprites: dict[str, Any],
) -> None:
    fake_control = patched_sprites["control"]
    fake_control.next_ops.append(_FakeOpConn(stdout=b"ctx\n", exit_code=0))
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    capability = SpritesPlatformContext()
    capability.bind(inner)
    a = await capability.instructions(state.manifest)
    b = await capability.instructions(state.manifest)
    assert a == b
    # Only one start_op call total (cached on the second invocation).
    assert len(fake_control.start_op_calls) == 1


@pytest.mark.asyncio
async def test_sprites_platform_context_returns_none_when_file_missing(
    patched_sprites: dict[str, Any],
) -> None:
    fake_control = patched_sprites["control"]
    fake_control.next_ops.append(
        _FakeOpConn(stdout=b"", stderr=b"cat: /.sprite/llm.txt: No such file\n", exit_code=1)
    )
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    capability = SpritesPlatformContext()
    capability.bind(inner)
    assert await capability.instructions(state.manifest) is None


# ---------- 15. SpritesUrlAccess capability ----------


@pytest.mark.asyncio
async def test_sprites_url_access_default_blocks_public(
    patched_sprites: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents.extensions.sandbox.sprites import SpritesUrlAccess

    sprite = _FakeSprite(name=SPRITE_NAME)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    capability = SpritesUrlAccess()
    capability.bind(inner)
    result = await capability._apply_visibility("public")
    assert "disabled by application policy" in result
    # URL setting was NOT touched.
    assert sprite.update_url_settings_calls == []


@pytest.mark.asyncio
async def test_sprites_url_access_allow_public_calls_update(
    patched_sprites: dict[str, Any],
) -> None:
    from agents.extensions.sandbox.sprites import SpritesUrlAccess

    sprite = _FakeSprite(name=SPRITE_NAME)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    capability = SpritesUrlAccess(allow_public=True)
    capability.bind(inner)
    result = await capability._apply_visibility("public")
    assert "public" in result
    assert len(sprite.update_url_settings_calls) == 1
    settings = sprite.update_url_settings_calls[0]
    assert getattr(settings, "auth", None) == "public"


@pytest.mark.asyncio
async def test_sprites_url_access_sprite_value_works_without_allow_public(
    patched_sprites: dict[str, Any],
) -> None:
    from agents.extensions.sandbox.sprites import SpritesUrlAccess

    sprite = _FakeSprite(name=SPRITE_NAME)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    capability = SpritesUrlAccess(allow_public=False)
    capability.bind(inner)
    result = await capability._apply_visibility("sprite")
    assert "sprite" in result
    assert len(sprite.update_url_settings_calls) == 1


@pytest.mark.asyncio
async def test_sprites_url_access_invalid_value(patched_sprites: dict[str, Any]) -> None:
    from agents.extensions.sandbox.sprites import SpritesUrlAccess

    sprite = _FakeSprite(name=SPRITE_NAME)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"], sprite=sprite)
    capability = SpritesUrlAccess(allow_public=True)
    capability.bind(inner)
    result = await capability._apply_visibility("nonsense")
    assert "must be" in result
    assert sprite.update_url_settings_calls == []


def test_sprites_url_access_tools_omit_or_include_public() -> None:
    from agents.extensions.sandbox.sprites import SpritesUrlAccess

    cap = SpritesUrlAccess(allow_public=False)
    tools = cap.tools()
    assert len(tools) == 1
    cap_pub = SpritesUrlAccess(allow_public=True)
    tools_pub = cap_pub.tools()
    assert len(tools_pub) == 1


# ---------- 16. SpritesCheckpoints capability ----------


class _FakeCheckpoint:
    def __init__(self, *, id: str, comment: str = "", create_time: Any = None) -> None:
        from datetime import datetime, timezone

        self.id = id
        self.comment = comment
        self.create_time = create_time or datetime.now(timezone.utc)


from dataclasses import dataclass as _dataclass, field as _field  # noqa: E402


@_dataclass
class _CheckpointFakeOps:
    """Tracks calls + state for the checkpoint stubs attached to a sprite."""

    checkpoints: list[_FakeCheckpoint] = _field(default_factory=list)
    create_calls: list[str] = _field(default_factory=list)
    restore_calls: list[str] = _field(default_factory=list)
    create_messages: list[Any] = _field(default_factory=list)
    restore_messages: list[Any] = _field(default_factory=list)


def _attach_checkpoint_methods(sprite: _FakeSprite) -> _CheckpointFakeOps:
    """Wire create/list/restore stubs onto ``sprite`` and return the tracking ops."""

    ops = _CheckpointFakeOps()

    def _create(comment: str = "") -> Any:
        ops.create_calls.append(comment)
        from datetime import datetime, timedelta, timezone

        latest_time = max(
            (c.create_time for c in ops.checkpoints), default=datetime.now(timezone.utc)
        ) + timedelta(seconds=1)
        ops.checkpoints.append(
            _FakeCheckpoint(
                id=f"ckpt-{len(ops.checkpoints) + 1}",
                comment=comment,
                create_time=latest_time,
            )
        )
        return iter(ops.create_messages)

    def _list() -> list[_FakeCheckpoint]:
        return list(ops.checkpoints)

    def _restore(checkpoint_id: str) -> Any:
        ops.restore_calls.append(checkpoint_id)
        return iter(ops.restore_messages)

    setattr(sprite, "create_checkpoint", _create)  # noqa: B010
    setattr(sprite, "list_checkpoints", _list)  # noqa: B010
    setattr(sprite, "restore_checkpoint", _restore)  # noqa: B010
    return ops


@pytest.mark.asyncio
async def test_sprites_checkpoints_create_returns_id(patched_sprites: dict[str, Any]) -> None:
    from agents.extensions.sandbox.sprites import SpritesCheckpoints

    sprite = _FakeSprite(name=SPRITE_NAME)
    ops = _attach_checkpoint_methods(sprite)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    cap = SpritesCheckpoints()
    cap.bind(inner)
    out = await cap._create("before-refactor")
    assert "id='ckpt-1'" in out
    assert "before-refactor" in out
    assert ops.create_calls == ["before-refactor"]


@pytest.mark.asyncio
async def test_sprites_checkpoints_list_renders_rows(patched_sprites: dict[str, Any]) -> None:
    from agents.extensions.sandbox.sprites import SpritesCheckpoints

    sprite = _FakeSprite(name=SPRITE_NAME)
    _attach_checkpoint_methods(sprite)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    cap = SpritesCheckpoints()
    cap.bind(inner)
    await cap._create("first")
    await cap._create("second")
    out = await cap._list()
    assert "ckpt-1" in out
    assert "ckpt-2" in out


@pytest.mark.asyncio
async def test_sprites_checkpoints_restore_blocked_by_default(
    patched_sprites: dict[str, Any],
) -> None:
    from agents.extensions.sandbox.sprites import SpritesCheckpoints

    sprite = _FakeSprite(name=SPRITE_NAME)
    ops = _attach_checkpoint_methods(sprite)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    cap = SpritesCheckpoints(allow_restore=False)
    cap.bind(inner)
    out = await cap._restore("ckpt-1")
    assert "disabled" in out
    assert ops.restore_calls == []


@pytest.mark.asyncio
async def test_sprites_checkpoints_restore_when_enabled(patched_sprites: dict[str, Any]) -> None:
    from agents.extensions.sandbox.sprites import SpritesCheckpoints

    sprite = _FakeSprite(name=SPRITE_NAME)
    ops = _attach_checkpoint_methods(sprite)
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=patched_sprites["client"], sprite=sprite)

    cap = SpritesCheckpoints(allow_restore=True)
    cap.bind(inner)
    out = await cap._restore("ckpt-7")
    assert "ckpt-7" in out
    assert ops.restore_calls == ["ckpt-7"]


def test_sprites_checkpoints_tool_count_depends_on_allow_restore() -> None:
    from agents.extensions.sandbox.sprites import SpritesCheckpoints

    cap_no = SpritesCheckpoints(allow_restore=False)
    cap_yes = SpritesCheckpoints(allow_restore=True)
    assert len(cap_no.tools()) == 2  # create + list
    assert len(cap_yes.tools()) == 3  # + restore


# ---------- 17. Lazy wake-up ----------


@pytest.mark.asyncio
async def test_named_attach_create_does_not_poll_for_running(
    patched_sprites: dict[str, Any],
) -> None:
    """create() with sprite_name should NOT call get_sprite during attach.

    The platform auto-wakes the sprite on first traffic; polling here would
    pay wake-up latency just to hand back a session handle. The first I/O
    operation drives the wake-up via _ensure_warm.
    """

    fake_client = patched_sprites["client"]
    fake_client._sprites_by_name["existing"] = _FakeSprite(name="existing")
    client = SpritesSandboxClient(token="tok")
    options = SpritesSandboxClientOptions(sprite_name="existing")
    session = await client.create(options=options)
    inner = session._inner
    assert isinstance(inner, SpritesSandboxSession)
    # No get_sprite calls because we did not poll for warmth.
    assert fake_client.get_sprite_calls == []
    # And the warmth flag stays False, so the next I/O will trigger the poll.
    assert inner._warmth_verified is False


@pytest.mark.asyncio
async def test_lazy_warm_polls_on_first_exec(patched_sprites: dict[str, Any]) -> None:
    fake_client = patched_sprites["client"]
    fake_client._sprites_by_name["existing"] = _FakeSprite(name="existing")
    fake_control = patched_sprites["control"]
    fake_control.next_ops.append(_FakeOpConn(stdout=b"", exit_code=0))

    client = SpritesSandboxClient(token="tok")
    session = await client.create(options=SpritesSandboxClientOptions(sprite_name="existing"))
    inner = session._inner
    assert isinstance(inner, SpritesSandboxSession)
    assert inner._warmth_verified is False
    assert fake_client.get_sprite_calls == []

    # First exec drives the wake-up poll.
    await inner._exec_internal("echo", "hi")
    assert fake_client.get_sprite_calls == ["existing"]
    assert inner._warmth_verified is True

    # Subsequent exec does NOT re-poll.
    fake_control.next_ops.append(_FakeOpConn(stdout=b"", exit_code=0))
    await inner._exec_internal("echo", "hi2")
    # Still just the one poll from the first call.
    assert fake_client.get_sprite_calls == ["existing"]


@pytest.mark.asyncio
async def test_lazy_warm_invalidate_forces_repoll(patched_sprites: dict[str, Any]) -> None:
    fake_client = patched_sprites["client"]
    fake_client._sprites_by_name["existing"] = _FakeSprite(name="existing")
    fake_control = patched_sprites["control"]
    fake_control.next_ops.extend([_FakeOpConn(exit_code=0), _FakeOpConn(exit_code=0)])

    client = SpritesSandboxClient(token="tok")
    session = await client.create(options=SpritesSandboxClientOptions(sprite_name="existing"))
    inner = session._inner
    assert isinstance(inner, SpritesSandboxSession)

    await inner._exec_internal("echo", "1")
    assert len(fake_client.get_sprite_calls) == 1

    inner._invalidate_warmth()
    await inner._exec_internal("echo", "2")
    assert len(fake_client.get_sprite_calls) == 2


# ---------- 18. Idle-close watcher ----------


@pytest.mark.asyncio
async def test_idle_watch_closes_control_connections_after_threshold(
    patched_sprites: dict[str, Any],
) -> None:
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)
    # Make the idle window vanishingly small so the test runs fast.
    inner._idle_close_seconds = 0.01

    # Touch activity to spawn the watcher, then wait long enough for the
    # watcher's idle threshold to elapse and close the control connection.
    inner._touch_activity()
    assert inner._idle_watch_task is not None
    await asyncio.wait_for(inner._idle_watch_task, timeout=1.0)
    assert sprite.close_control_connection_calls == 1


@pytest.mark.asyncio
async def test_idle_watch_disabled_when_seconds_is_zero(
    patched_sprites: dict[str, Any],
) -> None:
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state(idle_close_seconds=0)
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)
    inner._idle_close_seconds = 0  # belt-and-braces

    inner._touch_activity()
    assert inner._idle_watch_task is None
    # Wait briefly to confirm no close ever fires.
    await asyncio.sleep(0.05)
    assert sprite.close_control_connection_calls == 0


@pytest.mark.asyncio
async def test_idle_watch_skipped_when_pty_active(
    patched_sprites: dict[str, Any],
) -> None:
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)
    inner._idle_close_seconds = 0.01
    # Pretend a PTY is active so the watcher should refuse to close.
    inner._pty_processes[123] = object()  # type: ignore[assignment]

    await inner._close_idle_control_connections()
    assert sprite.close_control_connection_calls == 0


@pytest.mark.asyncio
async def test_activity_during_idle_window_keeps_connection_open(
    patched_sprites: dict[str, Any],
) -> None:
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)
    inner._idle_close_seconds = 0.05

    inner._touch_activity()
    # Half the window: nudge activity forward so the deadline shifts.
    await asyncio.sleep(0.025)
    inner._touch_activity()
    # Wait long enough for the original deadline to have passed had we not
    # touched activity, but short of the new deadline.
    await asyncio.sleep(0.04)
    # The connection should still be open at this point.
    assert sprite.close_control_connection_calls == 0
    # Now actually let it idle out fully.
    watcher = inner._idle_watch_task
    assert watcher is not None
    await asyncio.wait_for(watcher, timeout=0.2)
    assert sprite.close_control_connection_calls == 1


# ---------- 19. Platform-context cache survives cloning ----------


@pytest.mark.asyncio
async def test_sprites_platform_context_cache_survives_clone(
    patched_sprites: dict[str, Any],
) -> None:
    """Each agent turn re-clones capabilities; the cache must survive that.

    Without a module-level cache, a new clone wakes the sprite every turn
    just to re-read the (unchanged) platform-context file. With it, only
    the first turn for a given sprite-name pays the exec.
    """

    fake_control = patched_sprites["control"]
    fake_control.next_ops.append(_FakeOpConn(stdout=b"# Sprite\n", exit_code=0))
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    # Turn 1: a fresh clone fetches and caches.
    cap1 = SpritesPlatformContext()
    cap1.bind(inner)
    out1 = await cap1.instructions(state.manifest)
    assert out1 is not None
    assert "<sprites-platform-context>" in out1
    assert len(fake_control.start_op_calls) == 1

    # Turn 2: a NEW clone targeting the same sprite hits the module cache.
    cap2 = SpritesPlatformContext()
    cap2.bind(inner)
    out2 = await cap2.instructions(state.manifest)
    assert out2 == out1
    # Still just the one exec — turn 2 didn't touch the sprite.
    assert len(fake_control.start_op_calls) == 1


@pytest.mark.asyncio
async def test_sprites_platform_context_cache_clear_forces_refetch(
    patched_sprites: dict[str, Any],
) -> None:
    from agents.extensions.sandbox.sprites import clear_platform_context_cache

    fake_control = patched_sprites["control"]
    fake_control.next_ops.extend(
        [_FakeOpConn(stdout=b"v1\n", exit_code=0), _FakeOpConn(stdout=b"v2\n", exit_code=0)]
    )
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state()
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    cap = SpritesPlatformContext()
    cap.bind(inner)
    out1 = await cap.instructions(state.manifest)
    assert out1 is not None and "v1" in out1
    assert len(fake_control.start_op_calls) == 1

    # Cache invalidation forces a re-fetch.
    clear_platform_context_cache(SPRITE_NAME)
    out2 = await cap.instructions(state.manifest)
    assert out2 is not None and "v2" in out2
    assert len(fake_control.start_op_calls) == 2


# ---------- 20. Platform context includes service working-directory hint ----------


@pytest.mark.asyncio
async def test_platform_context_warns_about_service_cwd(
    patched_sprites: dict[str, Any],
) -> None:
    """The framing should warn the model that services run with cwd=$HOME by default.

    Without this warning, agents commonly create `python3 -m http.server` services
    and serve from the home directory instead of the workspace.
    """

    fake_control = patched_sprites["control"]
    fake_control.next_ops.append(_FakeOpConn(stdout=b"# Sprite\n", exit_code=0))
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state(manifest=Manifest(root="/workspace"))
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    cap = SpritesPlatformContext()
    cap.bind(inner)
    out = await cap.instructions(state.manifest)
    assert out is not None
    assert "/workspace" in out
    assert "--dir /workspace" in out
    assert "sprite-env services create" in out


@pytest.mark.asyncio
async def test_platform_context_uses_actual_manifest_root(
    patched_sprites: dict[str, Any],
) -> None:
    """The hint must use the agent's actual manifest.root, not a hardcoded path."""

    fake_control = patched_sprites["control"]
    fake_control.next_ops.append(_FakeOpConn(stdout=b"# Sprite\n", exit_code=0))
    fake_client = patched_sprites["client"]
    sprite = _FakeSprite(name=SPRITE_NAME)
    fake_client._sprites_by_name[SPRITE_NAME] = sprite
    state = _make_state(manifest=Manifest(root="/var/agent-home"))
    inner = SpritesSandboxSession.from_state(state, token="tok")
    _attach(inner, client=fake_client, sprite=sprite)

    cap = SpritesPlatformContext()
    cap.bind(inner)
    out = await cap.instructions(state.manifest)
    assert out is not None
    assert "/var/agent-home" in out
    assert "--dir /var/agent-home" in out
