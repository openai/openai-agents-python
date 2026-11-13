"""Provider resource ownership through the public native environment source."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import pytest

from agents import Agent, Environment
from agents.classic.sandbox.errors import MountConfigError
from agents.classic.sandbox.session.sandbox_client import BaseSandboxClientOptions
from agents.classic.testing.sandbox import scripted_sandbox_session
from agents.sandbox import (
    BaseSandboxClient,
    Dir,
    DockerVolumeMountStrategy,
    Manifest,
    S3Mount,
    SandboxSession,
    render_storage_layout,
    sandbox_environment,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider


class Options(BaseSandboxClientOptions):
    type: Literal["native_storage_test"] = "native_storage_test"
    labels: dict[str, str] = {}


class Client(BaseSandboxClient[Options]):
    backend_id = "docker"

    def __init__(self, events: list[str], failure: str = "") -> None:
        self.events = events
        self.failure = failure
        self.created = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.session: SandboxSession | None = None
        self.manifest: Manifest | None = None
        self.options: Options | None = None

    async def create(self, *, manifest=None, options, snapshot=None) -> SandboxSession:
        assert snapshot is None
        self._validate_manifest_for_create(manifest)
        self.manifest, self.options = manifest, options
        self.events.append("create")
        self.created.set()
        if self.failure == "create":
            raise RuntimeError("provider create failed before returning ownership")
        if self.failure == "late":
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.release.wait()
        session = self._wrap_session(scripted_sandbox_session(manifest=manifest))
        self.session = session

        async def start():
            self.events.append("start")
            if self.failure == "start":
                raise RuntimeError("start failed")
            if self.failure == "late_start":
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    await self.release.wait()

        async def close():
            self.events.append("close")
            if self.failure == "close":
                raise RuntimeError("close failed")

        session.start = start
        session.aclose = close
        return session

    async def delete(self, session: SandboxSession) -> SandboxSession:
        assert session is self.session
        self.events.append("delete")
        if self.failure == "delete":
            raise RuntimeError("delete failed")
        return session

    async def resume(self, state):
        pytest.fail("The adapter must never resume resources")

    def deserialize_session_state(self, payload):
        pytest.fail("The adapter must never deserialize authority")


def declaration(**changes: Any) -> S3Mount:
    return S3Mount(
        bucket="synthetic-bucket",
        mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
        **changes,
    )


@pytest.mark.parametrize(
    "failure", ["", "create", "start", "connect", "body", "exit", "close", "delete"]
)
async def test_adapter_cleanup_order_and_failures(failure: str) -> None:
    events: list[str] = []
    client = Client(events, failure)

    @asynccontextmanager
    async def connect(session: SandboxSession) -> AsyncIterator[Environment[None]]:
        assert session is client.session
        events.append("connect")
        if failure == "connect":
            raise RuntimeError("connect failed")
        try:
            yield Environment(url="wss://executor.invalid", cwd="/workspace")
        finally:
            events.append("exit")
            if failure == "exit":
                raise RuntimeError("exit failed")

    async def use():
        async with sandbox_environment(
            client, manifest=Manifest(), options=Options(), connect=connect
        ):
            events.append("body")
            if failure == "body":
                raise RuntimeError("body failed")

    if failure:
        with pytest.raises(RuntimeError, match=failure):
            await use()
    else:
        await use()
    expected = ["create"]
    if failure != "create":
        expected += ["start"]
        if failure != "start":
            expected += ["connect"]
            if failure != "connect":
                expected += ["body", "exit"]
        expected += ["close", "delete"]
    assert events == expected


async def test_later_cleanup_runs_when_connector_exit_and_close_fail() -> None:
    events: list[str] = []
    client = Client(events, "close")

    @asynccontextmanager
    async def connect(session):
        try:
            yield Environment(url="wss://executor.invalid", cwd="/workspace")
        finally:
            events.append("exit")
            raise RuntimeError("gateway cleanup failed")

    with pytest.raises(RuntimeError, match="close failed"):
        async with sandbox_environment(
            client, manifest=Manifest(), options=Options(), connect=connect
        ):
            pass
    assert events[-3:] == ["exit", "close", "delete"]


async def test_configuration_is_captured_and_strategies_are_explicit() -> None:
    events: list[str] = []
    client = Client(events)
    mount = declaration(secret_access_key="synthetic-secret")
    manifest = Manifest(entries={"data": mount})
    options = Options(labels={"owner": "original"})

    @asynccontextmanager
    async def connect(session):
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    source = sandbox_environment(client, manifest=manifest, options=options, connect=connect)
    mount.read_only = False
    manifest.entries.clear()
    options.labels["owner"] = "changed"
    async with source:
        assert client.manifest is not None
        captured = client.manifest.entries["data"]
        assert isinstance(captured, S3Mount) and captured.read_only is True
        assert captured.secret_access_key == "synthetic-secret"
        assert client.options is not None and client.options.labels == {"owner": "original"}
    with pytest.raises(ValueError, match="mount_strategy"):
        S3Mount(bucket="synthetic")


async def test_provider_option_dictionaries_use_owning_models() -> None:
    client = Client([])

    @asynccontextmanager
    async def connect(session):
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    async with sandbox_environment(
        client,
        manifest=Manifest(entries={"data": declaration()}),
        options={"type": "native_storage_test", "labels": {"owner": "dict"}},
        connect=connect,
    ):
        assert isinstance(client.options, Options)
        assert client.options.labels == {"owner": "dict"}
        assert isinstance(client.manifest.entries["data"], S3Mount)


@pytest.mark.parametrize("mapping_proxy", [False, True])
@pytest.mark.parametrize("helper", ["source", "layout"])
def test_manifest_mappings_cannot_select_provider_secrets(mapping_proxy: bool, helper: str) -> None:
    from dataclasses import asdict
    from types import MappingProxyType

    from agents.classic.extensions.sandbox.modal.mounts import ModalCloudBucketMountStrategy

    secret = "synthetic-application-secret"
    mount = S3Mount(
        bucket="synthetic-bucket",
        mount_strategy=ModalCloudBucketMountStrategy(
            secret_name=secret, secret_environment_name="synthetic-environment"
        ),
    )
    payload = {"entries": {"data": mount.model_dump()}}
    value: Any = MappingProxyType(payload) if mapping_proxy else payload
    client = Client([])

    @asynccontextmanager
    async def connect(session):
        pytest.fail("A mapping must not select provider credentials")
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    with pytest.raises(MountConfigError, match="trusted Manifest") as caught:
        if helper == "layout":
            render_storage_layout(value)
        else:
            sandbox_environment(client, manifest=value, options=Options(), connect=connect)
    assert client.events == []
    assert secret not in json.dumps(asdict(caught.value), default=str)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


async def test_trusted_manifest_preserves_provider_secret_selection() -> None:
    from agents.classic.extensions.sandbox.modal.mounts import ModalCloudBucketMountStrategy

    client = Client([])
    client.backend_id = "modal"
    strategy = ModalCloudBucketMountStrategy(
        secret_name="synthetic-application-secret", secret_environment_name="synthetic-environment"
    )
    manifest = Manifest(
        entries={"data": S3Mount(bucket="synthetic-bucket", mount_strategy=strategy)}
    )

    @asynccontextmanager
    async def connect(session):
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    async with sandbox_environment(client, manifest=manifest, options=Options(), connect=connect):
        assert client.manifest is not manifest
        assert client.manifest.entries["data"].mount_strategy == strategy
        assert strategy.secret_name not in render_storage_layout(client.manifest)


@pytest.mark.parametrize("kind", ["grant", "file", "directory"])
@pytest.mark.parametrize("mapping_proxy", [False, True])
@pytest.mark.parametrize("helper", ["source", "layout"])
def test_manifest_mappings_do_not_grant_host_authority(
    kind: str, mapping_proxy: bool, helper: str
) -> None:
    from types import MappingProxyType

    payload: dict[str, Any] = (
        {"extra_path_grants": [{"path": "/data", "host_path": "/private", "read_only": False}]}
        if kind == "grant"
        else {
            "entries": {
                "data": {"type": "local_file" if kind == "file" else "local_dir", "src": "private"}
            }
        }
    )
    value = MappingProxyType(payload) if mapping_proxy else payload
    client = Client([])

    @asynccontextmanager
    async def connect(session):
        pytest.fail("A mapping must not grant access to host paths")
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    with pytest.raises(MountConfigError):
        if helper == "layout":
            render_storage_layout(value)
        else:
            sandbox_environment(client, manifest=value, options=Options(), connect=connect)
    assert client.events == []


@pytest.mark.parametrize("root", ["/workspace", "/vercel/sandbox"])
def test_layout_only_renders_explicit_safe_fields_and_preserves_identity(root: str) -> None:
    from agents.classic.sandbox.entries import S3Mount as OwningMount

    mount = declaration(
        secret_access_key="secret-do-not-render",
        access_key_id="id-do-not-render",
        endpoint_url="https://secret-endpoint.invalid",
        description='data\n"quoted"',
        mount_path=Path("/data/reports"),
    )
    manifest = Manifest(
        root=root, entries={"data": mount, "writable": declaration(read_only=False)}
    )
    rendered = render_storage_layout(manifest)
    layout = json.loads(rendered)
    layout["storage"].sort(key=lambda entry: entry["path"])
    assert layout == {
        "workspace": root,
        "storage": [
            {"path": "/data/reports", "access": "read_only", "description": 'data\n"quoted"'},
            {"path": f"{root}/writable", "access": "read_write"},
        ],
    }
    assert "do-not-render" not in rendered and "endpoint" not in rendered
    assert S3Mount is OwningMount
    assert S3Mount.__module__.startswith("agents.classic.")


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("drive_path", ["", "/mounted/drive", "relative/drive"])
def test_blaxel_drive_layout_uses_provider_access_and_path(
    read_only: bool, drive_path: str
) -> None:
    from agents.classic.extensions.sandbox.blaxel.mounts import (
        BlaxelDriveMount,
        BlaxelDriveMountStrategy,
    )

    mount = BlaxelDriveMount(
        drive_name="synthetic-drive",
        drive_read_only=read_only,
        drive_mount_path=drive_path,
        mount_strategy=BlaxelDriveMountStrategy(),
    )
    assert json.loads(render_storage_layout(Manifest(entries={"drive": mount})))["storage"] == [
        {
            "path": (Path("/") / (drive_path or "/workspace/drive")).as_posix(),
            "access": "read_only" if read_only else "read_write",
        }
    ]


@pytest.mark.parametrize("read_only", [False, True])
async def test_blaxel_drive_mount_forwards_read_only_to_provider(read_only: bool) -> None:
    from types import SimpleNamespace

    from agents.classic.extensions.sandbox.blaxel.mounts import (
        BlaxelDriveMountConfig,
        _attach_drive,
    )

    observed = {}

    async def mount(drive_name, mount_path, drive_path="/", read_only=False):
        observed.update(name=drive_name, path=mount_path, source=drive_path, read_only=read_only)

    await _attach_drive(
        SimpleNamespace(drives=SimpleNamespace(mount=mount)),
        BlaxelDriveMountConfig("synthetic-drive", "/mounted/drive", "/source", read_only),
    )
    assert observed == {
        "name": "synthetic-drive",
        "path": "/mounted/drive",
        "source": "/source",
        "read_only": read_only,
    }


@pytest.mark.parametrize("stage", ["create", "start", "connect"])
async def test_native_source_joins_late_create_and_cleans_it(
    responses: ResponsesFixture, stage: str
) -> None:
    events: list[str] = []
    client = Client(
        events, "late" if stage == "create" else "late_start" if stage == "start" else ""
    )

    @asynccontextmanager
    async def connect(session):
        events.append("connect")
        try:
            if stage == "connect":
                try:
                    await client.release.wait()
                except asyncio.CancelledError:
                    client.cancelled.set()
                    await client.release.wait()
            yield Environment(url="wss://executor.invalid", cwd="/workspace")
        finally:
            events.append("exit")

    async def open_session():
        async with Agent(instructions="Inspect.", provider=provider(responses)).session(
            environment=sandbox_environment(
                client, manifest=Manifest(), options=Options(), connect=connect
            )
        ):
            pytest.fail("A cancelled opening acquired native resources")

    async with timeout(20):
        opening = asyncio.create_task(open_session())
        await client.created.wait()
        opening.cancel()
        await client.cancelled.wait()
        assert not opening.done()
        client.release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
    assert client.session is not None
    assert events[-2:] == ["close", "delete"]
    assert responses.requests == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executor fixture")
@pytest.mark.parametrize(
    "run_command", [False, pytest.param(True, marks=pytest.mark.requires_native_linux_sandbox)]
)
async def test_native_agent_uses_adapter_gateway_and_retires_before_cleanup(
    tmp_path: Path,
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    run_command: bool,
) -> None:
    events: list[str] = []
    client = Client(events)
    peer = ExecutorPeer(tmp_path / "remote")
    manifest = Manifest(
        root=str(peer.root),
        entries={
            "data": declaration(
                access_key_id="storage-id-sentinel", secret_access_key="storage-secret-sentinel"
            ),
        },
    )

    @asynccontextmanager
    async def connect(session):
        assert session is client.session
        async with peer.serve():
            monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
            monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
            events.append("connect")
            yield Environment(
                url=peer.url,
                cwd=str(session.state.manifest.root),
                headers={"Authorization": "Bearer " + peer.token},
                tools=["exec_command"],
            )
            await peer.wait_disconnected()
            events.append("retired")

    if run_command:
        responses.respond(
            function_call(
                "exec_command",
                {"cmd": "printf adapter-read > adapter-proof; cat adapter-proof"},
                "adapter-command",
            )
        )
    responses.respond(assistant_output("done"))
    async with timeout(30):
        async with Agent(
            instructions="Read.\n" + render_storage_layout(manifest), provider=provider(responses)
        ).session(
            environment=sandbox_environment(
                client,
                manifest=manifest,
                options=Options(),
                connect=connect,
            )
        ) as native:
            result = await (await native.submit("Inspect.")).get_result()
            saved = (await native.save_state()).to_json()
            for secret in ("storage-id-sentinel", "storage-secret-sentinel", peer.token):
                assert secret not in saved
                assert secret not in json.dumps(responses.requests)
    assert result.final_output == "done"
    if run_command:
        assert "adapter-read" in tool_output(responses.requests[1], "adapter-command")
        assert (peer.root / "adapter-proof").read_text() == "adapter-read"
    assert events == ["create", "start", "connect", "retired", "close", "delete"]


def configured_mount(kind: str):
    from agents.sandbox import (
        AzureBlobMount,
        BoxMount,
        GCSMount,
        InContainerMountStrategy,
        R2Mount,
        S3FilesMount,
        S3FilesMountPattern,
    )

    strategy = DockerVolumeMountStrategy(driver="rclone")
    mounts = {
        "s3": declaration(),
        "gcs": GCSMount(bucket="synthetic", mount_strategy=strategy),
        "r2": R2Mount(bucket="synthetic", account_id="account", mount_strategy=strategy),
        "azure": AzureBlobMount(account="account", container="data", mount_strategy=strategy),
        "box": BoxMount(mount_strategy=strategy),
        "s3_files": S3FilesMount(
            file_system_id="fs-synthetic",
            mount_strategy=InContainerMountStrategy(pattern=S3FilesMountPattern()),
        ),
    }
    return mounts[kind]


@pytest.mark.parametrize("kind", ["s3", "gcs", "r2", "azure", "box", "s3_files"])
async def test_all_typed_mounts_reach_owning_docker_preparation(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from unittest.mock import MagicMock

    from agents.classic.sandbox.sandboxes.docker import (
        DockerSandboxClient,
        DockerSandboxClientOptions,
        _build_docker_volume_mounts,
    )

    mount = configured_mount(kind)
    manifest = Manifest(entries={"data": mount})
    if kind == "s3_files":
        manifest = manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
    api = MagicMock()
    client = DockerSandboxClient(api)
    prepared = []
    volume_wire = []

    async def prepare(image, *, manifest, **kwargs):
        prepared.extend(manifest.mount_targets())
        if kind != "s3_files":
            volume_wire.extend(_build_docker_volume_mounts(manifest, session_id=None))
        raise RuntimeError("stop before real infrastructure")

    monkeypatch.setattr(client, "_create_container", prepare)

    @asynccontextmanager
    async def connect(session):
        pytest.fail("No resource was returned by the intercepted provider")
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    with pytest.raises((RuntimeError, MountConfigError)):
        async with sandbox_environment(
            client,
            manifest=manifest,
            options=DockerSandboxClientOptions(image="synthetic"),
            connect=connect,
        ):
            pytest.fail("The provider probe must stop before resource creation")
    if kind != "s3_files":
        assert len(volume_wire) == 1 and volume_wire[0]["ReadOnly"] is True
    assert len(prepared) == 1
    captured, path = prepared[0]
    assert type(captured) is type(mount) and captured.read_only is True
    assert path.as_posix() == "/workspace/data"
    assert api.containers.create.call_count == 0


@pytest.mark.parametrize("invalid", ["s3", "gcs", "r2", "azure", "box", "s3_files", "credentials"])
async def test_provider_rejects_before_infrastructure_or_connector(
    monkeypatch: pytest.MonkeyPatch,
    responses: ResponsesFixture,
    invalid: str,
) -> None:
    from unittest.mock import MagicMock

    from agents.classic.sandbox.sandboxes.docker import (
        DockerSandboxClient,
        DockerSandboxClientOptions,
    )
    from agents.sandbox import InContainerMountStrategy, RcloneMountPattern

    if invalid != "credentials":
        mount = configured_mount(invalid)
        raw = mount.model_dump()
        if invalid == "s3_files":
            raw["mount_strategy"]["pattern"] = RcloneMountPattern().model_dump()
        else:
            raw["mount_strategy"]["driver"] = "unsupported"
        manifest = {"entries": {"data": raw}}
    else:
        mount = S3Mount(
            bucket="synthetic",
            access_key_id="synthetic-id",
            secret_access_key="secret-sentinel",
            mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
        )
        manifest = Manifest(entries={"data": mount})
    api = MagicMock()
    client = DockerSandboxClient(api)
    api.containers.create.side_effect = AssertionError("Container creation must not run")

    @asynccontextmanager
    async def connect(session):
        pytest.fail("The gateway must not start")
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    if invalid != "credentials":
        with pytest.raises(MountConfigError):
            render_storage_layout(manifest)

    with pytest.raises(MountConfigError) as error:
        async with Agent(instructions="Inspect.", provider=provider(responses)).session(
            environment=sandbox_environment(
                client,
                manifest=manifest,
                options=DockerSandboxClientOptions(image="synthetic"),
                connect=connect,
            )
        ):
            pytest.fail("Invalid provider mount opened a session")
    assert "secret-sentinel" not in str(error.value)
    api.containers.create.assert_not_called()
    api.images.pull.assert_not_called()
    api.volumes.create.assert_not_called()
    assert responses.requests == []


@pytest.mark.skipif(
    not os.environ.get("OPENAI_AGENTS_DOCKER_TEST_IMAGE"),
    reason="requires an explicitly selected preloaded Docker test image",
)
async def test_actual_docker_provider_mount_and_disposal(tmp_path: Path) -> None:
    import docker

    from agents.classic.sandbox.sandboxes.docker import (
        DockerSandboxClient,
        DockerSandboxClientOptions,
    )
    from agents.classic.sandbox.workspace_paths import SandboxPathGrant

    source = tmp_path / "data"
    source.mkdir()
    marker = source / "marker"
    marker.write_text("caller-owned")
    mode = marker.stat().st_mode
    api = docker.from_env()
    client = DockerSandboxClient(api)
    container_id = None

    @asynccontextmanager
    async def connect(session: SandboxSession):
        nonlocal container_id
        container_id = session.state.container_id
        container = api.containers.get(container_id)
        assert container.status == "running"
        mounted = next(item for item in container.attrs["Mounts"] if item["Destination"] == "/data")
        assert mounted["RW"] is False
        command = (
            'set -e; test "$(cat /data/marker)" = caller-owned; '
            "if (printf forbidden > /data/marker) 2>/dev/null; then exit 12; fi"
        )
        result = await session.exec("sh", "-c", command, shell=False)
        assert result.ok()
        yield Environment(url="wss://executor.invalid", cwd="/workspace")
        assert api.containers.get(container_id).status == "running"

    try:
        async with sandbox_environment(
            client,
            manifest=Manifest(
                extra_path_grants=(
                    SandboxPathGrant(path="/data", host_path=str(source), read_only=True),
                )
            ),
            options=DockerSandboxClientOptions(
                image=os.environ["OPENAI_AGENTS_DOCKER_TEST_IMAGE"],
                network_mode="none",
            ),
            connect=connect,
        ):
            assert marker.read_text() == "caller-owned"
        assert container_id is not None
        with pytest.raises(docker.errors.NotFound):
            api.containers.get(container_id)
    finally:
        api.close()
    assert marker.read_text() == "caller-owned" and marker.stat().st_mode == mode


@pytest.mark.parametrize("helper", ["layout", "source"])
def test_empty_manifest_mapping_is_not_a_trusted_manifest(helper: str) -> None:
    client = Client([])
    payload: Any = {}

    @asynccontextmanager
    async def connect(session):
        pytest.fail("A mapping must not create a sandbox")
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    with pytest.raises(MountConfigError, match="trusted Manifest"):
        if helper == "layout":
            render_storage_layout(payload)
        else:
            sandbox_environment(client, manifest=payload, options=Options(), connect=connect)
    assert client.events == []


async def test_live_session_is_borrowed_and_never_deleted() -> None:
    events: list[str] = []
    client = Client(events)
    session = await client.create(manifest=Manifest(), options=Options())
    # The scripted session uses its own synthetic backend identifier.
    client.backend_id = session.state.type
    events.clear()

    @asynccontextmanager
    async def connect(selected):
        assert selected is session
        events.append("connect")
        try:
            yield Environment(url="wss://executor.invalid", cwd="/workspace")
        finally:
            events.append("gateway")

    async with sandbox_environment(
        client, manifest=Manifest(), options=Options(), session=session, connect=connect
    ):
        events.append("body")
    assert events == ["connect", "body", "gateway"]


async def test_authorized_resume_uses_current_manifest_and_owns_cleanup() -> None:
    events: list[str] = []
    client = Client(events)
    original = await client.create(manifest=Manifest(root="/current"), options=Options())
    client.backend_id = original.state.type
    events.clear()
    saved = original.state
    seen = []

    async def resume(state):
        seen.append(state)
        events.append("resume")
        original._inner.state = state
        return original

    client.resume = resume

    @asynccontextmanager
    async def connect(selected):
        assert selected is original
        events.append("connect")
        yield Environment(url="wss://executor.invalid", cwd="/current")

    async with sandbox_environment(
        client,
        manifest=Manifest(root="/current"),
        options=Options(),
        authorized_resume_state=saved,
        connect=connect,
    ):
        pass
    assert seen[0].manifest.root == "/current"
    assert saved.manifest.root == "/current"
    assert seen[0].manifest is not saved.manifest
    assert events == ["resume", "start", "connect", "close", "delete"]


def test_resume_rejects_mapping_backend_mismatch_and_conflicting_lifecycle() -> None:
    client = Client([])

    def connect(session):
        return None

    with pytest.raises(TypeError, match="application-authorized"):
        sandbox_environment(
            client,
            manifest=Manifest(),
            options=Options(),
            connect=connect,
            authorized_resume_state={"type": "docker"},
        )
    with pytest.raises(ValueError, match="mutually exclusive"):
        sandbox_environment(
            client,
            manifest=Manifest(),
            options=Options(),
            connect=connect,
            snapshot=object(),
            authorized_resume_state=object(),
        )


async def test_creation_forwards_snapshot_and_budgets_before_start() -> None:
    from agents.classic.sandbox.snapshot import NoopSnapshot
    from agents.sandbox import SandboxArchiveLimits, SandboxConcurrencyLimits

    events: list[str] = []
    client = Client(events)
    create = client.create
    snapshot = NoopSnapshot(id="synthetic-snapshot")
    limits = SandboxArchiveLimits(max_members=8)
    concurrency = SandboxConcurrencyLimits(manifest_entries=3)

    async def create_with_snapshot(*, manifest, options, snapshot):
        assert snapshot is not None
        selected = await create(manifest=manifest, options=options)
        start = selected.start

        async def start_with_budgets():
            assert selected._archive_limits == limits
            await start()

        selected.start = start_with_budgets
        return selected

    client.create = create_with_snapshot

    @asynccontextmanager
    async def connect(selected):
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    async with sandbox_environment(
        client,
        manifest=Manifest(),
        options=Options(),
        connect=connect,
        snapshot=snapshot,
        archive_limits=limits,
        concurrency_limits=concurrency,
    ):
        pass
    assert events == ["create", "start", "close", "delete"]


async def test_native_sandbox_pure_utilities_preserve_behavior() -> None:
    from agents.sandbox import (
        BackoffStrategy,
        TruncationPolicy,
        apply_diff,
        retry_async,
        truncate_text,
    )

    assert apply_diff("before\n", "@@\n-before\n+after\n") == "after\n"
    assert truncate_text("short", TruncationPolicy.bytes(10)) == "short"
    attempts = 0

    @retry_async(interval=0, max_attempt=2, backoff=BackoffStrategy.FIXED, retry_if=lambda _: True)
    async def transient():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic")
        return "done"

    assert await transient() == "done"
    assert attempts == 2


def test_native_sandbox_truncation_exposes_formatted_output_and_original_token_count() -> None:
    from agents.sandbox import (
        TruncationPolicy,
        formatted_truncate_text,
        formatted_truncate_text_with_token_count,
        truncate_with_token_budget,
    )

    text = "a" * 64 + "\n" + "é" * 80 + "\n" + "z" * 64
    policy = TruncationPolicy.tokens(30)
    bounded, count = truncate_with_token_budget(text, policy)
    formatted, formatted_count = formatted_truncate_text_with_token_count(text, 30)

    assert count == formatted_count == 73
    assert bounded.startswith("a")
    assert formatted.startswith("Total output lines: 3\n\n")
    assert formatted == formatted_truncate_text(text, policy)
    for result in (bounded, formatted):
        assert result.endswith("z")
        assert "tokens truncated" in result
        assert len(result.encode("utf-8")) <= policy.byte_budget()


@pytest.mark.parametrize("mode", ["borrowed", "resume"])
@pytest.mark.parametrize("mismatch", ["root", "topology", "credentials", "environment"])
async def test_live_mount_authority_must_match_before_gateway_or_resume(mode, mismatch) -> None:
    events: list[str] = []
    client = Client(events)
    original = Manifest(
        entries={
            "data": declaration(access_key_id="old-principal", secret_access_key="synthetic-secret")
        }
    )
    live = await client.create(manifest=original, options=Options())
    live.state.type = "docker"
    current = original.model_copy(deep=True)
    if mismatch == "root":
        current.root = "/other"
    elif mismatch == "topology":
        current.entries.clear()
    elif mismatch == "credentials":
        current.entries["data"].access_key_id = "new-principal"
    else:
        current.environment.value["AWS_ACCESS_KEY_ID"] = "new-principal"
    events.clear()

    @asynccontextmanager
    async def connect(selected):
        events.append("connect")
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    with pytest.raises((MountConfigError, ValueError, RuntimeError)):
        async with sandbox_environment(
            client,
            manifest=current,
            options=Options(),
            connect=connect,
            **(
                {"session": live} if mode == "borrowed" else {"authorized_resume_state": live.state}
            ),
        ):
            pytest.fail("Mismatched authority opened the gateway")
    assert events == []


async def test_borrowed_vercel_uses_current_provider_manifest_defaults() -> None:
    from agents.classic.extensions.sandbox.vercel.sandbox import (
        VercelSandboxClient,
        VercelSandboxClientOptions,
        VercelSandboxSessionState,
        _resolve_manifest_root,
    )
    from agents.classic.sandbox.session.sandbox_client import BaseSandboxClient

    client = VercelSandboxClient()
    inner = scripted_sandbox_session()
    inner.state = VercelSandboxSessionState(
        sandbox_id="synthetic-vercel",
        snapshot=inner.state.snapshot,
        manifest=_resolve_manifest_root(Manifest()),
    )
    # The provider-free resource uses the real provider's state/default resolver.
    live = BaseSandboxClient._wrap_session(client, inner)
    connected = []

    @asynccontextmanager
    async def connect(selected):
        connected.append(selected)
        yield Environment(url="wss://fixture.invalid", cwd=selected.state.manifest.root)

    async with sandbox_environment(
        client,
        manifest=Manifest(),
        options=VercelSandboxClientOptions(),
        session=live,
        connect=connect,
    ):
        pass
    assert connected == [live]
    connected.clear()
    with pytest.raises(MountConfigError):
        async with sandbox_environment(
            client,
            manifest=Manifest(root="/different-authority"),
            options=VercelSandboxClientOptions(),
            session=live,
            connect=connect,
        ):
            pass
    assert connected == []


@pytest.mark.parametrize(
    "change",
    ["unchanged", "removed", "source", "read_only", "network_tightened", "network_unchanged"],
)
async def test_borrowed_docker_checks_live_host_bind_authority(tmp_path: Path, change: str) -> None:
    from agents.classic.sandbox.sandboxes.docker import (
        DockerSandboxClient,
        DockerSandboxClientOptions,
        DockerSandboxSession,
        DockerSandboxSessionState,
    )

    host = tmp_path / "host"
    host.mkdir()
    original = {"path": "/mnt/trusted", "host_path": str(host), "read_only": False}
    manifest = Manifest(extra_path_grants=[original])
    events: list[str] = []

    class Container:
        attrs = {
            "Mounts": [
                {"Type": "bind", "Source": str(host), "Destination": "/mnt/trusted", "RW": True}
            ],
            "HostConfig": {"NetworkMode": "none" if change == "network_unchanged" else "bridge"},
            "NetworkSettings": {
                "Networks": {} if change == "network_unchanged" else {"bridge": {}}
            },
        }

        def reload(self) -> None:
            events.append("inspect")

    live = SandboxSession(
        DockerSandboxSession(
            docker_client=None,
            container=Container(),
            state=DockerSandboxSessionState(
                snapshot=scripted_sandbox_session(manifest=manifest).state.snapshot,
                manifest=manifest,
                image="synthetic",
                container_id="synthetic-container",
                network_mode="none" if change == "network_unchanged" else None,
            ),
        )
    )
    changed = dict(original)
    if change == "source":
        changed["host_path"] = str(tmp_path / "other")
    if change == "read_only":
        changed["read_only"] = True
    current = Manifest(extra_path_grants=[] if change == "removed" else [changed])
    client = Client(events)
    client.backend_id = "docker"

    @asynccontextmanager
    async def connect(selected):
        assert selected is live
        events.append("connect")
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    if change.startswith("network_"):
        client = DockerSandboxClient(None)
        options = DockerSandboxClientOptions(image="synthetic", network_mode="none")
    else:
        options = Options()
    source = sandbox_environment(
        client, manifest=current, options=options, session=live, connect=connect
    )
    if change in {"unchanged", "network_unchanged"}:
        async with source:
            pass
        assert events.count("connect") == 1
    else:
        with pytest.raises(ValueError, match="bind mounts|path grant mount|network configuration"):
            async with source:
                pytest.fail("Changed host authority must fail before gateway connection")
        assert "connect" not in events and not {"close", "delete"}.intersection(events)


@pytest.mark.parametrize("supplied", [False, True])
async def test_borrowed_limits_apply_before_gateway_and_persist(supplied: bool) -> None:
    from agents.sandbox import SandboxArchiveLimits, SandboxConcurrencyLimits

    events: list[str] = []
    client = Client(events)
    session = await client.create(manifest=Manifest(), options=Options())
    client.backend_id = session.state.type
    old_archive = SandboxArchiveLimits(max_members=11)
    session._set_archive_limits(old_archive)
    session._set_concurrency_limits(SandboxConcurrencyLimits(manifest_entries=7))
    archive = SandboxArchiveLimits(max_members=3)
    concurrency = SandboxConcurrencyLimits(manifest_entries=2)
    events.clear()

    def assert_limits():
        assert session._archive_limits == (archive if supplied else old_archive)
        assert session._max_manifest_entry_concurrency == (2 if supplied else 7)

    @asynccontextmanager
    async def connect(selected):
        assert selected is session
        assert_limits()
        events.append("connect")
        try:
            yield Environment(url="wss://executor.invalid", cwd="/workspace")
        finally:
            events.append("gateway")

    async with sandbox_environment(
        client,
        manifest=Manifest(),
        options=Options(),
        session=session,
        connect=connect,
        **({"archive_limits": archive, "concurrency_limits": concurrency} if supplied else {}),
    ):
        assert_limits()
    assert_limits()
    assert events == ["connect", "gateway"]


@pytest.mark.parametrize("failure", ["manifest", "provider"])
async def test_borrowed_failed_authority_preserves_limits(failure: str) -> None:
    from agents.sandbox import SandboxArchiveLimits, SandboxConcurrencyLimits

    client = Client([])
    session = await client.create(manifest=Manifest(), options=Options())
    client.backend_id = session.state.type
    archive = SandboxArchiveLimits(max_members=11)
    session._set_archive_limits(archive)
    session._set_concurrency_limits(SandboxConcurrencyLimits(manifest_entries=7))

    async def reject(*args, **kwargs):
        raise ValueError("Borrowed authority rejected")

    if failure == "manifest":
        session._validate_manifest_application = reject
    else:
        client.assert_borrowed_session_authority = reject

    @asynccontextmanager
    async def connect(selected):
        pytest.fail("Rejected authority must not connect")
        yield

    with pytest.raises(ValueError, match="Borrowed authority rejected"):
        async with sandbox_environment(
            client,
            manifest=Manifest(),
            options=Options(),
            session=session,
            connect=connect,
            archive_limits=SandboxArchiveLimits(max_members=3),
            concurrency_limits=SandboxConcurrencyLimits(manifest_entries=2),
        ):
            pytest.fail("Rejected authority must not yield")
    assert session._archive_limits == archive
    assert session._max_manifest_entry_concurrency == 7


async def test_workspace_guidance_captures_prepared_resource_without_private_values() -> None:
    from _agents_codex.session import _EnvironmentLease, _release_environment

    class PreparedClient(Client):
        async def create(self, *, manifest=None, options, snapshot=None):
            selected = await super().create(manifest=manifest, options=options, snapshot=snapshot)
            start = selected.start

            async def prepare():
                await start()
                selected.state.manifest.root = "/prepared"

            selected.start = prepare
            return selected

    client = PreparedClient([])
    manifest = Manifest(
        entries={
            "data": declaration(read_only=False, secret_access_key="SYNTHETIC_MOUNT_SECRET"),
            "notes": Dir(),
        },
        environment={"value": {"PRIVATE": "SYNTHETIC_ENV_SECRET"}},
        remote_mount_command_allowlist=["cat"],
    )

    @asynccontextmanager
    async def connect(selected):
        assert selected.state.manifest.root == "/prepared"
        yield Environment(
            url="wss://synthetic-executor.invalid",
            headers={"Authorization": "SYNTHETIC_HEADER_SECRET"},
            cwd="/prepared/project",
            read_only=True,
        )

    source = sandbox_environment(
        client,
        manifest=manifest,
        options=Options(labels={"private": "SYNTHETIC_PROVIDER_SECRET"}),
        connect=connect,
    )
    manifest.entries.clear()
    lease = _EnvironmentLease(source)
    try:
        await lease.acquire()
        text = lease._workspace_instructions
        assert isinstance(text, str)
        assert text.count("# Current SDK workspace snapshot") == 1
        assert '"workspace": "/prepared"' in text
        assert 'Working directory: "/prepared/project"' in text
        assert "Executor read-only: true" in text
        assert "/prepared/data" in text and "read+write" in text
        assert "/prepared/notes" in text
        assert "untrusted data" in text and "`cat`" in text
        assert "copy it back" in text
        assert "Use `apply_patch` directly" not in text
        for secret in (
            "SYNTHETIC_MOUNT_SECRET",
            "SYNTHETIC_ENV_SECRET",
            "SYNTHETIC_HEADER_SECRET",
            "SYNTHETIC_PROVIDER_SECRET",
            "synthetic-executor.invalid",
        ):
            assert secret not in text
        assert client.session is not None
        client.session.state.manifest.root = "/later"
        assert lease._workspace_instructions == text
    finally:
        await _release_environment(lease)
    assert client.events == ["create", "start", "close", "delete"]


async def test_failed_gateway_does_not_publish_workspace_guidance() -> None:
    from _agents_codex.session import _EnvironmentLease

    client = Client([])

    @asynccontextmanager
    async def connect(selected):
        raise RuntimeError("gateway failed")
        yield Environment(url="wss://synthetic.invalid", cwd="/workspace")

    lease = _EnvironmentLease(
        sandbox_environment(client, manifest=Manifest(), options=Options(), connect=connect)
    )
    with pytest.raises(RuntimeError, match="gateway failed"):
        await lease.acquire()
    assert lease._workspace_instructions is None
    assert client.events == ["create", "start", "close", "delete"]


async def test_cancelled_gateway_cannot_publish_late_workspace_guidance() -> None:
    from _agents_codex.session import _EnvironmentLease

    client = Client([])
    entered = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def connect(selected):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        yield Environment(url="wss://synthetic.invalid", cwd="/workspace")

    lease = _EnvironmentLease(
        sandbox_environment(client, manifest=Manifest(), options=Options(), connect=connect)
    )
    opening = asyncio.create_task(lease.acquire())
    await entered.wait()
    opening.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert lease._workspace_instructions is None
    assert client.events == ["create", "start", "close", "delete"]


def test_sandbox_state_equality_ignores_memory_lock_identity() -> None:
    original = scripted_sandbox_session(manifest=Manifest(root="/workspace")).state
    state_type = type(original)
    reconstructed = state_type.model_validate(original.model_dump())
    restored = state_type.model_validate_json(original.model_dump_json())
    assert reconstructed == original
    assert restored == original
    assert reconstructed._native_memory_flush_lock is not original._native_memory_flush_lock
    assert reconstructed._native_skill_copy_budget is not original._native_skill_copy_budget
    assert restored._native_skill_copy_budget is not original._native_skill_copy_budget
    assert not reconstructed._native_skill_copy_budget.trusted
    assert not restored._native_skill_copy_budget.trusted
    for deep in (False, True):
        copied = original.model_copy(deep=deep)
        assert copied == original
        assert copied._native_memory_flush_lock is original._native_memory_flush_lock
        assert copied._native_skill_copy_budget is original._native_skill_copy_budget
    assert original.model_copy(update={"workspace_root_ready": True}) != original
    for name, value in (
        ("_path_grants_require_rebind", ("data",)),
        ("_mount_authority_redacted", True),
        ("_mount_authority_rebound", True),
    ):
        changed = original.model_copy()
        setattr(changed, name, value)
        assert changed != original


@pytest.mark.parametrize("change", ["addition", "replacement"])
@pytest.mark.parametrize("alias", ["original", "wrapped"])
async def test_borrowed_manifest_changes_reject_before_any_partial_write(
    tmp_path: Path, change: str, alias: str
) -> None:
    from agents.classic.sandbox.entries import File
    from agents.sandbox import SandboxCapability

    current_entries = (
        {Path("first.txt"): File(content=b"existing edited content")}
        if change == "replacement"
        else {}
    )
    original = Manifest(root=str(tmp_path), entries=current_entries)
    if current_entries:
        (tmp_path / "first.txt").write_bytes(b"existing edited content")
    client = Client([])
    selected = await client.create(manifest=original, options=Options())
    client.backend_id = selected.state.type
    saved_before_update = selected.state.model_copy(deep=True)
    client.events.clear()
    calls = []

    async def unsafe_batch(entries, **kwargs):
        calls.append("write")
        (tmp_path / "first.txt").write_bytes(b"synthetic-private-partial-write")
        raise OSError("second synthetic write failed")

    selected._apply_entry_batch = unsafe_batch
    reused = selected if alias == "original" else SandboxSession(SandboxSession(selected._inner))

    class Probe(SandboxCapability):
        name = "no-write-probe"

        async def bind(self, session, *, user, workspace):
            calls.append("bind")

    @asynccontextmanager
    async def connect(session):
        calls.append("connect")
        yield Environment(url="wss://executor.invalid", cwd=str(tmp_path))

    requested = Manifest(
        root=str(tmp_path),
        entries={
            Path("first.txt"): File(content=b"synthetic-private-partial-write"),
            Path("second.txt"): File(content=b"second file"),
        },
    )
    with pytest.raises(ValueError, match="atomic provider transaction"):
        async with sandbox_environment(
            client,
            manifest=requested,
            options=Options(),
            session=reused,
            connect=connect,
            capabilities=[Probe()],
        ):
            pytest.fail("Nonatomic borrowed update was published")
    assert calls == [] and client.events == []
    assert selected.state == saved_before_update
    assert selected.state.manifest == original
    if change == "replacement":
        assert (tmp_path / "first.txt").read_bytes() == b"existing edited content"
    else:
        assert not (tmp_path / "first.txt").exists()
    # A failed preflight keeps the original clean resource usable, including old state references.
    assert saved_before_update.manifest == original
    async with sandbox_environment(
        client,
        manifest=original,
        options=Options(),
        session=reused,
        connect=connect,
    ):
        pass
    assert calls == ["connect"] and client.events == []


@pytest.mark.parametrize("changed", [False, True])
async def test_shared_classic_borrowed_acquisition_requires_atomic_changes(
    tmp_path: Path, changed: bool
) -> None:
    from types import SimpleNamespace

    from agents.classic.sandbox.entries import File
    from agents.classic.sandbox.runtime_session_manager import SandboxRuntimeSessionManager

    manifest = Manifest(root=str(tmp_path))
    client = Client([])
    selected = await client.create(manifest=manifest, options=Options())
    writes = []

    async def unsafe_batch(*args, **kwargs):
        writes.append("write")
        pytest.fail("Shared acquisition reached nonatomic writes")

    selected._apply_entry_batch = unsafe_batch

    class AddFile:
        def process_manifest(self, current):
            current.entries[Path("private.txt")] = File(content=b"private")
            return current

    arguments: dict[str, Any] = {
        "agent": SimpleNamespace(run_as=None),
        "capabilities": [AddFile()] if changed else [],
        "session": selected,
    }
    if changed:
        with pytest.raises(ValueError, match="atomic provider transaction"):
            await SandboxRuntimeSessionManager._process_live_session_manifest(**arguments)
    else:
        update = await SandboxRuntimeSessionManager._process_live_session_manifest(**arguments)
        assert not update.entries_to_apply
    assert not writes and not (tmp_path / "private.txt").exists()


@pytest.mark.parametrize("snapshot_mode", [False, True])
async def test_owned_and_snapshot_additive_manifest_remain_supported(
    tmp_path: Path, snapshot_mode: bool
) -> None:
    from agents.classic.sandbox.entries import File
    from agents.classic.sandbox.snapshot import LocalSnapshotSpec

    root = tmp_path / "owned"
    snapshots = []

    class MaterializingClient(Client):
        async def create(self, *, manifest=None, options, snapshot=None):
            snapshots.append(snapshot)
            selected = await super().create(manifest=manifest, options=options)
            original_start = selected.start

            async def start():
                await original_start()
                root.mkdir()
                for path, entry in selected.state.manifest.entries.items():
                    (root / path).write_bytes(entry.content)

            selected.start = start
            return selected

    client = MaterializingClient([])

    @asynccontextmanager
    async def connect(selected):
        assert (root / "first.txt").read_bytes() == b"owned first"
        assert (root / "second.txt").read_bytes() == b"owned second"
        yield Environment(url="wss://executor.invalid", cwd=str(root))

    kwargs = (
        {"snapshot": LocalSnapshotSpec(base_path=tmp_path / "snapshots")} if snapshot_mode else {}
    )
    async with sandbox_environment(
        client,
        manifest=Manifest(
            root=str(root),
            entries={
                Path("first.txt"): File(content=b"owned first"),
                Path("second.txt"): File(content=b"owned second"),
            },
        ),
        options=Options(),
        connect=connect,
        **kwargs,
    ):
        pass
    assert bool(snapshots[0]) is snapshot_mode
    assert client.events == ["create", "start", "close", "delete"]
