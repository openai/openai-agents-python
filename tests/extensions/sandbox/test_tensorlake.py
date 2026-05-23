from __future__ import annotations

import importlib
import io
import sys
import tarfile
import types
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, cast

import pytest

from agents.sandbox import Manifest
from agents.sandbox.entries import File
from agents.sandbox.snapshot import LocalSnapshot, NoopSnapshot
from tests._fake_workspace_paths import resolve_fake_workspace_path


class _FakeCommandResult:
    def __init__(self, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class _FakeSnapshotInfo:
    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id


class _FakeCheckpointType:
    # Mirror the real `CheckpointType` str-Enum shape (members expose `.value`) so the
    # integration's `_resolve_checkpoint_type(...).value` path is exercised by the fake.
    class _Member:
        def __init__(self, value: str) -> None:
            self.value = value

    FILESYSTEM = _Member("filesystem")
    MEMORY = _Member("memory")


class _FakeSandboxStatus(str, Enum):
    # Real `SandboxStatus` is a str-Enum, so members compare equal both to each other and
    # to their raw string value. Mirror that here so `status == SandboxStatus.RUNNING`
    # works against the fake.
    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"


class _FakeSandboxError(Exception):
    """Mimics `tensorlake.sandbox.SandboxError`."""


class _FakeRemoteAPIError(_FakeSandboxError):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(f"API error (status {status_code}): {message}")
        self.status_code = status_code
        self.message = message


class _FakeSandboxInfo:
    def __init__(self, *, sandbox_url: str | None = None) -> None:
        self.sandbox_url = sandbox_url


class _FakeTraced:
    """Mimics the Tensorlake SDK `Traced[T]` wrapper returned by `read_file`."""

    def __init__(self, value: Any) -> None:
        self.trace_id = "trace-fake"
        self._value = value

    @property
    def value(self) -> Any:
        return self._value

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_value"), name)


class _FakeSandbox:
    """Async fake mirroring the Tensorlake `AsyncSandbox` surface used by the integration."""

    create_calls: list[dict[str, object]] = []
    connect_calls: list[dict[str, object]] = []
    sandboxes: dict[str, _FakeSandbox] = {}
    snapshots: dict[str, dict[str, bytes]] = {}
    next_sandbox_index: int = 0
    create_failures: list[BaseException] = []
    connect_failures: dict[str, BaseException] = {}
    update_failures: list[BaseException] = []

    def __init__(
        self,
        *,
        sandbox_id: str,
        name: str | None = None,
        status: str = "running",
        files: dict[str, bytes] | None = None,
        sandbox_url: str | None = None,
        status_refresh_sandbox_url: str | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.name = name
        self._status = _FakeSandboxStatus(status)
        self.files: dict[str, bytes] = dict(files or {})
        self.run_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []
        self.terminated = False
        self.terminate_count = 0
        self.closed = False
        self.close_count = 0
        self.suspended = False
        self.resumed = False
        self.resume_failure: BaseException | None = None
        self.update_failure: BaseException | None = None
        self.next_run_result: _FakeCommandResult | None = None
        self.symlinks: dict[str, str] = {}
        self.sandbox_url = sandbox_url
        self.status_refresh_sandbox_url = status_refresh_sandbox_url
        self.info_calls = 0
        self.status_calls = 0
        self.last_checkpoint_wait_until: str | None = None

    @classmethod
    def reset(cls) -> None:
        cls.create_calls = []
        cls.connect_calls = []
        cls.sandboxes = {}
        cls.snapshots = {}
        cls.next_sandbox_index = 0
        cls.create_failures = []
        cls.connect_failures = {}
        cls.update_failures = []

    @classmethod
    async def create(cls, **kwargs: object) -> _FakeSandbox:
        cls.create_calls.append(dict(kwargs))
        if cls.create_failures:
            raise cls.create_failures.pop(0)
        cls.next_sandbox_index += 1
        sandbox_id = f"tensorlake-sandbox-{cls.next_sandbox_index}"
        files: dict[str, bytes] = {}
        snapshot_id = kwargs.get("snapshot_id")
        if isinstance(snapshot_id, str) and snapshot_id in cls.snapshots:
            files = dict(cls.snapshots[snapshot_id])
        sandbox = cls(
            sandbox_id=sandbox_id,
            name=cast(str | None, kwargs.get("name")),
            files=files,
        )
        cls.sandboxes[sandbox_id] = sandbox
        return sandbox

    @classmethod
    async def connect(cls, sandbox_id: str, **kwargs: object) -> _FakeSandbox:
        cls.connect_calls.append({"sandbox_id": sandbox_id, **kwargs})
        if sandbox_id in cls.connect_failures:
            raise cls.connect_failures[sandbox_id]
        sandbox = cls.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise RuntimeError(f"sandbox {sandbox_id} not found")
        return sandbox

    async def status(self) -> Any:
        self.status_calls += 1
        if self.status_refresh_sandbox_url is not None:
            self.sandbox_url = self.status_refresh_sandbox_url
        return self._status

    async def info(self) -> _FakeSandboxInfo:
        self.info_calls += 1
        return _FakeSandboxInfo(sandbox_url=self.sandbox_url)

    async def run(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        working_dir: str | None = None,
        timeout: float | None = None,
    ) -> _FakeTraced:
        _ = (env, timeout)
        args = args or []
        self.run_calls.append(
            {
                "command": command,
                "args": list(args),
                "working_dir": working_dir,
            }
        )

        resolved = resolve_fake_workspace_path(
            (command, *args), symlinks=self.symlinks, home_dir="/workspace"
        )
        if resolved is not None:
            return _FakeTraced(
                _FakeCommandResult(
                    exit_code=resolved.exit_code,
                    stdout=resolved.stdout,
                    stderr=resolved.stderr,
                )
            )

        if self.next_run_result is not None:
            result = self.next_run_result
            self.next_run_result = None
            return _FakeTraced(result)

        if command == "mkdir":
            return _FakeTraced(_FakeCommandResult())

        cwd = working_dir or "/workspace"

        if command == "tar" and args and args[0] == "cf":
            archive_path = args[1]
            assert "-C" in args
            tar_root = args[args.index("-C") + 1]
            include_dot = args[-1] == "."
            exclusions = {
                arg.removeprefix("--exclude=./") for arg in args if arg.startswith("--exclude=./")
            }
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as tar:
                for path, content in sorted(self.files.items()):
                    if not path.startswith(tar_root.rstrip("/") + "/"):
                        continue
                    rel_path = path[len(tar_root.rstrip("/")) + 1 :]
                    if any(rel_path == ex or rel_path.startswith(f"{ex}/") for ex in exclusions):
                        continue
                    info = tarfile.TarInfo(name=rel_path if include_dot else path)
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
            self.files[archive_path] = buffer.getvalue()
            return _FakeTraced(_FakeCommandResult())

        if command == "tar" and args and args[0] == "xf":
            archive_path = args[1]
            destination = args[args.index("-C") + 1]
            raw = self.files[archive_path]
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    self.files[f"{destination.rstrip('/')}/{member.name}"] = extracted.read()
            return _FakeTraced(_FakeCommandResult())

        if command == "test" and args and args[0] == "-d":
            return _FakeTraced(_FakeCommandResult(exit_code=0))

        _ = cwd
        return _FakeTraced(_FakeCommandResult())

    async def read_file(self, path: str) -> _FakeTraced:
        if path not in self.files:
            raise _FakeRemoteAPIError(404, f"file not found: {path}")
        return _FakeTraced(self.files[path])

    async def write_file(self, path: str, content: bytes) -> _FakeTraced:
        self.files[path] = bytes(content)
        return _FakeTraced(None)

    async def delete_file(self, path: str) -> _FakeTraced:
        self.files.pop(path, None)
        return _FakeTraced(None)

    async def terminate(self) -> None:
        self.terminated = True
        self.terminate_count += 1
        self._status = _FakeSandboxStatus.TERMINATED
        # The real SDK closes the local Rust client inside `terminate()`; mirror that
        # so leak-coverage tests see the same call count as production.
        self.close()

    def close(self) -> None:
        self.closed = True
        self.close_count += 1

    async def suspend(
        self, wait: bool = True, timeout: float = 300.0, poll_interval: float = 1.0
    ) -> None:
        _ = (wait, timeout, poll_interval)
        if not self.name:
            raise _FakeRemoteAPIError(400, "only named sandboxes can be suspended")
        self.suspended = True
        self._status = _FakeSandboxStatus.SUSPENDED

    async def resume(
        self, wait: bool = True, timeout: float = 300.0, poll_interval: float = 1.0
    ) -> None:
        _ = (wait, timeout, poll_interval)
        if self.resume_failure is not None:
            raise self.resume_failure
        self.resumed = True
        self._status = _FakeSandboxStatus.RUNNING

    async def update(
        self,
        name: str | None = None,
        *,
        allow_unauthenticated_access: bool | None = None,
        exposed_ports: list[int] | None = None,
    ) -> _FakeTraced:
        self.update_calls.append(
            {
                "name": name,
                "allow_unauthenticated_access": allow_unauthenticated_access,
                "exposed_ports": list(exposed_ports) if exposed_ports is not None else None,
            }
        )
        if self.update_failure is not None:
            raise self.update_failure
        if type(self).update_failures:
            raise type(self).update_failures.pop(0)
        return _FakeTraced(_FakeSandboxInfo(sandbox_url=self.sandbox_url))

    async def checkpoint(
        self,
        wait: bool = True,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
        checkpoint_type: Any = None,
        wait_until: str = "local_ready",
    ) -> _FakeSnapshotInfo:
        _ = (wait, timeout, poll_interval, checkpoint_type)
        self.last_checkpoint_wait_until = wait_until
        snapshot_id = f"snap-{len(type(self).snapshots) + 1}"
        type(self).snapshots[snapshot_id] = dict(self.files)
        return _FakeSnapshotInfo(snapshot_id)


@pytest.fixture(autouse=True)
def _reset_fake_sandbox_state() -> None:
    _FakeSandbox.reset()


def _load_tensorlake_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    _FakeSandbox.reset()

    fake_pkg = types.ModuleType("tensorlake")
    fake_sandbox_pkg = cast(Any, types.ModuleType("tensorlake.sandbox"))
    fake_sandbox_pkg.AsyncSandbox = _FakeSandbox
    fake_sandbox_pkg.CheckpointType = _FakeCheckpointType
    fake_sandbox_pkg.SandboxStatus = _FakeSandboxStatus
    fake_sandbox_pkg.RemoteAPIError = _FakeRemoteAPIError
    fake_sandbox_pkg.SandboxError = _FakeSandboxError

    monkeypatch.setitem(sys.modules, "tensorlake", fake_pkg)
    monkeypatch.setitem(sys.modules, "tensorlake.sandbox", fake_sandbox_pkg)
    sys.modules.pop("agents.extensions.sandbox.tensorlake.sandbox", None)
    sys.modules.pop("agents.extensions.sandbox.tensorlake", None)

    return importlib.import_module("agents.extensions.sandbox.tensorlake.sandbox")


def test_tensorlake_package_re_exports_backend_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    pkg = importlib.import_module("agents.extensions.sandbox.tensorlake")

    assert pkg.TensorlakeSandboxClient is module.TensorlakeSandboxClient
    assert pkg.TensorlakeSandboxSessionState is module.TensorlakeSandboxSessionState


def test_tensorlake_supports_pty_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-no-pty",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-no-pty")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)
    assert session.supports_pty() is False


@pytest.mark.asyncio
async def test_resolve_sandbox_id_handles_raising_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real `AsyncSandbox.sandbox_id` is a property that raises `SandboxError` before
    # `info()` populates the cache. `_resolve_sandbox_id` must swallow that, seed the
    # cache via `info()`, and read again — instead of propagating an SDK exception out
    # of the integration's create/restore paths.
    module = _load_tensorlake_module(monkeypatch)

    class _AlwaysRaisingSandbox:
        info_calls = 0

        @property
        def sandbox_id(self) -> str:
            raise _FakeSandboxError("sandbox_id is not yet known; call `info()` first.")

        async def info(self) -> None:
            type(self).info_calls += 1

    sandbox = _AlwaysRaisingSandbox()
    assert await module._resolve_sandbox_id(sandbox) is None
    # `info()` is awaited at most once even if the second read also fails.
    assert _AlwaysRaisingSandbox.info_calls == 1

    class _EmptySandbox:
        sandbox_id = ""

        async def info(self) -> None:
            pass

    assert await module._resolve_sandbox_id(_EmptySandbox()) is None

    class _ReadySandbox:
        sandbox_id = "sb-123"

        async def info(self) -> None:
            pass

    assert await module._resolve_sandbox_id(_ReadySandbox()) == "sb-123"

    class _LateBoundSandbox:
        # Mirrors the real SDK shape: `sandbox_id` raises until `info()` populates the
        # cache, after which the property returns the id.
        def __init__(self) -> None:
            self._id: str | None = None

        @property
        def sandbox_id(self) -> str:
            if self._id is None:
                raise _FakeSandboxError("sandbox_id is not yet known")
            return self._id

        async def info(self) -> None:
            self._id = "sb-late"

    assert await module._resolve_sandbox_id(_LateBoundSandbox()) == "sb-late"


@pytest.mark.asyncio
async def test_create_passes_options_and_drops_unset_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(
            image="my-image",
            cpus=2.0,
            memory_mb=2048,
            disk_mb=20480,
            timeout_secs=600,
            name="demo",
            secret_names=("OPENAI_KEY",),
            allow_internet_access=False,
            allow_out=("10.0.0.0/8",),
            deny_out=("example.com",),
            exposed_ports=(8080,),
            allow_unauthenticated_port_access=True,
        ),
    )

    assert _FakeSandbox.create_calls == [
        {
            "image": "my-image",
            "cpus": 2.0,
            "memory_mb": 2048,
            "disk_mb": 20480,
            "timeout_secs": 600,
            "name": "demo",
            "secret_names": ["OPENAI_KEY"],
            "allow_internet_access": False,
            "allow_out": ["10.0.0.0/8"],
            "deny_out": ["example.com"],
        }
    ]
    inner = session._inner
    assert inner.state.sandbox_id == "tensorlake-sandbox-1"
    sandbox = _FakeSandbox.sandboxes["tensorlake-sandbox-1"]
    assert sandbox.update_calls == [
        {
            "name": None,
            "allow_unauthenticated_access": True,
            "exposed_ports": [8080],
        }
    ]


@pytest.mark.asyncio
async def test_create_fails_when_exposed_port_update_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    cause = RuntimeError("port update failed")
    _FakeSandbox.update_failures.append(cause)

    from agents.sandbox.errors import WorkspaceStartError

    with pytest.raises(WorkspaceStartError) as exc_info:
        await client.create(
            manifest=Manifest(),
            options=module.TensorlakeSandboxClientOptions(exposed_ports=(8080,)),
        )

    assert exc_info.value.__cause__ is cause
    assert exc_info.value.message == "failed to expose Tensorlake sandbox ports"
    assert exc_info.value.context["reason"] == "tensorlake_exposed_ports_update_failed"
    assert exc_info.value.context["sandbox_id"] == "tensorlake-sandbox-1"
    assert exc_info.value.context["ports"] == [8080]
    assert _FakeSandbox.sandboxes["tensorlake-sandbox-1"].terminate_count == 1


@pytest.mark.asyncio
async def test_create_generates_name_when_pause_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()

    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(pause_on_exit=True),
    )

    generated_name = _FakeSandbox.create_calls[0]["name"]
    assert isinstance(generated_name, str)
    assert generated_name.startswith("openai-agents-")
    assert session._inner.state.name == generated_name
    assert session._inner._sandbox.name == generated_name


@pytest.mark.asyncio
async def test_create_passes_routing_options(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(
            entrypoint=("python", "-m", "app"),
            startup_timeout=90.0,
            proxy_url="https://proxy.tensorlake.dev",
            api_url="https://api.tensorlake.dev",
            namespace="tenant-a",
        ),
    )

    assert _FakeSandbox.create_calls == [
        {
            "allow_internet_access": True,
            "entrypoint": ["python", "-m", "app"],
            "startup_timeout": 90.0,
            "proxy_url": "https://proxy.tensorlake.dev",
            "api_url": "https://api.tensorlake.dev",
            "namespace": "tenant-a",
        }
    ]
    state = session._inner.state
    assert state.entrypoint == ("python", "-m", "app")
    assert state.startup_timeout == 90.0
    assert state.proxy_url == "https://proxy.tensorlake.dev"
    assert state.api_url == "https://api.tensorlake.dev"
    assert state.namespace == "tenant-a"


@pytest.mark.asyncio
async def test_resume_forwards_routing_to_connect_and_recreate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)

    existing = _FakeSandbox(sandbox_id="sandbox-dead", status="terminated")
    _FakeSandbox.sandboxes["sandbox-dead"] = existing

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-dead",
        entrypoint=("python", "-m", "app"),
        startup_timeout=90.0,
        proxy_url="https://proxy.tensorlake.dev",
        api_url="https://api.tensorlake.dev",
        namespace="tenant-a",
    )

    client = module.TensorlakeSandboxClient()
    await client.resume(state)

    assert _FakeSandbox.connect_calls == [
        {
            "sandbox_id": "sandbox-dead",
            "proxy_url": "https://proxy.tensorlake.dev",
            "api_url": "https://api.tensorlake.dev",
            "namespace": "tenant-a",
        }
    ]
    assert len(_FakeSandbox.create_calls) == 1
    create_kwargs = _FakeSandbox.create_calls[0]
    assert create_kwargs["entrypoint"] == ["python", "-m", "app"]
    assert create_kwargs["startup_timeout"] == 90.0
    assert create_kwargs["proxy_url"] == "https://proxy.tensorlake.dev"
    assert create_kwargs["api_url"] == "https://api.tensorlake.dev"
    assert create_kwargs["namespace"] == "tenant-a"


@pytest.mark.asyncio
async def test_create_omits_optional_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(),
    )

    assert _FakeSandbox.create_calls == [{"allow_internet_access": True}]


@pytest.mark.asyncio
async def test_create_rejects_snapshot_with_lifetime_le_checkpoint_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When workspace_persistence='snapshot' is in play and `timeout_secs` is not greater
    # than `checkpoint_timeout_s`, the sandbox can be auto-terminated during checkpoint
    # polling and orphan the snapshot. Reject the misconfiguration at create() time.
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()

    with pytest.raises(ValueError, match="timeout_secs must be strictly greater"):
        await client.create(
            manifest=Manifest(),
            options=module.TensorlakeSandboxClientOptions(
                workspace_persistence="snapshot",
                timeout_secs=300,
                checkpoint_timeout_s=300.0,
            ),
        )

    # Default tar persistence should not trigger the validation even with the same timings.
    await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(
            timeout_secs=300,
            checkpoint_timeout_s=300.0,
        ),
    )

    # Snapshot persistence with strictly greater lifetime is accepted.
    await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(
            workspace_persistence="snapshot",
            timeout_secs=600,
            checkpoint_timeout_s=300.0,
        ),
    )

    # `timeout_secs=0` requests the plan maximum (≥1h on every Tensorlake plan), which
    # is always larger than any reasonable `checkpoint_timeout_s`, so the guard must
    # let it through even though `0 <= checkpoint_timeout_s` is literally true.
    await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(
            workspace_persistence="snapshot",
            timeout_secs=0,
            checkpoint_timeout_s=300.0,
        ),
    )


@pytest.mark.asyncio
async def test_create_rewrites_default_manifest_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # Callers that construct `Manifest(entries=...)` leave `root` at the cross-provider
    # default `/workspace`, which is not writable for `tl-user` in the default Tensorlake
    # image. The client should rewrite that default to the Tensorlake-writable path so
    # those manifests work without callers having to know the backend-specific root.
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(),
    )

    assert session._inner.state.manifest.root == module.DEFAULT_TENSORLAKE_WORKSPACE_ROOT


@pytest.mark.asyncio
async def test_create_preserves_explicit_manifest_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-default manifest root must be honored verbatim; only the default `/workspace`
    # placeholder is rewritten.
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(root="/tmp/custom"),
        options=module.TensorlakeSandboxClientOptions(),
    )

    assert session._inner.state.manifest.root == "/tmp/custom"


@pytest.mark.asyncio
async def test_exec_read_write_and_mkdir(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-rw",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-rw")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    await session.write(Path("notes.txt"), io.BytesIO(b"hello"))
    payload = await session.read(Path("notes.txt"))
    assert payload.read() == b"hello"

    await session.mkdir(Path("subdir"), parents=True)
    mkdir_calls = [c for c in fake.run_calls if c["command"] == "mkdir"]
    assert mkdir_calls and mkdir_calls[-1]["args"] == ["-p", "--", "/workspace/subdir"]

    fake.next_run_result = _FakeCommandResult(stdout="hi\n", exit_code=0)
    result = await session.exec("printf", "hi", shell=False)
    assert result.ok()
    assert result.stdout == b"hi\n"


@pytest.mark.asyncio
async def test_exec_user_param_wraps_with_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tensorlake's AsyncSandbox.run does not accept user=; switching users goes through
    # the base class's `sudo -u <name> --` wrap like every other backend.
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000beef"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-user",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-user")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    fake.next_run_result = _FakeCommandResult(stdout="ok\n", exit_code=0)
    await session.exec("printf", "ok", shell=False, user="tl-user")

    last = fake.run_calls[-1]
    assert last["command"] == "sudo"
    assert last["args"] == ["-u", "tl-user", "--", "printf", "ok"]
    assert "user" not in last


@pytest.mark.asyncio
async def test_mkdir_user_param_wraps_with_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `session.mkdir(..., user=...)` must run as the requested sandbox-local user so the
    # directory is created with the correct ownership; otherwise Tensorlake would run
    # mkdir as the default `tl-user`, which fails for directories only the requested
    # user can create.
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-0000000d1ca1"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-mkdir-user",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-mkdir-user")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    await session.mkdir(Path("subdir"), parents=True, user="root")

    mkdir_calls = [
        c
        for c in fake.run_calls
        if c["command"] == "sudo" and "mkdir" in cast(list[str], c["args"])
    ]
    assert mkdir_calls, "expected mkdir to be wrapped with `sudo` when user= is set"
    last = mkdir_calls[-1]
    assert last["args"] == ["-u", "root", "--", "mkdir", "-p", "--", "/workspace/subdir"]


@pytest.mark.asyncio
async def test_exec_checked_nonzero_runs_privileged_metadata_as_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Manifest account provisioning and file metadata commands (groupadd/useradd/
    # usermod/chgrp/chmod) funnel through `_exec_checked_nonzero`. The default
    # Tensorlake image's process user is the non-root `tl-user`, and the SDK
    # exposes no process-user knob on `run()` — so without the sudo wrap these
    # privileged operations fail with permission denied and any manifest that
    # declares users or groups cannot start.
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000feed"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-provision",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-provision")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    await session._exec_checked_nonzero("groupadd", "researchers")
    await session._exec_checked_nonzero("useradd", "-U", "-M", "alice")
    await session._exec_checked_nonzero("chmod", "0600", "/tmp/secret")

    sudo_calls = [c for c in fake.run_calls if c["command"] == "sudo"]
    expected_args = [
        ["-u", "root", "--", "groupadd", "researchers"],
        ["-u", "root", "--", "useradd", "-U", "-M", "alice"],
        ["-u", "root", "--", "chmod", "0600", "/tmp/secret"],
    ]
    assert [c["args"] for c in sudo_calls] == expected_args, (
        f"expected privileged commands to be wrapped with `sudo -u root --`, got {sudo_calls!r}"
    )


@pytest.mark.asyncio
async def test_read_missing_file_raises_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-missing",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-missing")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    from agents.sandbox.errors import WorkspaceReadNotFoundError

    with pytest.raises(WorkspaceReadNotFoundError):
        await session.read(Path("nope.txt"))


@pytest.mark.asyncio
async def test_read_non_404_remote_api_error_raises_archive_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-404 `RemoteAPIError` from `read_file` surfaces as `WorkspaceArchiveReadError`.

    Only the 404 path is treated as a missing-file signal; other statuses (e.g. 403/500)
    indicate a transport/auth failure and must not be reported as "not found".
    """

    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000050"),
        manifest=Manifest(entries={"notes.txt": File(content=b"x")}),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-read-500",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-read-500")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)
    await session.start()

    async def _raise_500(_path: str) -> _FakeTraced:
        raise _FakeRemoteAPIError(500, "internal server error")

    fake.read_file = _raise_500  # type: ignore[assignment]

    from agents.sandbox.errors import WorkspaceArchiveReadError, WorkspaceReadNotFoundError

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.read(Path("notes.txt"))

    assert not isinstance(exc_info.value, WorkspaceReadNotFoundError)
    cause = exc_info.value.__cause__
    assert isinstance(cause, _FakeRemoteAPIError)
    assert cause.status_code == 500


@pytest.mark.asyncio
async def test_exposed_port_resolution_uses_sandbox_id(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000004"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-ports",
        exposed_ports=(3000,),
    )
    fake = _FakeSandbox(sandbox_id="sandbox-ports")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    endpoint = await session.resolve_exposed_port(3000)
    assert endpoint.host == "3000-sandbox-ports.sandbox.tensorlake.ai"
    assert endpoint.port == 443
    assert endpoint.tls is True


@pytest.mark.asyncio
async def test_exposed_port_resolution_uses_named_sandbox_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000005"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-id",
        name="demo",
        exposed_ports=(8080,),
    )
    fake = _FakeSandbox(sandbox_id="sandbox-id", name="demo")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    endpoint = await session.resolve_exposed_port(8080)
    assert endpoint.host == "8080-demo.sandbox.tensorlake.ai"


@pytest.mark.asyncio
async def test_exposed_port_resolution_uses_backend_sandbox_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000000a"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-dev",
        name="dev-env",
        exposed_ports=(8080,),
    )
    fake = _FakeSandbox(
        sandbox_id="sandbox-dev",
        name="dev-env",
        sandbox_url="https://dev-env.sandbox.tensorlake.dev",
    )
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    endpoint = await session.resolve_exposed_port(8080)
    assert endpoint.host == "8080-dev-env.sandbox.tensorlake.dev"


@pytest.mark.asyncio
async def test_custom_proxy_exposed_port_resolution_refreshes_minimal_cached_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000003a"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-dev",
        name="dev-env",
        exposed_ports=(8080,),
        proxy_url="https://sandbox.tensorlake.dev",
    )
    fake = _FakeSandbox(
        sandbox_id="sandbox-dev",
        name="dev-env",
        sandbox_url=None,
        status_refresh_sandbox_url="https://dev-env.sandbox.tensorlake.dev",
    )
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    endpoint = await session.resolve_exposed_port(8080)

    assert endpoint.host == "8080-dev-env.sandbox.tensorlake.dev"
    assert fake.status_calls == 1
    assert fake.info_calls == 2


@pytest.mark.asyncio
async def test_exposed_port_resolution_caches_proxy_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000000c"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-cache",
        exposed_ports=(8080, 9090),
    )
    fake = _FakeSandbox(
        sandbox_id="sandbox-cache",
        sandbox_url="https://sandbox-cache.sandbox.tensorlake.ai",
    )
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    await session.resolve_exposed_port(8080)
    await session.resolve_exposed_port(9090)

    assert fake.info_calls == 1


@pytest.mark.asyncio
async def test_custom_proxy_exposed_port_resolution_retries_after_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000004a"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-dev",
        name="dev-env",
        exposed_ports=(8080,),
        proxy_url="https://sandbox.tensorlake.dev",
    )
    fake = _FakeSandbox(
        sandbox_id="sandbox-dev",
        name="dev-env",
        sandbox_url=None,
    )

    info_failures = {"count": 1}
    status_failures = {"count": 1}
    original_info = fake.info
    original_status = fake.status

    async def flaky_info() -> Any:
        if info_failures["count"] > 0:
            info_failures["count"] -= 1
            raise RuntimeError("transient info() failure")
        return await original_info()

    async def flaky_status() -> Any:
        if status_failures["count"] > 0:
            status_failures["count"] -= 1
            raise RuntimeError("transient status() failure")
        return await original_status()

    fake.info = flaky_info  # type: ignore[assignment]
    fake.status = flaky_status  # type: ignore[assignment]

    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    # First lookup hits the transient failures and falls back to the public template;
    # because this is a custom-control-plane deployment, the cache must NOT latch.
    first = await session.resolve_exposed_port(8080)
    assert first.host == "8080-dev-env.sandbox.tensorlake.ai"

    # Recover and supply a sandbox_url so the next info() refresh returns it.
    fake.sandbox_url = "https://dev-env.sandbox.tensorlake.dev"

    # The retry must reach the backend instead of short-circuiting on the cached miss.
    second = await session.resolve_exposed_port(8080)
    assert second.host == "8080-dev-env.sandbox.tensorlake.dev"


@pytest.mark.asyncio
async def test_delete_terminates_remote_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(),
    )
    fake = session._inner._sandbox
    assert fake.terminated is False

    await client.delete(session)

    assert fake.terminate_count == 1


@pytest.mark.asyncio
async def test_delete_terminates_even_when_pause_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(pause_on_exit=True),
    )
    fake = session._inner._sandbox

    await client.delete(session)

    assert fake.terminate_count == 1
    assert fake.suspended is False


@pytest.mark.asyncio
async def test_shutdown_then_delete_does_not_double_terminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(),
    )
    fake = session._inner._sandbox

    await session.shutdown()
    await client.delete(session)

    assert fake.terminate_count == 1


@pytest.mark.asyncio
async def test_shutdown_pause_then_delete_preserves_suspended_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(pause_on_exit=True),
    )
    fake = session._inner._sandbox

    await session.shutdown()
    await client.delete(session)

    assert fake.suspended is True
    assert fake.terminate_count == 0


@pytest.mark.asyncio
async def test_shutdown_terminates_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000006"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-shutdown",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-shutdown")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)
    await session.shutdown()

    assert fake.terminated is True
    assert fake.suspended is False


@pytest.mark.asyncio
async def test_shutdown_suspends_when_pause_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000007"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-pause",
        name="sandbox-pause-name",
        pause_on_exit=True,
    )
    fake = _FakeSandbox(sandbox_id="sandbox-pause", name="sandbox-pause-name")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)
    await session.shutdown()

    assert fake.suspended is True
    assert fake.terminated is False
    assert session._backend_lifecycle_finalized is True


@pytest.mark.asyncio
async def test_shutdown_pause_closes_local_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """`suspend()` does not close the SDK's Rust client; shutdown must close it manually."""
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-pause-close",
        name="sandbox-pause-close-name",
        pause_on_exit=True,
    )
    fake = _FakeSandbox(sandbox_id="sandbox-pause-close", name="sandbox-pause-close-name")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)
    await session.shutdown()

    assert fake.suspended is True
    assert fake.closed is True
    assert fake.close_count == 1
    assert session._sandbox is None


@pytest.mark.asyncio
async def test_shutdown_terminate_closes_local_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000021"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-terminate-close",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-terminate-close")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)
    await session.shutdown()

    assert fake.terminated is True
    assert fake.closed is True
    # `AsyncSandbox.terminate()` already closes the local Rust client; the integration
    # must not call `close()` a second time afterward.
    assert fake.close_count == 1
    assert session._sandbox is None


@pytest.mark.asyncio
async def test_failed_shutdown_terminate_lets_client_delete_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient terminate failure during shutdown must not orphan the remote sandbox.

    Regression: an earlier version of the leak fix nulled `_sandbox` even on the
    shutdown failure path, which made `client.delete()` short-circuit and skip the
    retry — leaving the remote sandbox running until its own `timeout_secs`.
    """
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(),
    )
    inner = session._inner
    fake = inner._sandbox

    original_terminate = fake.terminate
    call_count = {"n": 0}

    async def flaky_terminate() -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient backend error")
        await original_terminate()

    fake.terminate = flaky_terminate

    await session.shutdown()

    # First terminate raised; the local handle must still be attached so delete can retry.
    assert inner._sandbox is fake
    assert inner._backend_lifecycle_finalized is False

    await client.delete(session)

    # `client.delete()` retried terminate; remote is now finalized and the handle freed.
    assert call_count["n"] == 2
    assert inner._sandbox is None
    assert inner._backend_lifecycle_finalized is True


@pytest.mark.asyncio
async def test_failed_shutdown_suspend_then_terminate_fallback_retries_via_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both suspend and the terminate fallback fail during shutdown, delete must retry."""
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(pause_on_exit=True),
    )
    inner = session._inner
    fake = inner._sandbox

    # Force suspend to raise so the fallback terminate path runs.
    async def failing_suspend(
        wait: bool = True, timeout: float = 300.0, poll_interval: float = 1.0
    ) -> None:
        raise RuntimeError("suspend offline")

    original_terminate = fake.terminate
    terminate_calls = {"n": 0}

    async def flaky_terminate() -> None:
        terminate_calls["n"] += 1
        if terminate_calls["n"] == 1:
            raise RuntimeError("fallback terminate offline")
        await original_terminate()

    fake.suspend = failing_suspend
    fake.terminate = flaky_terminate

    await session.shutdown()

    assert inner._sandbox is fake
    assert inner._backend_lifecycle_finalized is False

    await client.delete(session)

    # Two terminate attempts total: the in-shutdown fallback (failed) and the delete retry.
    assert terminate_calls["n"] == 2
    assert inner._sandbox is None
    assert inner._backend_lifecycle_finalized is True


@pytest.mark.asyncio
async def test_delete_closes_local_handle_after_pause_on_exit_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct `client.delete()` after suspend must release the local handle, not just no-op."""
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(pause_on_exit=True),
    )
    inner = session._inner
    fake = inner._sandbox

    # Simulate a session where shutdown completed remote suspend but somehow left the
    # local Rust client attached. delete() must close it without sending terminate.
    inner._backend_lifecycle_finalized = True
    fake.closed = False  # reset the close flag set by `_close_sandbox_handle` paths

    await client.delete(session)

    assert fake.closed is True
    assert fake.terminate_count == 0
    assert inner._sandbox is None


@pytest.mark.asyncio
async def test_delete_closes_local_handle_on_terminate_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    client = module.TensorlakeSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=module.TensorlakeSandboxClientOptions(),
    )
    inner = session._inner
    fake = inner._sandbox

    await client.delete(session)

    assert fake.terminate_count == 1
    assert fake.closed is True
    assert inner._sandbox is None


@pytest.mark.asyncio
async def test_persist_workspace_via_tar_round_trips_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000008"),
        manifest=Manifest(entries={"notes.txt": File(content=b"payload")}),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-tar",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-tar")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    await session.start()
    archive = await session.persist_workspace()
    raw = archive.read()
    assert isinstance(raw, bytes) and raw

    # Hydrate into a new sandbox and ensure files are restored.
    other_state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000009"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-tar-restore",
    )
    other_fake = _FakeSandbox(sandbox_id="sandbox-tar-restore")
    other_session = module.TensorlakeSandboxSession.from_state(other_state, sandbox=other_fake)
    await other_session.hydrate_workspace(io.BytesIO(raw))
    restored = await other_session.read(Path("notes.txt"))
    assert restored.read() == b"payload"


@pytest.mark.asyncio
async def test_persist_workspace_via_tar_excludes_archive_when_root_is_tmp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When manifest.root is /tmp the tar archive lives inside the workspace tree.

    The archive file must be excluded from the tar command so GNU tar does not hit
    its "file is the archive" error (exit code 1).
    """
    module = _load_tensorlake_module(monkeypatch)
    sid = uuid.UUID("00000000-0000-0000-0000-000000000040")
    state = module.TensorlakeSandboxSessionState(
        session_id=sid,
        manifest=Manifest(root="/tmp", entries={"data.txt": File(content=b"val")}),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-tmp-root",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-tmp-root")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    await session.start()
    await session.persist_workspace()

    tar_calls = [c for c in fake.run_calls if c["command"] == "tar"]
    assert tar_calls, "expected at least one tar call"
    last_tar_args = cast(list[str], tar_calls[-1]["args"])
    expected_archive_name = f"openai-agents-{sid.hex}.tar"
    assert any(expected_archive_name in arg for arg in last_tar_args if "--exclude" in arg), (
        f"archive file {expected_archive_name!r} not excluded from tar args: {last_tar_args}"
    )


@pytest.mark.asyncio
async def test_persist_workspace_via_tar_nonzero_raises_archive_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-tar-failure",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-tar-failure")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    from agents.sandbox.errors import WorkspaceArchiveReadError

    fake.next_run_result = _FakeCommandResult(stderr="tar failed", exit_code=2)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert "tar failed" in str(exc_info.value.__cause__)


@pytest.mark.asyncio
async def test_persist_workspace_via_checkpoint_returns_snapshot_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000000a"),
        manifest=Manifest(entries={"notes.txt": File(content=b"snapshot-payload")}),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-checkpoint",
        workspace_persistence="snapshot",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-checkpoint")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    await session.start()
    archive = await session.persist_workspace()
    raw = archive.read()

    assert raw.startswith(module._TENSORLAKE_SNAPSHOT_MAGIC)
    snapshot_id = module._decode_tensorlake_snapshot_ref(raw)
    assert snapshot_id == "snap-1"
    assert _FakeSandbox.snapshots["snap-1"]["/workspace/notes.txt"] == b"snapshot-payload"
    # Default matches the Tensorlake SDK: `local_ready` is enough to resume from the
    # snapshot and avoids blocking on remote-storage upload. Callers needing a durable
    # `snapshot_uri` opt in via `checkpoint_wait_until="completed"`.
    assert fake.last_checkpoint_wait_until == "local_ready"


@pytest.mark.asyncio
async def test_persist_workspace_via_checkpoint_honors_wait_until_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000000b"),
        manifest=Manifest(entries={"notes.txt": File(content=b"snapshot-payload")}),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-checkpoint-completed",
        workspace_persistence="snapshot",
        checkpoint_wait_until="completed",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-checkpoint-completed")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    await session.start()
    await session.persist_workspace()

    assert fake.last_checkpoint_wait_until == "completed"


@pytest.mark.asyncio
async def test_hydrate_workspace_via_checkpoint_replaces_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)

    # First, take a checkpoint via the tar-style helper so it is registered.
    initial = _FakeSandbox(sandbox_id="sandbox-source")
    initial.files["/workspace/from-snapshot.txt"] = b"snap-data"
    snap = await initial.checkpoint(checkpoint_type=_FakeCheckpointType.FILESYSTEM)

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000000b"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-pre-restore",
        workspace_persistence="snapshot",
    )
    pre_restore = _FakeSandbox(sandbox_id="sandbox-pre-restore")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=pre_restore)

    payload = module._encode_tensorlake_snapshot_ref(snapshot_id=snap.snapshot_id)
    await session.hydrate_workspace(io.BytesIO(payload))

    assert pre_restore.terminated is True
    assert state.sandbox_id != "sandbox-pre-restore"
    new_sandbox = _FakeSandbox.sandboxes[state.sandbox_id]
    assert new_sandbox.files["/workspace/from-snapshot.txt"] == b"snap-data"
    assert session._backend_lifecycle_finalized is False
    # Regression: `delete()` must still terminate the live post-restore sandbox.
    client = module.TensorlakeSandboxClient()
    wrapped = client._wrap_session(session, instrumentation=None)
    await client.delete(wrapped)
    assert new_sandbox.terminated is True


@pytest.mark.asyncio
async def test_restore_from_checkpoint_raises_when_post_terminate_create_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `AsyncSandbox.create(snapshot_id=...)` fails after the old sandbox is terminated,
    `_restore_from_checkpoint` must surface a `WorkspaceArchiveWriteError` with the create
    error as the cause — and the pre-restore sandbox must still be marked terminated.
    """

    module = _load_tensorlake_module(monkeypatch)

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000060"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-pre-restore-fail",
        workspace_persistence="snapshot",
    )
    pre_restore = _FakeSandbox(sandbox_id="sandbox-pre-restore-fail")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=pre_restore)

    create_error = RuntimeError("snapshot restore failed")
    _FakeSandbox.create_failures.append(create_error)

    payload = module._encode_tensorlake_snapshot_ref(snapshot_id="snap-missing")

    from agents.sandbox.errors import WorkspaceArchiveWriteError

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        await session.hydrate_workspace(io.BytesIO(payload))

    assert exc_info.value.__cause__ is create_error
    assert exc_info.value.context["reason"] == "tensorlake_checkpoint_restore_failed"
    assert exc_info.value.context["snapshot_id"] == "snap-missing"
    assert pre_restore.terminated is True


@pytest.mark.asyncio
async def test_resume_reconnects_running_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)

    existing = _FakeSandbox(sandbox_id="sandbox-existing", status="running")
    _FakeSandbox.sandboxes["sandbox-existing"] = existing

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000000c"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-existing",
    )

    client = module.TensorlakeSandboxClient()
    session = await client.resume(state)

    assert _FakeSandbox.connect_calls == [{"sandbox_id": "sandbox-existing"}]
    assert _FakeSandbox.create_calls == []
    assert session._inner.state.sandbox_id == "sandbox-existing"


@pytest.mark.asyncio
async def test_resume_fails_when_exposed_port_update_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)

    cause = RuntimeError("port update failed")
    existing = _FakeSandbox(sandbox_id="sandbox-existing", status="running")
    existing.update_failure = cause
    _FakeSandbox.sandboxes["sandbox-existing"] = existing

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000003b"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-existing",
        exposed_ports=(3000,),
    )

    client = module.TensorlakeSandboxClient()

    from agents.sandbox.errors import WorkspaceStartError

    with pytest.raises(WorkspaceStartError) as exc_info:
        await client.resume(state)

    assert exc_info.value.__cause__ is cause
    assert exc_info.value.context["reason"] == "tensorlake_exposed_ports_update_failed"
    assert exc_info.value.context["sandbox_id"] == "sandbox-existing"
    assert exc_info.value.context["ports"] == [3000]
    assert _FakeSandbox.create_calls == []
    assert existing.terminate_count == 0


@pytest.mark.asyncio
async def test_resume_cleans_up_recreated_sandbox_when_exposed_port_update_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)

    cause = RuntimeError("port update failed")
    _FakeSandbox.update_failures.append(cause)
    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000003c"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-missing",
        exposed_ports=(8080,),
    )

    client = module.TensorlakeSandboxClient()

    from agents.sandbox.errors import WorkspaceStartError

    with pytest.raises(WorkspaceStartError):
        await client.resume(state)

    assert _FakeSandbox.sandboxes["tensorlake-sandbox-1"].terminate_count == 1


@pytest.mark.asyncio
async def test_resume_creates_fresh_when_reconnect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000000d"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-missing",
    )

    client = module.TensorlakeSandboxClient()
    session = await client.resume(state)

    assert _FakeSandbox.connect_calls and _FakeSandbox.connect_calls[0]["sandbox_id"] == (
        "sandbox-missing"
    )
    assert len(_FakeSandbox.create_calls) == 1
    new_id = session._inner.state.sandbox_id
    assert new_id.startswith("tensorlake-sandbox-")
    assert state.workspace_root_ready is False


@pytest.mark.asyncio
async def test_resume_creates_fresh_when_paused_resume_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed `resume()` must not be reported as a preserved running sandbox."""

    module = _load_tensorlake_module(monkeypatch)

    existing = _FakeSandbox(sandbox_id="sandbox-paused", status="suspended")
    existing.resume_failure = RuntimeError("sandbox expired")
    _FakeSandbox.sandboxes["sandbox-paused"] = existing

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000000f"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-paused",
        pause_on_exit=True,
    )

    client = module.TensorlakeSandboxClient()
    session = await client.resume(state)

    assert len(_FakeSandbox.create_calls) == 1
    assert _FakeSandbox.create_calls[0]["name"] == (
        "openai-agents-0000000000000000000000000000000f"
    )
    new_id = session._inner.state.sandbox_id
    assert new_id != "sandbox-paused"
    assert new_id.startswith("tensorlake-sandbox-")
    assert session._inner.state.name == "openai-agents-0000000000000000000000000000000f"
    assert state.workspace_root_ready is False
    assert session._inner._workspace_state_preserved_on_start() is False
    assert session._inner._system_state_preserved_on_start() is False
    # With pause_on_exit=True the user expects backend state to survive resume; a probe
    # failure after a successful connect() is ambiguous and may be transient against a
    # still-suspended sandbox. Terminating here would destroy potentially recoverable
    # workspace state (especially with a Noop or stale snapshot), so we only release the
    # local handle and let the backend reclaim the sandbox via its own timeout.
    assert existing.terminate_count == 0
    assert existing.close_count == 1


@pytest.mark.asyncio
async def test_resume_closes_abandoned_handle_when_status_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_tensorlake_module(monkeypatch)

    existing = _FakeSandbox(sandbox_id="sandbox-dead", status="terminated")
    _FakeSandbox.sandboxes["sandbox-dead"] = existing

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000011"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-dead",
    )

    client = module.TensorlakeSandboxClient()
    session = await client.resume(state)

    assert len(_FakeSandbox.create_calls) == 1
    assert existing.terminate_count == 1
    assert session._inner._workspace_state_preserved_on_start() is False


@pytest.mark.asyncio
async def test_resume_recreates_directly_from_snapshot_when_reconnect_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Snapshot-mode resume must skip the throwaway empty-sandbox create when reconnect fails."""

    module = _load_tensorlake_module(monkeypatch)

    snapshot = LocalSnapshot(id="snap", base_path=tmp_path)
    payload = module._encode_tensorlake_snapshot_ref(snapshot_id="snap-stored")
    await snapshot.persist(io.BytesIO(payload))
    _FakeSandbox.snapshots["snap-stored"] = {"/workspace/from-snapshot.txt": b"snap-data"}

    existing = _FakeSandbox(sandbox_id="sandbox-paused", status="suspended")
    existing.resume_failure = RuntimeError("sandbox expired")
    _FakeSandbox.sandboxes["sandbox-paused"] = existing

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        manifest=Manifest(),
        snapshot=snapshot,
        sandbox_id="sandbox-paused",
        pause_on_exit=True,
        workspace_persistence="snapshot",
    )

    client = module.TensorlakeSandboxClient()
    session = await client.resume(state)

    assert len(_FakeSandbox.create_calls) == 1
    assert _FakeSandbox.create_calls[0].get("snapshot_id") == "snap-stored"
    assert _FakeSandbox.create_calls[0]["name"] == (
        "openai-agents-00000000000000000000000000000010"
    )
    new_id = session._inner.state.sandbox_id
    assert new_id != "sandbox-paused"
    new_sandbox = _FakeSandbox.sandboxes[new_id]
    assert new_sandbox.files["/workspace/from-snapshot.txt"] == b"snap-data"
    assert state.workspace_root_ready is True
    assert session._inner._workspace_state_preserved_on_start() is True
    assert session._inner._system_state_preserved_on_start() is True


def test_serialize_session_state_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_tensorlake_module(monkeypatch)

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-00000000000e"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-serialize",
        image="custom",
        cpus=4.0,
        memory_mb=4096,
        disk_mb=20480,
        timeout_secs=120,
        name="serialize",
        allow_internet_access=False,
        allow_out=("10.0.0.0/8",),
        deny_out=("example.com",),
        workspace_persistence="snapshot",
        checkpoint_mode="memory",
    )
    client = module.TensorlakeSandboxClient()
    payload = client.serialize_session_state(state)
    restored = client.deserialize_session_state(payload)

    assert isinstance(restored, module.TensorlakeSandboxSessionState)
    assert restored.image == "custom"
    assert restored.cpus == 4.0
    assert restored.disk_mb == 20480
    assert restored.workspace_persistence == "snapshot"
    assert restored.checkpoint_mode == "memory"


@pytest.mark.asyncio
async def test_restore_from_checkpoint_marks_system_state_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After _restore_from_checkpoint, system state must be flagged as preserved.

    When resume() cannot read a RemoteSnapshot without dependencies it creates a fresh
    empty sandbox and sets _start_system_state_preserved=False.  hydrate_workspace()
    later replaces that sandbox with a full Tensorlake checkpoint (which already contains
    OS users and groups).  The base start flow must not re-run groupadd/useradd against
    accounts that are already present in the restored image.
    """
    module = _load_tensorlake_module(monkeypatch)

    # Seed a snapshot so the checkpoint restore can find it.
    initial = _FakeSandbox(sandbox_id="sandbox-snap-src")
    initial.files["/workspace/data.txt"] = b"hello"
    snap = await initial.checkpoint(checkpoint_type=_FakeCheckpointType.FILESYSTEM)

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000030"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-before-restore",
        workspace_persistence="snapshot",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-before-restore")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    # Simulate the fresh-sandbox case: resume() could not read the snapshot and set
    # preserved=False before handing the session to start().
    session._set_start_state_preserved(False, system=False)

    payload = module._encode_tensorlake_snapshot_ref(snapshot_id=snap.snapshot_id)
    await session.hydrate_workspace(io.BytesIO(payload))

    assert session.should_provision_manifest_accounts_on_resume() is False


@pytest.mark.asyncio
async def test_restore_from_memory_checkpoint_strips_snapshot_owned_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory-checkpoint restore must omit fields the backend sources from the snapshot.

    Tensorlake docs (https://docs.tensorlake.ai/sandboxes/snapshots) say image, resources
    (CPUs, memory, disk), entrypoint, and secrets come from the memory snapshot and cannot
    be passed at restore time. Forwarding them from session state — which they will be set
    in for any non-default user config — would make `AsyncSandbox.create(snapshot_id=...)`
    reject the call.
    """
    module = _load_tensorlake_module(monkeypatch)

    initial = _FakeSandbox(sandbox_id="sandbox-snap-src")
    snap = await initial.checkpoint(checkpoint_type=_FakeCheckpointType.MEMORY)

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000061"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-before-restore",
        image="custom-image",
        cpus=4.0,
        memory_mb=4096,
        disk_mb=20480,
        secret_names=("OPENAI_API_KEY",),
        entrypoint=("/bin/bash",),
        allow_internet_access=False,
        timeout_secs=120,
        workspace_persistence="snapshot",
        checkpoint_mode="memory",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-before-restore")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    _FakeSandbox.create_calls.clear()
    payload = module._encode_tensorlake_snapshot_ref(snapshot_id=snap.snapshot_id)
    await session.hydrate_workspace(io.BytesIO(payload))

    assert len(_FakeSandbox.create_calls) == 1
    create_kwargs = _FakeSandbox.create_calls[0]
    assert create_kwargs.get("snapshot_id") == snap.snapshot_id
    for forbidden in ("image", "cpus", "memory_mb", "disk_mb", "entrypoint", "secret_names"):
        assert forbidden not in create_kwargs, (
            f"{forbidden} must be omitted for memory-checkpoint restore"
        )
    # Fields that the docs do not restrict on memory restore must still flow through.
    assert create_kwargs["allow_internet_access"] is False
    assert create_kwargs["timeout_secs"] == 120


@pytest.mark.asyncio
async def test_restore_from_filesystem_checkpoint_keeps_resource_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem-checkpoint restore must still forward image/resources/entrypoint/secrets.

    Per Tensorlake docs, the memory-snapshot restriction does not apply to filesystem
    snapshots — resources are explicitly modifiable on restore. The kwargs filter must
    only kick in for `checkpoint_mode="memory"`.
    """
    module = _load_tensorlake_module(monkeypatch)

    initial = _FakeSandbox(sandbox_id="sandbox-snap-src-fs")
    snap = await initial.checkpoint(checkpoint_type=_FakeCheckpointType.FILESYSTEM)

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000062"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-before-restore-fs",
        image="custom-image",
        cpus=4.0,
        memory_mb=4096,
        disk_mb=20480,
        secret_names=("OPENAI_API_KEY",),
        entrypoint=("/bin/bash",),
        workspace_persistence="snapshot",
        checkpoint_mode="filesystem",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-before-restore-fs")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    _FakeSandbox.create_calls.clear()
    payload = module._encode_tensorlake_snapshot_ref(snapshot_id=snap.snapshot_id)
    await session.hydrate_workspace(io.BytesIO(payload))

    assert len(_FakeSandbox.create_calls) == 1
    create_kwargs = _FakeSandbox.create_calls[0]
    assert create_kwargs.get("snapshot_id") == snap.snapshot_id
    assert create_kwargs["image"] == "custom-image"
    assert create_kwargs["cpus"] == 4.0
    assert create_kwargs["memory_mb"] == 4096
    assert create_kwargs["disk_mb"] == 20480
    assert create_kwargs["secret_names"] == ["OPENAI_API_KEY"]
    assert create_kwargs["entrypoint"] == ["/bin/bash"]


@pytest.mark.asyncio
async def test_resume_recreate_from_memory_checkpoint_strips_snapshot_owned_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The resume-recreate path must also strip memory-snapshot-owned kwargs.

    `client.resume(state)` falls back to `AsyncSandbox.create(snapshot_id=...)` when the
    paused sandbox cannot be reconnected. That second create site has to apply the same
    memory-snapshot filtering as `_restore_from_checkpoint`, otherwise any non-default user
    config breaks the recreate flow.
    """
    module = _load_tensorlake_module(monkeypatch)

    snapshot = LocalSnapshot(id="snap", base_path=tmp_path)
    payload = module._encode_tensorlake_snapshot_ref(snapshot_id="snap-stored-mem")
    await snapshot.persist(io.BytesIO(payload))
    _FakeSandbox.snapshots["snap-stored-mem"] = {}

    existing = _FakeSandbox(sandbox_id="sandbox-paused-mem", status="suspended")
    existing.resume_failure = RuntimeError("sandbox expired")
    _FakeSandbox.sandboxes["sandbox-paused-mem"] = existing

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000063"),
        manifest=Manifest(),
        snapshot=snapshot,
        sandbox_id="sandbox-paused-mem",
        image="custom-image",
        cpus=2.0,
        memory_mb=2048,
        secret_names=("API_KEY",),
        entrypoint=("/bin/sh",),
        pause_on_exit=True,
        workspace_persistence="snapshot",
        checkpoint_mode="memory",
    )

    _FakeSandbox.create_calls.clear()
    client = module.TensorlakeSandboxClient()
    await client.resume(state)

    assert len(_FakeSandbox.create_calls) == 1
    create_kwargs = _FakeSandbox.create_calls[0]
    assert create_kwargs.get("snapshot_id") == "snap-stored-mem"
    for forbidden in ("image", "cpus", "memory_mb", "disk_mb", "entrypoint", "secret_names"):
        assert forbidden not in create_kwargs


@pytest.mark.asyncio
async def test_after_start_reinstalls_helpers_when_sandbox_id_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_after_start must reinstall runtime helpers when sandbox_id changed mid-start.

    checkpoint restore replaces the sandbox and sandbox_id during start(); the helper
    cache key becomes stale. _after_start() detects the mismatch and re-runs
    _ensure_runtime_helpers() so the new backend has the helpers installed.
    """
    module = _load_tensorlake_module(monkeypatch)

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000031"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-new",
        workspace_persistence="tar",
    )
    fake = _FakeSandbox(sandbox_id="sandbox-new")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    # Simulate helpers installed on the pre-restore sandbox: cache key is stale.
    session._runtime_helper_cache_key = "sandbox-old"
    session._runtime_helpers_installed = set()

    await session._after_start()

    assert session._runtime_helper_cache_key == "sandbox-new"
    assert any(c["command"] == "sh" for c in fake.run_calls)


@pytest.mark.asyncio
async def test_running_returns_false_when_workspace_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """running() must return False when the workspace has not been set up yet.

    A Tensorlake sandbox can be in RUNNING state (backend alive) while the workspace
    hasn't been provisioned. Callers must not treat such a session as usable.
    """
    module = _load_tensorlake_module(monkeypatch)

    state = module.TensorlakeSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-0000-0000-000000000032"),
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap"),
        sandbox_id="sandbox-not-ready",
        workspace_persistence="tar",
    )
    # Backend is running but workspace_root_ready is False (before start()).
    fake = _FakeSandbox(sandbox_id="sandbox-not-ready", status="running")
    session = module.TensorlakeSandboxSession.from_state(state, sandbox=fake)

    assert await session.running() is False

    state.workspace_root_ready = True
    assert await session.running() is True
