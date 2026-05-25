from __future__ import annotations

import asyncio
import io
import json
import sys
import tarfile
import time
import types
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("agents.sandbox")

from agents.sandbox.entries import File
from agents.sandbox.errors import (
    ExecTransportError,
    ExposedPortUnavailableError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
)
from agents.sandbox.manifest import Manifest
from agents.sandbox.session import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot


@dataclass
class App:
    id: str
    name: str
    created_at: int

    @staticmethod
    def find(*, name: str, mint_if_missing: bool = False) -> App:
        _ = mint_if_missing
        return App(id=f"app_{name}", name=name, created_at=0)


class Image:
    debian_amd64 = object()
    debian_arm64 = object()


class _SdkSailbox:
    @staticmethod
    def create(**kwargs: object) -> _SdkSailbox:
        _ = kwargs
        raise NotImplementedError

    @staticmethod
    def connect(sailbox_id: str) -> _SdkSailbox:
        _ = sailbox_id
        raise NotImplementedError


def _install_fake_sail_sdk() -> None:
    sail_module = types.ModuleType("sail")
    app_module = types.ModuleType("sail.app")
    image_module = types.ModuleType("sail.image")
    sailbox_module = types.ModuleType("sail.sailbox")

    cast(Any, app_module).App = App
    cast(Any, image_module).Image = Image
    cast(Any, image_module).ImageDefinition = object
    cast(Any, sailbox_module).Sailbox = _SdkSailbox

    sys.modules.setdefault("sail", sail_module)
    sys.modules["sail.app"] = app_module
    sys.modules["sail.image"] = image_module
    sys.modules["sail.sailbox"] = sailbox_module


_install_fake_sail_sdk()


from agents.extensions.sandbox.sailbox.sandbox import (  # noqa: E402
    SailboxSandboxClient,
    SailboxSandboxClientOptions,
    SailboxSandboxSession,
    SailboxSandboxSessionState,
)


def test_sailbox_package_re_exports_backend_symbols() -> None:
    package_module = __import__(
        "agents.extensions.sandbox.sailbox",
        fromlist=["SailboxSandboxClient"],
    )

    assert package_module.SailboxSandboxClient is SailboxSandboxClient


@dataclass
class _FakeExecResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class _FakeExecRequest:
    def __init__(self, result: _FakeExecResult) -> None:
        self._result = result

    def wait(self) -> _FakeExecResult:
        return self._result


class _BlockingExecRequest:
    def __init__(self, sailbox: _BlockingFakeSailbox) -> None:
        self._sailbox = sailbox

    def wait(self) -> _FakeExecResult:
        time.sleep(0.02)
        self._sailbox.active_execs -= 1
        return _FakeExecResult(stdout="ok\n", returncode=0)


class _FakeListener:
    url = "https://listener.example.test/route?token=abc"


class _FakeSailbox:
    def __init__(self, sailbox_id: str = "sb-test") -> None:
        self.sailbox_id = sailbox_id
        self.name = "agent-sailbox"
        self.status = "running"
        self.worker_address = "worker.internal:50051"
        self.exec_endpoint = "worker.proxy:443"
        self.exec_commands: list[tuple[str, int | None]] = []
        self.files: dict[str, bytes] = {}
        self.terminated = False
        self.paused = False

    def exec(self, command: str, *, timeout: int | None = None) -> Any:
        self.exec_commands.append((command, timeout))
        return _FakeExecRequest(_FakeExecResult(stdout="ok\n", returncode=0))

    def read(self, path: str) -> bytes:
        try:
            return self.files[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    def write(self, path: str, data: bytes) -> None:
        self.files[path] = bytes(data)

    def listener(self, port: int) -> _FakeListener:
        assert port == 8080
        return _FakeListener()

    def pause(self) -> None:
        self.paused = True
        self.status = "paused"

    def terminate(self) -> None:
        self.terminated = True
        self.status = "terminated"


class _FailingExecSailbox(_FakeSailbox):
    def exec(self, command: str, *, timeout: int | None = None) -> Any:
        _ = (command, timeout)
        raise RuntimeError("worker unavailable")


class _BlockingFakeSailbox(_FakeSailbox):
    def __init__(self) -> None:
        super().__init__()
        self.active_execs = 0
        self.max_active_execs = 0

    def exec(self, command: str, *, timeout: int | None = None) -> _BlockingExecRequest:
        self.exec_commands.append((command, timeout))
        self.active_execs += 1
        self.max_active_execs = max(self.max_active_execs, self.active_execs)
        return _BlockingExecRequest(self)


def _state(sailbox: _FakeSailbox) -> SailboxSandboxSessionState:
    return SailboxSandboxSessionState(
        session_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="test"),
        sailbox_id=sailbox.sailbox_id,
        sailbox_name=sailbox.name,
        exec_endpoint=sailbox.exec_endpoint,
        worker_address=sailbox.worker_address,
        status=sailbox.status,
        exposed_ports=(8080,),
    )


def test_client_create_creates_sailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, object]] = []
    fake_sailbox = _FakeSailbox("sb-created")

    def fake_create(**kwargs: object) -> _FakeSailbox:
        created.append(kwargs)
        return fake_sailbox

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox.Sailbox.create",
        staticmethod(fake_create),
    )

    app = App(id="app_test", name="agents", created_at=1)
    options = SailboxSandboxClientOptions(
        app=app,
        image=Image.debian_amd64,
        exposed_ports=(8080,),
        pause_on_exit=True,
    )
    client = SailboxSandboxClient()

    session = asyncio.run(
        client.create(snapshot=NoopSnapshot(id="test"), manifest=Manifest(), options=options)
    )

    inner = session._inner
    assert isinstance(inner, SailboxSandboxSession)
    assert inner.state.sailbox_id == "sb-created"
    assert inner.state.exposed_ports == (8080,)
    assert inner.state.pause_on_exit is True
    assert created[0]["app"] == app
    assert created[0]["ingress_ports"] == [8080]


def test_session_exec_file_io_and_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sailbox = _FakeSailbox()
    session = SailboxSandboxSession.from_state(
        _state(fake_sailbox),
        sailbox=fake_sailbox,
    )

    async def fake_validate(
        self: SailboxSandboxSession,
        path: Path | str,
        *,
        for_write: bool = False,
    ) -> Path:
        _ = (self, for_write)
        return Path("/workspace") / Path(path)

    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", fake_validate)

    result = asyncio.run(session.exec("printf ok", timeout=1.2))
    assert result.stdout == b"ok\n"
    assert fake_sailbox.exec_commands[-1] == (
        "cd /workspace && sh -lc 'printf ok'",
        2,
    )

    asyncio.run(session.write(Path("notes.txt"), io.BytesIO(b"hello")))
    assert fake_sailbox.files["/workspace/notes.txt"] == b"hello"
    assert asyncio.run(session.read(Path("notes.txt"))).read() == b"hello"

    endpoint = asyncio.run(session.resolve_exposed_port(8080))
    assert endpoint.host == "listener.example.test"
    assert endpoint.port == 443
    assert endpoint.tls is True
    assert endpoint.query == "token=abc"


def test_session_state_json_roundtrip_preserves_sailbox_fields() -> None:
    state = _state(_FakeSailbox("sb-roundtrip"))
    payload = json.loads(state.model_dump_json())

    reconstructed = SandboxSessionState.parse(payload)

    assert isinstance(reconstructed, SailboxSandboxSessionState)
    assert reconstructed.sailbox_id == "sb-roundtrip"
    assert reconstructed.exec_endpoint == "worker.proxy:443"
    assert reconstructed.exposed_ports == (8080,)


def test_options_json_dump_serializes_sdk_objects() -> None:
    options = SailboxSandboxClientOptions(
        app=App(id="app_test", name="agents", created_at=1),
        image=Image.debian_amd64,
        exposed_ports=(8080,),
    )

    dumped = options.model_dump(mode="json")

    assert dumped["type"] == "sailbox"
    assert dumped["app"] == "app_test"
    assert dumped["image"] is None
    assert dumped["exposed_ports"] == [8080]


def test_prepare_backend_workspace_bootstraps_root_without_cd() -> None:
    fake_sailbox = _FakeSailbox()
    session = SailboxSandboxSession.from_state(
        _state(fake_sailbox),
        sailbox=fake_sailbox,
    )

    asyncio.run(session._prepare_backend_workspace())

    assert fake_sailbox.exec_commands[-1] == ("mkdir -p /workspace", None)


def test_prepare_backend_workspace_quotes_literal_root() -> None:
    fake_sailbox = _FakeSailbox()
    state = _state(fake_sailbox)
    state.manifest = Manifest(root="/workspace/my app")
    session = SailboxSandboxSession.from_state(
        state,
        sailbox=fake_sailbox,
    )

    asyncio.run(session._prepare_backend_workspace())

    assert fake_sailbox.exec_commands[-1] == ("mkdir -p '/workspace/my app'", None)


def test_exec_calls_can_overlap() -> None:
    fake_sailbox = _BlockingFakeSailbox()
    session = SailboxSandboxSession.from_state(
        _state(fake_sailbox),
        sailbox=fake_sailbox,
    )

    async def run_two_execs() -> None:
        await asyncio.gather(
            session.exec("printf one"),
            session.exec("printf two"),
        )

    asyncio.run(run_two_execs())

    assert fake_sailbox.max_active_execs == 2
    assert len(fake_sailbox.exec_commands) == 2


def test_exec_transport_error_includes_provider_error() -> None:
    fake_sailbox = _FailingExecSailbox()
    session = SailboxSandboxSession.from_state(
        _state(fake_sailbox),
        sailbox=fake_sailbox,
    )

    with pytest.raises(ExecTransportError) as exc_info:
        asyncio.run(session.exec("printf ok"))

    assert exc_info.value.context["backend"] == "sailbox"
    assert exc_info.value.context["provider_error"] == "RuntimeError: worker unavailable"
    assert "Sailbox exec failed: RuntimeError: worker unavailable" in str(exc_info.value)


def test_hydrate_workspace_rejects_unsafe_tar_members() -> None:
    fake_sailbox = _FakeSailbox()
    session = SailboxSandboxSession.from_state(
        _state(fake_sailbox),
        sailbox=fake_sailbox,
    )
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"unsafe"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    raw.seek(0)

    with pytest.raises(WorkspaceArchiveWriteError):
        asyncio.run(session.hydrate_workspace(raw))

    assert fake_sailbox.files == {}
    assert fake_sailbox.exec_commands == []


def test_hydrate_workspace_rejects_external_symlink_before_upload() -> None:
    fake_sailbox = _FakeSailbox()
    session = SailboxSandboxSession.from_state(
        _state(fake_sailbox),
        sailbox=fake_sailbox,
    )
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo("leak")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    raw.seek(0)

    with pytest.raises(WorkspaceArchiveWriteError):
        asyncio.run(session.hydrate_workspace(raw))

    assert fake_sailbox.files == {}
    assert fake_sailbox.exec_commands == []


def test_hydrate_workspace_uploads_extracts_and_cleans_archive() -> None:
    fake_sailbox = _FakeSailbox()
    state = _state(fake_sailbox)
    session = SailboxSandboxSession.from_state(state, sailbox=fake_sailbox)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo("README.md")
        payload = b"hello"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    raw.seek(0)

    asyncio.run(session.hydrate_workspace(raw))

    archive_path = f"/tmp/openai-agents-{state.session_id.hex}.tar"
    assert fake_sailbox.files[archive_path] == raw.getvalue()
    assert fake_sailbox.exec_commands == [
        ("cd /workspace && mkdir -p /workspace", None),
        (
            f"cd /workspace && tar xf {archive_path} -C /workspace",
            None,
        ),
        (f"cd /workspace && rm -f {archive_path}", None),
    ]


def test_persist_workspace_tars_excluding_ephemeral_paths_and_cleans_up() -> None:
    fake_sailbox = _FakeSailbox()
    state = _state(fake_sailbox)
    state.manifest = Manifest(
        root="/workspace",
        entries={"tmp.txt": File(content=b"tmp", ephemeral=True)},
    )
    archive_path = f"/tmp/openai-agents-{state.session_id.hex}.tar"
    fake_sailbox.files[archive_path] = b"archive"
    session = SailboxSandboxSession.from_state(state, sailbox=fake_sailbox)

    archive = asyncio.run(session.persist_workspace())

    assert archive.read() == b"archive"
    assert "--exclude=./tmp.txt" in fake_sailbox.exec_commands[0][0]
    assert f"tar cf {archive_path}" in fake_sailbox.exec_commands[0][0]
    assert fake_sailbox.exec_commands[-1] == (
        f"cd /workspace && rm -f {archive_path}",
        None,
    )


def test_read_missing_file_maps_to_openai_workspace_error() -> None:
    fake_sailbox = _FakeSailbox()
    session = SailboxSandboxSession.from_state(
        _state(fake_sailbox),
        sailbox=fake_sailbox,
    )

    with pytest.raises(WorkspaceReadNotFoundError):
        asyncio.run(session.read(Path("missing.txt")))


def test_invalid_listener_url_maps_to_openai_exposed_port_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sailbox = _FakeSailbox()
    session = SailboxSandboxSession.from_state(
        _state(fake_sailbox),
        sailbox=fake_sailbox,
    )
    monkeypatch.setattr(_FakeListener, "url", "not-a-valid-listener-url")

    with pytest.raises(ExposedPortUnavailableError):
        asyncio.run(session.resolve_exposed_port(8080))


def test_client_resume_reconnects_existing_sailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sailbox = _FakeSailbox("sb-existing")

    def fake_connect(sailbox_id: str) -> _FakeSailbox:
        assert sailbox_id == "sb-existing"
        return fake_sailbox

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox._connect_sailbox",
        fake_connect,
    )

    client = SailboxSandboxClient()
    session = asyncio.run(client.resume(_state(fake_sailbox)))
    inner = session._inner

    assert isinstance(inner, SailboxSandboxSession)
    assert inner.state.sailbox_id == "sb-existing"
    assert inner._workspace_state_preserved_on_start() is True


def test_client_resume_recreates_sailbox_when_reconnect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []
    replacement = _FakeSailbox("sb-recreated")

    def fake_connect(_sailbox_id: str) -> _FakeSailbox:
        raise LookupError("missing")

    def fake_create(**kwargs: object) -> _FakeSailbox:
        created.append(kwargs)
        return replacement

    monkeypatch.setattr("agents.extensions.sandbox.sailbox.sandbox._connect_sailbox", fake_connect)
    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox.Sailbox.create",
        staticmethod(fake_create),
    )

    app = App(id="app_test", name="agents", created_at=1)
    state = _state(_FakeSailbox("sb-missing"))
    state.app_name = "agents"
    state.workspace_root_ready = True
    client = SailboxSandboxClient(app=app)

    session = asyncio.run(client.resume(state))
    inner = session._inner

    assert isinstance(inner, SailboxSandboxSession)
    assert inner.state.sailbox_id == "sb-recreated"
    assert inner.state.workspace_root_ready is False
    assert inner._workspace_state_preserved_on_start() is False
    assert created[0]["app"] == app


def test_client_create_failure_includes_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_create(**kwargs: object) -> _FakeSailbox:
        _ = kwargs
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox.Sailbox.create",
        staticmethod(fake_create),
    )

    app = App(id="app_test", name="agents", created_at=1)
    client = SailboxSandboxClient(app=app)

    with pytest.raises(WorkspaceStartError) as exc_info:
        asyncio.run(client.create(options=SailboxSandboxClientOptions()))

    assert exc_info.value.context["backend"] == "sailbox"
    assert exc_info.value.context["provider_error"] == "RuntimeError: quota exceeded"
    assert "Sailbox create failed: RuntimeError: quota exceeded" in str(exc_info.value)


def test_shutdown_pauses_or_terminates_sailbox() -> None:
    paused_sailbox = _FakeSailbox("sb-paused")
    paused_state = _state(paused_sailbox)
    paused_state.pause_on_exit = True
    paused_session = SailboxSandboxSession.from_state(
        paused_state,
        sailbox=paused_sailbox,
    )
    asyncio.run(paused_session.shutdown())
    assert paused_sailbox.paused is True
    assert paused_sailbox.terminated is False
    assert paused_session.state.status == "paused"

    terminated_sailbox = _FakeSailbox("sb-terminated")
    terminated_session = SailboxSandboxSession.from_state(
        _state(terminated_sailbox),
        sailbox=terminated_sailbox,
    )
    asyncio.run(terminated_session.shutdown())
    assert terminated_sailbox.terminated is True
    assert terminated_session.state.status == "terminated"
