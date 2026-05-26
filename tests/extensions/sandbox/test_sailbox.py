from __future__ import annotations

import asyncio
import io
import json
import shlex
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
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceWriteTypeError,
)
from agents.sandbox.manifest import Environment, Manifest
from agents.sandbox.session import BaseSandboxClientOptions, SandboxSession, SandboxSessionState
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult


@dataclass
class App:
    id: str
    name: str
    created_at: int

    @staticmethod
    def find(*, name: str, mint_if_missing: bool = False) -> App:
        _ = mint_if_missing
        return App(id=f"app_{name}", name=name, created_at=0)


class _FakeImageSpec:
    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload

    def SerializeToString(self) -> bytes:
        return self.payload

    def ParseFromString(self, payload: bytes) -> None:
        self.payload = payload


@dataclass(frozen=True)
class ImageDefinition:
    _spec: _FakeImageSpec
    _image_id: str | None = None

    def to_proto(self) -> _FakeImageSpec:
        return self._spec


class Image:
    debian_amd64 = ImageDefinition(_FakeImageSpec(b"debian-amd64"))
    debian_arm64 = ImageDefinition(_FakeImageSpec(b"debian-arm64"))


class _SdkSailbox:
    @staticmethod
    def create(**kwargs: object) -> _SdkSailbox:
        _ = kwargs
        raise NotImplementedError

    @staticmethod
    def connect(sailbox_id: str) -> _SdkSailbox:
        _ = sailbox_id
        raise NotImplementedError

    @staticmethod
    def get(sailbox_id: str) -> object:
        _ = sailbox_id
        raise NotImplementedError


def _install_fake_sail_sdk() -> None:
    sail_module = types.ModuleType("sail")
    app_module = types.ModuleType("sail.app")
    image_module = types.ModuleType("sail.image")
    sail_pb_module = types.ModuleType("sail.pb")
    sail_pb_image_module = types.ModuleType("sail.pb.image")
    sail_pb_image_v1_module = types.ModuleType("sail.pb.image.v1")
    image_pb2_module = types.ModuleType("sail.pb.image.v1.image_pb2")
    sailbox_module = types.ModuleType("sail.sailbox")

    cast(Any, app_module).App = App
    cast(Any, image_module).Image = Image
    cast(Any, image_module).ImageDefinition = ImageDefinition
    cast(Any, image_pb2_module).ImageSpec = _FakeImageSpec
    cast(Any, sailbox_module).Sailbox = _SdkSailbox

    sys.modules.setdefault("sail", sail_module)
    sys.modules["sail.app"] = app_module
    sys.modules["sail.image"] = image_module
    sys.modules["sail.pb"] = sail_pb_module
    sys.modules["sail.pb.image"] = sail_pb_image_module
    sys.modules["sail.pb.image.v1"] = sail_pb_image_v1_module
    sys.modules["sail.pb.image.v1.image_pb2"] = image_pb2_module
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
    stdout: object = ""
    stderr: object = ""
    returncode: int = 0


class _OpaqueOutput:
    def __str__(self) -> str:
        return "opaque-output"


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

    def resume(self) -> _FakeSailbox:
        self.status = "running"
        return self

    def terminate(self) -> None:
        self.terminated = True
        self.status = "terminated"


class _StatusError(RuntimeError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _WaitFailingRequest:
    def wait(self) -> _FakeExecResult:
        raise RuntimeError("wait failed")


class _WaitFailingSailbox(_FakeSailbox):
    def exec(self, command: str, *, timeout: int | None = None) -> Any:
        self.exec_commands.append((command, timeout))
        return _WaitFailingRequest()


class _FailingExecSailbox(_FakeSailbox):
    def exec(self, command: str, *, timeout: int | None = None) -> Any:
        _ = (command, timeout)
        raise RuntimeError("worker unavailable")


class _NonzeroExecSailbox(_FakeSailbox):
    def exec(self, command: str, *, timeout: int | None = None) -> Any:
        self.exec_commands.append((command, timeout))
        return _FakeExecRequest(_FakeExecResult(stdout="out", stderr="err", returncode=7))


class _BytesExecSailbox(_FakeSailbox):
    def exec(self, command: str, *, timeout: int | None = None) -> Any:
        self.exec_commands.append((command, timeout))
        return _FakeExecRequest(_FakeExecResult(stdout=b"\xffok\n", stderr=bytearray(b"\xfeerr\n")))


class _ScriptedExecSailbox(_FakeSailbox):
    def __init__(self, results: list[_FakeExecResult | BaseException]) -> None:
        super().__init__()
        self.results = results

    def exec(self, command: str, *, timeout: int | None = None) -> Any:
        self.exec_commands.append((command, timeout))
        if not self.results:
            return super().exec(command, timeout=timeout)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return _FakeExecRequest(result)


class _FailingReadSailbox(_FakeSailbox):
    def read(self, path: str) -> bytes:
        _ = path
        raise RuntimeError("read failed")


class _FailingWriteSailbox(_FakeSailbox):
    def write(self, path: str, data: bytes) -> None:
        _ = (path, data)
        raise RuntimeError("write failed")


class _NoSudoSailbox(_FakeSailbox):
    def exec(self, command: str, *, timeout: int | None = None) -> Any:
        if "sudo" in command:
            raise RuntimeError("sudo: not found")
        return super().exec(command, timeout=timeout)


class _OwnershipTrackingSailbox(_FakeSailbox):
    def __init__(self) -> None:
        super().__init__()
        self.owners: dict[str, str] = {}

    def write(self, path: str, data: bytes) -> None:
        super().write(path, data)
        self.owners[path] = "root"

    def exec(self, command: str, *, timeout: int | None = None) -> Any:
        self.exec_commands.append((command, timeout))
        if command.startswith("runuser "):
            parts = shlex.split(command)
            user = parts[2]
            temp_path = parts[-2]
            target_path = parts[-1]
            self.files[target_path] = self.files[temp_path]
            self.owners[target_path] = user
        return _FakeExecRequest(_FakeExecResult(stdout="ok\n", returncode=0))


class _FailingListenerSailbox(_FakeSailbox):
    def listener(self, port: int) -> _FakeListener:
        _ = port
        raise RuntimeError("listener failed")


class _FailingPauseSailbox(_FakeSailbox):
    def pause(self) -> None:
        raise RuntimeError("pause failed")


class _FailingResumeSailbox(_FakeSailbox):
    def resume(self) -> _FakeSailbox:
        raise RuntimeError("resume failed")


class _FailingTerminateSailbox(_FakeSailbox):
    def terminate(self) -> None:
        raise RuntimeError("terminate failed")


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


def _session(sailbox: _FakeSailbox) -> SailboxSandboxSession:
    return SailboxSandboxSession.from_state(_state(sailbox), sailbox=sailbox)


def _tar_bytes(name: str = "README.md", payload: bytes = b"hello") -> io.BytesIO:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    raw.seek(0)
    return raw


class _InvalidPayload(io.IOBase):
    def read(self, *args: object, **kwargs: object) -> object:
        _ = (args, kwargs)
        return object()


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
    assert inner.state.image is Image.debian_amd64
    assert inner.state.exposed_ports == (8080,)
    assert inner.state.pause_on_exit is True
    assert created[0]["app"] == app
    assert created[0]["image"] is Image.debian_amd64
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
    assert dumped["app"] == {"id": "app_test", "name": "agents", "created_at": 1}
    assert dumped["image"] == {"image_id": None, "spec": "ZGViaWFuLWFtZDY0"}
    assert dumped["exposed_ports"] == [8080]


def test_options_json_roundtrip_preserves_app() -> None:
    options = SailboxSandboxClientOptions(
        app=App(id="app_test", name="agents", created_at=1),
        app_name=None,
        exposed_ports=(8080,),
    )

    parsed = BaseSandboxClientOptions.parse(options.model_dump(mode="json"))

    assert isinstance(parsed, SailboxSandboxClientOptions)
    assert parsed == options
    assert parsed.app == App(id="app_test", name="agents", created_at=1)


def test_options_json_roundtrip_preserves_image_definition() -> None:
    options = SailboxSandboxClientOptions(
        image=ImageDefinition(_FakeImageSpec(b"custom-image"), _image_id="img_123"),
        exposed_ports=(8080,),
    )

    parsed = BaseSandboxClientOptions.parse(options.model_dump(mode="json"))

    assert isinstance(parsed, SailboxSandboxClientOptions)
    assert isinstance(parsed.image, ImageDefinition)
    assert parsed.image._image_id == "img_123"
    assert parsed.image.to_proto().SerializeToString() == b"custom-image"
    assert parsed.exposed_ports == (8080,)


def test_options_json_parse_accepts_legacy_image_id_string() -> None:
    parsed = BaseSandboxClientOptions.parse(
        {
            "type": "sailbox",
            "image": "img_legacy",
        }
    )

    assert isinstance(parsed, SailboxSandboxClientOptions)
    assert isinstance(parsed.image, ImageDefinition)
    assert parsed.image._image_id == "img_legacy"


def test_options_json_parse_accepts_legacy_app_id_string() -> None:
    parsed = BaseSandboxClientOptions.parse(
        {
            "type": "sailbox",
            "app": "app_test",
            "app_name": None,
        }
    )

    assert isinstance(parsed, SailboxSandboxClientOptions)
    assert parsed.app == App(id="app_test", name="app_test", created_at=0)


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
    state.image = Image.debian_amd64
    state.workspace_root_ready = True
    client = SailboxSandboxClient(app=app)

    session = asyncio.run(client.resume(state))
    inner = session._inner

    assert isinstance(inner, SailboxSandboxSession)
    assert inner.state.sailbox_id == "sb-recreated"
    assert inner.state.workspace_root_ready is False
    assert inner._workspace_state_preserved_on_start() is False
    assert created[0]["app"] == app
    assert created[0]["image"] is Image.debian_amd64


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


def test_client_options_defaults_match_provider_defaults() -> None:
    options = SailboxSandboxClientOptions()

    assert options.type == "sailbox"
    assert options.model_fields_set == {"type"}
    assert options.app_name == "openai-agents-sandbox"
    assert options.name_prefix == "openai-agent"
    assert options.memory_mib == 1024
    assert options.cpu == 1
    assert options.disk_gib == 8
    assert options.exposed_ports == ()
    assert options.pause_on_exit is False


def test_client_resolve_options_uses_client_defaults() -> None:
    app = App(id="app_client", name="client", created_at=1)
    client = SailboxSandboxClient(
        app=app,
        app_name="client-app",
        image=Image.debian_amd64,
        name_prefix="client-prefix",
        image_build_timeout=33,
        memory_mib=2048,
        cpu=2,
        disk_gib=16,
        pause_on_exit=True,
    )

    options = client._resolve_options(None)

    assert options.app == app
    assert options.app_name == "client-app"
    assert options.image is Image.debian_amd64
    assert options.name_prefix == "client-prefix"
    assert options.image_build_timeout == 33
    assert options.memory_mib == 2048
    assert options.cpu == 2
    assert options.disk_gib == 16
    assert options.pause_on_exit is True


def test_client_resolve_options_prefers_explicit_values() -> None:
    client = SailboxSandboxClient(
        app=App(id="app_client", name="client", created_at=1),
        image=Image.debian_arm64,
        name_prefix="client-prefix",
        image_build_timeout=33,
        memory_mib=2048,
        cpu=2,
        disk_gib=16,
        pause_on_exit=True,
    )
    explicit_app = App(id="app_explicit", name="explicit", created_at=2)

    options = client._resolve_options(
        SailboxSandboxClientOptions(
            app=explicit_app,
            app_name="explicit-app",
            image=Image.debian_amd64,
            name_prefix="explicit-prefix",
            image_build_timeout=44,
            memory_mib=4096,
            cpu=4,
            disk_gib=32,
            exposed_ports=(3000, 8080),
            pause_on_exit=False,
        )
    )

    assert options.app == explicit_app
    assert options.app_name == "explicit-app"
    assert options.image is Image.debian_amd64
    assert options.name_prefix == "explicit-prefix"
    assert options.image_build_timeout == 44
    assert options.memory_mib == 4096
    assert options.cpu == 4
    assert options.disk_gib == 32
    assert options.exposed_ports == (3000, 8080)
    assert options.pause_on_exit is False


def test_client_resolve_options_partial_options_preserve_client_defaults() -> None:
    app = App(id="app_client", name="client", created_at=1)
    client = SailboxSandboxClient(
        app=app,
        app_name="client-app",
        image=Image.debian_amd64,
        name_prefix="client-prefix",
        image_build_timeout=33,
        memory_mib=2048,
        cpu=2,
        disk_gib=16,
        pause_on_exit=True,
    )

    options = client._resolve_options(SailboxSandboxClientOptions(exposed_ports=(8080,)))

    assert options.app == app
    assert options.app_name == "client-app"
    assert options.image is Image.debian_amd64
    assert options.name_prefix == "client-prefix"
    assert options.image_build_timeout == 33
    assert options.memory_mib == 2048
    assert options.cpu == 2
    assert options.disk_gib == 16
    assert options.exposed_ports == (8080,)
    assert options.pause_on_exit is True


def test_client_resolve_options_falls_back_to_client_app() -> None:
    app = App(id="app_client", name="client", created_at=1)
    client = SailboxSandboxClient(app=app, app_name="client-app")

    options = client._resolve_options(SailboxSandboxClientOptions(app_name=None))

    assert options.app == app
    assert options.app_name == "client-app"


def test_resolve_app_returns_explicit_app_without_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    app = App(id="app_direct", name="direct", created_at=1)
    client = SailboxSandboxClient()

    def fail_find(**kwargs: object) -> App:
        _ = kwargs
        raise AssertionError("App.find should not be called")

    monkeypatch.setattr("agents.extensions.sandbox.sailbox.sandbox.App.find", fail_find)

    resolved = asyncio.run(client._resolve_app(SailboxSandboxClientOptions(app=app)))

    assert resolved == app


def test_resolve_app_finds_and_mints_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    client = SailboxSandboxClient()

    def fake_find(**kwargs: object) -> App:
        calls.append(kwargs)
        return App(id="app_found", name=cast(str, kwargs["name"]), created_at=1)

    monkeypatch.setattr("agents.extensions.sandbox.sailbox.sandbox.App.find", fake_find)

    resolved = asyncio.run(
        client._resolve_app(SailboxSandboxClientOptions(app=None, app_name="agents"))
    )

    assert resolved.id == "app_found"
    assert calls == [{"name": "agents", "mint_if_missing": True}]


def test_resolve_app_requires_app_or_name() -> None:
    client = SailboxSandboxClient()

    with pytest.raises(ValueError, match="requires app or app_name"):
        asyncio.run(client._resolve_app(SailboxSandboxClientOptions(app=None, app_name=None)))


def test_create_without_manifest_uses_default_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sailbox = _FakeSailbox("sb-default-manifest")

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox.Sailbox.create",
        staticmethod(lambda **kwargs: fake_sailbox),
    )

    session = asyncio.run(
        SailboxSandboxClient(app=App(id="app_test", name="agents", created_at=1)).create()
    )

    assert session.state.manifest.root == "/workspace"
    assert session.state.sailbox_id == "sb-default-manifest"


def test_create_passes_resource_options_and_generated_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []
    fake_sailbox = _FakeSailbox("sb-resources")

    def fake_create(**kwargs: object) -> _FakeSailbox:
        created.append(kwargs)
        return fake_sailbox

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox.Sailbox.create",
        staticmethod(fake_create),
    )
    app = App(id="app_test", name="agents", created_at=1)

    session = asyncio.run(
        SailboxSandboxClient(app=app).create(
            options=SailboxSandboxClientOptions(
                image=Image.debian_amd64,
                name_prefix="custom-prefix",
                image_build_timeout=44,
                memory_mib=4096,
                cpu=4,
                disk_gib=32,
                exposed_ports=(8080, 3000),
            )
        )
    )

    assert session.state.sailbox_name == fake_sailbox.name
    assert created[0]["image"] is Image.debian_amd64
    assert created[0]["app"] == app
    assert cast(str, created[0]["name"]).startswith("custom-prefix-")
    assert len(cast(str, created[0]["name"]).removeprefix("custom-prefix-")) == 12
    assert created[0]["image_build_timeout"] == 44
    assert created[0]["memory_mib"] == 4096
    assert created[0]["cpu"] == 4
    assert created[0]["disk_gib"] == 32
    assert created[0]["ingress_ports"] == [8080, 3000]


def test_create_failure_context_includes_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_create(**kwargs: object) -> _FakeSailbox:
        _ = kwargs
        raise _StatusError("forbidden", status_code=403)

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox.Sailbox.create",
        staticmethod(fake_create),
    )

    client = SailboxSandboxClient(app=App(id="app_test", name="agents", created_at=1))

    with pytest.raises(WorkspaceStartError) as exc_info:
        asyncio.run(client.create())

    assert exc_info.value.context["backend"] == "sailbox"
    assert exc_info.value.context["http_status"] == 403
    assert exc_info.value.context["provider_error"] == "HTTP 403: forbidden"


def test_resume_rejects_wrong_state_type() -> None:
    state = SandboxSessionState(
        type="other",
        session_id=uuid.uuid4(),
        snapshot=NoopSnapshot(id="test"),
        manifest=Manifest(),
    )

    with pytest.raises(TypeError, match="SailboxSandboxSessionState"):
        asyncio.run(SailboxSandboxClient().resume(state))


def test_deserialize_session_state_returns_sailbox_state() -> None:
    state = _state(_FakeSailbox("sb-json"))
    payload = json.loads(state.model_dump_json())

    parsed = SailboxSandboxClient().deserialize_session_state(payload)

    assert isinstance(parsed, SailboxSandboxSessionState)
    assert parsed.sailbox_id == "sb-json"
    assert parsed.exposed_ports == (8080,)


def test_session_state_defaults_are_serializable() -> None:
    state = SailboxSandboxSessionState(
        session_id=uuid.uuid4(),
        snapshot=NoopSnapshot(id="test"),
        manifest=Manifest(),
    )

    payload = state.model_dump(mode="json")

    assert payload["type"] == "sailbox"
    assert payload["sailbox_id"] == ""
    assert payload["image_build_timeout"] == 1800
    assert payload["image"] is None
    assert payload["memory_mib"] == 1024
    assert payload["cpu"] == 1
    assert payload["disk_gib"] == 8


def test_running_false_without_backend() -> None:
    state = _state(_FakeSailbox())
    state.status = "paused"
    without_backend = SailboxSandboxSession.from_state(state, sailbox=None)

    assert asyncio.run(without_backend.running()) is False


def test_running_rechecks_backend_status(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(sailbox_id: str) -> object:
        calls.append(sailbox_id)
        return types.SimpleNamespace(status="running")

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox.Sailbox.get",
        staticmethod(fake_get),
    )
    sailbox = _FakeSailbox("sb-running")
    session = SailboxSandboxSession.from_state(_state(sailbox), sailbox=sailbox)

    assert asyncio.run(session.running()) is True
    assert calls == ["sb-running"]
    assert session.state.status == "running"
    assert sailbox.status == "running"


def test_running_returns_false_when_backend_is_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox.Sailbox.get",
        staticmethod(lambda sailbox_id: types.SimpleNamespace(status="paused")),
    )
    sailbox = _FakeSailbox("sb-paused")
    session = SailboxSandboxSession.from_state(_state(sailbox), sailbox=sailbox)
    session.state.status = "running"
    sailbox.status = "running"

    assert asyncio.run(session.running()) is False
    assert session.state.status == "paused"
    assert sailbox.status == "paused"


def test_running_returns_false_when_backend_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(_sailbox_id: str) -> object:
        raise LookupError("missing")

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox.Sailbox.get",
        staticmethod(fake_get),
    )
    sailbox = _FakeSailbox("sb-missing")
    session = SailboxSandboxSession.from_state(_state(sailbox), sailbox=sailbox)

    assert asyncio.run(session.running()) is False
    assert session.state.status == "terminated"
    assert sailbox.status == "terminated"


def test_running_returns_false_on_status_lookup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(_sailbox_id: str) -> object:
        raise RuntimeError("status unavailable")

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox.Sailbox.get",
        staticmethod(fake_get),
    )
    sailbox = _FakeSailbox("sb-unavailable")
    session = SailboxSandboxSession.from_state(_state(sailbox), sailbox=sailbox)

    assert asyncio.run(session.running()) is False
    assert session.state.status == "running"


def test_set_sailbox_updates_state_fields() -> None:
    session = SailboxSandboxSession.from_state(_state(_FakeSailbox("sb-old")))
    sailbox = _FakeSailbox("sb-new")
    sailbox.name = "new-name"
    sailbox.worker_address = "worker-new:50051"
    sailbox.exec_endpoint = "exec-new:443"

    session._set_sailbox(sailbox)

    assert session.state.sailbox_id == "sb-new"
    assert session.state.sailbox_name == "new-name"
    assert session.state.status == "running"
    assert session.state.worker_address == "worker-new:50051"
    assert session.state.exec_endpoint == "exec-new:443"


def test_ensure_backend_started_resumes_paused_sailbox() -> None:
    sailbox = _FakeSailbox("sb-paused")
    sailbox.status = "paused"
    session = SailboxSandboxSession.from_state(_state(sailbox), sailbox=sailbox)

    asyncio.run(session._ensure_backend_started())

    assert sailbox.status == "running"
    assert session.state.status == "running"


def test_ensure_backend_started_resume_failure_maps_start_error() -> None:
    sailbox = _FailingResumeSailbox("sb-paused")
    sailbox.status = "paused"
    session = SailboxSandboxSession.from_state(_state(sailbox), sailbox=sailbox)

    with pytest.raises(WorkspaceStartError) as exc_info:
        asyncio.run(session._ensure_backend_started())

    assert exc_info.value.context["backend"] == "sailbox"
    assert exc_info.value.context["reason"] == "resume_failed"
    assert exc_info.value.context["sailbox_id"] == "sb-paused"
    assert "Sailbox resume failed: RuntimeError: resume failed" in str(exc_info.value)


def test_ensure_backend_started_requires_sailbox_id() -> None:
    state = _state(_FakeSailbox())
    state.sailbox_id = ""
    session = SailboxSandboxSession.from_state(state)

    with pytest.raises(WorkspaceStartError) as exc_info:
        asyncio.run(session._ensure_backend_started())

    assert exc_info.value.context["reason"] == "missing_sailbox_id"


def test_ensure_backend_started_connects_existing_sailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = _FakeSailbox("sb-connect")

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox._connect_sailbox",
        lambda sailbox_id: connected,
    )

    session = SailboxSandboxSession.from_state(_state(_FakeSailbox("sb-connect")))
    asyncio.run(session._ensure_backend_started())

    assert session.sailbox is connected
    assert session._workspace_state_preserved_on_start() is True


def test_ensure_backend_started_connect_failure_maps_start_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox._connect_sailbox",
        lambda sailbox_id: (_ for _ in ()).throw(RuntimeError("gone")),
    )

    session = SailboxSandboxSession.from_state(_state(_FakeSailbox("sb-missing")))

    with pytest.raises(WorkspaceStartError) as exc_info:
        asyncio.run(session._ensure_backend_started())

    assert exc_info.value.context["backend"] == "sailbox"
    assert exc_info.value.context["reason"] == "connect_failed"
    assert exc_info.value.context["sailbox_id"] == "sb-missing"


def test_prepare_backend_workspace_nonzero_raises_start_error() -> None:
    sailbox = _NonzeroExecSailbox()
    session = _session(sailbox)

    with pytest.raises(WorkspaceStartError) as exc_info:
        asyncio.run(session._prepare_backend_workspace())

    assert exc_info.value.context["exit_code"] == 7
    assert exc_info.value.context["stdout"] == "out"
    assert exc_info.value.context["stderr"] == "err"


def test_prepare_backend_workspace_normalizes_non_string_output() -> None:
    sailbox = _ScriptedExecSailbox(
        [_FakeExecResult(stdout=None, stderr=_OpaqueOutput(), returncode=7)]
    )
    session = _session(sailbox)

    with pytest.raises(WorkspaceStartError) as exc_info:
        asyncio.run(session._prepare_backend_workspace())

    assert exc_info.value.context["exit_code"] == 7
    assert exc_info.value.context["stdout"] == ""
    assert exc_info.value.context["stderr"] == "opaque-output"


def test_prepare_backend_workspace_wait_failure_maps_start_error() -> None:
    session = _session(_WaitFailingSailbox())

    with pytest.raises(WorkspaceStartError) as exc_info:
        asyncio.run(session._prepare_backend_workspace())

    assert exc_info.value.context["backend"] == "sailbox"
    assert exc_info.value.context["reason"] == "mkdir_failed"


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [
        (None, None),
        (0, 1),
        (-1, 1),
        (1.01, 2),
    ],
)
def test_exec_timeout_is_coerced(timeout: float | None, expected: int | None) -> None:
    sailbox = _FakeSailbox()
    session = _session(sailbox)

    asyncio.run(session.exec("printf ok", timeout=timeout))

    assert sailbox.exec_commands[-1][1] == expected


def test_exec_shell_false_uses_direct_command() -> None:
    sailbox = _FakeSailbox()
    session = _session(sailbox)

    asyncio.run(session.exec("printf", "ok", shell=False))

    assert sailbox.exec_commands[-1] == ("cd /workspace && printf ok", None)


def test_exec_custom_shell_prefix_is_respected() -> None:
    sailbox = _FakeSailbox()
    session = _session(sailbox)

    asyncio.run(session.exec("printf ok", shell=["bash", "-lc"]))

    assert sailbox.exec_commands[-1] == ("cd /workspace && bash -lc 'printf ok'", None)


def test_exec_user_wraps_command_with_sudo() -> None:
    sailbox = _FakeSailbox()
    session = _session(sailbox)

    asyncio.run(session.exec("id", shell=False, user="sandbox-user"))

    assert sailbox.exec_commands[-1] == (
        "cd /workspace && sudo -u sandbox-user -- id",
        None,
    )


def test_exec_includes_sorted_manifest_environment() -> None:
    sailbox = _FakeSailbox()
    state = _state(sailbox)
    state.manifest = Manifest(
        root="/workspace",
        environment=Environment(value={"ZED": "two words", "ALPHA": "1"}),
    )
    session = SailboxSandboxSession.from_state(state, sailbox=sailbox)

    asyncio.run(session.exec("printf ok"))

    assert sailbox.exec_commands[-1] == (
        "cd /workspace && env ALPHA=1 ZED='two words' sh -lc 'printf ok'",
        None,
    )


def test_exec_nonzero_result_is_returned_to_caller() -> None:
    session = _session(_NonzeroExecSailbox())

    result = asyncio.run(session.exec("false"))

    assert result.exit_code == 7
    assert result.stdout == b"out"
    assert result.stderr == b"err"


def test_exec_accepts_bytes_stdout_and_stderr() -> None:
    session = _session(_BytesExecSailbox())

    result = asyncio.run(session.exec("printf ok"))

    assert result.exit_code == 0
    assert result.stdout == b"\xffok\n"
    assert result.stderr == b"\xfeerr\n"


def test_exec_normalizes_none_stdout_and_non_string_stderr() -> None:
    sailbox = _ScriptedExecSailbox([_FakeExecResult(stdout=None, stderr=123, returncode=0)])
    session = _session(sailbox)

    result = asyncio.run(session.exec("printf ok"))

    assert result.stdout == b""
    assert result.stderr == b"123"


def test_exec_wait_failure_maps_transport_error() -> None:
    session = _session(_WaitFailingSailbox())

    with pytest.raises(ExecTransportError) as exc_info:
        asyncio.run(session.exec("printf ok"))

    assert exc_info.value.context["backend"] == "sailbox"
    assert "wait failed" in exc_info.value.context["provider_error"]


async def _validate_direct_path(
    self: SailboxSandboxSession,
    path: Path | str,
    *,
    for_write: bool = False,
) -> Path:
    _ = (self, for_write)
    return Path("/workspace") / Path(path)


def test_read_generic_failure_maps_archive_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    session = _session(_FailingReadSailbox())

    with pytest.raises(WorkspaceArchiveReadError):
        asyncio.run(session.read(Path("notes.txt")))


def test_read_with_user_checks_access_without_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    sailbox = _NoSudoSailbox()
    sailbox.files["/workspace/notes.txt"] = b"hello"
    session = _session(sailbox)

    result = asyncio.run(session.read(Path("notes.txt"), user="app"))

    assert result.read() == b"hello"
    assert all("sudo" not in command for command, _ in sailbox.exec_commands)
    assert sailbox.exec_commands[0][0].startswith("runuser -u app --")


def test_read_with_user_denied_maps_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    sailbox = _ScriptedExecSailbox([_FakeExecResult(stdout="out", stderr="err", returncode=1)])
    session = _session(sailbox)

    with pytest.raises(WorkspaceReadNotFoundError) as exc_info:
        asyncio.run(session.read(Path("notes.txt"), user="app"))

    assert exc_info.value.context["command"] == [
        "runuser",
        "-u",
        "app",
        "--",
        "sh",
        "-lc",
        "<read_check>",
    ]
    assert exc_info.value.context["stdout"] == "out"
    assert exc_info.value.context["stderr"] == "err"


def test_read_with_user_denied_normalizes_non_string_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    sailbox = _ScriptedExecSailbox(
        [_FakeExecResult(stdout=None, stderr=_OpaqueOutput(), returncode=1)]
    )
    session = _session(sailbox)

    with pytest.raises(WorkspaceReadNotFoundError) as exc_info:
        asyncio.run(session.read(Path("notes.txt"), user="app"))

    assert exc_info.value.context["stdout"] == ""
    assert exc_info.value.context["stderr"] == "opaque-output"


def test_write_accepts_text_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    sailbox = _FakeSailbox()
    session = _session(sailbox)

    asyncio.run(session.write(Path("notes.txt"), io.StringIO("hello")))

    assert sailbox.files["/workspace/notes.txt"] == b"hello"


def test_write_with_user_stages_then_writes_through_user_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    sailbox = _FakeSailbox()
    session = _session(sailbox)

    asyncio.run(session.write(Path("notes.txt"), io.BytesIO(b"hello"), user="app"))

    temp_paths = [path for path in sailbox.files if path.startswith("/tmp/openai-agents-write-")]
    assert len(temp_paths) == 1
    assert sailbox.files[temp_paths[0]] == b"hello"
    assert "/workspace/notes.txt" not in sailbox.files
    assert len(sailbox.exec_commands) == 2
    assert sailbox.exec_commands[0][0].startswith("runuser -u app -- sh -lc")
    assert "cat \"$tmp\" > \"$target\"" in sailbox.exec_commands[0][0]
    assert temp_paths[0] in sailbox.exec_commands[0][0]
    assert "/workspace/notes.txt" in sailbox.exec_commands[0][0]
    assert temp_paths[0] in sailbox.exec_commands[1][0]


def test_write_with_user_does_not_require_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    sailbox = _NoSudoSailbox()
    session = _session(sailbox)

    asyncio.run(session.write(Path("notes.txt"), io.BytesIO(b"hello"), user="app"))

    assert all("sudo" not in command for command, _ in sailbox.exec_commands)
    assert sailbox.exec_commands[0][0].startswith("runuser -u app --")


def test_write_with_user_creates_destination_as_requested_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    sailbox = _OwnershipTrackingSailbox()
    session = _session(sailbox)

    asyncio.run(session.write(Path("notes.txt"), io.BytesIO(b"hello"), user="app"))

    assert sailbox.files["/workspace/notes.txt"] == b"hello"
    assert sailbox.owners["/workspace/notes.txt"] == "app"


def test_write_with_user_nonzero_exec_maps_archive_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    sailbox = _ScriptedExecSailbox(
        [
            _FakeExecResult(stdout="out", stderr="err", returncode=23),
            _FakeExecResult(returncode=0),
        ]
    )
    session = _session(sailbox)

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        asyncio.run(session.write(Path("notes.txt"), io.BytesIO(b"hello"), user="app"))

    assert exc_info.value.context["reason"] == "write_as_user_nonzero_exit"
    assert exc_info.value.context["exit_code"] == 23
    assert exc_info.value.context["stdout"] == "out"
    assert exc_info.value.context["stderr"] == "err"


def test_write_with_user_nonzero_exec_normalizes_non_string_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    sailbox = _ScriptedExecSailbox(
        [
            _FakeExecResult(stdout=None, stderr=_OpaqueOutput(), returncode=23),
            _FakeExecResult(returncode=0),
        ]
    )
    session = _session(sailbox)

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        asyncio.run(session.write(Path("notes.txt"), io.BytesIO(b"hello"), user="app"))

    assert exc_info.value.context["reason"] == "write_as_user_nonzero_exit"
    assert exc_info.value.context["stdout"] == ""
    assert exc_info.value.context["stderr"] == "opaque-output"


def test_write_rejects_invalid_payload_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    session = _session(_FakeSailbox())

    with pytest.raises(WorkspaceWriteTypeError) as exc_info:
        asyncio.run(session.write(Path("notes.txt"), _InvalidPayload()))

    assert exc_info.value.context["actual_type"] == "object"


def test_write_generic_failure_maps_archive_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SailboxSandboxSession, "_validate_path_access", _validate_direct_path)
    session = _session(_FailingWriteSailbox())

    with pytest.raises(WorkspaceArchiveWriteError):
        asyncio.run(session.write(Path("notes.txt"), io.BytesIO(b"hello")))


def test_resolve_exposed_port_rejects_unconfigured_port() -> None:
    session = _session(_FakeSailbox())

    with pytest.raises(ExposedPortUnavailableError) as exc_info:
        asyncio.run(session.resolve_exposed_port(3000))

    assert exc_info.value.context["reason"] == "not_configured"


def test_resolve_exposed_port_listener_failure_maps_error() -> None:
    state = _state(_FailingListenerSailbox())
    state.exposed_ports = (8080,)
    session = SailboxSandboxSession.from_state(state, sailbox=_FailingListenerSailbox())

    with pytest.raises(ExposedPortUnavailableError) as exc_info:
        asyncio.run(session.resolve_exposed_port(8080))

    assert exc_info.value.context["reason"] == "backend_unavailable"
    assert exc_info.value.context["backend"] == "sailbox"
    assert exc_info.value.context["detail"] == "listener_lookup_failed"


def test_resolve_exposed_port_parses_http_default_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_FakeListener, "url", "http://listener.example.test/path")
    session = _session(_FakeSailbox())

    endpoint = asyncio.run(session.resolve_exposed_port(8080))

    assert endpoint.host == "listener.example.test"
    assert endpoint.port == 80
    assert endpoint.tls is False
    assert endpoint.query == ""


def test_persist_workspace_nonzero_tar_raises_archive_error() -> None:
    session = _session(_NonzeroExecSailbox())

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        asyncio.run(session.persist_workspace())

    assert exc_info.value.context["exit_code"] == 7
    assert exc_info.value.context["stdout"] == "out"
    assert exc_info.value.context["stderr"] == "err"


def test_persist_workspace_read_failure_maps_archive_error() -> None:
    session = _session(_FailingReadSailbox())

    with pytest.raises(WorkspaceArchiveReadError):
        asyncio.run(session.persist_workspace())


def test_persist_workspace_cleanup_failure_is_suppressed() -> None:
    sailbox = _ScriptedExecSailbox(
        [
            _FakeExecResult(returncode=0),
            RuntimeError("cleanup failed"),
        ]
    )
    state = _state(sailbox)
    archive_path = f"/tmp/openai-agents-{state.session_id.hex}.tar"
    sailbox.files[archive_path] = b"archive"
    session = SailboxSandboxSession.from_state(state, sailbox=sailbox)

    archive = asyncio.run(session.persist_workspace())

    assert archive.read() == b"archive"
    assert len(sailbox.exec_commands) == 2


def test_hydrate_workspace_rejects_invalid_payload_type() -> None:
    session = _session(_FakeSailbox())

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        asyncio.run(session.hydrate_workspace(_InvalidPayload()))

    assert exc_info.value.context["reason"] == "invalid_archive_payload"


def test_hydrate_workspace_write_failure_maps_archive_error() -> None:
    session = _session(_FailingWriteSailbox())

    with pytest.raises(WorkspaceArchiveWriteError):
        asyncio.run(session.hydrate_workspace(_tar_bytes()))


def test_hydrate_workspace_extract_failure_maps_archive_error() -> None:
    sailbox = _ScriptedExecSailbox(
        [
            _FakeExecResult(returncode=0),
            _FakeExecResult(stdout="out", stderr="err", returncode=2),
            _FakeExecResult(returncode=0),
        ]
    )
    session = _session(sailbox)

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        asyncio.run(session.hydrate_workspace(_tar_bytes()))

    assert exc_info.value.context["exit_code"] == 2
    assert exc_info.value.context["stdout"] == "out"
    assert exc_info.value.context["stderr"] == "err"


def test_hydrate_workspace_cleanup_failure_is_suppressed() -> None:
    sailbox = _ScriptedExecSailbox(
        [
            _FakeExecResult(returncode=0),
            _FakeExecResult(returncode=0),
            RuntimeError("cleanup failed"),
        ]
    )
    session = _session(sailbox)

    asyncio.run(session.hydrate_workspace(_tar_bytes()))

    assert len(sailbox.exec_commands) == 3


def test_shutdown_pause_failure_propagates() -> None:
    state = _state(_FailingPauseSailbox())
    state.pause_on_exit = True
    session = SailboxSandboxSession.from_state(state, sailbox=_FailingPauseSailbox())

    with pytest.raises(RuntimeError, match="pause failed"):
        asyncio.run(session.shutdown())


def test_shutdown_terminate_failure_propagates() -> None:
    session = _session(_FailingTerminateSailbox())

    with pytest.raises(RuntimeError, match="terminate failed"):
        asyncio.run(session.shutdown())


def test_client_delete_terminates_live_sailbox() -> None:
    sailbox = _FakeSailbox("sb-delete")
    state = _state(sailbox)
    state.pause_on_exit = True
    inner = SailboxSandboxSession.from_state(state, sailbox=sailbox)
    session = SandboxSession(inner)

    asyncio.run(SailboxSandboxClient().delete(session))

    assert sailbox.terminated is True
    assert sailbox.paused is False
    assert inner.state.status == "terminated"
    assert inner.state.worker_address == ""


def test_client_delete_terminates_after_pause_on_exit_shutdown() -> None:
    sailbox = _FakeSailbox("sb-delete-paused")
    state = _state(sailbox)
    state.pause_on_exit = True
    inner = SailboxSandboxSession.from_state(state, sailbox=sailbox)
    session = SandboxSession(inner)

    asyncio.run(inner.shutdown())
    assert sailbox.paused is True
    assert inner.state.status == "paused"

    asyncio.run(SailboxSandboxClient().delete(session))

    assert sailbox.terminated is True
    assert inner.state.status == "terminated"


def test_client_delete_reconnects_and_terminates_without_live_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sailbox = _FakeSailbox("sb-delete-reconnect")
    state = _state(sailbox)
    inner = SailboxSandboxSession.from_state(state, sailbox=None)
    session = SandboxSession(inner)
    reconnected: list[str] = []

    def fake_connect(sailbox_id: str) -> _FakeSailbox:
        reconnected.append(sailbox_id)
        return sailbox

    monkeypatch.setattr(
        "agents.extensions.sandbox.sailbox.sandbox._connect_sailbox",
        fake_connect,
    )

    asyncio.run(SailboxSandboxClient().delete(session))

    assert reconnected == ["sb-delete-reconnect"]
    assert sailbox.terminated is True
    assert inner.state.status == "terminated"


def test_client_delete_rejects_non_sailbox_session() -> None:
    class _OtherInner(BaseSandboxSession):
        state: SandboxSessionState

        def __init__(self) -> None:
            self.state = SandboxSessionState(
                type="other",
                session_id=uuid.uuid4(),
                snapshot=NoopSnapshot(id="test"),
                manifest=Manifest(),
            )

        async def _exec_internal(
            self,
            *command: str | Path,
            timeout: float | None = None,
        ) -> ExecResult:
            _ = (command, timeout)
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        async def read(self, path: Path, *, user: str | None = None) -> io.IOBase:
            _ = (path, user)
            return io.BytesIO()

        async def write(
            self,
            path: Path,
            data: io.IOBase,
            *,
            user: str | None = None,
        ) -> None:
            _ = (path, data, user)

        async def running(self) -> bool:
            return True

        async def persist_workspace(self) -> io.IOBase:
            return io.BytesIO()

        async def hydrate_workspace(self, data: io.IOBase) -> None:
            _ = data

    other = SandboxSession(_OtherInner())

    with pytest.raises(TypeError, match="SailboxSandboxSession"):
        asyncio.run(SailboxSandboxClient().delete(other))
