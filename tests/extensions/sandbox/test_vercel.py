from __future__ import annotations

import asyncio
import builtins
import importlib
import io
import json
import sys
import tarfile
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
from pydantic import BaseModel, PrivateAttr

from agents.sandbox import Manifest, SandboxAgent, SandboxPathGrant, SandboxRunConfig
from agents.sandbox.capabilities import Capability
from agents.sandbox.entries import (
    Dir,
    File,
    InContainerMountStrategy,
    Mount,
    MountpointMountPattern,
    RcloneMountPattern,
    S3Mount,
)
from agents.sandbox.entries.mounts.base import InContainerMountAdapter
from agents.sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecTransportError,
    ExposedPortUnavailableError,
    InvalidManifestPathError,
    MountCommandError,
    MountConfigError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
)
from agents.sandbox.manifest import EnvEntry, Environment, StrEnvValue
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.runtime_session_manager import SandboxRuntimeSessionManager
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.dependencies import Dependencies
from agents.sandbox.session.manager import Instrumentation
from agents.sandbox.session.sinks import CallbackSink
from agents.sandbox.snapshot import NoopSnapshot, SnapshotBase
from agents.sandbox.types import User
from tests._fake_workspace_paths import resolve_fake_workspace_path
from tests.fake_model import FakeModel


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


class _AddFileCapability(Capability):
    type: str = "vercel-add-file"

    def process_manifest(self, manifest: Manifest) -> Manifest:
        manifest.entries["capability.txt"] = File(content=b"capability")
        return manifest


class Resources(BaseModel):
    memory: int | None = None


class SnapshotSource(BaseModel):
    type: Literal["snapshot"] = "snapshot"
    snapshot_id: str


class _FakeVercelSandboxError(Exception):
    pass


class _FakeVercelAPIError(_FakeVercelSandboxError):
    def __init__(self, message: str, *, status_code: int, data: object | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = types.SimpleNamespace(status_code=status_code)
        self.data = data


class _FakeVercelSandboxAuthError(_FakeVercelAPIError):
    def __init__(self, message: str = "auth failed", *, data: object | None = None) -> None:
        super().__init__(message, status_code=401, data=data)


class _FakeVercelSandboxNotFoundError(_FakeVercelAPIError):
    def __init__(self, message: str = "not found", *, data: object | None = None) -> None:
        super().__init__(message, status_code=404, data=data)


class _FakeVercelSandboxPermissionError(_FakeVercelAPIError):
    def __init__(self, message: str = "permission denied", *, data: object | None = None) -> None:
        super().__init__(message, status_code=403, data=data)


class _FakeVercelSandboxRateLimitError(_FakeVercelAPIError):
    def __init__(self, message: str = "rate limited", *, data: object | None = None) -> None:
        super().__init__(message, status_code=429, data=data)


class _FakeVercelSandboxServerError(_FakeVercelAPIError):
    def __init__(self, message: str = "server error", *, data: object | None = None) -> None:
        super().__init__(message, status_code=500, data=data)


class _FakeVercelSandboxValidationError(_FakeVercelSandboxError):
    def __init__(self, message: str = "validation failed") -> None:
        super().__init__(message)


def _install_hostile_exception_descriptors(
    error_type: type[BaseException],
    *,
    reject_stringification: bool,
) -> None:
    def get_base_args(error: BaseException) -> tuple[object, ...]:
        return cast(
            tuple[object, ...],
            cast(Any, BaseException.args).__get__(error, type(error)),
        )

    def reject_slot_access(error: BaseException) -> object:
        _ = error
        raise AssertionError("provider-defined exception descriptor was accessed")

    type.__setattr__(error_type, "args", property(get_base_args))
    for name in ("__traceback__", "__cause__", "__context__"):
        type.__setattr__(error_type, name, property(reject_slot_access))
    if reject_stringification:

        def reject_string_access(error: BaseException) -> str:
            _ = error
            raise RuntimeError("provider exception stringification failed")

        type.__setattr__(error_type, "__str__", reject_string_access)


def _assert_base_exception_slots_cleared(error: BaseException) -> None:
    assert cast(Any, BaseException.args).__get__(error, type(error)) == ()
    for descriptor in (
        cast(Any, BaseException.__traceback__),
        cast(Any, BaseException.__cause__),
        cast(Any, BaseException.__context__),
    ):
        assert descriptor.__get__(error, type(error)) is None


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


class _FailingPersistSnapshot(SnapshotBase):
    type: Literal["test-vercel-failing-persist"] = "test-vercel-failing-persist"

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = (data, dependencies)
        raise RuntimeError("snapshot persist failed")

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        return io.BytesIO()

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return False


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
        self.run_command_options: list[tuple[str, dict[str, str] | None, bool]] = []
        self.command_results: dict[str, list[_FakeCommandFinished]] = {}
        self.command_started: dict[str, asyncio.Event] = {}
        self.command_waiters: dict[str, asyncio.Event] = {}
        self.refresh_calls = 0
        self.read_file_calls: list[tuple[str, str | None]] = []
        self.stop_calls = 0
        self.stop_blocking_calls: list[bool] = []
        self.stop_failures: list[BaseException] = []
        self.stop_started: asyncio.Event | None = None
        self.stop_waiters: list[asyncio.Event] = []
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
        args = args or []
        self.run_command_calls.append((cmd, list(args), cwd))
        self.run_command_options.append((cmd, env, sudo))
        if started := self.command_started.get(cmd):
            started.set()
        if waiter := self.command_waiters.get(cmd):
            await waiter.wait()
        queued_results = self.command_results.get(cmd)
        if queued_results:
            return queued_results.pop(0)
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
        self.stop_blocking_calls.append(blocking)
        if self.stop_started is not None:
            self.stop_started.set()
        if self.stop_waiters:
            await self.stop_waiters.pop(0).wait()
        if self.stop_failures:
            raise self.stop_failures.pop(0)
        self.status = "stopped"

    async def snapshot(self, *, expiration: int | None = None) -> _FakeAsyncSnapshot:
        _ = expiration
        type(self).snapshot_counter += 1
        snapshot_id = f"vercel-snapshot-{type(self).snapshot_counter}"
        type(self).snapshots[snapshot_id] = dict(self.files)
        self.status = "stopped"
        return _FakeAsyncSnapshot(snapshot_id)


@pytest.fixture(autouse=True)
def _trust_recording_mounts_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.sandbox import _mount_security

    original = _mount_security._mount_class_is_trusted
    monkeypatch.setattr(
        _mount_security,
        "_mount_class_is_trusted",
        lambda mount: isinstance(mount, _RecordingMount) or original(mount),
    )


class _RecordingMount(Mount):
    type: str = "test_vercel_recording_mount"
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
    fake_vercel_sandbox.SandboxAuthError = _FakeVercelSandboxAuthError
    fake_vercel_sandbox.SandboxNotFoundError = _FakeVercelSandboxNotFoundError
    fake_vercel_sandbox.SandboxPermissionError = _FakeVercelSandboxPermissionError
    fake_vercel_sandbox.SandboxRateLimitError = _FakeVercelSandboxRateLimitError
    fake_vercel_sandbox.SandboxServerError = _FakeVercelSandboxServerError
    fake_vercel_sandbox.SandboxStatus = types.SimpleNamespace(RUNNING="running")
    fake_vercel_sandbox.SandboxValidationError = _FakeVercelSandboxValidationError
    fake_vercel_sandbox.SnapshotSource = SnapshotSource
    cast(Any, fake_vercel).sandbox = fake_vercel_sandbox

    monkeypatch.setitem(sys.modules, "vercel", fake_vercel)
    monkeypatch.setitem(sys.modules, "vercel.sandbox", fake_vercel_sandbox)
    sys.modules.pop("agents.extensions.sandbox.vercel.mounts", None)
    sys.modules.pop("agents.extensions.sandbox.vercel.sandbox", None)
    sys.modules.pop("agents.extensions.sandbox.vercel", None)

    return importlib.import_module("agents.extensions.sandbox.vercel.sandbox")


async def _noop_sleep(*_args: object, **_kwargs: object) -> None:
    return None


def test_vercel_package_re_exports_backend_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")

    assert package_module.VercelCloudBucketMountStrategy.__name__ == (
        "VercelCloudBucketMountStrategy"
    )
    assert package_module.VercelSandboxClient is vercel_module.VercelSandboxClient
    assert package_module.VercelSandboxSessionState is vercel_module.VercelSandboxSessionState


def _vercel_s3_manifest(
    package_module: Any,
    *,
    credentials: bool = False,
    mount_path: Path | None = None,
) -> Manifest:
    return Manifest(
        root="/workspace",
        entries={
            "remote": S3Mount(
                bucket="test-bucket",
                access_key_id="test-access-key" if credentials else None,
                secret_access_key="test-secret-key" if credentials else None,
                session_token="test-session-token" if credentials else None,
                region="us-west-2",
                mount_path=mount_path,
                mount_strategy=package_module.VercelCloudBucketMountStrategy(),
            )
        },
    )


def _queue_successful_s3_mounts(sandbox: _FakeAsyncSandbox, count: int = 1) -> None:
    sandbox.command_results.update(
        {
            "/usr/bin/test": [_FakeCommandFinished() for _ in range(count)],
            "/usr/bin/rpm": [_FakeCommandFinished(stdout="1.21.0") for _ in range(count)],
            "/usr/bin/find": [_FakeCommandFinished() for _ in range(count)],
            "/usr/bin/mount-s3": [_FakeCommandFinished() for _ in range(count)],
        }
    )


def test_vercel_s3_mount_validates_credentials_and_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")

    with pytest.raises(MountConfigError, match="require both"):
        S3Mount(
            bucket="test-bucket",
            access_key_id="test-access-key",
            mount_strategy=package_module.VercelCloudBucketMountStrategy(),
        )

    with pytest.raises(MountConfigError, match="must be ephemeral"):
        S3Mount(
            bucket="test-bucket",
            ephemeral=False,
            mount_strategy=package_module.VercelCloudBucketMountStrategy(),
        )


def test_vercel_from_state_rejects_mismatched_trusted_mount_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = _vercel_s3_manifest(package_module)
    trusted_s3_mounts = vercel_module._vercel_s3_mount_map(manifest)
    trusted_mount = next(iter(trusted_s3_mounts.values())).model_copy(deep=True)
    trusted_mount.bucket = "different-bucket"
    trusted_s3_mounts = {next(iter(trusted_s3_mounts)): trusted_mount}
    state = vercel_module.VercelSandboxSessionState(
        manifest=manifest,
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-existing",
    )

    with pytest.raises(MountConfigError, match="topology must match"):
        vercel_module.VercelSandboxSession.from_state(
            state,
            trusted_s3_mounts=trusted_s3_mounts,
            trusted_manifest=manifest,
        )


def test_vercel_from_state_accepts_released_trusted_mount_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = _vercel_s3_manifest(package_module)
    state = vercel_module.VercelSandboxSessionState(
        manifest=manifest,
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-existing",
    )

    session = vercel_module.VercelSandboxSession.from_state(
        state,
        trusted_s3_mounts=vercel_module._vercel_s3_mount_map(manifest),
    )

    assert session._trusted_manifest == manifest


def test_vercel_from_state_accepts_released_credential_exposure_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    trusted_manifest = _vercel_s3_manifest(package_module, credentials=True)
    state_manifest = vercel_module._manifest_without_vercel_s3_credentials(trusted_manifest)
    state = vercel_module.VercelSandboxSessionState(
        manifest=state_manifest,
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-existing",
    )

    session = vercel_module.VercelSandboxSession.from_state(
        state,
        allow_s3_credential_exposure=True,
        trusted_s3_mounts=vercel_module._vercel_s3_mount_map(trusted_manifest),
    )

    assert session._trusted_manifest.model_dump(mode="json") == state_manifest.model_dump(
        mode="json"
    )
    assert session._runtime_s3_mount_sensitive_values() == (
        "test-access-key",
        "test-secret-key",
        "test-session-token",
    )


@pytest.mark.parametrize("entrypoint", ["constructor", "from_state"])
@pytest.mark.parametrize("include_trusted_manifest", [False, True])
def test_vercel_direct_session_normalizes_released_credentialed_state_shape(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    include_trusted_manifest: bool,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = _vercel_s3_manifest(package_module, credentials=True)
    state = vercel_module.VercelSandboxSessionState(
        manifest=manifest,
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-existing",
    )
    kwargs: dict[str, Any] = {
        "state": state,
        "allow_s3_credential_exposure": True,
        "trusted_s3_mounts": vercel_module._vercel_s3_mount_map(manifest),
    }
    if include_trusted_manifest:
        kwargs["trusted_manifest"] = manifest

    if entrypoint == "constructor":
        session = vercel_module.VercelSandboxSession(**kwargs)
    else:
        session = vercel_module.VercelSandboxSession.from_state(**kwargs)

    state_mount = state.manifest.entries["remote"]
    assert isinstance(state_mount, S3Mount)
    assert state_mount.access_key_id is None
    assert state_mount.secret_access_key is None
    assert state_mount.session_token is None
    assert session.state is state
    assert session._runtime_s3_mount_sensitive_values() == (
        "test-access-key",
        "test-secret-key",
        "test-session-token",
    )
    payload = session.state.model_dump(mode="json")
    assert "test-access-key" not in repr(payload)
    assert "test-secret-key" not in repr(payload)
    assert "test-session-token" not in repr(payload)


def test_vercel_from_state_rejects_custom_mount_before_deepcopy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = _vercel_s3_manifest(package_module)
    deepcopy_called = False

    class CustomS3Mount(S3Mount):
        type: Literal["custom_vercel_s3_mount"] = "custom_vercel_s3_mount"  # type: ignore[assignment]

        def __deepcopy__(self, memo: dict[int, Any] | None = None) -> CustomS3Mount:
            _ = memo
            nonlocal deepcopy_called
            deepcopy_called = True
            raise AssertionError("custom mount deepcopy must not run")

    state = vercel_module.VercelSandboxSessionState(
        manifest=manifest,
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-existing",
    )
    trusted_mount = CustomS3Mount(
        bucket="test-bucket",
        mount_path=Path("/vercel/sandbox/remote"),
        mount_strategy=package_module.VercelCloudBucketMountStrategy(),
    )

    with pytest.raises(MountConfigError, match="sandbox mount configuration is invalid"):
        vercel_module.VercelSandboxSession.from_state(
            state,
            trusted_s3_mounts={"/vercel/sandbox/remote": trusted_mount},
            trusted_manifest=manifest,
        )

    assert deepcopy_called is False


@pytest.mark.parametrize("mismatch", ["logical_path", "root"])
def test_vercel_from_state_rejects_mismatched_trusted_mount_topology(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    mount = S3Mount(
        bucket="test-bucket",
        mount_path=Path("/vercel/sandbox/shared"),
        mount_strategy=package_module.VercelCloudBucketMountStrategy(),
    )
    trusted_manifest = Manifest(
        root="/vercel/sandbox",
        entries={"trusted": mount},
    )
    state_manifest = Manifest(
        root=("/vercel" if mismatch == "root" else "/vercel/sandbox"),
        entries={("declared" if mismatch == "logical_path" else "trusted"): mount},
    )
    state = vercel_module.VercelSandboxSessionState(
        manifest=state_manifest,
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-existing",
    )

    with pytest.raises(MountConfigError, match="topology must match"):
        vercel_module.VercelSandboxSession.from_state(
            state,
            trusted_s3_mounts=vercel_module._vercel_s3_mount_map(trusted_manifest),
            trusted_manifest=trusted_manifest,
        )


@pytest.mark.parametrize(
    ("allow_s3_credential_exposure", "mismatched_topology"),
    [(False, False), (True, True)],
)
def test_vercel_from_state_redacts_trusted_mount_credentials_from_failure_tracebacks(
    monkeypatch: pytest.MonkeyPatch,
    allow_s3_credential_exposure: bool,
    mismatched_topology: bool,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    sentinel = "trusted-from-state-secret"
    manifest = _vercel_s3_manifest(package_module)
    trusted_s3_mounts = vercel_module._vercel_s3_mount_map(manifest)
    trusted_mount = next(iter(trusted_s3_mounts.values())).model_copy(deep=True)
    trusted_mount.access_key_id = "trusted-access-key"
    trusted_mount.secret_access_key = sentinel
    if mismatched_topology:
        trusted_mount.bucket = "different-bucket"
    trusted_s3_mounts = {next(iter(trusted_s3_mounts)): trusted_mount}
    state = vercel_module.VercelSandboxSessionState(
        manifest=manifest,
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-existing",
    )

    with pytest.raises(
        MountConfigError, match="sandbox mount configuration is invalid"
    ) as exc_info:
        vercel_module.VercelSandboxSession.from_state(
            state,
            allow_s3_credential_exposure=allow_s3_credential_exposure,
            trusted_s3_mounts=trusted_s3_mounts,
            trusted_manifest=manifest,
        )

    assert sentinel not in str(exc_info.value)
    traceback = exc_info.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.parametrize(
    ("allow_s3_credential_exposure", "mismatched_topology"),
    [(False, False), (True, True)],
)
def test_vercel_constructor_redacts_trusted_mount_credentials_from_failure_tracebacks(
    monkeypatch: pytest.MonkeyPatch,
    allow_s3_credential_exposure: bool,
    mismatched_topology: bool,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    sentinel = "trusted-constructor-secret"
    manifest = _vercel_s3_manifest(package_module)
    trusted_s3_mounts = vercel_module._vercel_s3_mount_map(manifest)
    trusted_mount = next(iter(trusted_s3_mounts.values())).model_copy(deep=True)
    trusted_mount.access_key_id = "trusted-access-key"
    trusted_mount.secret_access_key = sentinel
    if mismatched_topology:
        trusted_mount.bucket = "different-bucket"
    trusted_s3_mounts = {next(iter(trusted_s3_mounts)): trusted_mount}
    state = vercel_module.VercelSandboxSessionState(
        manifest=manifest,
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-existing",
    )

    with pytest.raises(
        MountConfigError, match="sandbox mount configuration is invalid"
    ) as exc_info:
        vercel_module.VercelSandboxSession(
            state=state,
            allow_s3_credential_exposure=allow_s3_credential_exposure,
            trusted_s3_mounts=trusted_s3_mounts,
            trusted_manifest=manifest,
        )

    assert sentinel not in str(exc_info.value)
    assert sentinel not in repr(exc_info.value.args)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    traceback = exc_info.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_type"),
    [
        ("exec", ExecTransportError),
        ("read", WorkspaceArchiveReadError),
        ("write", WorkspaceArchiveWriteError),
        ("resolve_exposed_port", ExposedPortUnavailableError),
    ],
)
async def test_vercel_protected_session_public_operations_redact_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    expected_type: type[
        ExecTransportError
        | WorkspaceArchiveReadError
        | WorkspaceArchiveWriteError
        | ExposedPortUnavailableError
    ],
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module, credentials=True),
        options=vercel_module.VercelSandboxClientOptions(
            allow_s3_credential_exposure=True,
            exposed_ports=(3000,),
        ),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    sentinel = f"public-{operation}-provider-secret"
    source_error = RuntimeError(sentinel)

    async def fail_async(*args: object, **kwargs: object) -> Any:
        _ = (args, kwargs)
        raise source_error

    if operation == "exec":
        monkeypatch.setattr(sandbox, "run_command", fail_async)

        async def invoke() -> object:
            return await session.exec("true", shell=False)

    elif operation == "read":
        monkeypatch.setattr(sandbox, "read_file", fail_async)

        async def invoke() -> object:
            return await session.read(Path("/vercel/sandbox/file.txt"))

    elif operation == "write":
        monkeypatch.setattr(sandbox, "write_files", fail_async)

        async def invoke() -> object:
            return await session.write(
                Path("/vercel/sandbox/file.txt"),
                io.BytesIO(b"content"),
            )

    else:

        def fail_domain(port: int) -> str:
            _ = port
            raise source_error

        monkeypatch.setattr(sandbox, "domain", fail_domain)

        async def invoke() -> object:
            return await session.resolve_exposed_port(3000)

    with pytest.raises(expected_type, match="protected mount configuration") as exc_info:
        await invoke()

    assert sentinel not in str(exc_info.value)
    assert exc_info.value.context == {}
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    _assert_base_exception_slots_cleared(source_error)
    traceback = exc_info.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.asyncio
async def test_vercel_create_requires_explicit_s3_credential_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    manifest = _vercel_s3_manifest(package_module, credentials=True)

    with pytest.raises(MountConfigError, match="mount-scoped credentials") as exc:
        await client.create(
            manifest=manifest,
            options=vercel_module.VercelSandboxClientOptions(),
        )

    assert _FakeAsyncSandbox.create_calls == []
    traceback = exc.value.__traceback__
    while traceback is not None:
        module_name = traceback.tb_frame.f_globals.get("__name__", "")
        if isinstance(module_name, str) and module_name.startswith("agents."):
            assert "test-secret-key" not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.asyncio
async def test_vercel_create_accepts_manifest_mount_scoped_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    manifest = _vercel_s3_manifest(
        package_module,
        credentials=True,
    ).with_in_container_mount_credential_exposure_acknowledged("remote")

    session = await client.create(
        manifest=manifest,
        options=vercel_module.VercelSandboxClientOptions(),
    )

    assert _FakeAsyncSandbox.create_calls
    await client.delete(session)


@pytest.mark.asyncio
async def test_vercel_injected_session_accepts_unchanged_s3_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=session),
        run_state=None,
    )

    manager.acquire_agent(agent)
    restored = await manager.ensure_session(
        agent=agent,
        capabilities=[Capability(type="noop")],
        is_resumed_state=False,
    )

    assert restored is session


@pytest.mark.asyncio
async def test_vercel_injected_session_revalidates_preexisting_s3_topology_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    mount = cast(S3Mount, session.state.manifest.entries["remote"])
    mount.bucket = "tampered-bucket"
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=session),
        run_state=None,
    )

    manager.acquire_agent(agent)
    with pytest.raises(MountConfigError, match="dynamic manifest application"):
        await manager.ensure_session(
            agent=agent,
            capabilities=[Capability(type="noop")],
            is_resumed_state=False,
        )

    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    assert sandbox.write_files_calls == []


@pytest.mark.asyncio
async def test_vercel_injected_session_applies_non_mount_delta_with_fixed_s3_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=session),
        run_state=None,
    )

    manager.acquire_agent(agent)
    restored = await manager.ensure_session(
        agent=agent,
        capabilities=[_AddFileCapability()],
        is_resumed_state=False,
    )

    assert restored is session
    assert restored.state.manifest.entries["capability.txt"] == File(content=b"capability")
    assert sandbox.write_files_calls == [
        [{"path": "/vercel/sandbox/capability.txt", "content": b"capability"}]
    ]
    session._inner._runtime_assert_s3_mount_topology()


@pytest.mark.asyncio
async def test_vercel_live_manifest_update_uses_one_running_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    running_calls = 0

    async def running_once() -> bool:
        nonlocal running_calls
        running_calls += 1
        if running_calls > 1:
            raise AssertionError("live manifest processing queried running state more than once")
        return True

    monkeypatch.setattr(session, "running", running_once)
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")

    update = await SandboxRuntimeSessionManager._process_live_session_manifest(
        agent=agent,
        capabilities=[_AddFileCapability()],
        session=session,
    )

    assert running_calls == 1
    assert update.processed_manifest is not None
    assert update.processed_manifest.entries["capability.txt"] == File(content=b"capability")
    assert update.entries_to_apply == [
        (Path("/vercel/sandbox/capability.txt"), File(content=b"capability"))
    ]


@pytest.mark.asyncio
async def test_vercel_stopped_injected_session_rejects_non_mount_delta_before_state_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    sandbox.status = "stopped"
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=session),
        run_state=None,
    )

    manager.acquire_agent(agent)
    with pytest.raises(MountConfigError, match="must be running"):
        await manager.ensure_session(
            agent=agent,
            capabilities=[_AddFileCapability()],
            is_resumed_state=False,
        )

    assert "capability.txt" not in session.state.manifest.entries
    assert sandbox.write_files_calls == []
    assert len(_FakeAsyncSandbox.create_calls) == 1
    session._inner._runtime_assert_s3_mount_topology()


@pytest.mark.asyncio
async def test_vercel_create_revalidates_mutated_s3_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = _vercel_s3_manifest(package_module)
    mount = cast(S3Mount, manifest.entries["remote"])
    mount.ephemeral = False

    with pytest.raises(MountConfigError, match="must be ephemeral"):
        await vercel_module.VercelSandboxClient().create(
            manifest=manifest,
            options=vercel_module.VercelSandboxClientOptions(),
        )

    assert _FakeAsyncSandbox.create_calls == []


@pytest.mark.asyncio
async def test_vercel_credential_opt_in_does_not_allow_signed_endpoint_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = _vercel_s3_manifest(package_module, credentials=True)
    mount = cast(S3Mount, manifest.entries["remote"])
    mount.endpoint_url = "https://example.test?signature=endpoint-secret"

    with pytest.raises(MountConfigError, match="does not support exposing"):
        await vercel_module.VercelSandboxClient().create(
            manifest=manifest,
            options=vercel_module.VercelSandboxClientOptions(
                allow_s3_credential_exposure=True,
            ),
        )

    assert _FakeAsyncSandbox.create_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_configuration", ["partial_credentials", "workspace_root"])
async def test_vercel_credential_opt_in_redacts_provider_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    invalid_configuration: str,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    sentinel = "vercel-validation-secret"
    manifest = _vercel_s3_manifest(package_module, credentials=True)
    mount = cast(S3Mount, manifest.entries["remote"])
    mount.secret_access_key = sentinel
    if invalid_configuration == "partial_credentials":
        mount.access_key_id = None
    else:
        manifest.root = "/custom-workspace"
        mount.mount_path = Path("/custom-workspace")

    with pytest.raises(MountConfigError) as exc:
        await vercel_module.VercelSandboxClient().create(
            manifest=manifest,
            options=vercel_module.VercelSandboxClientOptions(
                allow_s3_credential_exposure=True,
            ),
        )

    assert sentinel not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    traceback = exc.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    assert _FakeAsyncSandbox.create_calls == []


@pytest.mark.asyncio
async def test_vercel_apply_manifest_uses_central_mount_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    manifest = Manifest(
        entries={
            "remote": S3Mount(
                bucket="example-bucket",
                access_key_id="example-access-key",
                secret_access_key="example-secret-key",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    )
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000300",
        manifest=manifest,
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-central-validation",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-central-validation")
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(MountConfigError, match="mount-scoped credentials"):
        await session.apply_manifest()

    assert sandbox.run_command_calls == []


@pytest.mark.asyncio
async def test_vercel_rejects_root_and_overlapping_s3_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    strategy = package_module.VercelCloudBucketMountStrategy
    client = vercel_module.VercelSandboxClient()

    root_manifest = Manifest(
        root="/custom-workspace",
        entries={
            "remote": S3Mount(
                bucket="root-bucket",
                mount_path=Path("/custom-workspace"),
                mount_strategy=strategy(),
            )
        },
    )
    with pytest.raises(MountConfigError, match="workspace root"):
        await client.create(
            manifest=root_manifest,
            options=vercel_module.VercelSandboxClientOptions(),
        )

    outside_manifest = Manifest(
        root="/workspace",
        entries={
            "remote": S3Mount(
                bucket="outside-bucket",
                mount_path=Path("/tmp/remote"),
                mount_strategy=strategy(),
            )
        },
        extra_path_grants=(SandboxPathGrant(path="/tmp/remote"),),
    )
    with pytest.raises(MountConfigError, match="within the workspace root"):
        await client.create(
            manifest=outside_manifest,
            options=vercel_module.VercelSandboxClientOptions(),
        )

    overlapping_manifest = Manifest(
        root="/workspace",
        entries={
            "remote": S3Mount(bucket="outer", mount_strategy=strategy()),
            "remote/nested": S3Mount(bucket="inner", mount_strategy=strategy()),
        },
    )
    with pytest.raises(MountConfigError, match="must not overlap"):
        await client.create(
            manifest=overlapping_manifest,
            options=vercel_module.VercelSandboxClientOptions(),
        )

    physical_overlap_manifest = Manifest(
        root="/workspace",
        entries={
            "remote": S3Mount(
                bucket="physical-overlap",
                mount_path=Path("actual"),
                mount_strategy=strategy(),
            ),
            "actual/config.json": File(content=b"{}"),
        },
    )
    with pytest.raises(MountConfigError, match="must not overlap manifest entries"):
        await client.create(
            manifest=physical_overlap_manifest,
            options=vercel_module.VercelSandboxClientOptions(),
        )

    assert _FakeAsyncSandbox.create_calls == []


@pytest.mark.asyncio
async def test_vercel_s3_mount_is_create_time_only_and_credentials_are_not_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module, credentials=True),
        options=vercel_module.VercelSandboxClientOptions(allow_s3_credential_exposure=True),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    sandbox.command_results = {
        "/usr/bin/test": [_FakeCommandFinished()],
        "/usr/bin/rpm": [_FakeCommandFinished(stdout="1.21.0")],
        "/usr/bin/find": [_FakeCommandFinished()],
        "/usr/bin/mount-s3": [_FakeCommandFinished()],
    }

    await session.start()

    mount_call = next(
        options for options in sandbox.run_command_options if options[0] == "/usr/bin/mount-s3"
    )
    assert mount_call == (
        "/usr/bin/mount-s3",
        {
            "AWS_ACCESS_KEY_ID": "test-access-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret-key",
            "AWS_SESSION_TOKEN": "test-session-token",
            "AWS_REGION": "us-west-2",
        },
        True,
    )
    state_mount = cast(S3Mount, session.state.manifest.entries["remote"])
    assert state_mount.access_key_id is None
    assert state_mount.secret_access_key is None
    assert state_mount.session_token is None

    payload = client.serialize_session_state(session.state)
    serialized = json.dumps(payload, sort_keys=True)
    assert "test-access-key" not in serialized
    assert "test-secret-key" not in serialized
    assert "test-session-token" not in serialized
    assert "vercel_cloud_bucket" in serialized

    remote_mount = session.state.manifest.entries.pop("remote")
    session.state.manifest.entries = {
        "before.txt": File(content=b"must-not-write", ephemeral=True),
        "remote": remote_mount,
    }
    write_call_count = len(sandbox.write_files_calls)
    with pytest.raises(MountConfigError, match="dynamic manifest application") as exc:
        await session.apply_manifest(only_ephemeral=True)
    assert len(sandbox.write_files_calls) == write_call_count
    traceback = exc.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert "test-secret-key" not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next

    session.state.manifest.entries.pop("remote")
    mutated_payload = client.serialize_session_state(session.state)
    assert mutated_payload["s3_mounts_non_resumable"] is True
    assert "vercel_cloud_bucket" not in json.dumps(mutated_payload, sort_keys=True)

    restored = client.deserialize_session_state(mutated_payload)
    with pytest.raises(MountConfigError, match="cannot be resumed"):
        await client.resume(restored)
    assert _FakeAsyncSandbox.get_calls == []


@pytest.mark.asyncio
async def test_vercel_s3_dynamic_mount_is_rejected_before_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=Manifest(entries={"before.txt": File(content=b"must-not-write", ephemeral=True)}),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    session.state.manifest.entries["remote"] = S3Mount(
        bucket="test-bucket",
        mount_strategy=package_module.VercelCloudBucketMountStrategy(),
    )

    with pytest.raises(MountConfigError, match="dynamic manifest application"):
        await session.apply_manifest(only_ephemeral=True)

    assert sandbox.write_files_calls == []
    with pytest.raises(MountConfigError, match="topology cannot change"):
        await session.persist_workspace()
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_mount_starts_after_restorable_tar_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass
    snapshot = _MemorySnapshot(
        id="snapshot",
        payload=archive.getvalue(),
        is_restorable=True,
    )
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        snapshot=snapshot,
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)

    await session.start()

    assert len([call for call in sandbox.run_command_calls if call[0] == "/usr/bin/mount-s3"]) == 1
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_snapshot_entries_materialize_before_mount_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass
    manifest = Manifest(
        root="/workspace",
        entries={
            "remote": S3Mount(
                bucket="test-bucket",
                mount_path=Path("actual/remote"),
                mount_strategy=package_module.VercelCloudBucketMountStrategy(),
            ),
            "alias/remote/config.json": File(content=b"{}", ephemeral=True),
        },
    )
    session = await vercel_module.VercelSandboxClient().create(
        snapshot=_MemorySnapshot(
            id="snapshot",
            payload=archive.getvalue(),
            is_restorable=True,
        ),
        manifest=manifest,
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    sandbox.symlinks["/vercel/sandbox/alias"] = "/vercel/sandbox/actual"
    sandbox.command_results = {
        "/usr/bin/rpm": [_FakeCommandFinished(stdout="1.21.0")],
        "/usr/bin/test": [_FakeCommandFinished()],
        "/usr/bin/find": [_FakeCommandFinished(stdout="/vercel/sandbox/actual/remote/config.json")],
    }

    with pytest.raises(MountConfigError, match="require an empty mount directory"):
        await session.start()

    assert [
        {"path": "/vercel/sandbox/alias/remote/config.json", "content": b"{}"}
    ] in sandbox.write_files_calls
    assert not any(call[0] == "/usr/bin/mount-s3" for call in sandbox.run_command_calls)
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_mount_snapshots_trusted_create_time_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = _vercel_s3_manifest(package_module)
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=manifest,
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)

    supplied_mount = cast(S3Mount, manifest.entries["remote"])
    supplied_mount.bucket = "mutated-bucket"
    supplied_mount.access_key_id = "mutated-access-key"
    supplied_mount.secret_access_key = "mutated-secret-key"

    await session.start()

    mount_call = next(call for call in sandbox.run_command_calls if call[0] == "/usr/bin/mount-s3")
    mount_options = next(
        options for options in sandbox.run_command_options if options[0] == "/usr/bin/mount-s3"
    )
    assert mount_call[1][0] == "test-bucket"
    assert mount_options[1] == {"AWS_REGION": "us-west-2"}

    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_mount_rejects_symlink_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module, mount_path=Path("link/remote")),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    sandbox.symlinks["/vercel/sandbox/link"] = "/vercel/sandbox/durable"

    with pytest.raises(MountConfigError, match="must not resolve through symlinks"):
        await session.start()

    assert not any(call[0] == "/usr/bin/mount-s3" for call in sandbox.run_command_calls)
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_entry_failure_happens_before_mount_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = _vercel_s3_manifest(package_module)
    manifest.entries["later.txt"] = File(content=b"later")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=manifest,
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    sandbox.write_failures = [RuntimeError("later entry failed")]

    with pytest.raises(vercel_module.WorkspaceArchiveWriteError):
        await session.start()

    mount_calls = [call for call in sandbox.run_command_calls if call[0] == "/usr/bin/mount-s3"]
    assert mount_calls == []
    assert sandbox.stop_calls == 0

    await session.start()
    assert len([call for call in sandbox.run_command_calls if call[0] == "/usr/bin/mount-s3"]) == 1
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_entry_cancellation_happens_before_mount_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = _vercel_s3_manifest(package_module)
    manifest.entries["later.txt"] = File(content=b"later")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=manifest,
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    write_started = asyncio.Event()
    hold_write = asyncio.Event()
    original_write_files = sandbox.write_files

    async def blocking_write_files(files: list[dict[str, object]]) -> None:
        _ = files
        write_started.set()
        await hold_write.wait()

    monkeypatch.setattr(sandbox, "write_files", blocking_write_files)
    start_task = asyncio.create_task(session.start())
    await asyncio.wait_for(write_started.wait(), timeout=1)
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert sandbox.stop_calls == 0
    assert not any(call[0] == "/usr/bin/mount-s3" for call in sandbox.run_command_calls)

    monkeypatch.setattr(sandbox, "write_files", original_write_files)
    await session.start()
    assert len([call for call in sandbox.run_command_calls if call[0] == "/usr/bin/mount-s3"]) == 1
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_nested_activation_serializes_workspace_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = Manifest(
        entries={
            "parent": Dir(
                children={
                    "remote": S3Mount(
                        bucket="test-bucket",
                        mount_strategy=package_module.VercelCloudBucketMountStrategy(),
                    )
                }
            )
        }
    )
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=manifest,
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    mount_started = asyncio.Event()
    release_mount = asyncio.Event()
    sandbox.command_started["/usr/bin/mount-s3"] = mount_started
    sandbox.command_waiters["/usr/bin/mount-s3"] = release_mount

    start_task = asyncio.create_task(session.start())
    await asyncio.wait_for(mount_started.wait(), timeout=1)
    apply_task = asyncio.create_task(session.apply_manifest(only_ephemeral=True))
    exec_task = asyncio.create_task(session.exec("true", shell=False))
    await asyncio.sleep(0)
    assert not exec_task.done()

    release_mount.set()
    await start_task
    with pytest.raises(MountConfigError, match="dynamic manifest application"):
        await apply_task
    assert (await exec_task).ok()
    assert len([call for call in sandbox.run_command_calls if call[0] == "/usr/bin/mount-s3"]) == 1
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_mount_lock_does_not_leak_to_child_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    inner = session._inner
    child_entered = asyncio.Event()

    async def enter_from_child() -> None:
        async with inner._s3_mount_operation(force_lock=True):
            child_entered.set()

    async with inner._s3_mount_operation(force_lock=True):
        child_task = asyncio.create_task(enter_from_child())
        await asyncio.sleep(0)
        assert not child_entered.is_set()

    await asyncio.wait_for(child_task, timeout=1)
    assert child_entered.is_set()
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_manifest_sanitization_preserves_typed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    manifest = _vercel_s3_manifest(package_module, credentials=True)
    manifest.environment = Environment(
        value={
            "DIRECT": StrEnvValue(value="direct-value"),
            "ENTRY": EnvEntry(
                description="typed entry",
                ephemeral=True,
                value=StrEnvValue(value="entry-value"),
            ),
        }
    )
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=manifest,
        options=vercel_module.VercelSandboxClientOptions(
            allow_s3_credential_exposure=True,
        ),
    )

    state_environment = session.state.manifest.environment.value
    assert state_environment["DIRECT"] == StrEnvValue(value="direct-value")
    assert state_environment["ENTRY"] == EnvEntry(
        description="typed entry",
        ephemeral=True,
        value=StrEnvValue(value="entry-value"),
    )
    payload = client.serialize_session_state(session.state)
    serialized_environment = cast(
        dict[str, object],
        cast(dict[str, object], payload["manifest"])["environment"],
    )
    assert serialized_environment == {
        "value": {
            "DIRECT": {"type": "str", "value": "direct-value"},
            "ENTRY": {
                "description": "typed entry",
                "ephemeral": True,
                "value": {"type": "str", "value": "entry-value"},
            },
        }
    }
    serialized = json.dumps(payload, sort_keys=True)
    assert "test-access-key" not in serialized
    assert "test-secret-key" not in serialized
    assert "test-session-token" not in serialized

    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_mount_detaches_for_tar_persistence_and_unmounts_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    snapshot = _MemorySnapshot(id="snapshot")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        snapshot=snapshot,
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(
            workspace_persistence="snapshot",
        ),
    )
    fingerprint_called = False

    async def unexpected_fingerprint() -> dict[str, str]:
        nonlocal fingerprint_called
        fingerprint_called = True
        return {"fingerprint": "unexpected", "version": "unexpected"}

    monkeypatch.setattr(
        session._inner,
        "_compute_and_cache_snapshot_fingerprint",
        unexpected_fingerprint,
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    sandbox.files[f"{session.state.manifest.root}/kept.txt"] = b"kept"
    sandbox.command_results = {
        "/usr/bin/test": [_FakeCommandFinished(), _FakeCommandFinished()],
        "/usr/bin/rpm": [
            _FakeCommandFinished(stdout="1.21.0"),
            _FakeCommandFinished(stdout="1.21.0"),
        ],
        "/usr/bin/find": [_FakeCommandFinished(), _FakeCommandFinished()],
        "/usr/bin/mount-s3": [_FakeCommandFinished(), _FakeCommandFinished()],
        "/usr/bin/findmnt": [
            _FakeCommandFinished(stdout="mountpoint-s3"),
            _FakeCommandFinished(stdout="mountpoint-s3"),
        ],
        "/usr/bin/umount": [_FakeCommandFinished(), _FakeCommandFinished()],
    }

    await session.start()
    await session.stop()
    await session.shutdown()

    assert fingerprint_called is False
    lifecycle_commands = [
        command
        for command, _args, _cwd in sandbox.run_command_calls
        if command
        in {
            "/usr/bin/findmnt",
            "/usr/bin/mount-s3",
            "/usr/bin/umount",
            "tar",
        }
    ]
    assert lifecycle_commands == [
        "/usr/bin/mount-s3",
        "/usr/bin/findmnt",
        "/usr/bin/umount",
        "tar",
        "/usr/bin/mount-s3",
        "/usr/bin/findmnt",
        "/usr/bin/umount",
    ]
    assert _FakeAsyncSandbox.snapshot_counter == 0
    assert sandbox.stop_calls == 1
    with tarfile.open(fileobj=io.BytesIO(snapshot.payload), mode="r") as archive:
        assert [member.name for member in archive.getmembers()] == ["kept.txt"]


@pytest.mark.asyncio
async def test_vercel_s3_hydrate_rejects_mount_overlaps_before_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    await session.start()

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        payload = b"must-not-be-written"
        member = tarfile.TarInfo("remote/hidden.txt")
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))

    with pytest.raises(
        vercel_module.WorkspaceArchiveWriteError,
        match="failed to write archive",
    ):
        await session.hydrate_workspace(io.BytesIO(archive.getvalue()))
    with pytest.raises(MountConfigError, match="native snapshot"):
        await session.hydrate_workspace(
            io.BytesIO(vercel_module._encode_snapshot_ref(snapshot_id="snapshot-id"))
        )

    assert not any(call[0] == "/usr/bin/findmnt" for call in sandbox.run_command_calls)
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_aclose_retries_failed_transition_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        snapshot=_MemorySnapshot(id="snapshot"),
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [
                _FakeCommandFinished(stdout="mountpoint-s3"),
                _FakeCommandFinished(stdout="mountpoint-s3"),
            ],
            "/usr/bin/umount": [_FakeCommandFinished(stderr="busy", exit_code=32)],
        }
    )
    sandbox.stop_failures = [RuntimeError("stop failed")]
    await session.start()

    with pytest.raises(vercel_module.WorkspaceArchiveReadError):
        await session.aclose()
    create_count = len(_FakeAsyncSandbox.create_calls)
    with pytest.raises(vercel_module.WorkspaceStartError, match="failed to start session"):
        await session.exec("true", shell=False)

    assert sandbox.stop_calls == 2
    assert sandbox.stop_blocking_calls == [True, True]
    assert session._inner._sandbox is None
    assert len(_FakeAsyncSandbox.create_calls) == create_count


@pytest.mark.asyncio
async def test_vercel_s3_shutdown_preserves_first_stop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [
                _FakeCommandFinished(stdout="mountpoint-s3"),
                _FakeCommandFinished(stdout="mountpoint-s3"),
            ],
            "/usr/bin/umount": [_FakeCommandFinished(stderr="busy", exit_code=32)],
        }
    )
    sandbox.stop_failures = [
        RuntimeError("first stop failed"),
        RuntimeError("second stop failed"),
    ]
    await session.start()

    with pytest.raises(RuntimeError, match="first stop failed"):
        await session.shutdown()

    assert sandbox.stop_calls == 2
    assert sandbox.stop_blocking_calls == [True, True]


@pytest.mark.asyncio
async def test_vercel_s3_missing_tracked_mount_stops_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        snapshot=_MemorySnapshot(id="snapshot"),
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    await session.start()
    sandbox.command_results["/usr/bin/findmnt"] = [_FakeCommandFinished(exit_code=1)]

    with pytest.raises(vercel_module.WorkspaceArchiveReadError):
        await session.persist_workspace()

    assert sandbox.stop_calls == 1
    assert sandbox.stop_blocking_calls == [True]
    assert session._inner._sandbox is None
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_mount_disappearing_during_unmount_stops_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        snapshot=_MemorySnapshot(id="snapshot"),
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    await session.start()
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [
                _FakeCommandFinished(stdout="mountpoint-s3"),
                _FakeCommandFinished(exit_code=1),
            ],
            "/usr/bin/umount": [_FakeCommandFinished(stderr="missing", exit_code=32)],
        }
    )

    with pytest.raises(vercel_module.WorkspaceArchiveReadError):
        await session.persist_workspace()

    assert sandbox.stop_calls == 1
    assert sandbox.stop_blocking_calls == [True]
    assert session._inner._sandbox is None
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_unexpected_persist_error_restores_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        snapshot=_MemorySnapshot(id="snapshot"),
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox, count=2)
    await session.start()
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )

    async def missing_archive(_path: str, *, cwd: str | None = None) -> bytes | None:
        _ = cwd
        return None

    monkeypatch.setattr(sandbox, "read_file", missing_archive)

    with pytest.raises(vercel_module.WorkspaceReadNotFoundError):
        await session.persist_workspace()

    assert sandbox.stop_calls == 0
    assert session._inner._sandbox is sandbox
    assert session._inner._active_s3_mount_paths == {"/vercel/sandbox/remote"}
    assert session._inner._detached_s3_mount_paths == set()
    assert (await session.exec("true", shell=False)).ok()
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()
    assert sandbox.stop_calls == 1


@pytest.mark.asyncio
async def test_vercel_s3_aclose_shuts_down_after_snapshot_persist_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        snapshot=_FailingPersistSnapshot(id="snapshot"),
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox, count=2)
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [
                _FakeCommandFinished(stdout="mountpoint-s3"),
                _FakeCommandFinished(stdout="mountpoint-s3"),
            ],
            "/usr/bin/umount": [
                _FakeCommandFinished(),
                _FakeCommandFinished(),
            ],
        }
    )

    with pytest.raises(RuntimeError, match="snapshot persist failed"):
        async with session:
            pass

    assert sandbox.stop_calls == 1
    assert sandbox.stop_blocking_calls == [True]
    assert session._inner._sandbox is None


@pytest.mark.asyncio
async def test_vercel_s3_stop_preserves_session_after_snapshot_persist_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        snapshot=_FailingPersistSnapshot(id="snapshot"),
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox, count=2)
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [
                _FakeCommandFinished(stdout="mountpoint-s3"),
                _FakeCommandFinished(stdout="mountpoint-s3"),
            ],
            "/usr/bin/umount": [
                _FakeCommandFinished(),
                _FakeCommandFinished(),
            ],
        }
    )
    await session.start()

    with pytest.raises(RuntimeError, match="snapshot persist failed"):
        await session.stop()

    assert sandbox.stop_calls == 0
    assert session._inner._sandbox is sandbox
    assert session._inner._active_s3_mount_paths == {"/vercel/sandbox/remote"}

    await session.shutdown()
    assert sandbox.stop_calls == 1


@pytest.mark.asyncio
async def test_vercel_s3_aclose_shuts_down_after_pre_stop_hook_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )

    async def failing_hook() -> None:
        raise RuntimeError("pre-stop hook failed")

    session.register_pre_stop_hook(failing_hook)
    with pytest.raises(RuntimeError, match="pre-stop hook failed"):
        async with session:
            pass

    assert sandbox.stop_calls == 1
    assert sandbox.stop_blocking_calls == [True]
    assert session._inner._sandbox is None


@pytest.mark.asyncio
async def test_vercel_s3_aclose_bypasses_failing_instrumentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")

    def fail_stop_start(event: Any, _session: BaseSandboxSession) -> None:
        if event.op == "stop" and event.phase == "start":
            raise RuntimeError("stop sink failed")

    client = vercel_module.VercelSandboxClient(
        instrumentation=Instrumentation(
            sinks=[CallbackSink(fail_stop_start, mode="sync", on_error="raise")]
        )
    )
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.start()

    with pytest.raises(RuntimeError, match="sandbox event sink failed"):
        await session.aclose()

    assert sandbox.stop_calls == 1
    assert sandbox.stop_blocking_calls == [True]
    assert session._inner._sandbox is None


@pytest.mark.asyncio
async def test_vercel_s3_closed_session_does_not_recreate_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.start()
    create_count = len(_FakeAsyncSandbox.create_calls)

    await session.shutdown()
    await session.shutdown()
    await session.aclose()

    with pytest.raises(vercel_module.WorkspaceStartError) as exec_error:
        await session.exec("true", shell=False)
    assert exec_error.value.context["reason"] == "mounted_session_closed"
    with pytest.raises(vercel_module.WorkspaceStartError):
        await session.start()

    assert sandbox.stop_calls == 1
    assert len(_FakeAsyncSandbox.create_calls) == create_count


@pytest.mark.asyncio
async def test_vercel_s3_mount_cancellation_settles_and_restores_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        snapshot=_MemorySnapshot(id="snapshot"),
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox, count=2)
    output_started = asyncio.Event()
    hold_output = asyncio.Event()

    class _BlockingUnmountResult(_FakeCommandFinished):
        async def stdout(self) -> str:
            output_started.set()
            await hold_output.wait()
            return ""

    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_BlockingUnmountResult()],
        }
    )
    await session.start()

    stop_task = asyncio.create_task(session.stop())
    await asyncio.wait_for(output_started.wait(), timeout=1)
    stop_task.cancel()
    hold_output.set()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    assert sandbox.stop_calls == 0
    assert session._inner._sandbox is sandbox
    assert session._inner._active_s3_mount_paths == {"/vercel/sandbox/remote"}
    assert session._inner._detached_s3_mount_paths == set()
    assert session._inner._s3_mount_failure is None
    assert (await session.exec("true", shell=False)).ok()
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()
    assert sandbox.stop_calls == 1


@pytest.mark.asyncio
async def test_vercel_s3_shutdown_cancellation_finishes_stop_and_marks_session_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    await session.start()
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    sandbox.stop_started = asyncio.Event()
    sandbox.stop_waiters = [asyncio.Event()]
    shutdown_task = asyncio.create_task(session.shutdown())
    await asyncio.wait_for(sandbox.stop_started.wait(), timeout=1)
    shutdown_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await shutdown_task

    assert sandbox.stop_calls == 2
    assert sandbox.stop_blocking_calls == [True, True]
    assert session._inner._sandbox is None
    with pytest.raises(vercel_module.WorkspaceStartError) as exc_info:
        await session.exec("true", shell=False)
    assert exc_info.value.context["reason"] == "mount_transition_failed"
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_archive_cancellation_restores_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        snapshot=_MemorySnapshot(id="snapshot"),
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox, count=2)
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    archive_started = asyncio.Event()
    hold_archive = asyncio.Event()
    sandbox.command_started["tar"] = archive_started
    sandbox.command_waiters["tar"] = hold_archive
    await session.start()

    stop_task = asyncio.create_task(session.stop())
    await asyncio.wait_for(archive_started.wait(), timeout=1)
    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    assert sandbox.stop_calls == 0
    assert session._inner._sandbox is sandbox
    assert session._inner._active_s3_mount_paths == {"/vercel/sandbox/remote"}
    assert session._inner._detached_s3_mount_paths == set()
    assert session._inner._s3_mount_failure is None
    assert (await session.exec("true", shell=False)).ok()
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()
    assert sandbox.stop_calls == 1


@pytest.mark.asyncio
async def test_vercel_s3_rejects_state_topology_changes_and_cleans_fixed_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    await session.start()

    state_mount = cast(S3Mount, session.state.manifest.entries["remote"])
    state_mount.mount_path = Path("/vercel/sandbox/moved")
    with pytest.raises(MountConfigError, match="cannot change after sandbox creation"):
        await session._inner.persist_workspace()
    assert not any(call[0] == "/usr/bin/findmnt" for call in sandbox.run_command_calls)

    state_mount.mount_path = None
    state_mount.ephemeral = False
    with pytest.raises(MountConfigError, match="cannot change after sandbox creation"):
        await session._inner.persist_workspace()
    assert not any(call[0] == "/usr/bin/findmnt" for call in sandbox.run_command_calls)

    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()
    unmount_call = next(call for call in sandbox.run_command_calls if call[0] == "/usr/bin/umount")
    assert unmount_call[1] == ["/vercel/sandbox/remote"]


@pytest.mark.asyncio
async def test_vercel_s3_rejects_logical_path_and_root_changes_with_explicit_mount_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    manifest = _vercel_s3_manifest(
        package_module,
        mount_path=Path("/vercel/sandbox/actual"),
    )
    manifest.root = vercel_module.DEFAULT_VERCEL_WORKSPACE_ROOT
    session = await client.create(
        manifest=manifest,
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    await session.start()

    mount = session.state.manifest.entries.pop("remote")
    session.state.manifest.entries["durable"] = mount
    with pytest.raises(MountConfigError, match="cannot change after sandbox creation"):
        await session._inner.persist_workspace()
    assert not any(call[0] == "/usr/bin/findmnt" for call in sandbox.run_command_calls)

    session.state.manifest.entries["remote"] = session.state.manifest.entries.pop("durable")
    session.state.manifest.root = "/vercel/sandbox/link/.."
    with pytest.raises(MountConfigError, match="cannot change after sandbox creation"):
        await session.exec("true", shell=False)
    assert not any(call[0] == "/usr/bin/findmnt" for call in sandbox.run_command_calls)

    session.state.manifest.root = vercel_module.DEFAULT_VERCEL_WORKSPACE_ROOT
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_s3_mount_transition_serializes_workspace_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        snapshot=_MemorySnapshot(id="snapshot"),
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox, count=2)
    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [
                _FakeCommandFinished(stdout="mountpoint-s3"),
                _FakeCommandFinished(stdout="mountpoint-s3"),
            ],
            "/usr/bin/umount": [_FakeCommandFinished(), _FakeCommandFinished()],
        }
    )
    unmount_started = asyncio.Event()
    release_unmount = asyncio.Event()
    sandbox.command_started["/usr/bin/umount"] = unmount_started
    sandbox.command_waiters["/usr/bin/umount"] = release_unmount
    await session.start()

    stop_task = asyncio.create_task(session.stop())
    await asyncio.wait_for(unmount_started.wait(), timeout=1)
    exec_task = asyncio.create_task(session.exec("true", shell=False))
    await asyncio.sleep(0)
    assert not exec_task.done()

    release_unmount.set()
    await stop_task
    assert (await exec_task).ok()
    await session.shutdown()


@pytest.mark.asyncio
async def test_vercel_without_s3_mounts_does_not_serialize_workspace_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000200",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-without-mounts",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-without-mounts")
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    sandbox.command_started["slow"] = slow_started
    sandbox.command_waiters["slow"] = release_slow
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    slow_task = asyncio.create_task(session.exec("slow", shell=False))
    await asyncio.wait_for(slow_started.wait(), timeout=1)
    try:
        fast_result = await asyncio.wait_for(session.exec("true", shell=False), timeout=1)
        assert fast_result.ok()
    finally:
        release_slow.set()
        await slow_task


@pytest.mark.asyncio
async def test_vercel_mount_command_timeout_includes_output_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    mounts_module = importlib.import_module("agents.extensions.sandbox.vercel.mounts")
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000201",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-output-timeout",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-output-timeout")
    hold_output = asyncio.Event()

    class _BlockingOutputResult(_FakeCommandFinished):
        async def stdout(self) -> str:
            await hold_output.wait()
            return ""

    sandbox.command_results["/usr/bin/test"] = [_BlockingOutputResult()]
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(MountCommandError) as exc_info:
        await mounts_module._run_vercel_command(
            session,
            "/usr/bin/test",
            [],
            timeout=0.01,
        )
    assert exc_info.value.context["stderr"] == "TimeoutError: "


@pytest.mark.asyncio
async def test_vercel_s3_mount_upgrades_mountpoint_below_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    mounts_module = importlib.import_module("agents.extensions.sandbox.vercel.mounts")
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000202",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-old-mountpoint",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-old-mountpoint")
    sandbox.command_results = {
        "/usr/bin/rpm": [
            _FakeCommandFinished(stdout="1.20.0"),
            _FakeCommandFinished(stdout="1.21.0"),
        ],
        "/usr/bin/test": [_FakeCommandFinished(), _FakeCommandFinished()],
        "/usr/bin/dnf": [_FakeCommandFinished()],
    }
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    await mounts_module._ensure_mountpoint(session)

    dnf_call = next(call for call in sandbox.run_command_calls if call[0] == "/usr/bin/dnf")
    assert dnf_call[1][-2:] == ["fuse", "mount-s3"]


@pytest.mark.asyncio
async def test_vercel_s3_mount_failure_redacts_full_activation_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module, credentials=True),
        options=vercel_module.VercelSandboxClientOptions(allow_s3_credential_exposure=True),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    sandbox.command_results = {
        "/usr/bin/test": [_FakeCommandFinished()],
        "/usr/bin/rpm": [_FakeCommandFinished(stdout="1.21.0")],
        "/usr/bin/find": [_FakeCommandFinished()],
    }
    secrets = ("test-access-key", "test-secret-key", "test-session-token")
    transformed_secret = "test%2Dsecret%2Dkey"
    provider_error = _FakeVercelSandboxRateLimitError(f"provider rejected {transformed_secret}")
    original_run_command = sandbox.run_command

    def assert_activation_traceback_is_redacted(error: BaseException) -> None:
        traceback = error.__traceback__
        while traceback is not None:
            frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
            if "/src/agents/" in frame_path:
                locals_repr = repr(traceback.tb_frame.f_locals)
                for secret in secrets:
                    assert secret not in locals_repr
            traceback = traceback.tb_next

    async def fail_command(
        cmd: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sudo: bool = False,
    ) -> _FakeCommandFinished:
        if cmd == "/usr/bin/mount-s3":
            assert env == {
                "AWS_ACCESS_KEY_ID": secrets[0],
                "AWS_SECRET_ACCESS_KEY": secrets[1],
                "AWS_SESSION_TOKEN": secrets[2],
                "AWS_REGION": "us-west-2",
            }
            raise provider_error
        return await original_run_command(cmd, args, cwd=cwd, env=env, sudo=sudo)

    monkeypatch.setattr(sandbox, "run_command", fail_command)

    with pytest.raises(MountCommandError) as exc_info:
        await session.start()

    assert exc_info.value.error_code is ErrorCode.MOUNT_FAILED
    assert exc_info.value.op == "materialize"
    assert exc_info.value.context == {}
    assert transformed_secret not in str(exc_info.value)
    assert transformed_secret not in repr(exc_info.value.context)
    assert exc_info.value.retryable is True
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert provider_error.__traceback__ is None
    assert provider_error.__cause__ is None
    assert provider_error.__context__ is None
    assert_activation_traceback_is_redacted(exc_info.value)
    assert sandbox.stop_calls == 1


@pytest.mark.asyncio
async def test_vercel_s3_mount_failure_discards_transformed_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module, credentials=True),
        options=vercel_module.VercelSandboxClientOptions(allow_s3_credential_exposure=True),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    transformed_secret = "test%2Dsecret%2Dkey"
    sandbox.command_results = {
        "/usr/bin/test": [_FakeCommandFinished()],
        "/usr/bin/rpm": [_FakeCommandFinished(stdout="1.21.0")],
        "/usr/bin/mount-s3": [
            _FakeCommandFinished(
                stderr=f"provider echoed {transformed_secret}",
                exit_code=1,
            )
        ],
        "/usr/bin/find": [_FakeCommandFinished()],
    }

    with pytest.raises(MountCommandError) as exc_info:
        await session.start()

    assert exc_info.value.error_code is ErrorCode.MOUNT_FAILED
    assert exc_info.value.op == "materialize"
    assert exc_info.value.context == {}
    assert transformed_secret not in str(exc_info.value)
    assert transformed_secret not in repr(exc_info.value.context)
    assert sandbox.stop_calls == 1


@pytest.mark.asyncio
async def test_vercel_s3_mount_failure_ignores_hostile_exception_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module, credentials=True),
        options=vercel_module.VercelSandboxClientOptions(allow_s3_credential_exposure=True),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    sandbox.command_results = {
        "/usr/bin/test": [_FakeCommandFinished()],
        "/usr/bin/rpm": [_FakeCommandFinished(stdout="1.21.0")],
        "/usr/bin/find": [_FakeCommandFinished()],
    }

    class HostileProviderError(_FakeVercelSandboxRateLimitError):
        pass

    _install_hostile_exception_descriptors(
        HostileProviderError,
        reject_stringification=True,
    )
    provider_error = HostileProviderError("provider returned test-secret-key")
    original_run_command = sandbox.run_command

    async def fail_command(
        cmd: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sudo: bool = False,
    ) -> _FakeCommandFinished:
        if cmd == "/usr/bin/mount-s3":
            raise provider_error
        return await original_run_command(cmd, args, cwd=cwd, env=env, sudo=sudo)

    monkeypatch.setattr(sandbox, "run_command", fail_command)

    with pytest.raises(MountCommandError) as exc_info:
        await session.start()

    assert type(exc_info.value) is MountCommandError
    assert exc_info.value.error_code is ErrorCode.MOUNT_FAILED
    assert exc_info.value.op == "materialize"
    assert exc_info.value.context == {}
    assert exc_info.value.retryable is True
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    _assert_base_exception_slots_cleared(provider_error)
    assert sandbox.stop_calls == 1


@pytest.mark.asyncio
async def test_vercel_s3_shutdown_redacts_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module, credentials=True),
        options=vercel_module.VercelSandboxClientOptions(allow_s3_credential_exposure=True),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    await session.start()
    provider_error = RuntimeError("provider cleanup failed with test-secret-key")
    original_run_command = sandbox.run_command

    async def fail_findmnt(
        cmd: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sudo: bool = False,
    ) -> _FakeCommandFinished:
        if cmd == "/usr/bin/findmnt":
            raise provider_error
        return await original_run_command(cmd, args, cwd=cwd, env=env, sudo=sudo)

    monkeypatch.setattr(sandbox, "run_command", fail_findmnt)

    with pytest.raises(MountCommandError) as exc_info:
        await session.shutdown()

    assert "test-secret-key" not in str(exc_info.value)
    assert "test-secret-key" not in repr(exc_info.value.context)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    _assert_base_exception_slots_cleared(provider_error)
    traceback = exc_info.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert "test-secret-key" not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    assert sandbox.stop_calls == 1


@pytest.mark.asyncio
async def test_vercel_s3_mount_cancellation_redacts_full_activation_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module, credentials=True),
        options=vercel_module.VercelSandboxClientOptions(allow_s3_credential_exposure=True),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    sandbox.command_results = {
        "/usr/bin/test": [_FakeCommandFinished()],
        "/usr/bin/rpm": [_FakeCommandFinished(stdout="1.21.0")],
        "/usr/bin/find": [_FakeCommandFinished()],
    }
    mount_started = asyncio.Event()
    sandbox.command_started["/usr/bin/mount-s3"] = mount_started
    sandbox.command_waiters["/usr/bin/mount-s3"] = asyncio.Event()
    secrets = ("test-access-key", "test-secret-key", "test-session-token")

    start_task = asyncio.create_task(session.start())
    await asyncio.wait_for(mount_started.wait(), timeout=1)
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await start_task

    traceback = exc_info.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            locals_repr = repr(traceback.tb_frame.f_locals)
            for secret in secrets:
                assert secret not in locals_repr
        traceback = traceback.tb_next
    assert sandbox.stop_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation_source", ["run_command", "stdout"])
async def test_vercel_s3_mount_cancellation_ignores_hostile_exception_descriptors(
    monkeypatch: pytest.MonkeyPatch,
    cancellation_source: Literal["run_command", "stdout"],
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module, credentials=True),
        options=vercel_module.VercelSandboxClientOptions(allow_s3_credential_exposure=True),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    sandbox.command_results = {
        "/usr/bin/test": [_FakeCommandFinished()],
        "/usr/bin/rpm": [_FakeCommandFinished(stdout="1.21.0")],
        "/usr/bin/find": [_FakeCommandFinished()],
    }

    class HostileCancelledError(asyncio.CancelledError):
        pass

    _install_hostile_exception_descriptors(
        HostileCancelledError,
        reject_stringification=True,
    )
    provider_error = HostileCancelledError("provider cancelled with test-secret-key")
    original_run_command = sandbox.run_command

    class CancelledOutput(_FakeCommandFinished):
        async def stdout(self) -> str:
            raise provider_error

    async def cancel_command(
        cmd: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sudo: bool = False,
    ) -> _FakeCommandFinished:
        if cmd == "/usr/bin/mount-s3":
            raise provider_error
        return await original_run_command(cmd, args, cwd=cwd, env=env, sudo=sudo)

    if cancellation_source == "run_command":
        monkeypatch.setattr(sandbox, "run_command", cancel_command)
    else:
        sandbox.command_results["/usr/bin/mount-s3"] = [CancelledOutput()]

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await session.start()

    assert type(exc_info.value) is asyncio.CancelledError
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    _assert_base_exception_slots_cleared(provider_error)
    assert sandbox.stop_calls == 1


@pytest.mark.asyncio
async def test_vercel_exec_timeout_includes_output_collection_and_releases_mount_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    sandbox = cast(_FakeAsyncSandbox, session._inner._sandbox)
    _queue_successful_s3_mounts(sandbox)
    await session.start()
    hold_output = asyncio.Event()

    class _BlockingOutputResult(_FakeCommandFinished):
        async def stdout(self) -> str:
            await hold_output.wait()
            return ""

    sandbox.command_results["slow"] = [_BlockingOutputResult()]

    with pytest.raises(vercel_module.ExecTimeoutError):
        await session.exec("slow", timeout=0.01, shell=False)

    sandbox.command_results.update(
        {
            "/usr/bin/findmnt": [_FakeCommandFinished(stdout="mountpoint-s3")],
            "/usr/bin/umount": [_FakeCommandFinished()],
        }
    )
    await asyncio.wait_for(session.shutdown(), timeout=1)


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
async def test_vercel_exec_marks_typed_not_found_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000120",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-exec-missing",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-exec-missing")
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    async def _raise_not_found(*args: object, **kwargs: object) -> object:
        _ = (args, kwargs)
        raise vercel_module.vercel_sandbox.SandboxNotFoundError("sandbox missing")

    monkeypatch.setattr(sandbox, "run_command", _raise_not_found)

    with pytest.raises(vercel_module.ExecTransportError) as exc_info:
        await session.exec("pwd", shell=False)

    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_vercel_exec_marks_typed_rate_limit_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000121",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-exec-rate-limit",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-exec-rate-limit")
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    async def _raise_rate_limit(*args: object, **kwargs: object) -> object:
        _ = (args, kwargs)
        raise vercel_module.vercel_sandbox.SandboxRateLimitError("rate limited")

    monkeypatch.setattr(sandbox, "run_command", _raise_rate_limit)

    with pytest.raises(vercel_module.ExecTransportError) as exc_info:
        await session.exec("pwd", shell=False)

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_vercel_write_marks_typed_validation_error_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    monkeypatch.setattr("agents.sandbox.util.retry.asyncio.sleep", _noop_sleep)

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000122",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-write-validation",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-write-validation")
    sandbox.write_failures = [vercel_module.vercel_sandbox.SandboxValidationError("invalid write")]
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(vercel_module.WorkspaceArchiveWriteError) as exc_info:
        await session.write(Path("hello.txt"), io.BytesIO(b"world"))

    assert len(sandbox.write_files_calls) == 1
    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    ("status", "expected_retryable"),
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (425, True),
        (422, False),
        (429, True),
        (500, True),
        (502, True),
        (503, True),
        (504, True),
    ],
)
def test_vercel_retryability_status_table(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_retryable: bool,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)

    class FakeStatusError(Exception):
        status_code = status

    assert vercel_module._vercel_provider_retryability(FakeStatusError()) is expected_retryable


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
    # Sandbox is already RUNNING, so wait_for_status should not be called.
    assert existing.wait_for_status_calls == []
    assert resumed._inner._workspace_state_preserved_on_start() is True  # noqa: SLF001
    assert resumed._inner._system_state_preserved_on_start() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_vercel_resume_waits_when_sandbox_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sandbox-existing", status="pending")
    _FakeAsyncSandbox.sandboxes[existing.sandbox_id] = existing

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000200",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.sandbox_id,
    )

    client = vercel_module.VercelSandboxClient()
    resumed = await client.resume(state)

    assert resumed._inner.state.sandbox_id == "sandbox-existing"
    assert _FakeAsyncSandbox.create_calls == []
    assert existing.wait_for_status_calls == [
        ("running", vercel_module.DEFAULT_VERCEL_WAIT_FOR_RUNNING_TIMEOUT_S)
    ]
    assert resumed._inner._workspace_state_preserved_on_start() is True  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status", ["stopping", "stopped", "failed", "aborted", "snapshotting"]
)
async def test_vercel_resume_recreates_sandbox_when_cannot_reach_running(
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: str,
) -> None:
    """A sandbox in any state that cannot transition to RUNNING must be recreated
    immediately, without waiting for the wait_for_status timeout."""
    vercel_module = _load_vercel_module(monkeypatch)
    existing = _FakeAsyncSandbox(sandbox_id="sandbox-terminal", status=terminal_status)
    _FakeAsyncSandbox.sandboxes[existing.sandbox_id] = existing

    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000201",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=existing.sandbox_id,
    )

    client = vercel_module.VercelSandboxClient()
    resumed = await client.resume(state)

    assert existing.wait_for_status_calls == []
    assert existing.client.closed is True
    assert len(_FakeAsyncSandbox.create_calls) == 1
    assert resumed._inner.state.sandbox_id != "sandbox-terminal"
    assert resumed._inner.state.workspace_root_ready is False
    assert resumed._inner._workspace_state_preserved_on_start() is False  # noqa: SLF001


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
    # Use "pending" so that the code enters the wait path (not already RUNNING).
    existing = _FakeAsyncSandbox(sandbox_id="sandbox-existing", status="pending")
    existing.wait_for_status_error = asyncio.TimeoutError()
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
    assert len(_FakeAsyncSandbox.create_calls) == 1
    assert resumed._inner.state.sandbox_id == session._inner.state.sandbox_id


@pytest.mark.asyncio
async def test_vercel_deserialize_discards_surviving_resource_identity_after_mount_erasure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module),
        options=vercel_module.VercelSandboxClientOptions(),
    )
    original_sandbox_id = session.state.sandbox_id
    payload = client.serialize_session_state(session.state)
    cast(dict[str, object], payload["manifest"])["entries"] = {}
    payload.pop("__openai_agents_redacted_mount_authority", None)
    payload["s3_mounts_non_resumable"] = False

    restored = client.deserialize_session_state(payload)
    assert restored.sandbox_id == ""
    assert restored.workspace_root_ready is False
    resumed = await client.resume(restored)

    assert restored.sandbox_id == resumed.state.sandbox_id
    assert restored.sandbox_id != original_sandbox_id
    assert restored.workspace_root_ready is False
    assert _FakeAsyncSandbox.get_calls == []
    assert len(_FakeAsyncSandbox.create_calls) == 2


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
async def test_vercel_hydrate_workspace_rejects_external_symlink_target_before_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000105",
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sandbox-hydrate-external-link",
        workspace_persistence="tar",
    )
    sandbox = _FakeAsyncSandbox(sandbox_id="sandbox-hydrate-external-link")
    session = vercel_module.VercelSandboxSession.from_state(state, sandbox=sandbox)

    unsafe_buf = io.BytesIO()
    with tarfile.open(fileobj=unsafe_buf, mode="w") as archive:
        info = tarfile.TarInfo(name="leak")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)

    with pytest.raises(vercel_module.WorkspaceArchiveWriteError) as exc_info:
        await session.hydrate_workspace(io.BytesIO(unsafe_buf.getvalue()))

    assert "absolute symlink target not allowed" in str(exc_info.value.__cause__)
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
async def test_vercel_tar_persistence_treats_mount_exclusions_as_literal_paths(
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
            "/workspace/cache[1]/mounted.txt": b"mounted-content",
            "/workspace/cache1/durable.txt": b"durable-content",
        },
    )
    state = vercel_module.VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000008",
        manifest=Manifest(root="/workspace", entries={"cache[1]": mount}),
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

    assert mount._events == [
        ("unmount", "/workspace/cache[1]"),
        ("mount", "/workspace/cache[1]"),
    ]
    assert tar_calls == [
        (
            "tar",
            [
                "cf",
                "/tmp/openai-agents-00000000000000000000000000000008.tar",
                "--no-wildcards",
                "--exclude=./cache[1]",
                ".",
            ],
            "/workspace",
        )
    ]
    assert archived_names == ["cache1/durable.txt", "kept.txt"]
    assert sandbox.files["/workspace/cache[1]/mounted.txt"] == b"mounted-content"


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


@pytest.mark.asyncio
async def test_vercel_direct_persist_redacts_protected_mount_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    session = await vercel_module.VercelSandboxClient().create(
        manifest=_vercel_s3_manifest(package_module, credentials=True),
        options=vercel_module.VercelSandboxClientOptions(allow_s3_credential_exposure=True),
    )
    inner = cast(Any, session._inner)
    sentinel = "direct-vercel-persist-secret"
    source_error = RuntimeError(f"provider echoed {sentinel}")

    async def run_operation(
        _session: object,
        operation: Callable[[], Any],
        **_kwargs: object,
    ) -> Any:
        return await operation()

    async def fail_persist() -> io.IOBase:
        raise source_error

    monkeypatch.setattr(vercel_module, "with_ephemeral_mounts_removed", run_operation)
    monkeypatch.setattr(inner, "_persist_workspace_internal", fail_persist)

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
        await inner.persist_workspace()

    assert sentinel not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert source_error.args == ()
    assert source_error.__traceback__ is None


@pytest.mark.asyncio
async def test_vercel_client_delete_propagates_redacted_shutdown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vercel_module = _load_vercel_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.vercel")
    client = vercel_module.VercelSandboxClient()
    session = await client.create(
        manifest=_vercel_s3_manifest(package_module, credentials=True),
        options=vercel_module.VercelSandboxClientOptions(allow_s3_credential_exposure=True),
    )
    sentinel = "vercel-delete-secret"
    source_error = RuntimeError(f"provider echoed {sentinel}")

    async def fail_shutdown() -> None:
        raise source_error

    monkeypatch.setattr(session._inner, "shutdown", fail_shutdown)

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
        await client.delete(session)

    assert sentinel not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert source_error.args == ()
    assert source_error.__traceback__ is None
