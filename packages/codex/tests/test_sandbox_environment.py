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


async def test_dictionary_configuration_uses_owning_models() -> None:
    client = Client([])

    @asynccontextmanager
    async def connect(session):
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    async with sandbox_environment(
        client,
        manifest={"entries": {"data": declaration()}},
        options={"type": "native_storage_test", "labels": {"owner": "dict"}},
        connect=connect,
    ):
        assert isinstance(client.options, Options)
        assert client.options.labels == {"owner": "dict"}
        assert isinstance(client.manifest.entries["data"], S3Mount)


def test_layout_only_renders_explicit_safe_fields_and_preserves_identity() -> None:
    from agents.classic.sandbox.entries import S3Mount as OwningMount

    mount = declaration(
        secret_access_key="secret-do-not-render",
        access_key_id="id-do-not-render",
        endpoint_url="https://secret-endpoint.invalid",
        description='data\n"quoted"',
        mount_path=Path("/data/reports"),
    )
    manifest = Manifest(entries={"data": mount, "writable": declaration(read_only=False)})
    rendered = render_storage_layout(manifest)
    assert json.loads(rendered) == {
        "workspace": "/workspace",
        "storage": [
            {"path": "/data/reports", "access": "read_only", "description": 'data\n"quoted"'},
            {"path": "/workspace/writable", "access": "read_write"},
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
@pytest.mark.parametrize("declaration_kind", ["mapping", "mutated_model"])
async def test_provider_rejects_before_infrastructure_or_connector(
    monkeypatch: pytest.MonkeyPatch,
    responses: ResponsesFixture,
    invalid: str,
    declaration_kind: str,
) -> None:
    from unittest.mock import MagicMock

    from agents.classic.sandbox.sandboxes.docker import (
        DockerSandboxClient,
        DockerSandboxClientOptions,
    )
    from agents.sandbox import InContainerMountStrategy, RcloneMountPattern

    if invalid != "credentials":
        mount = configured_mount(invalid)
        if declaration_kind == "mutated_model":
            if invalid == "s3_files":
                mount.mount_strategy.pattern = RcloneMountPattern()
            else:
                mount.mount_strategy.driver = "unsupported"
            manifest = Manifest(entries={"data": mount})
        else:
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


@pytest.mark.parametrize("missing", ["bucket", "mount_strategy"])
@pytest.mark.parametrize("helper", ["layout", "source"])
def test_mapping_validation_diagnostics_do_not_retain_credentials(
    missing: str, helper: str
) -> None:
    from dataclasses import asdict

    from pydantic import ValidationError

    secret = "fixture-secret-redaction-probe"
    raw = declaration(secret_access_key=secret).model_dump()
    del raw[missing]
    payload = {"entries": {"data": raw}}
    client = Client([])

    @asynccontextmanager
    async def connect(session):
        pytest.fail("Invalid configuration must not open a gateway")
        yield Environment(url="wss://executor.invalid", cwd="/workspace")

    with pytest.raises((ValidationError, MountConfigError)) as caught:
        if helper == "layout":
            render_storage_layout(payload)
        else:
            sandbox_environment(client, manifest=payload, options=Options(), connect=connect)
    error = caught.value
    diagnostic = (
        error.json()
        if isinstance(error, ValidationError)
        else json.dumps(asdict(error), default=str)
    )
    assert secret not in diagnostic
    assert missing in str(error)
    assert error.__context__ is None and error.__cause__ is None
    assert client.events == []
