from __future__ import annotations

import importlib
import io
import json
import sys
import tarfile
import types
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from agents.sandbox import Manifest
from agents.sandbox.entries import RcloneMountPattern, S3Mount
from agents.sandbox.errors import (
    ConfigurationError,
    ExecTimeoutError,
    MountConfigError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
)
from agents.sandbox.manifest import Environment
from agents.sandbox.session.sandbox_client import BaseSandboxClientOptions
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot


class _FakeApiError(Exception):
    def __init__(self, *, status_code: int | None = None, body: object = None) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"status_code={status_code}, body={body}")


class _FakeExecResult:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out

    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class _FakeSandboxResponse:
    def __init__(
        self,
        *,
        sandbox_id: str,
        name: str,
        status: str = "running",
    ) -> None:
        self.id = sandbox_id
        self.name = name
        self.status = status


class _FakeSnapshotResponse:
    def __init__(self, *, name: str) -> None:
        self.name = name
        self.id = f"snapshot-{name}"
        self.status = "ready"


class _FakeHttpxClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeClientWrapper:
    def __init__(self, *, compute_url: str, headers: dict[str, str]) -> None:
        self._compute_url = compute_url
        self._headers = headers
        self.httpx_client = _FakeHttpxClient()

    def get_compute_url(self) -> str:
        return self._compute_url

    async def async_get_headers(self) -> dict[str, str]:
        return dict(self._headers)


class _FakeSandboxesClient:
    sandboxes: dict[str, _FakeSandboxResponse] = {}
    create_calls: list[dict[str, object]] = []
    get_calls: list[str] = []
    delete_calls: list[str] = []
    pause_calls: list[str] = []
    resume_calls: list[str] = []
    create_count = 0

    @classmethod
    def reset(cls) -> None:
        cls.sandboxes = {}
        cls.create_calls = []
        cls.get_calls = []
        cls.delete_calls = []
        cls.pause_calls = []
        cls.resume_calls = []
        cls.create_count = 0

    async def create_sandbox(
        self,
        *,
        name: str | None = None,
        image: str | None = None,
        vcpus: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
        snapshot_name: str | None = None,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        gateway_profile: str | None = None,
        cache_key: str | None = None,
        init: dict[str, str] | None = None,
    ) -> _FakeSandboxResponse:
        kwargs: dict[str, object] = {
            "name": name,
            "image": image,
            "vcpus": vcpus,
            "memory_mb": memory_mb,
            "disk_gb": disk_gb,
            "snapshot_name": snapshot_name,
            "env": env,
            "workdir": workdir,
            "gateway_profile": gateway_profile,
            "cache_key": cache_key,
            "init": init,
        }
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        type(self).create_count += 1
        type(self).create_calls.append(dict(kwargs))
        name = name or f"islo-{type(self).create_count}"
        sandbox = _FakeSandboxResponse(
            sandbox_id=f"sb-{type(self).create_count}",
            name=name,
        )
        type(self).sandboxes[name] = sandbox
        return sandbox

    async def get_sandbox(self, sandbox_name: str) -> _FakeSandboxResponse:
        type(self).get_calls.append(sandbox_name)
        sandbox = type(self).sandboxes.get(sandbox_name)
        if sandbox is None:
            raise _FakeApiError(status_code=404, body="missing")
        return sandbox

    async def delete_sandbox(self, sandbox_name: str) -> None:
        type(self).delete_calls.append(sandbox_name)
        type(self).sandboxes.pop(sandbox_name, None)

    async def pause_sandbox(self, sandbox_name: str) -> _FakeSandboxResponse:
        type(self).pause_calls.append(sandbox_name)
        sandbox = await self.get_sandbox(sandbox_name)
        sandbox.status = "paused"
        return sandbox

    async def resume_sandbox(self, sandbox_name: str) -> _FakeSandboxResponse:
        type(self).resume_calls.append(sandbox_name)
        sandbox = await self.get_sandbox(sandbox_name)
        sandbox.status = "running"
        return sandbox


class _FakeSnapshotsClient:
    create_calls: list[dict[str, object]] = []

    @classmethod
    def reset(cls) -> None:
        cls.create_calls = []

    async def create_snapshot(
        self,
        *,
        sandbox_name: str,
        name: str | None = None,
    ) -> _FakeSnapshotResponse:
        type(self).create_calls.append({"sandbox_name": sandbox_name, "name": name})
        name = name or "snapshot"
        return _FakeSnapshotResponse(name=name)


class _FakeAsyncIslo:
    instances: list[_FakeAsyncIslo] = []

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        compute_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url or "https://api.islo.dev"
        self.compute_url = compute_url or "https://ca.compute.islo.dev"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client_wrapper = _FakeClientWrapper(compute_url=self.compute_url, headers=headers)
        self.sandboxes = _FakeSandboxesClient()
        self.snapshots = _FakeSnapshotsClient()
        self.exec_calls: list[dict[str, object]] = []
        self.exec_results: list[_FakeExecResult | BaseException] = []
        type(self).instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        _FakeSandboxesClient.reset()
        _FakeSnapshotsClient.reset()


async def _fake_exec_and_wait(
    client: _FakeAsyncIslo,
    sandbox_name: str,
    command: list[str],
    **kwargs: object,
) -> _FakeExecResult:
    client.exec_calls.append({"sandbox_name": sandbox_name, "command": list(command), **kwargs})
    if client.exec_results:
        result = client.exec_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result
    return _FakeExecResult(stdout="ok\n")


def _command_str(command: list[str], user: object | None = None) -> str:
    command_text = " ".join(command)
    if user is None:
        return command_text
    return f"sudo -u {user} -- {command_text}"


async def _fake_get_client_internals(client: _FakeAsyncIslo) -> tuple[str, dict[str, str]]:
    return (
        client._client_wrapper.get_compute_url(),
        await client._client_wrapper.async_get_headers(),
    )


class _FakeHttpResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b"",
        json_body: dict[str, object] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self._json_body = json_body
        self.text = (
            json.dumps(json_body)
            if json_body is not None
            else content.decode("utf-8", errors="replace")
        )

    def json(self) -> object:
        if self._json_body is not None:
            return self._json_body
        return json.loads(self.text)


class _FakeAsyncHttpClient:
    get_responses: list[_FakeHttpResponse] = []
    post_responses: list[_FakeHttpResponse] = []
    calls: list[dict[str, object]] = []

    @classmethod
    def reset(cls) -> None:
        cls.get_responses = []
        cls.post_responses = []
        cls.calls = []

    async def __aenter__(self) -> _FakeAsyncHttpClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, **kwargs: object) -> _FakeHttpResponse:
        type(self).calls.append({"method": "GET", "url": url, **kwargs})
        if type(self).get_responses:
            return type(self).get_responses.pop(0)
        return _FakeHttpResponse(content=_valid_tar_bytes())

    async def post(self, url: str, **kwargs: object) -> _FakeHttpResponse:
        type(self).calls.append({"method": "POST", "url": url, **kwargs})
        if type(self).post_responses:
            return type(self).post_responses.pop(0)
        return _FakeHttpResponse()


def _valid_tar_bytes() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="hello.txt")
        data = b"hello"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _unsafe_tar_bytes() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="../escape.txt")
        data = b"bad"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _load_islo_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    _FakeAsyncIslo.reset()
    _FakeAsyncHttpClient.reset()

    fake_islo = types.ModuleType("islo")
    cast(Any, fake_islo).AsyncIslo = _FakeAsyncIslo
    fake_custom = types.ModuleType("islo.custom")
    fake_exec = types.ModuleType("islo.custom.exec")
    cast(Any, fake_exec).exec_and_wait = _fake_exec_and_wait
    fake_files = types.ModuleType("islo.custom.files")
    cast(Any, fake_files)._async_get_client_internals = _fake_get_client_internals
    fake_core = types.ModuleType("islo.core")
    fake_api_error = types.ModuleType("islo.core.api_error")
    cast(Any, fake_api_error).ApiError = _FakeApiError

    monkeypatch.setitem(sys.modules, "islo", fake_islo)
    monkeypatch.setitem(sys.modules, "islo.custom", fake_custom)
    monkeypatch.setitem(sys.modules, "islo.custom.exec", fake_exec)
    monkeypatch.setitem(sys.modules, "islo.custom.files", fake_files)
    monkeypatch.setitem(sys.modules, "islo.core", fake_core)
    monkeypatch.setitem(sys.modules, "islo.core.api_error", fake_api_error)

    sys.modules.pop("agents.extensions.sandbox.islo.sandbox", None)
    sys.modules.pop("agents.extensions.sandbox.islo", None)
    sys.modules.pop("agents.extensions.sandbox", None)
    module = importlib.import_module("agents.extensions.sandbox.islo.sandbox")
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncHttpClient)
    return module


def _make_state(islo_module: Any, **overrides: object) -> Any:
    data: dict[str, object] = {
        "session_id": uuid.uuid4(),
        "manifest": Manifest(root="/workspace"),
        "snapshot": NoopSnapshot(id="snapshot"),
        "sandbox_id": "sb-1",
        "sandbox_name": "islo-1",
        "base_url": "https://api.islo.dev",
        "compute_url": "https://ca.compute.islo.dev",
    }
    data.update(overrides)
    return islo_module.IsloSandboxSessionState(**data)


class _FakeIsloMountSession:
    def __init__(
        self,
        islo_module: Any,
        *,
        command_results: dict[str, list[_FakeExecResult]] | None = None,
    ) -> None:
        self.state = _make_state(islo_module)
        self.exec_calls: list[str] = []
        self.mkdir_calls: list[Path] = []
        self.write_calls: list[tuple[Path, bytes]] = []
        self.skip_paths: list[Path] = []
        self._command_results = {
            command: list(results) for command, results in (command_results or {}).items()
        }

    async def exec(
        self,
        *command: str,
        shell: bool = False,
        timeout: float | None = None,
        user: str | None = None,
    ) -> _FakeExecResult:
        _ = (shell, timeout)
        command_text = _command_str(list(command), user)
        self.exec_calls.append(command_text)
        results = self._command_results.get(command_text)
        if results:
            return results.pop(0)
        return _FakeExecResult()

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | None = None,
    ) -> None:
        _ = (parents, user)
        self.mkdir_calls.append(Path(path))

    async def write(
        self,
        path: Path | str,
        data: io.IOBase,
        *,
        user: str | None = None,
    ) -> None:
        _ = user
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.write_calls.append((Path(path), bytes(payload)))

    def register_persist_workspace_skip_path(self, path: Path | str) -> None:
        self.skip_paths.append(Path(path))

    def normalize_path(self, path: Path | str) -> Path:
        return Path(path)

    async def _exec_checked_nonzero(self, *command: str | Path) -> _FakeExecResult:
        result = await self.exec(*(str(part) for part in command), shell=False)
        if not result.ok():
            raise RuntimeError(f"command failed: {command!r}")
        return result


def _successful_mount_command_results() -> dict[str, list[_FakeExecResult]]:
    return {
        "sh -lc test -c /dev/fuse": [_FakeExecResult()],
        "sh -lc grep -qw fuse /proc/filesystems": [_FakeExecResult()],
        "sh -lc command -v fusermount3 >/dev/null 2>&1 || test -x /usr/local/bin/fusermount3": [
            _FakeExecResult()
        ],
        "sh -lc command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone": [
            _FakeExecResult()
        ],
    }


def _make_recorded_islo_mount_session(
    monkeypatch: pytest.MonkeyPatch,
    islo_module: Any,
    *,
    command_results: dict[str, list[_FakeExecResult]] | None = None,
) -> tuple[Any, _FakeIsloMountSession]:
    session = islo_module.IsloSandboxSession.from_state(
        _make_state(islo_module),
        client=_FakeAsyncIslo(),
    )
    recorder = _FakeIsloMountSession(islo_module, command_results=command_results)
    monkeypatch.setattr(session, "exec", recorder.exec)
    monkeypatch.setattr(session, "mkdir", recorder.mkdir)
    monkeypatch.setattr(session, "write", recorder.write)
    monkeypatch.setattr(session, "_exec_checked_nonzero", recorder._exec_checked_nonzero)
    monkeypatch.setattr(
        session,
        "register_persist_workspace_skip_path",
        recorder.register_persist_workspace_skip_path,
    )
    monkeypatch.setattr(session, "normalize_path", recorder.normalize_path)
    return session, recorder


def test_islo_package_re_exports_backend_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    islo_module = _load_islo_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.islo")
    parent_module = importlib.import_module("agents.extensions.sandbox")

    assert package_module.IsloSandboxClient is islo_module.IsloSandboxClient
    assert parent_module.IsloSandboxClient is islo_module.IsloSandboxClient
    assert "IsloSandboxClient" in parent_module.__all__


def test_islo_options_round_trip_through_base_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    islo_module = _load_islo_module(monkeypatch)
    options = islo_module.IsloSandboxClientOptions(
        base_url="https://api.test",
        compute_url="https://compute.test",
        name="agents-test",
        image="ubuntu:24.04",
        vcpus=4,
        memory_mb=8192,
        disk_gb=20,
        snapshot_name="base-snapshot",
        env={"ONLY_OPTION": "1"},
        workdir="repo",
        gateway_profile="locked-down",
        cache_key="cache-key",
        pause_on_exit=True,
        workspace_persistence="snapshot",
    )

    payload = options.model_dump(mode="json")
    restored = BaseSandboxClientOptions.parse(payload)

    assert "api_key" not in payload
    assert "exposed_ports" not in payload
    assert (type(restored).__module__, type(restored).__qualname__) == (
        islo_module.IsloSandboxClientOptions.__module__,
        islo_module.IsloSandboxClientOptions.__qualname__,
    )
    assert restored.model_dump(mode="json") == payload


def test_islo_session_state_round_trip_through_base_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)
    state = _make_state(
        islo_module,
        compute_url="https://compute.test",
        name="agents-test",
        image="ubuntu:24.04",
        vcpus=4,
        memory_mb=8192,
        disk_gb=20,
        snapshot_name="base-snapshot",
        base_env={"ONLY_OPTION": "1"},
        workdir="repo",
        gateway_profile="locked-down",
        cache_key="cache-key",
        pause_on_exit=True,
        workspace_persistence="snapshot",
    )

    payload = state.model_dump(mode="json")
    restored = SandboxSessionState.parse(payload)

    assert "api_key" not in payload
    assert type(restored) is islo_module.IsloSandboxSessionState
    assert restored.model_dump(mode="json") == payload


@pytest.mark.asyncio
async def test_create_forwards_options_and_omits_api_key_from_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)

    client = islo_module.IsloSandboxClient(api_key="client-key")
    session = await client.create(
        options=islo_module.IsloSandboxClientOptions(
            base_url="https://api.test",
            compute_url="https://compute.test",
            name="agents-test",
            image="ubuntu:24.04",
            vcpus=4,
            memory_mb=8192,
            disk_gb=20,
            snapshot_name="base-snapshot",
            env={"ONLY_OPTION": "1"},
            workdir="repo",
            gateway_profile="locked-down",
            cache_key="cache-key",
            pause_on_exit=True,
        )
    )

    assert _FakeAsyncIslo.instances[-1].api_key == "client-key"
    assert _FakeAsyncIslo.instances[-1].base_url == "https://api.test"
    assert _FakeAsyncIslo.instances[-1].compute_url == "https://compute.test"
    assert _FakeSandboxesClient.create_calls == [
        {
            "init": {"type": "minimal"},
            "name": "agents-test",
            "image": "ubuntu:24.04",
            "vcpus": 4,
            "memory_mb": 8192,
            "disk_gb": 20,
            "snapshot_name": "base-snapshot",
            "env": {"ONLY_OPTION": "1"},
            "workdir": "repo",
            "gateway_profile": "locked-down",
            "cache_key": "cache-key",
        }
    ]
    assert session.state.manifest.root == islo_module.DEFAULT_ISLO_WORKSPACE_ROOT
    assert "api_key" not in session.state.model_dump(mode="json")
    assert session.state.base_url == "https://api.test"
    assert session.state.compute_url == "https://compute.test"
    assert session.state.exposed_ports == ()
    assert session.state.pause_on_exit is True


@pytest.mark.asyncio
async def test_create_omits_default_compute_url_for_published_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)

    class _PublishedAsyncIslo(_FakeAsyncIslo):
        def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
            super().__init__(api_key=api_key, base_url=base_url)

    monkeypatch.setattr(islo_module, "_import_islo_sdk", lambda: _PublishedAsyncIslo)

    session = await islo_module.IsloSandboxClient(api_key="client-key").create(
        options=islo_module.IsloSandboxClientOptions(name="published")
    )

    assert _FakeAsyncIslo.instances[-1].base_url == "https://api.islo.dev"
    assert session.state.compute_url is None


@pytest.mark.asyncio
async def test_create_rejects_compute_url_when_sdk_does_not_support_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)

    class _PublishedAsyncIslo(_FakeAsyncIslo):
        def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
            super().__init__(api_key=api_key, base_url=base_url)

    monkeypatch.setattr(islo_module, "_import_islo_sdk", lambda: _PublishedAsyncIslo)

    with pytest.raises(ConfigurationError, match="compute_url requires"):
        await islo_module.IsloSandboxClient(api_key="client-key").create(
            options=islo_module.IsloSandboxClientOptions(
                name="published",
                compute_url="https://compute.test",
            )
        )


@pytest.mark.asyncio
async def test_create_uses_legacy_init_capabilities_when_init_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)
    create_calls: list[dict[str, object]] = []

    class _LegacySandboxesClient:
        async def create_sandbox(
            self,
            *,
            name: str | None = None,
            init_capabilities: list[str] | None = None,
        ) -> _FakeSandboxResponse:
            create_calls.append({"name": name, "init_capabilities": init_capabilities})
            return _FakeSandboxResponse(sandbox_id="sb-legacy", name=name or "legacy")

    class _LegacyAsyncIslo(_FakeAsyncIslo):
        def __init__(
            self,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
            compute_url: str | None = None,
        ) -> None:
            super().__init__(api_key=api_key, base_url=base_url, compute_url=compute_url)
            cast(Any, self).sandboxes = _LegacySandboxesClient()

    monkeypatch.setattr(islo_module, "_import_islo_sdk", lambda: _LegacyAsyncIslo)

    client = islo_module.IsloSandboxClient(api_key="client-key")
    await client.create(
        options=islo_module.IsloSandboxClientOptions(
            name="legacy",
        )
    )

    assert create_calls == [{"name": "legacy", "init_capabilities": []}]


@pytest.mark.asyncio
async def test_create_errors_when_explicit_init_is_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)

    class _UnsupportedSandboxesClient:
        async def create_sandbox(self, *, name: str | None = None) -> _FakeSandboxResponse:
            return _FakeSandboxResponse(sandbox_id="sb-unsupported", name=name or "unsupported")

    class _UnsupportedAsyncIslo(_FakeAsyncIslo):
        def __init__(
            self,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
            compute_url: str | None = None,
        ) -> None:
            super().__init__(api_key=api_key, base_url=base_url, compute_url=compute_url)
            cast(Any, self).sandboxes = _UnsupportedSandboxesClient()

    monkeypatch.setattr(islo_module, "_import_islo_sdk", lambda: _UnsupportedAsyncIslo)

    client = islo_module.IsloSandboxClient(api_key="client-key")
    with pytest.raises(ConfigurationError, match="explicit init"):
        await client.create(options=islo_module.IsloSandboxClientOptions(name="unsupported"))


@pytest.mark.asyncio
async def test_exec_merges_manifest_env_and_maps_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)
    state = _make_state(
        islo_module,
        manifest=Manifest(
            root="/workspace",
            environment=Environment(value={"SHARED": "manifest", "ONLY_MANIFEST": "1"}),
        ),
        base_env={"SHARED": "option", "ONLY_OPTION": "1"},
    )
    client = _FakeAsyncIslo()
    client.exec_results.append(_FakeExecResult(stdout="hello", stderr="", exit_code=0))
    session = islo_module.IsloSandboxSession.from_state(state, client=client)

    result = await session.exec("echo", "hello", shell=False, user="islo")

    assert result.stdout == b"hello"
    assert client.exec_calls[-1]["command"] == ["echo", "hello"]
    assert client.exec_calls[-1]["user"] == "islo"
    assert client.exec_calls[-1]["workdir"] == "/workspace"
    assert client.exec_calls[-1]["env"] == {
        "SHARED": "manifest",
        "ONLY_OPTION": "1",
        "ONLY_MANIFEST": "1",
    }

    client.exec_results.append(_FakeExecResult(timed_out=True, exit_code=-1))
    with pytest.raises(ExecTimeoutError):
        await session.exec("sleep", "999", shell=False, timeout=0.01)


@pytest.mark.asyncio
async def test_read_and_write_use_islo_file_http(monkeypatch: pytest.MonkeyPatch) -> None:
    islo_module = _load_islo_module(monkeypatch)
    state = _make_state(islo_module)
    client = _FakeAsyncIslo(
        api_key="key",
        base_url="https://api.test",
        compute_url="https://compute.test",
    )
    session = islo_module.IsloSandboxSession.from_state(state, client=client)
    _FakeAsyncHttpClient.get_responses.append(_FakeHttpResponse(content=b"file contents"))

    read_result = await session.read("notes.txt")
    await session.write("notes.txt", io.BytesIO(b"updated"))

    assert read_result.read() == b"file contents"
    assert _FakeAsyncHttpClient.calls[0]["method"] == "GET"
    assert _FakeAsyncHttpClient.calls[0]["url"] == "https://compute.test/sandboxes/islo-1/files"
    assert _FakeAsyncHttpClient.calls[0]["params"] == {"path": "/workspace/notes.txt"}
    assert _FakeAsyncHttpClient.calls[1]["method"] == "POST"
    assert _FakeAsyncHttpClient.calls[1]["url"] == "https://compute.test/sandboxes/islo-1/files"
    assert _FakeAsyncHttpClient.calls[1]["params"] == {"path": "/workspace/notes.txt"}


@pytest.mark.asyncio
async def test_write_with_user_copies_uploaded_payload_as_requested_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)
    state = _make_state(islo_module)
    client = _FakeAsyncIslo(api_key="key", base_url="https://api.test")
    session = islo_module.IsloSandboxSession.from_state(state, client=client)

    await session.write("owned.txt", io.BytesIO(b"owned"), user="islo")

    assert _FakeAsyncHttpClient.calls[0]["method"] == "POST"
    upload_params = cast(dict[str, str], _FakeAsyncHttpClient.calls[0]["params"])
    temp_path = upload_params["path"]
    assert temp_path.startswith("/tmp/openai-agents-islo-write-")
    user_write_call = next(
        call
        for call in client.exec_calls
        if call.get("user") == "islo" and cast(list[str], call["command"])[2] == 'cat "$1" > "$2"'
    )
    assert user_write_call["command"] == [
        "sh",
        "-lc",
        'cat "$1" > "$2"',
        "sh",
        temp_path,
        "/workspace/owned.txt",
    ]


@pytest.mark.asyncio
async def test_read_maps_404_to_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    islo_module = _load_islo_module(monkeypatch)
    state = _make_state(islo_module)
    client = _FakeAsyncIslo()
    session = islo_module.IsloSandboxSession.from_state(state, client=client)
    _FakeAsyncHttpClient.get_responses.append(
        _FakeHttpResponse(status_code=404, json_body={"detail": "missing"})
    )

    with pytest.raises(WorkspaceReadNotFoundError):
        await session.read("missing.txt")


@pytest.mark.asyncio
async def test_tar_persist_uses_excludes_and_downloads_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)
    state = _make_state(islo_module)
    client = _FakeAsyncIslo()
    session = islo_module.IsloSandboxSession.from_state(state, client=client)
    session.register_persist_workspace_skip_path("runtime.tmp")
    _FakeAsyncHttpClient.get_responses.append(_FakeHttpResponse(content=_valid_tar_bytes()))

    archive = await session.persist_workspace()

    assert archive.read() == _valid_tar_bytes()
    tar_command = cast(list[str], client.exec_calls[0]["command"])[2]
    assert "--exclude=runtime.tmp" in tar_command
    assert "-C /workspace" in tar_command
    assert _FakeAsyncHttpClient.calls[0]["method"] == "GET"


@pytest.mark.asyncio
async def test_hydrate_rejects_unsafe_tar_before_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    islo_module = _load_islo_module(monkeypatch)
    state = _make_state(islo_module)
    client = _FakeAsyncIslo()
    session = islo_module.IsloSandboxSession.from_state(state, client=client)

    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(_unsafe_tar_bytes()))

    assert _FakeAsyncHttpClient.calls == []


@pytest.mark.asyncio
async def test_resume_reconnects_paused_and_recreates_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)
    client = islo_module.IsloSandboxClient()

    _FakeSandboxesClient.sandboxes["paused"] = _FakeSandboxResponse(
        sandbox_id="sb-paused",
        name="paused",
        status="paused",
    )
    paused_state = _make_state(islo_module, sandbox_id="sb-paused", sandbox_name="paused")
    paused = await client.resume(paused_state)

    assert _FakeSandboxesClient.resume_calls == ["paused"]
    assert paused._inner._workspace_state_preserved_on_start() is True  # noqa: SLF001

    missing_state = _make_state(islo_module, sandbox_id="sb-missing", sandbox_name="missing")
    missing = await client.resume(missing_state)

    assert _FakeSandboxesClient.create_calls[-1]["name"] == "missing"
    assert _FakeSandboxesClient.create_calls[-1]["init"] == {"type": "minimal"}
    assert missing.state.sandbox_id != "sb-missing"
    assert missing._inner._workspace_state_preserved_on_start() is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_resume_recreates_stale_running_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)

    class _StaleAsyncIslo(_FakeAsyncIslo):
        def __init__(
            self,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
            compute_url: str | None = None,
        ) -> None:
            super().__init__(api_key=api_key, base_url=base_url, compute_url=compute_url)
            self.exec_results.append(_FakeApiError(status_code=404, body="VM not found"))

    monkeypatch.setattr(islo_module, "_import_islo_sdk", lambda: _StaleAsyncIslo)
    _FakeSandboxesClient.sandboxes["stale"] = _FakeSandboxResponse(
        sandbox_id="sb-stale",
        name="stale",
        status="running",
    )
    stale_state = _make_state(
        islo_module,
        sandbox_id="sb-stale",
        sandbox_name="stale",
        name="stale",
        workspace_root_ready=True,
    )

    resumed = await islo_module.IsloSandboxClient().resume(stale_state)

    assert _FakeSandboxesClient.create_calls[-1]["name"] == "stale"
    assert resumed.state.sandbox_id != "sb-stale"
    assert resumed._inner._workspace_state_preserved_on_start() is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_resume_recreates_with_generated_name_when_old_name_is_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)
    create_calls: list[dict[str, object]] = []

    class _ReservedNameSandboxesClient:
        async def get_sandbox(self, sandbox_name: str) -> _FakeSandboxResponse:
            return _FakeSandboxResponse(
                sandbox_id="sb-stale",
                name=sandbox_name,
                status="running",
            )

        async def create_sandbox(
            self,
            *,
            name: str | None = None,
            init: dict[str, str] | None = None,
        ) -> _FakeSandboxResponse:
            create_calls.append({"name": name, "init": init})
            if name == "stale":
                raise _FakeApiError(
                    status_code=400,
                    body={"message": "Sandbox 'stale' already exists for tenant"},
                )
            return _FakeSandboxResponse(sandbox_id="sb-recreated", name=name or "generated")

    class _StaleReservedNameAsyncIslo(_FakeAsyncIslo):
        def __init__(
            self,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
            compute_url: str | None = None,
        ) -> None:
            super().__init__(api_key=api_key, base_url=base_url, compute_url=compute_url)
            cast(Any, self).sandboxes = _ReservedNameSandboxesClient()
            self.exec_results.append(_FakeApiError(status_code=404, body="VM not found"))

    monkeypatch.setattr(islo_module, "_import_islo_sdk", lambda: _StaleReservedNameAsyncIslo)
    stale_state = _make_state(
        islo_module,
        sandbox_id="sb-stale",
        sandbox_name="stale",
        name="stale",
        workspace_root_ready=True,
    )

    resumed = await islo_module.IsloSandboxClient().resume(stale_state)

    assert create_calls == [
        {"name": "stale", "init": {"type": "minimal"}},
        {"name": None, "init": {"type": "minimal"}},
    ]
    assert resumed.state.sandbox_id == "sb-recreated"
    assert resumed.state.sandbox_name == "generated"
    assert resumed._inner._workspace_state_preserved_on_start() is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_snapshot_mode_persists_reference_and_hydrates_by_recreate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)
    state = _make_state(islo_module, workspace_persistence="snapshot")
    _FakeSandboxesClient.sandboxes["islo-1"] = _FakeSandboxResponse(
        sandbox_id="sb-1",
        name="islo-1",
    )
    client = _FakeAsyncIslo()
    session = islo_module.IsloSandboxSession.from_state(state, client=client)

    snapshot_ref = await session.persist_workspace()
    payload = snapshot_ref.read()
    assert payload.startswith(b"ISLO_SANDBOX_SNAPSHOT_V1\n")
    assert _FakeSnapshotsClient.create_calls[0]["sandbox_name"] == "islo-1"

    await session.hydrate_workspace(io.BytesIO(payload))

    assert _FakeSandboxesClient.delete_calls == ["islo-1"]
    restored_snapshot_name = cast(str, _FakeSandboxesClient.create_calls[-1]["snapshot_name"])
    assert restored_snapshot_name.startswith("openai-agents-")
    assert _FakeSandboxesClient.create_calls[-1]["init"] == {"type": "minimal"}


@pytest.mark.asyncio
async def test_snapshot_restore_recreates_with_generated_name_when_old_name_is_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    islo_module = _load_islo_module(monkeypatch)
    create_calls: list[dict[str, object]] = []

    class _ReservedNameSandboxesClient:
        async def delete_sandbox(self, sandbox_name: str) -> None:
            assert sandbox_name == "stale"

        async def create_sandbox(
            self,
            *,
            name: str | None = None,
            snapshot_name: str | None = None,
            init: dict[str, str] | None = None,
        ) -> _FakeSandboxResponse:
            create_calls.append({"name": name, "snapshot_name": snapshot_name, "init": init})
            if name == "stale":
                raise _FakeApiError(
                    status_code=400,
                    body={"message": "Sandbox 'stale' already exists for tenant"},
                )
            return _FakeSandboxResponse(sandbox_id="sb-recreated", name=name or "generated")

    class _ReservedNameAsyncIslo(_FakeAsyncIslo):
        def __init__(
            self,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
            compute_url: str | None = None,
        ) -> None:
            super().__init__(api_key=api_key, base_url=base_url, compute_url=compute_url)
            cast(Any, self).sandboxes = _ReservedNameSandboxesClient()

    monkeypatch.setattr(islo_module, "_import_islo_sdk", lambda: _ReservedNameAsyncIslo)
    state = _make_state(
        islo_module,
        sandbox_id="sb-stale",
        sandbox_name="stale",
        name="stale",
        workspace_persistence="snapshot",
    )
    session = islo_module.IsloSandboxSession.from_state(state, client=_ReservedNameAsyncIslo())
    snapshot_ref = b'ISLO_SANDBOX_SNAPSHOT_V1\n{"snapshot_name":"snap-1"}'

    await session.hydrate_workspace(io.BytesIO(snapshot_ref))

    assert create_calls == [
        {"name": "stale", "snapshot_name": "snap-1", "init": {"type": "minimal"}},
        {"name": None, "snapshot_name": "snap-1", "init": {"type": "minimal"}},
    ]
    assert session.state.sandbox_id == "sb-recreated"
    assert session.state.sandbox_name == "generated"


# ---------------------------------------------------------------------------
# IsloCloudBucketMountStrategy tests
# ---------------------------------------------------------------------------


def test_islo_cloud_bucket_mount_strategy_re_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_islo_module(monkeypatch)
    from agents.extensions.sandbox import IsloCloudBucketMountStrategy as TopLevelExport
    from agents.extensions.sandbox.islo import IsloCloudBucketMountStrategy

    assert IsloCloudBucketMountStrategy is TopLevelExport
    sandbox_extensions = __import__("agents.extensions.sandbox", fromlist=["__all__"])
    assert "IsloCloudBucketMountStrategy" in sandbox_extensions.__all__


def test_islo_cloud_bucket_mount_strategy_type_field() -> None:
    from agents.extensions.sandbox.islo.mounts import IsloCloudBucketMountStrategy

    strategy = IsloCloudBucketMountStrategy()
    assert strategy.type == "islo_cloud_bucket"


def test_islo_cloud_bucket_mount_strategy_round_trips_through_registry() -> None:
    from agents.extensions.sandbox.islo.mounts import IsloCloudBucketMountStrategy
    from agents.sandbox.entries.mounts.base import MountStrategyBase

    strategy = IsloCloudBucketMountStrategy()
    payload = strategy.model_dump(mode="json")

    restored = MountStrategyBase.parse(payload)

    assert payload["type"] == "islo_cloud_bucket"
    assert type(restored) is IsloCloudBucketMountStrategy
    assert restored.model_dump(mode="json") == payload


def test_islo_cloud_bucket_mount_strategy_rejects_wrong_session() -> None:
    import importlib

    mounts_module = importlib.import_module("agents.extensions.sandbox.islo.mounts")

    class _NotIsloSession:
        pass

    with pytest.raises(MountConfigError, match="IsloSandboxSession"):
        mounts_module._assert_islo_session(_NotIsloSession())


def test_islo_cloud_bucket_mount_strategy_accepts_islo_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    islo_module = _load_islo_module(monkeypatch)
    mounts_module = importlib.import_module("agents.extensions.sandbox.islo.mounts")
    state = _make_state(islo_module)
    session = islo_module.IsloSandboxSession.from_state(state, client=_FakeAsyncIslo())

    mounts_module._assert_islo_session(session)


@pytest.mark.asyncio
async def test_islo_ensure_rclone_installs_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    islo_module = _load_islo_module(monkeypatch)
    mounts_module = importlib.import_module("agents.extensions.sandbox.islo.mounts")
    rclone_check = "sh -lc command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone"
    apt_check = "sh -lc command -v apt-get >/dev/null 2>&1 || test -x /usr/local/bin/apt-get"
    install = (
        "sudo -u root -- sh -lc apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rclone"
    )
    session = _FakeIsloMountSession(
        islo_module,
        command_results={
            rclone_check: [_FakeExecResult(exit_code=1), _FakeExecResult()],
            apt_check: [_FakeExecResult()],
            install: [_FakeExecResult()],
        },
    )

    await mounts_module._ensure_rclone(session)

    assert session.exec_calls == [rclone_check, apt_check, install, rclone_check]


@pytest.mark.asyncio
async def test_islo_ensure_rclone_errors_without_package_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    islo_module = _load_islo_module(monkeypatch)
    mounts_module = importlib.import_module("agents.extensions.sandbox.islo.mounts")
    session = _FakeIsloMountSession(
        islo_module,
        command_results={
            "sh -lc command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone": [
                _FakeExecResult(exit_code=1)
            ],
            "sh -lc command -v apt-get >/dev/null 2>&1 || test -x /usr/local/bin/apt-get": [
                _FakeExecResult(exit_code=1)
            ],
            "sh -lc command -v apk >/dev/null 2>&1 || test -x /usr/local/bin/apk": [
                _FakeExecResult(exit_code=1)
            ],
        },
    )

    with pytest.raises(MountConfigError, match="no supported package manager"):
        await mounts_module._ensure_rclone(session)


@pytest.mark.asyncio
async def test_islo_ensure_rclone_errors_after_failed_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    islo_module = _load_islo_module(monkeypatch)
    mounts_module = importlib.import_module("agents.extensions.sandbox.islo.mounts")
    install = (
        "sudo -u root -- sh -lc apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rclone"
    )
    session = _FakeIsloMountSession(
        islo_module,
        command_results={
            "sh -lc command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone": [
                _FakeExecResult(exit_code=1)
            ],
            "sh -lc command -v apt-get >/dev/null 2>&1 || test -x /usr/local/bin/apt-get": [
                _FakeExecResult()
            ],
            install: [
                _FakeExecResult(exit_code=100),
                _FakeExecResult(exit_code=100),
                _FakeExecResult(exit_code=100),
            ],
        },
    )

    with pytest.raises(MountConfigError, match="failed to install rclone"):
        await mounts_module._ensure_rclone(session)

    assert session.exec_calls.count(install) == 3


@pytest.mark.asyncio
async def test_islo_ensure_fuse_support_errors_when_dev_fuse_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    islo_module = _load_islo_module(monkeypatch)
    mounts_module = importlib.import_module("agents.extensions.sandbox.islo.mounts")
    session = _FakeIsloMountSession(
        islo_module,
        command_results={"sh -lc test -c /dev/fuse": [_FakeExecResult(exit_code=1)]},
    )

    with pytest.raises(MountConfigError, match="/dev/fuse"):
        await mounts_module._ensure_fuse_support(session)


@pytest.mark.asyncio
async def test_islo_ensure_fuse_support_installs_fuse3_when_fusermount_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    islo_module = _load_islo_module(monkeypatch)
    mounts_module = importlib.import_module("agents.extensions.sandbox.islo.mounts")
    fusermount3_check = (
        "sh -lc command -v fusermount3 >/dev/null 2>&1 || test -x /usr/local/bin/fusermount3"
    )
    fuse_install = (
        "sudo -u root -- sh -lc apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fuse3"
    )
    session = _FakeIsloMountSession(
        islo_module,
        command_results={
            "sh -lc test -c /dev/fuse": [_FakeExecResult()],
            "sh -lc grep -qw fuse /proc/filesystems": [_FakeExecResult()],
            fusermount3_check: [_FakeExecResult(exit_code=1), _FakeExecResult()],
            "sh -lc command -v fusermount >/dev/null 2>&1 || test -x /usr/local/bin/fusermount": [
                _FakeExecResult(exit_code=1)
            ],
            "sh -lc command -v apt-get >/dev/null 2>&1 || test -x /usr/local/bin/apt-get": [
                _FakeExecResult()
            ],
            fuse_install: [_FakeExecResult()],
        },
    )

    await mounts_module._ensure_fuse_support(session)

    assert fuse_install in session.exec_calls


@pytest.mark.asyncio
async def test_islo_cloud_bucket_mount_strategy_activate_delegates_to_rclone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    islo_module = _load_islo_module(monkeypatch)
    mounts_module = importlib.import_module("agents.extensions.sandbox.islo.mounts")
    strategy = mounts_module.IsloCloudBucketMountStrategy()
    mount = S3Mount(
        bucket="test-bucket",
        prefix="fixtures",
        access_key_id="access-key",
        secret_access_key="secret-key",
        mount_strategy=strategy,
    )
    session, recorder = _make_recorded_islo_mount_session(
        monkeypatch,
        islo_module,
        command_results=_successful_mount_command_results(),
    )

    result = await strategy.activate(mount, session, Path("data"), Path("/workspace"))

    session_id = session.state.session_id.hex
    config_dir = Path(f".sandbox-rclone-config/{session_id}")
    assert result == []
    assert Path("/workspace/data") in recorder.mkdir_calls
    assert config_dir in recorder.mkdir_calls
    assert config_dir in recorder.skip_paths
    assert recorder.write_calls[0][0] == config_dir / f"sandbox_s3_{session_id}.conf"
    assert "secret_access_key = secret-key" in recorder.write_calls[0][1].decode("utf-8")
    assert any(
        call.startswith(f"rclone mount sandbox_s3_{session_id}:test-bucket/fixtures ")
        for call in recorder.exec_calls
    )


@pytest.mark.asyncio
async def test_islo_cloud_bucket_mount_strategy_restore_after_snapshot_replays_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    islo_module = _load_islo_module(monkeypatch)
    mounts_module = importlib.import_module("agents.extensions.sandbox.islo.mounts")
    strategy = mounts_module.IsloCloudBucketMountStrategy(pattern=RcloneMountPattern(mode="fuse"))
    mount = S3Mount(
        bucket="test-bucket",
        mount_strategy=strategy,
    )
    session, recorder = _make_recorded_islo_mount_session(
        monkeypatch,
        islo_module,
        command_results=_successful_mount_command_results(),
    )

    await strategy.restore_after_snapshot(mount, session, Path("/workspace/restored"))

    session_id = session.state.session_id.hex
    assert "sh -lc test -c /dev/fuse" in recorder.exec_calls
    assert "sh -lc command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone" in (
        recorder.exec_calls
    )
    assert any(
        call.startswith(f"rclone mount sandbox_s3_{session_id}:test-bucket /workspace/restored")
        for call in recorder.exec_calls
    )
