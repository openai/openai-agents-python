from __future__ import annotations

import importlib
import io
import sys
import tarfile
import types
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from agents.sandbox import Manifest
from agents.sandbox.entries import File
from agents.sandbox.errors import (
    ConfigurationError,
    ExposedPortUnavailableError,
    InvalidManifestPathError,
)
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import User
from tests._fake_workspace_paths import resolve_fake_workspace_path


class _FakeCommandResult:
    def __init__(self, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class _FakeSandboxInfo(BaseModel):
    status: str = "active"


class _FakeNetworkConfig(BaseModel):
    allow_out: list[str] | None = None
    deny_out: list[str] | None = None


class _SuperserveNotFoundError(Exception):
    status_code = 404


class _SuperserveAuthenticationError(Exception):
    status_code = 401


class _SuperserveValidationError(Exception):
    status_code = 400


class _SuperserveConflictError(Exception):
    status_code = 409


class _SuperserveServerError(Exception):
    status_code = 500


class _SuperserveSandboxTimeoutError(Exception):
    pass


class _SuperserveSandboxError(Exception):
    pass


class _FakeCommands:
    def __init__(self, sandbox: _FakeAsyncSandbox) -> None:
        self._sandbox = sandbox
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        on_stdout: object | None = None,
        on_stderr: object | None = None,
    ) -> _FakeCommandResult:
        _ = (on_stdout, on_stderr)
        self.calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env": dict(env) if env is not None else None,
                "timeout_seconds": timeout_seconds,
            }
        )
        # Test hooks can override the next result or throw.
        if self._sandbox.command_failures:
            raise self._sandbox.command_failures.pop(0)
        next_result = (
            self._sandbox.command_results.pop(0) if self._sandbox.command_results else None
        )
        if next_result is not None:
            return next_result

        # Handle workspace-path resolution helper used by the base session
        # for `_validate_remote_path_access`.
        resolved = resolve_fake_workspace_path(
            command,
            symlinks=self._sandbox.symlinks,
            home_dir="/workspace",
        )
        if resolved is not None:
            return _FakeCommandResult(
                exit_code=resolved.exit_code,
                stdout=resolved.stdout,
                stderr=resolved.stderr,
            )

        # Built-in handlers for common shell shapes used by the session.
        if command.startswith("mkdir -p"):
            return _FakeCommandResult(exit_code=0)
        if command.startswith("tar cf"):
            # tar cf <path> [--exclude=./X ...] .
            tokens = command.split()
            archive_path = tokens[2]
            include_root = tokens[-1] == "."
            exclusions = {
                token.removeprefix("--exclude=./")
                for token in tokens
                if token.startswith("--exclude=./")
            }
            cwd_eff = cwd or "/"
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for path, content in sorted(self._sandbox._file_store.items()):
                    if not path.startswith(cwd_eff.rstrip("/") + "/"):
                        continue
                    rel_path = path[len(cwd_eff.rstrip("/")) + 1 :]
                    if any(
                        rel_path == exclusion or rel_path.startswith(f"{exclusion}/")
                        for exclusion in exclusions
                    ):
                        continue
                    info = tarfile.TarInfo(name=rel_path if include_root else path)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            self._sandbox._file_store[archive_path] = buffer.getvalue()
            return _FakeCommandResult(exit_code=0)
        if command.startswith("tar xf"):
            tokens = command.split()
            archive_path = tokens[2]
            destination = tokens[-1]
            raw = self._sandbox._file_store.get(archive_path)
            if raw is None:
                return _FakeCommandResult(exit_code=1, stderr="archive missing")
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    extracted = archive.extractfile(member)
                    assert extracted is not None
                    self._sandbox._file_store[
                        f"{destination.rstrip('/')}/{member.name}"
                    ] = extracted.read()
            return _FakeCommandResult(exit_code=0)
        if command.startswith("rm -f --"):
            for token in command.split()[3:]:
                self._sandbox._file_store.pop(token, None)
            return _FakeCommandResult(exit_code=0)
        return _FakeCommandResult(exit_code=0)


class _FakeFiles:
    def __init__(self, sandbox: _FakeAsyncSandbox) -> None:
        self._sandbox = sandbox
        self.write_calls: list[tuple[str, bytes]] = []
        self.read_calls: list[str] = []

    async def write(self, path: str, content: bytes | str, *, timeout: float | None = None) -> None:
        _ = timeout
        if self._sandbox.write_failures:
            raise self._sandbox.write_failures.pop(0)
        payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        self.write_calls.append((path, payload))
        self._sandbox._file_store[path] = payload

    async def read(self, path: str, *, timeout: float | None = None) -> bytes:
        _ = timeout
        self.read_calls.append(path)
        if path not in self._sandbox._file_store:
            raise _SuperserveNotFoundError(f"missing {path}")
        return self._sandbox._file_store[path]


class _FakeAsyncSandbox:
    create_calls: list[dict[str, object]] = []
    connect_calls: list[dict[str, object]] = []
    sandboxes: dict[str, _FakeAsyncSandbox] = {}
    fail_connect_ids: set[str] = set()
    create_failures: list[BaseException] = []

    def __init__(self, *, sandbox_id: str, status: str = "active") -> None:
        self.id = sandbox_id
        self.name = sandbox_id
        self.status = status
        self.metadata: dict[str, str] = {}
        self._file_store: dict[str, bytes] = {}
        self.symlinks: dict[str, str] = {}
        self.command_results: list[_FakeCommandResult] = []
        self.command_failures: list[BaseException] = []
        self.write_failures: list[BaseException] = []
        self.pause_calls = 0
        self.resume_calls = 0
        self.kill_calls = 0
        self.commands = _FakeCommands(self)
        self.files = _FakeFiles(self)

    @classmethod
    def reset(cls) -> None:
        cls.create_calls = []
        cls.connect_calls = []
        cls.sandboxes = {}
        cls.fail_connect_ids = set()
        cls.create_failures = []

    @classmethod
    async def create(cls, **kwargs: object) -> _FakeAsyncSandbox:
        cls.create_calls.append(dict(kwargs))
        if cls.create_failures:
            raise cls.create_failures.pop(0)
        sandbox_id = f"sup-{len(cls.create_calls)}"
        sandbox = cls(sandbox_id=sandbox_id)
        sandbox.metadata = dict(cast(dict[str, str], kwargs.get("metadata") or {}))
        cls.sandboxes[sandbox_id] = sandbox
        return sandbox

    @classmethod
    async def connect(cls, sandbox_id: str, **kwargs: object) -> _FakeAsyncSandbox:
        cls.connect_calls.append({"sandbox_id": sandbox_id, **kwargs})
        if sandbox_id in cls.fail_connect_ids:
            raise _SuperserveNotFoundError(f"sandbox {sandbox_id} not found")
        sandbox = cls.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise _SuperserveNotFoundError(f"sandbox {sandbox_id} not found")
        return sandbox

    async def get_info(self) -> _FakeSandboxInfo:
        return _FakeSandboxInfo(status=self.status)

    async def pause(self) -> None:
        self.pause_calls += 1
        self.status = "paused"

    async def resume(self) -> None:
        self.resume_calls += 1
        self.status = "active"

    async def kill(self) -> None:
        self.kill_calls += 1
        self.status = "deleted"

async def _noop_sleep(*_args: object, **_kwargs: object) -> None:
    return None


def _load_superserve_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    _FakeAsyncSandbox.reset()

    fake_module = types.ModuleType("superserve")
    fake_module.AsyncSandbox = _FakeAsyncSandbox  # type: ignore[attr-defined]
    fake_module.NetworkConfig = _FakeNetworkConfig  # type: ignore[attr-defined]
    fake_module.NotFoundError = _SuperserveNotFoundError  # type: ignore[attr-defined]
    fake_module.AuthenticationError = _SuperserveAuthenticationError  # type: ignore[attr-defined]
    fake_module.ValidationError = _SuperserveValidationError  # type: ignore[attr-defined]
    fake_module.ConflictError = _SuperserveConflictError  # type: ignore[attr-defined]
    fake_module.ServerError = _SuperserveServerError  # type: ignore[attr-defined]
    fake_module.SandboxTimeoutError = _SuperserveSandboxTimeoutError  # type: ignore[attr-defined]
    fake_module.SandboxError = _SuperserveSandboxError  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "superserve", fake_module)
    sys.modules.pop("agents.extensions.sandbox.superserve.sandbox", None)
    sys.modules.pop("agents.extensions.sandbox.superserve", None)

    return importlib.import_module("agents.extensions.sandbox.superserve.sandbox")


# ---------------------------------------------------------------------------
# Package re-exports & basic shape
# ---------------------------------------------------------------------------


def test_superserve_package_re_exports_backend_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.superserve")

    assert package_module.SuperserveSandboxClient is superserve_module.SuperserveSandboxClient
    assert (
        package_module.SuperserveSandboxSessionState
        is superserve_module.SuperserveSandboxSessionState
    )
    assert (
        package_module.DEFAULT_SUPERSERVE_WORKSPACE_ROOT
        == superserve_module.DEFAULT_SUPERSERVE_WORKSPACE_ROOT
    )


def test_superserve_supports_pty_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000001",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
    )
    session = superserve_module.SuperserveSandboxSession.from_state(state)
    assert not session.supports_pty()


def test_superserve_options_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    options = superserve_module.SuperserveSandboxClientOptions(
        template="superserve/python-3.11",
        env_vars={"HELLO": "world"},
        metadata={"team": "agents"},
        pause_on_exit=True,
        timeout_seconds=300,
    )
    dumped = options.model_dump(mode="json")
    rebuilt = superserve_module.SuperserveSandboxClientOptions.model_validate(dumped)
    assert rebuilt.template == "superserve/python-3.11"
    assert rebuilt.env_vars == {"HELLO": "world"}
    assert rebuilt.metadata == {"team": "agents"}
    assert rebuilt.pause_on_exit is True
    assert rebuilt.timeout_seconds == 300


def test_superserve_session_state_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000099",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
        template="superserve/node-22",
        pause_on_exit=True,
        base_env_vars={"FLAG": "1"},
    )
    payload = state.model_dump(mode="json")
    client = superserve_module.SuperserveSandboxClient()
    rebuilt = client.deserialize_session_state(payload)
    assert isinstance(rebuilt, superserve_module.SuperserveSandboxSessionState)
    assert rebuilt.sandbox_id == "sup-existing"
    assert rebuilt.template == "superserve/node-22"
    assert rebuilt.pause_on_exit is True
    assert rebuilt.base_env_vars == {"FLAG": "1"}


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superserve_create_passes_provider_options(monkeypatch: pytest.MonkeyPatch) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    client = superserve_module.SuperserveSandboxClient()

    session = await client.create(
        manifest=Manifest(),
        options=superserve_module.SuperserveSandboxClientOptions(
            template="superserve/python-3.11",
            env_vars={"HELLO": "world"},
            metadata={"team": "agents"},
            timeout_seconds=600,
        ),
    )

    assert len(_FakeAsyncSandbox.create_calls) == 1
    call = _FakeAsyncSandbox.create_calls[0]
    assert call["from_template"] == "superserve/python-3.11"
    assert call["env_vars"] == {"HELLO": "world"}
    assert call["metadata"] == {"team": "agents"}
    assert call["timeout_seconds"] == 600
    assert session._inner.state.sandbox_id == "sup-1"
    assert (
        session._inner.state.manifest.root
        == superserve_module.DEFAULT_SUPERSERVE_WORKSPACE_ROOT
    )
    assert session._inner.state.template == "superserve/python-3.11"


@pytest.mark.asyncio
async def test_superserve_create_uses_default_template(monkeypatch: pytest.MonkeyPatch) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    client = superserve_module.SuperserveSandboxClient()

    session = await client.create(
        manifest=Manifest(),
        options=superserve_module.SuperserveSandboxClientOptions(),
    )

    call = _FakeAsyncSandbox.create_calls[0]
    assert call["from_template"] == superserve_module.DEFAULT_SUPERSERVE_TEMPLATE
    assert session._inner.state.template == superserve_module.DEFAULT_SUPERSERVE_TEMPLATE


@pytest.mark.asyncio
async def test_superserve_create_allows_manifest_root_outside_provider_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    client = superserve_module.SuperserveSandboxClient()

    session = await client.create(
        manifest=Manifest(root="/tmp/outside"),
        options=superserve_module.SuperserveSandboxClientOptions(),
    )

    assert session._inner.state.manifest.root == "/tmp/outside"


# ---------------------------------------------------------------------------
# exec / read / write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superserve_exec_propagates_command_result(monkeypatch: pytest.MonkeyPatch) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000002",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sup-existing")
    sandbox.command_results.append(
        _FakeCommandResult(stdout="hello\n", stderr="warn\n", exit_code=0)
    )
    session = superserve_module.SuperserveSandboxSession.from_state(state, sandbox=sandbox)

    result = await session.exec("echo", "hello", shell=False)

    assert result.ok()
    assert result.stdout == b"hello\n"
    assert result.stderr == b"warn\n"
    assert sandbox.commands.calls[0]["cwd"] == "/workspace"


@pytest.mark.asyncio
async def test_superserve_exec_translates_timeout_and_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000003",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sup-existing")
    sandbox.command_failures.append(_SuperserveSandboxTimeoutError("slow"))
    sandbox.command_failures.append(_SuperserveServerError("boom"))
    session = superserve_module.SuperserveSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(superserve_module.ExecTimeoutError):
        await session.exec("sleep", "1000", shell=False)
    with pytest.raises(superserve_module.ExecTransportError):
        await session.exec("true", shell=False)


@pytest.mark.asyncio
async def test_superserve_read_and_write_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000004",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sup-existing")
    session = superserve_module.SuperserveSandboxSession.from_state(state, sandbox=sandbox)

    await session.write(Path("notes.txt"), io.BytesIO(b"payload"))
    payload = await session.read(Path("notes.txt"))

    assert sandbox.files.write_calls == [("/workspace/notes.txt", b"payload")]
    assert payload.read() == b"payload"


@pytest.mark.asyncio
async def test_superserve_read_missing_file_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000005",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sup-existing")
    session = superserve_module.SuperserveSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(superserve_module.WorkspaceReadNotFoundError):
        await session.read(Path("nope.txt"))


@pytest.mark.asyncio
async def test_superserve_exec_read_write_reject_path_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    client = superserve_module.SuperserveSandboxClient()

    session = await client.create(
        manifest=Manifest(root="/workspace/project"),
        options=superserve_module.SuperserveSandboxClientOptions(),
    )

    with pytest.raises(InvalidManifestPathError):
        await session.read("../outside.txt")
    with pytest.raises(InvalidManifestPathError):
        await session.write("/etc/passwd", io.BytesIO(b"nope"))


@pytest.mark.asyncio
async def test_superserve_rejects_sandbox_local_user_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    client = superserve_module.SuperserveSandboxClient()
    session = await client.create(
        manifest=Manifest(root="/workspace/project"),
        options=superserve_module.SuperserveSandboxClientOptions(),
    )

    with pytest.raises(ConfigurationError, match="does not support sandbox-local users"):
        await session.exec("pwd", user="sandbox-user")
    with pytest.raises(ConfigurationError, match="does not support sandbox-local users"):
        await session.read("notes.txt", user=User(name="sandbox-user"))
    with pytest.raises(ConfigurationError, match="does not support sandbox-local users"):
        await session.write("notes.txt", io.BytesIO(b"payload"), user="sandbox-user")


# ---------------------------------------------------------------------------
# Workspace setup / manifest materialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superserve_start_creates_workspace_and_materializes_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000010",
        manifest=Manifest(
            root="/workspace",
            entries={"notes.txt": File(content=b"payload")},
        ),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sup-existing")
    session = superserve_module.SuperserveSandboxSession.from_state(state, sandbox=sandbox)

    await session.start()
    payload = await session.read(Path("notes.txt"))

    # First exec is the workspace-root mkdir.
    assert sandbox.commands.calls[0]["command"].startswith("mkdir -p")
    assert ("/workspace/notes.txt", b"payload") in sandbox.files.write_calls
    assert session.state.workspace_root_ready is True
    assert payload.read() == b"payload"


# ---------------------------------------------------------------------------
# Exposed ports (v1: unsupported)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superserve_resolve_exposed_port_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000020",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
        exposed_ports=(3000,),
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sup-existing")
    session = superserve_module.SuperserveSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(ExposedPortUnavailableError) as exc_info:
        await session.resolve_exposed_port(3000)

    assert exc_info.value.context["backend"] == "superserve"


# ---------------------------------------------------------------------------
# Shutdown semantics: pause-on-exit vs kill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superserve_shutdown_pauses_when_pause_on_exit_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000030",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
        pause_on_exit=True,
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sup-existing")
    session = superserve_module.SuperserveSandboxSession.from_state(state, sandbox=sandbox)

    await session.shutdown()

    assert sandbox.pause_calls == 1
    assert sandbox.kill_calls == 0


@pytest.mark.asyncio
async def test_superserve_shutdown_kills_when_pause_on_exit_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000031",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
        pause_on_exit=False,
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sup-existing")
    session = superserve_module.SuperserveSandboxSession.from_state(state, sandbox=sandbox)

    await session.shutdown()

    assert sandbox.kill_calls == 1
    assert sandbox.pause_calls == 0


# ---------------------------------------------------------------------------
# Resume contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superserve_resume_reconnects_active_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sup-existing", status="active")
    _FakeAsyncSandbox.sandboxes[existing.id] = existing

    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000040",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.id,
    )

    client = superserve_module.SuperserveSandboxClient()
    resumed = await client.resume(state)

    assert _FakeAsyncSandbox.connect_calls[0]["sandbox_id"] == existing.id
    assert resumed._inner.state.sandbox_id == existing.id
    assert _FakeAsyncSandbox.create_calls == []
    # Already active, no resume()
    assert existing.resume_calls == 0


@pytest.mark.asyncio
async def test_superserve_resume_calls_resume_for_paused_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sup-paused", status="paused")
    _FakeAsyncSandbox.sandboxes[existing.id] = existing

    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000041",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.id,
    )

    client = superserve_module.SuperserveSandboxClient()
    resumed = await client.resume(state)

    assert existing.resume_calls == 1
    assert resumed._inner.state.sandbox_id == existing.id
    assert _FakeAsyncSandbox.create_calls == []


@pytest.mark.asyncio
async def test_superserve_resume_polls_until_active_after_resume_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sup-paused-poll", status="paused")
    _FakeAsyncSandbox.sandboxes[existing.id] = existing

    # Make sandbox.resume() leave the status at "resuming" so _wait_until_active has to poll.
    original_resume = _FakeAsyncSandbox.resume

    async def _slow_resume(self: _FakeAsyncSandbox) -> None:
        self.resume_calls += 1
        self.status = "resuming"

    monkeypatch.setattr(_FakeAsyncSandbox, "resume", _slow_resume)

    # On the second get_info call, flip status to "active" so polling succeeds.
    get_info_count = {"n": 0}

    async def _get_info_then_active(self: _FakeAsyncSandbox) -> _FakeSandboxInfo:
        get_info_count["n"] += 1
        if get_info_count["n"] >= 2:
            self.status = "active"
        return _FakeSandboxInfo(status=self.status)

    monkeypatch.setattr(_FakeAsyncSandbox, "get_info", _get_info_then_active)

    # Tighten the poll cadence so the test doesn't actually sleep.
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000043",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.id,
        timeouts=superserve_module.SuperserveSandboxTimeouts(
            resume_ready_poll_interval_s=0.001,
            resume_ready_timeout_s=5,
        ),
    )

    client = superserve_module.SuperserveSandboxClient()
    resumed = await client.resume(state)

    # Restore original method to avoid leaking into other tests.
    monkeypatch.setattr(_FakeAsyncSandbox, "resume", original_resume)

    assert existing.resume_calls == 1
    assert get_info_count["n"] >= 2  # polled at least twice
    assert resumed._inner.state.sandbox_id == existing.id
    assert existing.status == "active"


@pytest.mark.asyncio
async def test_superserve_resume_skips_resume_call_when_already_resuming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sup-already-resuming", status="resuming")
    _FakeAsyncSandbox.sandboxes[existing.id] = existing

    # Flip to active on first get_info so the poll exits immediately.
    async def _get_info_active(self: _FakeAsyncSandbox) -> _FakeSandboxInfo:
        self.status = "active"
        return _FakeSandboxInfo(status="active")

    monkeypatch.setattr(_FakeAsyncSandbox, "get_info", _get_info_active)

    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000044",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.id,
    )

    client = superserve_module.SuperserveSandboxClient()
    resumed = await client.resume(state)

    # Critical: do NOT call resume() when status is already "resuming".
    assert existing.resume_calls == 0
    assert resumed._inner.state.sandbox_id == existing.id


@pytest.mark.asyncio
async def test_superserve_resume_recreates_on_unknown_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sup-stopping", status="stopping")
    _FakeAsyncSandbox.sandboxes[existing.id] = existing

    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000045",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.id,
        template="superserve/python-3.11",
    )

    client = superserve_module.SuperserveSandboxClient()
    resumed = await client.resume(state)

    # Unknown/stopping → recreate.
    assert len(_FakeAsyncSandbox.create_calls) == 1
    assert resumed._inner.state.sandbox_id != existing.id
    assert resumed._inner._workspace_state_preserved_on_start() is False


@pytest.mark.asyncio
async def test_superserve_resume_recreates_on_failed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sup-failed", status="failed")
    _FakeAsyncSandbox.sandboxes[existing.id] = existing

    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000046",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.id,
    )

    client = superserve_module.SuperserveSandboxClient()
    resumed = await client.resume(state)

    assert len(_FakeAsyncSandbox.create_calls) == 1
    assert resumed._inner.state.sandbox_id != existing.id
    # Original sandbox never had resume() called on it.
    assert existing.resume_calls == 0


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superserve_create_classifies_conflict_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    _FakeAsyncSandbox.create_failures = [_SuperserveConflictError("name already exists")]

    client = superserve_module.SuperserveSandboxClient()
    with pytest.raises(Exception) as exc_info:
        await client.create(
            manifest=Manifest(),
            options=superserve_module.SuperserveSandboxClientOptions(name="duplicate-name"),
        )
    assert exc_info.value.context.get("reason") == "name_collision"
    assert exc_info.value.context.get("http_status") == 409


def test_superserve_runtime_helper_cache_key_is_sandbox_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000060",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-cache-key",
    )
    session = superserve_module.SuperserveSandboxSession.from_state(state)
    assert session._current_runtime_helper_cache_key() == "sup-cache-key"

    empty_state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000061",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="",
    )
    empty_session = superserve_module.SuperserveSandboxSession.from_state(empty_state)
    assert empty_session._current_runtime_helper_cache_key() is None


@pytest.mark.asyncio
async def test_superserve_resume_falls_back_to_create_on_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    _FakeAsyncSandbox.fail_connect_ids.add("sup-missing")

    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000042",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-missing",
        template="superserve/python-3.11",
    )

    client = superserve_module.SuperserveSandboxClient()
    resumed = await client.resume(state)

    assert _FakeAsyncSandbox.connect_calls[0]["sandbox_id"] == "sup-missing"
    assert len(_FakeAsyncSandbox.create_calls) == 1
    assert _FakeAsyncSandbox.create_calls[0]["from_template"] == "superserve/python-3.11"
    # New backend ID
    assert resumed._inner.state.sandbox_id != "sup-missing"
    # System state is no longer preserved after a recreate.
    assert resumed._inner._workspace_state_preserved_on_start() is False


# ---------------------------------------------------------------------------
# Workspace tar round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superserve_persist_and_hydrate_workspace_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superserve_module = _load_superserve_module(monkeypatch)
    state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000050",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-existing",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sup-existing")
    sandbox._file_store["/workspace/notes.txt"] = b"payload"
    session = superserve_module.SuperserveSandboxSession.from_state(state, sandbox=sandbox)

    persisted = await session.persist_workspace()
    raw = persisted.read()
    assert isinstance(raw, bytes)
    assert raw  # non-empty tar

    # Hydrate into a *new* sandbox; verify the file lands at the expected path.
    other_sandbox = _FakeAsyncSandbox(sandbox_id="sup-other")
    other_state = superserve_module.SuperserveSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000051",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sup-other",
    )
    other = superserve_module.SuperserveSandboxSession.from_state(
        other_state, sandbox=other_sandbox
    )
    await other.hydrate_workspace(io.BytesIO(raw))
    assert other_sandbox._file_store["/workspace/notes.txt"] == b"payload"
