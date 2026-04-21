from __future__ import annotations

import builtins
import importlib
import io
import sys
import tarfile
import types
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
from pydantic import BaseModel, PrivateAttr

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.entries import File, InContainerMountStrategy, Mount, MountpointMountPattern
from agents.sandbox.entries.mounts.base import InContainerMountAdapter
from agents.sandbox.errors import ConfigurationError, InvalidManifestPathError
from agents.sandbox.manifest import Environment
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.dependencies import Dependencies
from agents.sandbox.snapshot import NoopSnapshot, SnapshotBase
from agents.sandbox.types import User
from tests._fake_workspace_paths import resolve_fake_workspace_path


class _FakeNetworkPolicyRule(BaseModel):
    pass


class _FakeNetworkPolicySubnets(BaseModel):
    allow: list[str] | None = None
    deny: list[str] | None = None


class _FakeNetworkPolicyCustom(BaseModel):
    allow: dict[str, list[_FakeNetworkPolicyRule]] | list[str] | None = None
    subnets: _FakeNetworkPolicySubnets | None = None


NetworkPolicy = _FakeNetworkPolicyCustom
NetworkPolicyCustom = _FakeNetworkPolicyCustom
NetworkPolicyRule = _FakeNetworkPolicyRule
NetworkPolicySubnets = _FakeNetworkPolicySubnets


class Resources(BaseModel):
    memory: int | None = None


class SnapshotSource(BaseModel):
    type: Literal["snapshot"] = "snapshot"
    snapshot_id: str


class _MemorySnapshot(SnapshotBase):
    type: Literal["test-vercel-memory"] = "test-vercel-memory"
    payload: bytes = b""
    is_restorable: bool = False

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = dependencies
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        assert isinstance(raw, bytes | bytearray)
        object.__setattr__(self, "payload", bytes(raw))
        object.__setattr__(self, "is_restorable", True)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        return io.BytesIO(self.payload)

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return self.is_restorable


class _FakeCommandFinished:
    def __init__(self, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.exit_code = exit_code

    async def stdout(self) -> str:
        return self._stdout

    async def stderr(self) -> str:
        return self._stderr


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeAsyncSnapshot:
    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id


class _FakeAsyncSandbox:
    create_calls: list[dict[str, object]] = []
    get_calls: list[dict[str, object]] = []
    snapshot_counter = 0
    sandboxes: dict[str, _FakeAsyncSandbox] = {}
    snapshots: dict[str, dict[str, bytes]] = {}
    fail_get_ids: set[str] = set()
    create_failures: list[BaseException] = []

    def __init__(
        self,
        *,
        sandbox_id: str,
        status: str = "running",
        routes: list[dict[str, object]] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.status = status
        self.routes = routes or [{"port": 3000, "url": "https://3000-sandbox.vercel.run"}]
        self.files = dict(files or {})
        self.client = _FakeClient()
        self.next_command_result = _FakeCommandFinished()
        self.run_command_calls: list[tuple[str, list[str], str | None]] = []
        self.refresh_calls = 0
        self.read_file_calls: list[tuple[str, str | None]] = []
        self.stop_calls = 0
        self.wait_for_status_calls: list[tuple[object, float | None]] = []
        self.wait_for_status_error: BaseException | None = None
        self.write_failures: list[BaseException] = []
        self.write_files_calls: list[list[dict[str, object]]] = []
        self.tar_create_result: _FakeCommandFinished | None = None
        self.tar_extract_result: _FakeCommandFinished | None = None
        self.symlinks: dict[str, str] = {}

    @classmethod
    def reset(cls) -> None:
        cls.create_calls = []
        cls.get_calls = []
        cls.snapshot_counter = 0
        cls.sandboxes = {}
        cls.snapshots = {}
        cls.fail_get_ids = set()
        cls.create_failures = []

    @classmethod
    async def create(cls, **kwargs: object) -> _FakeAsyncSandbox:
        cls.create_calls.append(dict(kwargs))
        if cls.create_failures:
            raise cls.create_failures.pop(0)
        source = kwargs.get("source")
        sandbox_id = f"vercel-sandbox-{len(cls.create_calls)}"
        files: dict[str, bytes] = {}
        snapshot_id = getattr(source, "snapshot_id", None)
        if getattr(source, "type", None) == "snapshot" and isinstance(snapshot_id, str):
            files = dict(cls.snapshots.get(snapshot_id, {}))
        ports = cast(list[int] | None, kwargs.get("ports"))
        sandbox = cls(
            sandbox_id=sandbox_id,
            routes=[
                {"port": port, "url": f"https://{port}-sandbox.vercel.run"}
                for port in (ports or [3000])
            ],
            files=files,
        )
        cls.sandboxes[sandbox_id] = sandbox
        return sandbox

    @classmethod
    async def get(cls, **kwargs: object) -> _FakeAsyncSandbox:
        cls.get_calls.append(dict(kwargs))
        sandbox_id = kwargs["sandbox_id"]
        assert isinstance(sandbox_id, str)
        if sandbox_id in cls.fail_get_ids:
            raise RuntimeError("sandbox missing")
        sandbox = cls.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise RuntimeError("sandbox missing")
        return sandbox

    async def refresh(self) -> None:
        self.refresh_calls += 1

    async def wait_for_status(self, status: object, timeout: float | None = None) -> None:
        self.wait_for_status_calls.append((status, timeout))
        if self.wait_for_status_error is not None:
            raise self.wait_for_status_error
        self.status = str(status)

    def domain(self, port: int) -> str:
        for route in self.routes:
            if route.get("port") == port:
                return str(route["url"])
        raise ValueError("missing route")

    async def run_command(
        self,
        cmd: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sudo: bool = False,
    ) -> _FakeCommandFinished:
        _ = (env, sudo)
        args = args or []
        self.run_command_calls.append((cmd, list(args), cwd))
        resolved = resolve_fake_workspace_path(
            (cmd, *args),
            symlinks=self.symlinks,
            home_dir="/workspace",
        )
        if resolved is not None:
            return _FakeCommandFinished(
                exit_code=resolved.exit_code,
                stdout=resolved.stdout,
                stderr=resolved.stderr,
            )
        if cmd == "tar" and len(args) >= 3 and args[0] == "cf":
            if self.tar_create_result is not None:
                return self.tar_create_result
            archive_path = args[1]
            assert cwd is not None
            include_root = args[-1] == "."
            exclusions = {
                argument.removeprefix("--exclude=./")
                for argument in args[2:-1]
                if argument.startswith("--exclude=./")
            }
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for path, content in sorted(self.files.items()):
                    if not path.startswith(cwd.rstrip("/") + "/"):
                        continue
                    rel_path = path[len(cwd.rstrip("/")) + 1 :]
                    if any(
                        rel_path == exclusion or rel_path.startswith(f"{exclusion}/")
                        for exclusion in exclusions
                    ):
                        continue
                    info = tarfile.TarInfo(name=rel_path if include_root else path)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            self.files[archive_path] = buffer.getvalue()
            return _FakeCommandFinished()
        if cmd == "tar" and len(args) >= 4 and args[0] == "xf":
            if self.tar_extract_result is not None:
                return self.tar_extract_result
            archive_path = args[1]
            destination = args[3]
            raw = self.files[archive_path]
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    extracted = archive.extractfile(member)
                    assert extracted is not None
                    self.files[f"{destination.rstrip('/')}/{member.name}"] = extracted.read()
            return _FakeCommandFinished()
        if cmd == "rm" and args:
            target = args[-1]
            self.files.pop(target, None)
            return _FakeCommandFinished()
        return self.next_command_result

    async def read_file(self, path: str, *, cwd: str | None = None) -> bytes | None:
        self.read_file_calls.append((path, cwd))
        resolved = path if path.startswith("/") or cwd is None else f"{cwd.rstrip('/')}/{path}"
        return self.files.get(resolved)

    async def write_files(self, files: list[dict[str, object]]) -> None:
        self.write_files_calls.append(files)
        if self.write_failures:
            raise self.write_failures.pop(0)
        for file in files:
            self.files[str(file["path"])] = bytes(cast(bytes, file["content"]))

    async def stop(
        self, *, blocking: bool = False, timeout: float = 30.0, poll_interval: float = 0.5
    ) -> None:
        _ = (blocking, timeout, poll_interval)
        self.stop_calls += 1
        self.status = "stopped"

    async def snapshot(self, *, expiration: int | None = None) -> _FakeAsyncSnapshot:
        _ = expiration
        type(self).snapshot_counter += 1
        snapshot_id = f"vercel-snapshot-{type(self).snapshot_counter}"
        type(self).snapshots[snapshot_id] = dict(self.files)
        self.status = "stopped"
        return _FakeAsyncSnapshot(snapshot_id)


class _RecordingMount(Mount):
    type: str = "test_vercel_recording_mount"
    bucket: str = "bucket"
    _events: list[tuple[str, str]] = PrivateAttr(default_factory=list)

    def supported_in_container_patterns(
        self,
    ) -> tuple[builtins.type[MountpointMountPattern], ...]:
        return (MountpointMountPattern,)

    def in_container_adapter(self) -> InContainerMountAdapter:
        mount = self

        class _Adapter(InContainerMountAdapter):
            def validate(self, strategy: InContainerMountStrategy) -> None:
                super().validate(strategy)

            async def activate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> list[MaterializedFile]:
                _ = (strategy, session, dest, base_dir)
                return []

            async def deactivate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> None:
                _ = (strategy, session, dest, base_dir)

            async def teardown_for_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = strategy
                mount._events.append(("unmount", path.as_posix()))
                sandbox = cast(Any, session)._sandbox
                if sandbox is not None:
                    sandbox.files.pop(f"{path.as_posix()}/mounted.txt", None)

            async def restore_after_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = strategy
                mount._events.append(("mount", path.as_posix()))
                sandbox = cast(Any, session)._sandbox
                if sandbox is not None:
                    sandbox.files[f"{path.as_posix()}/mounted.txt"] = b"mounted-content"

        return _Adapter(self)


def _load_vercel_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    _FakeAsyncSandbox.reset()

    fake_vercel = types.ModuleType("vercel")
    fake_vercel_sandbox = cast(Any, types.ModuleType("vercel.sandbox"))
    fake_vercel_sandbox.AsyncSandbox = _FakeAsyncSandbox
    fake_vercel_sandbox.NetworkPolicy = NetworkPolicy
    fake_vercel_sandbox.NetworkPolicyCustom = NetworkPolicyCustom
    fake_vercel_sandbox.NetworkPolicyRule = NetworkPolicyRule
    fake_vercel_sandbox.NetworkPolicySubnets = NetworkPolicySubnets
    fake_vercel_sandbox.Resources = Resources
    fake_vercel_sandbox.SandboxStatus = types.SimpleNamespace(RUNNING="running")
    fake_vercel_sandbox.SnapshotSource = SnapshotSource

    monkeypatch.setitem(sys.modules, "vercel", fake_vercel)
    monkeypatch.setitem(sys.modules, "vercel.sandbox", fake_vercel_sandbox)
    sys.modules.pop("agents.extensions.sandbox.vercel.sandbox", None)
    sys.modules.pop("agents.extensions.sandbox.vercel", None)

    return importlib.import_module("agents.extensions.sandbox.vercel.sandbox")


async def _noop_sleep(*_args: object, **_kwargs: object) -> None:
    return None


def test_vercel_package_re_exports_backend_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")

    assert package_module.VercelSandboxClient is vercel_module.VercelSandboxClient
    assert package_module.VercelSandboxSessionState is vercel_module.VercelSandboxSessionState


def test_vercel_supports_pty_is_disabled_until_provider_methods_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)

    noninteractive = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000000",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-noninteractive",
        interactive=False,
    )
    interactive = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000001",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-interactive",
        interactive=True,
    )

    assert not vercel_module.VercelSandboxSession.from_state(noninteractive).supports_pty()
    assert not vercel_module.VercelSandboxSession.from_state(interactive).supports_pty()


@pytest.mark.asyncio
async def test_vercel_create_passes_provider_options(monkeypatch: pytest.MonkeyPatch) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    network_policy = NetworkPolicyCustom(
        allow={
            "api.openai.com": [NetworkPolicyRule()],
        },
        subnets=NetworkPolicySubnets(allow=["10.0.0.0/8"]),
    )

    client = vercel_module.VercelSandboxClient(token="token")
    session = await client.create(
        manifest=Manifest(
            environment=Environment(value={"FLAG": "manifest", "FROM_MANIFEST": "1"})
        ),
        options=vercel_module.VercelSandboxClientOptions(
            project_id="project",
            team_id="team",
            timeout_ms=12_000,
            runtime="node22",
            resources={"memory": 1024},
            env={"FLAG": "options", "HELLO": "world"},
            exposed_ports=(3000, 4000),
            interactive=True,
            network_policy=network_policy,
        ),
    )

    assert _FakeAsyncSandbox.create_calls == [
        {
            "source": None,
            "ports": [3000, 4000],
            "timeout": 12_000,
            "resources": Resources(memory=1024),
            "runtime": "node22",
            "token": "token",
            "project_id": "project",
            "team_id": "team",
            "interactive": True,
            "env": {"FLAG": "manifest", "HELLO": "world", "FROM_MANIFEST": "1"},
            "network_policy": network_policy,
        }
    ]
    assert _FakeAsyncSandbox.sandboxes["vercel-sandbox-1"].wait_for_status_calls == [
        ("running", vercel_module.DEFAULT_VERCEL_WAIT_FOR_RUNNING_TIMEOUT_S)
    ]
    assert session._inner.state.sandbox_id == "vercel-sandbox-1"
    assert session._inner.state.manifest.root == vercel_module.DEFAULT_VERCEL_WORKSPACE_ROOT


@pytest.mark.asyncio
async def test_vercel_create_retries_transient_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    monkeypatch.setattr("agents.sandbox.util.retry.asyncio.sleep", _noop_sleep)
    _FakeAsyncSandbox.create_failures = [httpx.ReadError("read failed")]

    client = vercel_module.VercelSandboxClient(token="token")
    session = await client.create(
        manifest=Manifest(),
        options=vercel_module.VercelSandboxClientOptions(),
    )

    assert len(_FakeAsyncSandbox.create_calls) == 2
    assert _FakeAsyncSandbox.sandboxes[session._inner.state.sandbox_id].wait_for_status_calls == [
        ("running", vercel_module.DEFAULT_VERCEL_WAIT_FOR_RUNNING_TIMEOUT_S)
    ]


@pytest.mark.asyncio
async def test_vercel_create_does_not_retry_non_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    monkeypatch.setattr("agents.sandbox.util.retry.asyncio.sleep", _noop_sleep)

    class _BadRequestError(Exception):
        status_code = 400

    _FakeAsyncSandbox.create_failures = [_BadRequestError("bad request")]

    client = vercel_module.VercelSandboxClient()
    with pytest.raises(_BadRequestError):
        await client.create(
            manifest=Manifest(),
            options=vercel_module.VercelSandboxClientOptions(),
        )

    assert len(_FakeAsyncSandbox.create_calls) == 1


@pytest.mark.asyncio
async def test_vercel_exec_read_write_and_port_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    vercel_module = _load_vercel_module(monkeypatch)

    snapshot = NoopSnapshot(id="snapshot")
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000001",
        manifest=Manifest(),
        snapshot=snapshot,
        sandbox_id="sandbox-existing",
        exposed_ports=(3000,),
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-existing")
    sandbox.next_command_result = _FakeCommandFinished(stdout="hello\n", stderr="", exit_code=0)
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    await session.write(Path("notes.txt"), io.BytesIO(b"payload"))
    result = await session.exec("printf", "hello", shell=False)
    endpoint = await session.resolve_exposed_port(3000)
    payload = await session.read(Path("notes.txt"))

    assert result.ok()
    assert result.stdout == b"hello\n"
    assert endpoint == vercel_module.ExposedPortEndpoint(
        host="3000-sandbox.vercel.run",
        port=443,
        tls=True,
    )
    assert payload.read() == b"payload"


@pytest.mark.asyncio
async def test_vercel_start_uses_base_session_contract_and_materializes_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000012",
        manifest=Manifest(entries={"notes.txt": File(content=b"payload")}),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-start",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-start")
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    await session.start()
    payload = await session.read(Path("notes.txt"))

    assert sandbox.run_command_calls[0] == ("mkdir", ["-p", "--", "/workspace"], None)
    assert ("mkdir", ["-p", "/workspace"], "/workspace") in sandbox.run_command_calls
    assert session.state.workspace_root_ready is True
    assert payload.read() == b"payload"


@pytest.mark.asyncio
async def test_vercel_start_materializes_entries_under_literal_manifest_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000013",
        manifest=Manifest(
            root="/workspace/my app", entries={"notes.txt": File(content=b"payload")}
        ),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-start-literal",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-start-literal")
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    await session.start()
    payload = await session.read(Path("notes.txt"))

    assert sandbox.run_command_calls[0] == ("mkdir", ["-p", "--", "/workspace/my app"], None)
    assert ("mkdir", ["-p", "/workspace/my app"], "/workspace/my app") in sandbox.run_command_calls
    assert sandbox.write_files_calls == [
        [{"path": "/workspace/my app/notes.txt", "content": b"payload"}]
    ]
    assert payload.read() == b"payload"


@pytest.mark.asyncio
async def test_vercel_start_bootstraps_arbitrary_absolute_root_before_using_it_as_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000014",
        manifest=Manifest(root="/tmp/outside", entries={"notes.txt": File(content=b"payload")}),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-start-outside",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-start-outside")
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    await session.start()
    payload = await session.read(Path("notes.txt"))

    assert sandbox.run_command_calls[0] == ("mkdir", ["-p", "--", "/tmp/outside"], None)
    assert ("mkdir", ["-p", "/tmp/outside"], "/tmp/outside") in sandbox.run_command_calls
    assert payload.read() == b"payload"


@pytest.mark.asyncio
async def test_vercel_create_allows_manifest_root_outside_provider_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    client = vercel_module.VercelSandboxClient()

    session = await client.create(
        manifest=Manifest(root="/tmp/outside"),
        options=vercel_module.VercelSandboxClientOptions(),
    )

    assert session._inner.state.manifest.root == "/tmp/outside"


@pytest.mark.asyncio
async def test_vercel_create_allows_manifest_root_within_provider_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    client = vercel_module.VercelSandboxClient()

    session = await client.create(
        manifest=Manifest(root="/vercel/sandbox/my app"),
        options=vercel_module.VercelSandboxClientOptions(),
    )

    assert session._inner.state.manifest.root == "/vercel/sandbox/my app"


@pytest.mark.asyncio
async def test_vercel_normalize_path_rejects_workspace_escape_and_allows_absolute_in_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    client = vercel_module.VercelSandboxClient()

    session = await client.create(
        manifest=Manifest(root="/vercel/sandbox/project"),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    inner = session._inner

    with pytest.raises(InvalidManifestPathError):
        inner.normalize_path("../outside.txt")
    with pytest.raises(InvalidManifestPathError):
        inner.normalize_path("/etc/passwd")

    assert inner.normalize_path("/vercel/sandbox/project/nested/file.txt") == Path(
        "/vercel/sandbox/project/nested/file.txt"
    )


@pytest.mark.asyncio
async def test_vercel_read_and_write_reject_paths_outside_workspace_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    client = vercel_module.VercelSandboxClient()

    session = await client.create(
        manifest=Manifest(root="/vercel/sandbox/project"),
        options=vercel_module.VercelSandboxClientOptions(),
    )

    with pytest.raises(InvalidManifestPathError):
        await session.read("../outside.txt")
    with pytest.raises(InvalidManifestPathError):
        await session.write("/etc/passwd", io.BytesIO(b"nope"))


@pytest.mark.asyncio
async def test_vercel_read_rejects_workspace_symlink_to_ungranted_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000016",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-read-escape-link",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-read-escape-link")
    sandbox.symlinks["/workspace/link"] = "/private"
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(InvalidManifestPathError) as exc_info:
        await session.read("link/secret.txt")

    assert sandbox.read_file_calls == []
    assert str(exc_info.value) == "manifest path must not escape root: link/secret.txt"
    assert exc_info.value.context == {
        "rel": "link/secret.txt",
        "reason": "escape_root",
        "resolved_path": "workspace escape: /private/secret.txt",
    }


@pytest.mark.asyncio
async def test_vercel_write_rejects_workspace_symlink_to_read_only_extra_path_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000015",
        manifest=Manifest(
            root="/workspace",
            extra_path_grants=(SandboxPathGrant(path="/tmp/protected", read_only=True),),
        ),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-readonly-link",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-readonly-link")
    sandbox.symlinks["/workspace/link"] = "/tmp/protected"
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(vercel_module.WorkspaceArchiveWriteError) as exc_info:
        await session.write("link/out.txt", io.BytesIO(b"blocked"))

    assert sandbox.write_files_calls == []
    assert str(exc_info.value) == "failed to write archive for path: /workspace/link/out.txt"
    assert exc_info.value.context == {
        "path": "/workspace/link/out.txt",
        "reason": "read_only_extra_path_grant",
        "grant_path": "/tmp/protected",
        "resolved_path": "/tmp/protected/out.txt",
    }


@pytest.mark.asyncio
async def test_vercel_rejects_sandbox_local_user_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    client = vercel_module.VercelSandboxClient()

    session = await client.create(
        manifest=Manifest(root="/vercel/sandbox/project"),
        options=vercel_module.VercelSandboxClientOptions(),
    )

    with pytest.raises(ConfigurationError, match="does not support sandbox-local users"):
        await session.exec("pwd", user="sandbox-user")
    with pytest.raises(ConfigurationError, match="does not support sandbox-local users"):
        await session.read("notes.txt", user=User(name="sandbox-user"))
    with pytest.raises(ConfigurationError, match="does not support sandbox-local users"):
        await session.write("notes.txt", io.BytesIO(b"payload"), user="sandbox-user")


@pytest.mark.asyncio
async def test_vercel_resume_reconnects_existing_running_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sandbox-existing")
    _FakeAsyncSandbox.sandboxes[existing.sandbox_id] = existing

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000002",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.sandbox_id,
    )

    client = vercel_module.VercelSandboxClient()
    resumed = await client.resume(state)

    assert _FakeAsyncSandbox.get_calls == [
        {
            "sandbox_id": "sandbox-existing",
            "token": None,
            "project_id": None,
            "team_id": None,
        }
    ]
    assert resumed._inner.state.sandbox_id == "sandbox-existing"
    assert _FakeAsyncSandbox.create_calls == []
    assert existing.wait_for_status_calls == [
        ("running", vercel_module.DEFAULT_VERCEL_WAIT_FOR_RUNNING_TIMEOUT_S)
    ]
    assert resumed._inner._workspace_state_preserved_on_start() is True  # noqa: SLF001
    assert resumed._inner._system_state_preserved_on_start() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_vercel_resume_falls_back_to_recreate_when_sandbox_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    _FakeAsyncSandbox.fail_get_ids.add("sandbox-missing")

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000003",
        manifest=Manifest(environment=Environment(value={"FLAG": "manifest"})),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-missing",
        timeout_ms=90_000,
        runtime="python3.14",
        env={"FLAG": "options", "BASE": "1"},
        exposed_ports=(3000,),
    )

    client = vercel_module.VercelSandboxClient(token="token")
    resumed = await client.resume(state)

    assert resumed._inner.state.sandbox_id == "vercel-sandbox-1"
    assert resumed._inner.state.workspace_root_ready is False
    assert _FakeAsyncSandbox.create_calls[0]["runtime"] == "python3.14"
    assert _FakeAsyncSandbox.create_calls[0]["timeout"] == 90_000
    assert _FakeAsyncSandbox.create_calls[0]["token"] == "token"
    assert _FakeAsyncSandbox.create_calls[0]["env"] == {"FLAG": "manifest", "BASE": "1"}
    assert resumed._inner._workspace_state_preserved_on_start() is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_vercel_resume_recreates_sandbox_after_wait_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sandbox-existing")
    existing.wait_for_status_error = TimeoutError()
    _FakeAsyncSandbox.sandboxes[existing.sandbox_id] = existing

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000101",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.sandbox_id,
    )

    client = vercel_module.VercelSandboxClient()
    resumed = await client.resume(state)

    assert existing.client.closed is True
    assert resumed._inner.state.sandbox_id == "vercel-sandbox-1"
    assert len(_FakeAsyncSandbox.create_calls) == 1
    assert resumed._inner.state.workspace_root_ready is False
    assert resumed._inner._workspace_state_preserved_on_start() is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_vercel_create_does_not_read_token_or_scope_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERCEL_TOKEN", "env-token")
    monkeypatch.setenv("VERCEL_PROJECT_ID", "env-project")
    monkeypatch.setenv("VERCEL_TEAM_ID", "env-team")
    vercel_module = _load_vercel_module(monkeypatch)

    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=Manifest(),
        options=vercel_module.VercelSandboxClientOptions(),
    )

    assert _FakeAsyncSandbox.create_calls[-1]["token"] is None
    assert _FakeAsyncSandbox.create_calls[-1]["project_id"] is None
    assert _FakeAsyncSandbox.create_calls[-1]["team_id"] is None
    assert session._inner.state.project_id is None
    assert session._inner.state.team_id is None


@pytest.mark.asyncio
async def test_vercel_resume_uses_client_project_and_team_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sandbox-existing")
    _FakeAsyncSandbox.sandboxes[existing.sandbox_id] = existing

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000099",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.sandbox_id,
    )

    client = vercel_module.VercelSandboxClient(project_id="client-project", team_id="client-team")
    resumed = await client.resume(state)

    assert _FakeAsyncSandbox.get_calls[-1]["project_id"] == "client-project"
    assert _FakeAsyncSandbox.get_calls[-1]["team_id"] == "client-team"
    assert resumed._inner.state.project_id == "client-project"
    assert resumed._inner.state.team_id == "client-team"


@pytest.mark.asyncio
async def test_vercel_resume_does_not_read_token_or_scope_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERCEL_TOKEN", "env-token")
    monkeypatch.setenv("VERCEL_PROJECT_ID", "env-project")
    monkeypatch.setenv("VERCEL_TEAM_ID", "env-team")
    vercel_module = _load_vercel_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sandbox-existing")
    _FakeAsyncSandbox.sandboxes[existing.sandbox_id] = existing

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000100",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.sandbox_id,
    )

    client = vercel_module.VercelSandboxClient()
    resumed = await client.resume(state)

    assert _FakeAsyncSandbox.get_calls[-1]["token"] is None
    assert _FakeAsyncSandbox.get_calls[-1]["project_id"] is None
    assert _FakeAsyncSandbox.get_calls[-1]["team_id"] is None
    assert resumed._inner.state.project_id is None
    assert resumed._inner.state.team_id is None


@pytest.mark.asyncio
async def test_vercel_serialized_session_state_omits_token_and_resume_uses_live_client_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    network_policy = NetworkPolicyCustom(
        allow=["example.com"],
        subnets=NetworkPolicySubnets(deny=["192.168.0.0/16"]),
    )

    client = vercel_module.VercelSandboxClient(token="token-from-client")
    session = await client.create(
        manifest=Manifest(),
        options=vercel_module.VercelSandboxClientOptions(
            project_id="project",
            network_policy=network_policy,
        ),
    )

    payload = client.serialize_session_state(session.state)
    restored = client.deserialize_session_state(payload)
    resumed = await client.resume(restored)

    assert "token" not in payload
    assert restored.project_id == "project"
    assert payload["network_policy"] == {
        "allow": ["example.com"],
        "subnets": {"allow": None, "deny": ["192.168.0.0/16"]},
    }
    assert restored.network_policy == network_policy
    assert _FakeAsyncSandbox.get_calls[-1]["token"] == "token-from-client"
    assert resumed._inner.state.sandbox_id == session._inner.state.sandbox_id


@pytest.mark.asyncio
async def test_vercel_tar_persistence_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    snapshot = _MemorySnapshot(id="snapshot")

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000004",
        manifest=Manifest(),
        snapshot=snapshot,
        sandbox_id="sandbox-tar",
        workspace_persistence="tar",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-tar")
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    await session.write(Path("hello.txt"), io.BytesIO(b"world"))
    await session.stop()

    restored_state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000005",
        manifest=Manifest(),
        snapshot=snapshot,
        sandbox_id="sandbox-restored",
        workspace_persistence="tar",
    )
    restored = vercel_module.VercelSandboxSession.from_state(
        restored_state,
        sandbox=_FakeAsyncSandbox(sandbox_id="sandbox-restored"),
    )
    await restored.hydrate_workspace(await snapshot.restore())
    payload = await restored.read(Path("hello.txt"))

    assert payload.read() == b"world"


@pytest.mark.asyncio
async def test_vercel_tar_persist_raises_archive_error_on_nonzero_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000105",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-tar-fail",
        workspace_persistence="tar",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-tar-fail")
    sandbox.tar_create_result = _FakeCommandFinished(stderr="tar failed", exit_code=2)
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(vercel_module.WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert isinstance(exc_info.value.__cause__, vercel_module.ExecNonZeroError)
    assert exc_info.value.__cause__.exit_code == 2
    assert sandbox.run_command_calls[-1] == (
        "rm",
        ["/tmp/openai-agents-00000000000000000000000000000105.tar"],
        "/workspace",
    )


def test_vercel_validate_tar_bytes_rejects_unsafe_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000103",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-tar-validate",
    )
    session = vercel_module.VercelSandboxSession.from_state(state)

    absolute_buf = io.BytesIO()
    with tarfile.open(fileobj=absolute_buf, mode="w") as archive:
        info = tarfile.TarInfo(name="/etc/passwd")
        info.size = 4
        archive.addfile(info, io.BytesIO(b"root"))
    with pytest.raises(ValueError, match="absolute path"):
        session._validate_tar_bytes(absolute_buf.getvalue())

    with pytest.raises(ValueError, match="invalid tar stream"):
        session._validate_tar_bytes(b"not a tar file")


@pytest.mark.asyncio
async def test_vercel_hydrate_workspace_rejects_unsafe_tar_before_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000104",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-hydrate-unsafe",
        workspace_persistence="tar",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-hydrate-unsafe")
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    unsafe_buf = io.BytesIO()
    with tarfile.open(fileobj=unsafe_buf, mode="w") as archive:
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = 4
        archive.addfile(info, io.BytesIO(b"data"))

    with pytest.raises(vercel_module.WorkspaceArchiveWriteError) as exc_info:
        await session.hydrate_workspace(io.BytesIO(unsafe_buf.getvalue()))

    assert "parent traversal" in str(exc_info.value.__cause__)
    assert sandbox.write_files_calls == []
    assert not any(
        call for call in sandbox.run_command_calls if call[0] == "tar" and call[1][0] == "xf"
    )


@pytest.mark.asyncio
async def test_vercel_hydrate_workspace_raises_archive_error_on_nonzero_tar_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000106",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-hydrate-fail",
        workspace_persistence="tar",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-hydrate-fail")
    sandbox.tar_extract_result = _FakeCommandFinished(stderr="extract failed", exit_code=2)
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo(name="hello.txt")
        info.size = 5
        tar.addfile(info, io.BytesIO(b"hello"))

    with pytest.raises(vercel_module.WorkspaceArchiveWriteError) as exc_info:
        await session.hydrate_workspace(io.BytesIO(archive.getvalue()))

    assert isinstance(exc_info.value.__cause__, vercel_module.ExecNonZeroError)
    assert exc_info.value.__cause__.exit_code == 2
    assert sandbox.run_command_calls[-1] == (
        "rm",
        ["/tmp/openai-agents-00000000000000000000000000000106.tar"],
        "/workspace",
    )


@pytest.mark.asyncio
async def test_vercel_write_retries_transient_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    monkeypatch.setattr("agents.sandbox.util.retry.asyncio.sleep", _noop_sleep)

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000102",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-write-retry",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-write-retry")
    sandbox.write_failures = [httpx.ProtocolError("transient write failure")]
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    await session.write(Path("notes.txt"), io.BytesIO(b"payload"))
    payload = await session.read(Path("notes.txt"))

    assert payload.read() == b"payload"
    assert len(sandbox.write_files_calls) == 2


@pytest.mark.asyncio
async def test_vercel_snapshot_mode_resume_uses_native_snapshot_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    snapshot = _MemorySnapshot(id="snapshot")

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000006",
        manifest=Manifest(),
        snapshot=snapshot,
        sandbox_id="sandbox-snapshot",
        workspace_persistence="snapshot",
        snapshot_expiration_ms=60_000,
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-snapshot")
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    await session.write(Path("config.json"), io.BytesIO(b'{"version":1}'))
    await session.stop()

    resumed_state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000007",
        manifest=Manifest(),
        snapshot=snapshot,
        sandbox_id="sandbox-snapshot",
        workspace_persistence="snapshot",
        snapshot_expiration_ms=60_000,
    )
    client = vercel_module.VercelSandboxClient()
    resumed = await client.resume(resumed_state)
    payload = await resumed._inner.read(Path("config.json"))

    assert _FakeAsyncSandbox.create_calls[-1]["source"] == SnapshotSource(
        snapshot_id="vercel-snapshot-1"
    )
    assert resumed._inner.state.sandbox_id == "vercel-sandbox-1"
    assert payload.read() == b'{"version":1}'


@pytest.mark.asyncio
async def test_vercel_tar_persistence_tears_down_ephemeral_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    snapshot = _MemorySnapshot(id="snapshot")
    mount = _RecordingMount(
        mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern())
    )
    sandbox = _FakeAsyncSandbox(
        sandbox_id="sandbox-mount-tar",
        files={
            "/workspace/kept.txt": b"kept",
            "/workspace/remote/mounted.txt": b"mounted-content",
        },
    )
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000008",
        manifest=Manifest(root="/workspace", entries={"remote": mount}),
        snapshot=snapshot,
        sandbox_id=sandbox.sandbox_id,
        workspace_persistence="tar",
    )
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    await session.stop()

    with tarfile.open(fileobj=io.BytesIO(snapshot.payload), mode="r") as archive:
        archived_names = sorted(member.name for member in archive.getmembers())
    tar_calls = [
        call for call in sandbox.run_command_calls if call[0] == "tar" and call[1][0] == "cf"
    ]

    assert mount._events == [("unmount", "/workspace/remote"), ("mount", "/workspace/remote")]
    assert tar_calls == [
        (
            "tar",
            [
                "cf",
                "/tmp/openai-agents-00000000000000000000000000000008.tar",
                "--exclude=./remote",
                ".",
            ],
            "/workspace",
        )
    ]
    assert archived_names == ["kept.txt"]
    assert sandbox.files["/workspace/remote/mounted.txt"] == b"mounted-content"


@pytest.mark.asyncio
async def test_vercel_snapshot_persistence_tears_down_ephemeral_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    snapshot = _MemorySnapshot(id="snapshot")
    mount = _RecordingMount(
        mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern())
    )
    sandbox = _FakeAsyncSandbox(
        sandbox_id="sandbox-mount-snapshot",
        files={
            "/workspace/kept.txt": b"kept",
            "/workspace/remote/mounted.txt": b"mounted-content",
        },
    )
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000009",
        manifest=Manifest(root="/workspace", entries={"remote": mount}),
        snapshot=snapshot,
        sandbox_id=sandbox.sandbox_id,
        workspace_persistence="snapshot",
        snapshot_expiration_ms=60_000,
    )
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    await session.stop()

    restored_state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000010",
        manifest=Manifest(root="/workspace", entries={"remote": mount}),
        snapshot=snapshot,
        sandbox_id="sandbox-mount-snapshot",
        workspace_persistence="snapshot",
        snapshot_expiration_ms=60_000,
    )
    client = vercel_module.VercelSandboxClient()
    resumed = await client.resume(restored_state)

    assert mount._events == [("unmount", "/workspace/remote"), ("mount", "/workspace/remote")]
    assert "/workspace/remote/mounted.txt" not in _FakeAsyncSandbox.snapshots["vercel-snapshot-1"]
    with pytest.raises(vercel_module.WorkspaceReadNotFoundError):
        await resumed._inner.read(Path("remote/mounted.txt"))
    kept = await resumed._inner.read(Path("kept.txt"))
    assert kept.read() == b"kept"


@pytest.mark.asyncio
async def test_vercel_snapshot_hydrate_replaces_and_stops_superseded_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    current = _FakeAsyncSandbox(
        sandbox_id="sandbox-current",
        files={"/workspace/current.txt": b"before"},
    )
    _FakeAsyncSandbox.snapshots["vercel-snapshot-1"] = {"/workspace/restored.txt": b"after"}
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000011",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=current.sandbox_id,
        workspace_persistence="snapshot",
    )
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=current)

    await session.hydrate_workspace(
        io.BytesIO(vercel_module._encode_snapshot_ref(snapshot_id="vercel-snapshot-1"))
    )

    assert current.stop_calls == 1
    assert current.client.closed is True
    assert session._sandbox is not current
    assert session.state.sandbox_id == "vercel-sandbox-1"
    restored = await session.read(Path("restored.txt"))
    assert restored.read() == b"after"
