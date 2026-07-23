from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import ANY

import pytest
from azure.containerapps.sandbox import PortAuthConfig, SandboxPort, SandboxStateDetails
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError

from agents.extensions.sandbox.aca import (
    ACASandboxesClient,
    ACASandboxesClientOptions,
    ACASandboxesSession,
    ACASandboxesSessionState,
)
from agents.sandbox import Manifest
from agents.sandbox.entries import File, InContainerMountStrategy, MountpointMountPattern, S3Mount
from agents.sandbox.errors import (
    ExposedPortUnavailableError,
    MountConfigError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
)
from agents.sandbox.manifest import Environment
from agents.sandbox.snapshot import LocalSnapshot, NoopSnapshot


class _FakePoller:
    def __init__(self, result: object) -> None:
        self._result = result

    async def result(self) -> object:
        return self._result


class _FakeSandboxClient:
    def __init__(
        self,
        sandbox_id: str = "aca-sandbox-123",
        *,
        state: str = "Running",
        stopped_reason: Literal["Idle", "Disabled", "UserStopped"] | None = None,
        ports: list[SandboxPort] | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.state = state
        self.stopped_reason = stopped_reason
        self.ports = list(ports or [])
        self.exec_calls: list[tuple[str, str | None]] = []
        self.read_calls: list[str] = []
        self.write_calls: list[tuple[str, bytes]] = []
        self.stat_calls: list[str] = []
        self.mkdir_calls: list[str] = []
        self.ensure_running_calls: list[int] = []
        self.add_port_calls: list[tuple[int, bool]] = []
        self.stop_calls = 0
        self.close_calls = 0
        self.ensure_running_error: BaseException | None = None
        self.get_error: BaseException | None = None
        self.workspace_root_exists = False
        self.exec_result = SimpleNamespace(stdout="stdout", stderr="stderr", exit_code=7)
        self.files: dict[str, bytes] = {}

    async def exec(self, command: str, *, working_directory: str | None = None) -> object:
        self.exec_calls.append((command, working_directory))
        return self.exec_result

    async def read_file(self, path: str) -> bytes:
        self.read_calls.append(path)
        return self.files.get(path, b"file contents")

    async def write_file(self, path: str, content: str | bytes) -> None:
        payload = content.encode() if isinstance(content, str) else content
        self.write_calls.append((path, payload))
        self.files[path] = payload

    async def mkdir(self, path: str) -> None:
        self.mkdir_calls.append(path)
        self.workspace_root_exists = True

    async def stat_file(self, path: str) -> object:
        self.stat_calls.append(path)
        if not self.workspace_root_exists:
            raise ResourceNotFoundError("missing")
        return SimpleNamespace(path=path, is_directory=True)

    async def get(self) -> object:
        if self.get_error is not None:
            raise self.get_error
        details = (
            SandboxStateDetails(stopped_reason=self.stopped_reason)
            if self.stopped_reason is not None
            else None
        )
        return SimpleNamespace(state=self.state, state_details=details, ports=self.ports)

    async def ensure_running(self, *, timeout: int = 300) -> None:
        self.ensure_running_calls.append(timeout)
        if self.ensure_running_error is not None:
            raise self.ensure_running_error
        self.state = "Running"

    async def add_port(self, port: int, *, anonymous: bool = False) -> SandboxPort:
        self.add_port_calls.append((port, anonymous))
        added = SandboxPort(port=port, url=f"https://aca.example:{port}/?token=abc")
        self.ports.append(added)
        return added

    async def stop(self) -> None:
        self.stop_calls += 1
        self.state = "Stopped"

    async def close(self) -> None:
        self.close_calls += 1


class _FakeGroupClient:
    def __init__(self, sandbox_client: _FakeSandboxClient | None = None) -> None:
        self.sandbox_client = sandbox_client or _FakeSandboxClient()
        self.create_calls: list[dict[str, object]] = []
        self.get_client_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.close_calls = 0

    async def begin_create_sandbox(self, **kwargs: object) -> _FakePoller:
        self.create_calls.append(dict(kwargs))
        return _FakePoller(self.sandbox_client)

    def get_sandbox_client(self, sandbox_id: str) -> _FakeSandboxClient:
        self.get_client_calls.append(sandbox_id)
        return self.sandbox_client

    async def begin_delete_sandbox(self, sandbox_id: str) -> _FakePoller:
        self.delete_calls.append(sandbox_id)
        return _FakePoller(None)

    async def close(self) -> None:
        self.close_calls += 1


def _client(
    group_client: _FakeGroupClient,
    **overrides: object,
) -> ACASandboxesClient:
    return ACASandboxesClient(
        region=str(overrides.get("region", "eastus2")),
        subscription_id=str(overrides.get("subscription_id", "subscription-123")),
        resource_group=str(overrides.get("resource_group", "resource-group")),
        sandbox_group=str(overrides.get("sandbox_group", "sandbox-group")),
        group_client=group_client,  # type: ignore[arg-type]
    )


def _state(
    *,
    sandbox_id: str = "aca-sandbox-123",
    manifest: Manifest | None = None,
    exposed_ports: tuple[int, ...] = (),
    exposed_port_anonymous: bool = False,
    ensure_running_timeout_seconds: float = 12.5,
) -> ACASandboxesSessionState:
    return ACASandboxesSessionState(
        snapshot=NoopSnapshot(id="snapshot-123"),
        manifest=manifest or Manifest(),
        exposed_ports=exposed_ports,
        sandbox_id=sandbox_id,
        subscription_id="subscription-123",
        resource_group="resource-group",
        sandbox_group="sandbox-group",
        region="eastus2",
        exposed_port_anonymous=exposed_port_anonymous,
        ensure_running_timeout_seconds=ensure_running_timeout_seconds,
    )


@pytest.mark.asyncio
async def test_create_forwards_options_and_manifest_environment() -> None:
    sandbox_client = _FakeSandboxClient()
    group_client = _FakeGroupClient(sandbox_client)
    client = _client(group_client)
    manifest = Manifest(
        environment=Environment(value={"MANIFEST_VALUE": "manifest"}),
        entries={"README.md": File(content=b"hello")},
    )

    session = await client.create(
        manifest=manifest,
        options=ACASandboxesClientOptions(
            disk="ubuntu",
            cpu="2000m",
            memory="4096Mi",
            disk_size="20Gi",
            auto_suspend_seconds=600,
            auto_suspend_mode="Disk",
            labels={"purpose": "test"},
            environment={"OPTION_VALUE": "option"},
            exposed_ports=(8080,),
            exposed_port_anonymous=True,
            polling_interval_seconds=1.2,
            polling_timeout_seconds=9.1,
            ensure_running_timeout_seconds=17.0,
        ),
    )

    assert group_client.create_calls == [
        {
            "disk": "ubuntu",
            "auto_suspend_seconds": 600,
            "auto_suspend_mode": "Disk",
            "polling_interval": 2,
            "polling_timeout": 10,
            "cpu": "2000m",
            "memory": "4096Mi",
            "disk_size": "20Gi",
            "labels": {"purpose": "test"},
            "environment": {
                "OPTION_VALUE": "option",
                "MANIFEST_VALUE": "manifest",
            },
            "ports": [ANY],
        }
    ]
    port_request = group_client.create_calls[0]["ports"][0]  # type: ignore[index]
    assert port_request.port == 8080
    assert port_request.auth == PortAuthConfig(anonymous=True)
    state = cast(ACASandboxesSessionState, session.state)
    assert state.sandbox_id == "aca-sandbox-123"
    assert state.disk_size == "20Gi"
    assert state.auto_suspend_seconds == 600
    assert state.auto_suspend_mode == "Disk"
    assert state.environment == {"OPTION_VALUE": "option"}


@pytest.mark.asyncio
async def test_create_uses_authenticated_ports_by_default() -> None:
    group_client = _FakeGroupClient()
    client = _client(group_client)

    await client.create(options=ACASandboxesClientOptions(exposed_ports=(8080,)))

    port_request = group_client.create_calls[0]["ports"][0]  # type: ignore[index]
    assert port_request.port == 8080
    assert port_request.auth is None


@pytest.mark.asyncio
async def test_start_materializes_manifest_after_creating_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_client = _FakeSandboxClient()
    sandbox_client.exec_result = SimpleNamespace(stdout="", stderr="", exit_code=0)
    inner = ACASandboxesSession(
        state=_state(manifest=Manifest(entries={"README.md": File(content=b"hello")})),
        sandbox_client=sandbox_client,  # type: ignore[arg-type]
    )

    monkeypatch.setattr(inner, "_runtime_helpers", lambda: ())

    async def _validated_path(path: Path | str, *, for_write: bool = False) -> Path:
        _ = for_write
        value = Path(path)
        return value if value.is_absolute() else Path("/workspace") / value

    monkeypatch.setattr(inner, "_validate_path_access", _validated_path)

    await inner.start()

    assert sandbox_client.mkdir_calls == ["/workspace"]
    assert sandbox_client.stat_calls == ["/workspace"]
    assert sandbox_client.write_calls == [("/workspace/README.md", b"hello")]


@pytest.mark.asyncio
async def test_exec_read_and_write_use_aca_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_client = _FakeSandboxClient()
    inner = ACASandboxesSession(
        state=_state(),
        sandbox_client=sandbox_client,  # type: ignore[arg-type]
    )

    async def _validated_path(path: Path | str, *, for_write: bool = False) -> Path:
        _ = for_write
        return Path("/workspace") / Path(path)

    monkeypatch.setattr(inner, "_validate_path_access", _validated_path)

    result = await inner.exec("printf", "%s", "hello", shell=False)
    await inner.write(Path("written.txt"), io.BytesIO(b"payload"))
    data = await inner.read(Path("written.txt"))

    assert result.exit_code == 7
    assert result.stdout == b"stdout"
    assert result.stderr == b"stderr"
    assert sandbox_client.exec_calls == [
        ("printf %s hello", "/workspace"),
    ]
    assert sandbox_client.write_calls == [("/workspace/written.txt", b"payload")]
    assert sandbox_client.read_calls == ["/workspace/written.txt"]
    assert data.read() == b"payload"


@pytest.mark.asyncio
async def test_resume_reconnects_to_existing_sandbox() -> None:
    sandbox_client = _FakeSandboxClient(state="Suspended")
    group_client = _FakeGroupClient(sandbox_client)
    client = _client(group_client)
    state = _state()

    session = await client.resume(state)

    assert group_client.get_client_calls == ["aca-sandbox-123"]
    assert sandbox_client.ensure_running_calls == [13]
    assert session.state is state
    assert session._inner._workspace_state_preserved_on_start() is True


@pytest.mark.asyncio
async def test_resume_rejects_stale_state() -> None:
    sandbox_client = _FakeSandboxClient()
    sandbox_client.get_error = ResourceNotFoundError("missing")
    client = _client(_FakeGroupClient(sandbox_client))

    with pytest.raises(WorkspaceStartError, match="serialized session state is stale") as exc_info:
        await client.resume(_state())

    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_resume_rejects_administratively_disabled_sandbox() -> None:
    sandbox_client = _FakeSandboxClient(state="Stopped", stopped_reason="Disabled")
    client = _client(_FakeGroupClient(sandbox_client))

    with pytest.raises(WorkspaceStartError, match="administratively disabled") as exc_info:
        await client.resume(_state())

    assert exc_info.value.retryable is False
    assert sandbox_client.ensure_running_calls == []


@pytest.mark.asyncio
async def test_resume_rechecks_state_when_ensure_running_detects_disabled_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_client = _FakeSandboxClient(state="Suspended")
    client = _client(_FakeGroupClient(sandbox_client))

    async def _disabled_during_resume(*, timeout: int = 300) -> None:
        sandbox_client.ensure_running_calls.append(timeout)
        sandbox_client.state = "Stopped"
        sandbox_client.stopped_reason = "Disabled"
        raise RuntimeError("provider wording may change")

    monkeypatch.setattr(sandbox_client, "ensure_running", _disabled_during_resume)

    with pytest.raises(WorkspaceStartError, match="administratively disabled") as exc_info:
        await client.resume(_state())

    assert exc_info.value.retryable is False
    assert exc_info.value.context["stopped_reason"] == "Disabled"


@pytest.mark.asyncio
async def test_resume_reports_timeout() -> None:
    sandbox_client = _FakeSandboxClient(state="Suspended")
    sandbox_client.ensure_running_error = TimeoutError()
    client = _client(_FakeGroupClient(sandbox_client))

    with pytest.raises(WorkspaceStartError, match="within 12.5 seconds") as exc_info:
        await client.resume(_state())

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_resume_reports_unexpected_state() -> None:
    sandbox_client = _FakeSandboxClient(state="Creating")
    sandbox_client.ensure_running_error = RuntimeError("unexpected state")
    client = _client(_FakeGroupClient(sandbox_client))

    with pytest.raises(WorkspaceStartError, match="non-resumable state 'Creating'"):
        await client.resume(_state())


@pytest.mark.asyncio
async def test_resume_preserves_retryable_transport_error() -> None:
    sandbox_client = _FakeSandboxClient(state="Suspended")
    sandbox_client.ensure_running_error = ServiceRequestError("network unavailable")
    client = _client(_FakeGroupClient(sandbox_client))

    with pytest.raises(WorkspaceStartError, match="could not be resumed") as exc_info:
        await client.resume(_state())

    assert exc_info.value.retryable is True
    assert exc_info.value.context["state"] == "Suspended"


@pytest.mark.asyncio
async def test_delete_stops_handle_then_deletes_resource() -> None:
    sandbox_client = _FakeSandboxClient()
    group_client = _FakeGroupClient(sandbox_client)
    client = _client(group_client)
    session = await client.create()

    await client.delete(session)

    assert sandbox_client.stop_calls == 1
    assert sandbox_client.close_calls == 1
    assert group_client.delete_calls == ["aca-sandbox-123"]


@pytest.mark.asyncio
async def test_resolve_existing_and_missing_exposed_ports() -> None:
    existing = SandboxPort(port=8000, url="https://existing.example/?sig=one")
    sandbox_client = _FakeSandboxClient(ports=[existing])
    existing_session = ACASandboxesSession(
        state=_state(exposed_ports=(8000,)),
        sandbox_client=sandbox_client,  # type: ignore[arg-type]
    )
    added_session = ACASandboxesSession(
        state=_state(exposed_ports=(9000,), exposed_port_anonymous=True),
        sandbox_client=sandbox_client,  # type: ignore[arg-type]
    )

    existing_endpoint = await existing_session.resolve_exposed_port(8000)
    added_endpoint = await added_session.resolve_exposed_port(9000)

    assert existing_endpoint.host == "existing.example"
    assert existing_endpoint.port == 443
    assert existing_endpoint.tls is True
    assert existing_endpoint.query == "sig=one"
    assert added_endpoint.host == "aca.example"
    assert added_endpoint.port == 9000
    assert added_endpoint.tls is True
    assert added_endpoint.query == "token=abc"
    assert sandbox_client.add_port_calls == [(9000, True)]


@pytest.mark.asyncio
async def test_resolve_exposed_port_rejects_missing_url() -> None:
    sandbox_client = _FakeSandboxClient(ports=[SandboxPort(port=8080)])
    inner = ACASandboxesSession(
        state=_state(exposed_ports=(8080,)),
        sandbox_client=sandbox_client,  # type: ignore[arg-type]
    )

    with pytest.raises(ExposedPortUnavailableError, match="could not be resolved"):
        await inner.resolve_exposed_port(8080)


@pytest.mark.asyncio
async def test_read_maps_missing_file_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_client = _FakeSandboxClient()
    inner = ACASandboxesSession(
        state=_state(),
        sandbox_client=sandbox_client,  # type: ignore[arg-type]
    )

    async def _validated_path(path: Path | str, *, for_write: bool = False) -> Path:
        _ = (path, for_write)
        return Path("/workspace/missing.txt")

    async def _missing_file(path: str) -> bytes:
        _ = path
        raise ResourceNotFoundError("missing")

    monkeypatch.setattr(inner, "_validate_path_access", _validated_path)
    monkeypatch.setattr(sandbox_client, "read_file", _missing_file)

    with pytest.raises(WorkspaceReadNotFoundError, match="file not found"):
        await inner.read(Path("missing.txt"))


@pytest.mark.asyncio
async def test_v1_unsupported_features_fail_explicitly() -> None:
    sandbox_client = _FakeSandboxClient()
    inner = ACASandboxesSession(
        state=_state(),
        sandbox_client=sandbox_client,  # type: ignore[arg-type]
    )

    assert inner.supports_pty() is False
    with pytest.raises(NotImplementedError, match="does not support PTY sessions"):
        await inner.pty_exec_start("bash", tty=True)
    with pytest.raises(NotImplementedError, match="does not support PTY sessions"):
        await inner.pty_write_stdin(session_id=1, chars="x")
    with pytest.raises(NotImplementedError, match="does not integrate native ACA snapshots"):
        await inner.persist_workspace()
    with pytest.raises(NotImplementedError, match="does not integrate native ACA snapshots"):
        await inner.hydrate_workspace(io.BytesIO())


@pytest.mark.asyncio
async def test_create_rejects_manifest_mounts_before_allocation() -> None:
    group_client = _FakeGroupClient()
    client = _client(group_client)
    manifest = Manifest(
        entries={
            "mounted": S3Mount(
                bucket="test-bucket",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            )
        }
    )

    with pytest.raises(MountConfigError, match="does not support manifest mount entries"):
        await client.create(manifest=manifest)

    assert group_client.create_calls == []


@pytest.mark.asyncio
async def test_create_rejects_openai_snapshot_lifecycle_before_allocation(tmp_path: Path) -> None:
    group_client = _FakeGroupClient()
    client = _client(group_client)

    with pytest.raises(NotImplementedError, match="does not integrate native ACA snapshots"):
        await client.create(snapshot=LocalSnapshot(id="snapshot-123", base_path=tmp_path))

    assert group_client.create_calls == []


@pytest.mark.asyncio
async def test_resume_rejects_openai_snapshot_lifecycle_before_reconnect(tmp_path: Path) -> None:
    group_client = _FakeGroupClient()
    client = _client(group_client)
    state = _state()
    state.snapshot = LocalSnapshot(id="snapshot-123", base_path=tmp_path)

    with pytest.raises(NotImplementedError, match="does not integrate native ACA snapshots"):
        await client.resume(state)

    assert group_client.get_client_calls == []


def test_deserialize_session_state_and_scope_validation() -> None:
    client = _client(_FakeGroupClient())
    state = _state()
    payload = client.serialize_session_state(state)

    restored = client.deserialize_session_state(payload)

    assert isinstance(restored, ACASandboxesSessionState)
    assert restored == state


@pytest.mark.asyncio
async def test_close_does_not_close_injected_group_client() -> None:
    group_client = _FakeGroupClient()
    client = _client(group_client)

    await client.close()

    assert group_client.close_calls == 0
