from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest

from agents.sandbox import Manifest
from agents.sandbox.entries import (
    AzureBlobMount,
    BoxMount,
    DockerVolumeMountStrategy,
    FuseMountPattern,
    GCSMount,
    InContainerMountStrategy,
    Mount,
    MountpointMountPattern,
    MountStrategy,
    R2Mount,
    RcloneMountPattern,
    S3FilesMount,
    S3FilesMountPattern,
    S3Mount,
)
from agents.sandbox.entries.mounts.patterns import (
    FuseMountConfig,
    MountpointMountConfig,
    RcloneMountConfig,
    S3FilesMountConfig,
)
from agents.sandbox.errors import MountConfigError
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult
from tests.utils.factories import TestSessionState


class _MountConfigSession(BaseSandboxSession):
    def __init__(self, *, session_id: uuid.UUID | None = None, config_text: str = "") -> None:
        self.state = TestSessionState(
            session_id=session_id or uuid.uuid4(),
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self._config_text = config_text

    async def read(self, path: Path, *, user: object = None) -> io.BytesIO:
        _ = (path, user)
        return io.BytesIO(self._config_text.encode("utf-8"))

    async def shutdown(self) -> None:
        return None

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = (path, data, user)
        raise AssertionError("write() should not be called in these tests")

    async def running(self) -> bool:
        return True

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = (command, timeout)
        raise AssertionError("exec() should not be called in these tests")

    async def persist_workspace(self) -> io.IOBase:
        raise AssertionError("persist_workspace() should not be called in these tests")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        raise AssertionError("hydrate_workspace() should not be called in these tests")


class _MountpointApplySession(BaseSandboxSession):
    def __init__(self) -> None:
        self.state = TestSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self.exec_calls: list[list[str]] = []

    async def read(self, path: Path, *, user: object = None) -> io.BytesIO:
        _ = (path, user)
        raise AssertionError("read() should not be called in these tests")

    async def shutdown(self) -> None:
        return None

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = (path, data, user)
        raise AssertionError("write() should not be called in these tests")

    async def running(self) -> bool:
        return True

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        command_strs = [str(part) for part in command]
        self.exec_calls.append(command_strs)
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    async def persist_workspace(self) -> io.IOBase:
        raise AssertionError("persist_workspace() should not be called in these tests")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        raise AssertionError("hydrate_workspace() should not be called in these tests")


class _GeneratedConfigApplySession(BaseSandboxSession):
    def __init__(self, *, session_id: uuid.UUID) -> None:
        self.state = TestSessionState(
            session_id=session_id,
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self.exec_calls: list[list[str]] = []
        self.write_calls: list[tuple[Path, bytes]] = []

    async def read(self, path: Path, *, user: object = None) -> io.BytesIO:
        _ = (path, user)
        raise AssertionError("read() should not be called in these tests")

    async def shutdown(self) -> None:
        return None

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = user
        self.write_calls.append((path, data.read()))

    async def running(self) -> bool:
        return True

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        self.exec_calls.append([str(part) for part in command])
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    async def persist_workspace(self) -> io.IOBase:
        raise AssertionError("persist_workspace() should not be called in these tests")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        raise AssertionError("hydrate_workspace() should not be called in these tests")


class _NoStrategyMount(Mount):
    type: str = f"no_strategy_mount_{uuid.uuid4().hex}"
    mount_strategy: MountStrategy = DockerVolumeMountStrategy(driver="rclone")


def test_manifest_model_dump_preserves_mount_strategy_subtype_fields() -> None:
    manifest = Manifest(
        entries={
            "in-container": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
            "docker-volume": S3Mount(
                bucket="bucket",
                mount_strategy=DockerVolumeMountStrategy(
                    driver="rclone",
                    driver_options={"vfs-cache-mode": "off"},
                ),
            ),
        }
    )

    payload = manifest.model_dump(mode="json")

    assert payload["entries"]["in-container"]["mount_strategy"] == {
        "type": "in_container",
        "pattern": {
            "type": "mountpoint",
            "options": {
                "prefix": None,
                "region": None,
                "endpoint_url": None,
            },
        },
    }
    assert payload["entries"]["docker-volume"]["mount_strategy"] == {
        "type": "docker_volume",
        "driver": "rclone",
        "driver_options": {"vfs-cache-mode": "off"},
    }

    restored = Manifest.model_validate(payload)

    in_container = restored.entries["in-container"]
    docker_volume = restored.entries["docker-volume"]
    assert isinstance(in_container, S3Mount)
    assert isinstance(in_container.mount_strategy, InContainerMountStrategy)
    assert isinstance(in_container.mount_strategy.pattern, MountpointMountPattern)
    assert isinstance(docker_volume, S3Mount)
    assert isinstance(docker_volume.mount_strategy, DockerVolumeMountStrategy)
    assert docker_volume.mount_strategy.driver == "rclone"
    assert docker_volume.mount_strategy.driver_options == {"vfs-cache-mode": "off"}


def test_manifest_model_dump_round_trips_s3_files_mount() -> None:
    manifest = Manifest(
        entries={
            "remote": S3FilesMount(
                file_system_id="fs-1234567890abcdef0",
                subpath="/datasets",
                mount_target_ip="10.99.1.209",
                region="us-east-1",
                read_only=False,
                mount_strategy=InContainerMountStrategy(pattern=S3FilesMountPattern()),
            )
        }
    )

    payload = manifest.model_dump(mode="json")

    assert payload["entries"]["remote"]["type"] == "s3_files_mount"
    assert payload["entries"]["remote"]["mount_strategy"] == {
        "type": "in_container",
        "pattern": {
            "type": "s3files",
            "options": {
                "mount_target_ip": None,
                "access_point": None,
                "region": None,
                "extra_options": {},
            },
        },
    }

    restored = Manifest.model_validate(payload)

    mount = restored.entries["remote"]
    assert isinstance(mount, S3FilesMount)
    assert mount.file_system_id == "fs-1234567890abcdef0"
    assert mount.subpath == "/datasets"
    assert mount.mount_target_ip == "10.99.1.209"
    assert mount.region == "us-east-1"
    assert mount.read_only is False
    assert isinstance(mount.mount_strategy, InContainerMountStrategy)
    assert isinstance(mount.mount_strategy.pattern, S3FilesMountPattern)


@pytest.mark.asyncio
async def test_azure_blob_mount_builds_rclone_runtime_config_without_hidden_pattern_state() -> None:
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern(config_file_path=Path("rclone.conf"))
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="azureblob",
        mount_type="azure_blob_mount",
    )
    session = _MountConfigSession(
        session_id=session_id,
        config_text=f"[{remote_name}]\ntype = azureblob\n",
    )
    mount = AzureBlobMount(
        account="acct",
        container="container",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    apply_config = await mount.build_in_container_mount_config(
        session, pattern, include_config_text=True
    )
    unmount_config = await mount.build_in_container_mount_config(
        session, pattern, include_config_text=False
    )

    assert isinstance(apply_config, RcloneMountConfig)
    assert apply_config.remote_name == remote_name
    assert apply_config.remote_path == "container"
    assert apply_config.config_text is not None
    assert "account = acct" in apply_config.config_text
    assert isinstance(unmount_config, RcloneMountConfig)
    assert unmount_config.remote_name == remote_name
    assert unmount_config.config_text is None


@pytest.mark.asyncio
async def test_box_mount_builds_rclone_runtime_config_with_box_auth_options() -> None:
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern(config_file_path=Path("rclone.conf"))
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="box",
        mount_type="box_mount",
    )
    session = _MountConfigSession(
        session_id=session_id,
        config_text=f"[{remote_name}]\ntype = box\n",
    )
    mount = BoxMount(
        path="/Shared/Finance",
        client_id="client-id",
        client_secret="client-secret",
        token='{"access_token":"token"}',
        root_folder_id="12345",
        impersonate="user-42",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
        read_only=False,
    )

    apply_config = await mount.build_in_container_mount_config(
        session, pattern, include_config_text=True
    )
    unmount_config = await mount.build_in_container_mount_config(
        session, pattern, include_config_text=False
    )

    assert isinstance(apply_config, RcloneMountConfig)
    assert apply_config.remote_name == remote_name
    assert apply_config.remote_path == "Shared/Finance"
    assert apply_config.read_only is False
    assert apply_config.config_text is not None
    assert "type = box" in apply_config.config_text
    assert "client_id = client-id" in apply_config.config_text
    assert "client_secret = client-secret" in apply_config.config_text
    assert 'token = {"access_token":"token"}' in apply_config.config_text
    assert "root_folder_id = 12345" in apply_config.config_text
    assert "impersonate = user-42" in apply_config.config_text
    assert isinstance(unmount_config, RcloneMountConfig)
    assert unmount_config.remote_name == remote_name
    assert unmount_config.remote_path == "Shared/Finance"
    assert unmount_config.config_text is None


@pytest.mark.asyncio
async def test_gcs_mount_uses_runtime_endpoint_override_without_mutating_pattern_options() -> None:
    pattern = MountpointMountPattern()
    mount = GCSMount(
        bucket="bucket",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
        read_only=False,
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(),
        pattern,
        include_config_text=False,
    )

    assert isinstance(config, MountpointMountConfig)
    assert config.endpoint_url == "https://storage.googleapis.com"
    assert pattern.options.endpoint_url is None
    assert mount.read_only is False
    assert config.read_only is False

    session = _MountpointApplySession()

    await pattern.apply(
        session,
        Path("/workspace/remote"),
        MountpointMountConfig(
            bucket="bucket",
            access_key_id="access",
            secret_access_key="secret",
            session_token=None,
            prefix=None,
            region="us-east1",
            endpoint_url=config.endpoint_url,
            mount_type="gcs_mount",
        ),
    )

    assert session.exec_calls[:2] == [
        ["sh", "-lc", "command -v mount-s3 >/dev/null 2>&1"],
        ["mkdir", "-p", "/workspace/remote"],
    ]
    assert len(session.exec_calls) == 3

    mount_command = session.exec_calls[2]
    assert mount_command[:2] == ["sh", "-lc"]
    assert "mount-s3" in mount_command[2]
    assert "--region us-east1" in mount_command[2]
    assert "--endpoint-url https://storage.googleapis.com" in mount_command[2]
    assert "--upload-checksums off" in mount_command[2]
    assert mount_command[2].endswith("bucket /workspace/remote")


@pytest.mark.asyncio
async def test_s3_mountpoint_writable_mode_enables_overwrite_and_delete() -> None:
    session = _MountpointApplySession()
    pattern = MountpointMountPattern()

    await pattern.apply(
        session,
        Path("/workspace/remote"),
        MountpointMountConfig(
            bucket="bucket",
            access_key_id="access",
            secret_access_key="secret",
            session_token="token",
            prefix=None,
            region="us-east-1",
            endpoint_url=None,
            mount_type="s3_mount",
            read_only=False,
        ),
    )

    assert session.exec_calls[:2] == [
        ["sh", "-lc", "command -v mount-s3 >/dev/null 2>&1"],
        ["mkdir", "-p", "/workspace/remote"],
    ]
    assert len(session.exec_calls) == 3

    mount_command = session.exec_calls[2]
    assert mount_command[:2] == ["sh", "-lc"]
    assert "mount-s3" in mount_command[2]
    assert "--read-only" not in mount_command[2]
    assert "--allow-overwrite" in mount_command[2]
    assert "--allow-delete" in mount_command[2]
    assert "--region us-east-1" in mount_command[2]
    assert "AWS_ACCESS_KEY_ID=access" in mount_command[2]
    assert "AWS_SECRET_ACCESS_KEY=secret" in mount_command[2]
    assert "AWS_SESSION_TOKEN=token" in mount_command[2]
    assert mount_command[2].endswith("bucket /workspace/remote")


@pytest.mark.asyncio
async def test_gcs_mountpoint_writable_mode_enables_overwrite_and_delete() -> None:
    session = _MountpointApplySession()
    pattern = MountpointMountPattern()

    await pattern.apply(
        session,
        Path("/workspace/remote"),
        MountpointMountConfig(
            bucket="bucket",
            access_key_id="access",
            secret_access_key="secret",
            session_token=None,
            prefix=None,
            region="us-east1",
            endpoint_url="https://storage.googleapis.com",
            mount_type="gcs_mount",
            read_only=False,
        ),
    )

    assert session.exec_calls[:2] == [
        ["sh", "-lc", "command -v mount-s3 >/dev/null 2>&1"],
        ["mkdir", "-p", "/workspace/remote"],
    ]
    assert len(session.exec_calls) == 3

    mount_command = session.exec_calls[2]
    assert mount_command[:2] == ["sh", "-lc"]
    assert "mount-s3" in mount_command[2]
    assert "--read-only" not in mount_command[2]
    assert "--allow-overwrite" in mount_command[2]
    assert "--allow-delete" in mount_command[2]
    assert "--region us-east1" in mount_command[2]
    assert "--endpoint-url https://storage.googleapis.com" in mount_command[2]
    assert "--upload-checksums off" in mount_command[2]
    assert "AWS_ACCESS_KEY_ID=access" in mount_command[2]
    assert "AWS_SECRET_ACCESS_KEY=secret" in mount_command[2]
    assert mount_command[2].endswith("bucket /workspace/remote")


@pytest.mark.asyncio
async def test_s3_files_mount_builds_runtime_config_with_pattern_defaults() -> None:
    pattern = S3FilesMountPattern(
        options=S3FilesMountPattern.S3FilesOptions(
            mount_target_ip="10.99.1.209",
            access_point="fsap-pattern",
            region="us-east-1",
            extra_options={"tlsport": "3049"},
        )
    )
    mount = S3FilesMount(
        file_system_id="fs-1234567890abcdef0",
        subpath="/datasets",
        access_point="fsap-direct",
        extra_options={"tlsport": "4049", "iam": None},
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(),
        pattern,
        include_config_text=False,
    )

    assert isinstance(config, S3FilesMountConfig)
    assert config.file_system_id == "fs-1234567890abcdef0"
    assert config.subpath == "/datasets"
    assert config.mount_target_ip == "10.99.1.209"
    assert config.access_point == "fsap-direct"
    assert config.region == "us-east-1"
    assert config.extra_options == {"tlsport": "4049", "iam": None}


@pytest.mark.asyncio
async def test_s3_files_pattern_mounts_with_helper_options() -> None:
    session = _MountpointApplySession()
    pattern = S3FilesMountPattern()

    await pattern.apply(
        session,
        Path("/workspace/remote"),
        S3FilesMountConfig(
            file_system_id="fs-1234567890abcdef0",
            subpath="/datasets",
            mount_target_ip="10.99.1.209",
            access_point="fsap-123",
            region="us-east-1",
            extra_options={"tlsport": "4049"},
            mount_type="s3_files_mount",
            read_only=True,
        ),
    )

    assert session.exec_calls[:2] == [
        ["sh", "-lc", "command -v mount.s3files >/dev/null 2>&1"],
        ["mkdir", "-p", "/workspace/remote"],
    ]
    assert session.exec_calls[2] == [
        "mount",
        "-t",
        "s3files",
        "-o",
        ("tlsport=4049,ro,mounttargetip=10.99.1.209,accesspoint=fsap-123,region=us-east-1"),
        "fs-1234567890abcdef0:/datasets",
        "/workspace/remote",
    ]


@pytest.mark.asyncio
async def test_gcs_mount_builds_native_rclone_config_with_service_account_auth() -> None:
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern()
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="gcs",
        mount_type="gcs_mount",
    )
    mount = GCSMount(
        bucket="bucket",
        prefix="nested/prefix/",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
        service_account_file="/data/config/gcs.json",
        service_account_credentials='{"type":"service_account"}',
        access_token="token",
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(session_id=session_id),
        pattern,
        include_config_text=True,
    )

    assert isinstance(config, RcloneMountConfig)
    assert config.remote_name == remote_name
    assert config.remote_path == "bucket/nested/prefix/"
    assert config.config_text == (
        f"[{remote_name}]\n"
        "type = google cloud storage\n"
        "service_account_file = /data/config/gcs.json\n"
        'service_account_credentials = {"type":"service_account"}\n'
        "access_token = token\n"
        "env_auth = false\n"
    )


@pytest.mark.asyncio
async def test_gcs_mount_builds_s3_compatible_rclone_config_with_hmac_auth() -> None:
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern()
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="gcs_s3",
        mount_type="gcs_mount",
    )
    mount = GCSMount(
        bucket="bucket",
        access_id="access-id",
        secret_access_key="secret-key",
        prefix="nested/prefix/",
        region="auto",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(session_id=session_id),
        pattern,
        include_config_text=True,
    )

    assert isinstance(config, RcloneMountConfig)
    assert config.remote_name == remote_name
    assert config.remote_path == "bucket/nested/prefix/"
    assert config.config_text == (
        f"[{remote_name}]\n"
        "type = s3\n"
        "provider = GCS\n"
        "env_auth = false\n"
        "access_key_id = access-id\n"
        "secret_access_key = secret-key\n"
        "endpoint = https://storage.googleapis.com\n"
        "region = auto\n"
    )


@pytest.mark.asyncio
async def test_gcs_hmac_rclone_remote_name_does_not_collide_with_s3_mount() -> None:
    session_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    pattern = RcloneMountPattern()
    session = _MountConfigSession(session_id=session_id)
    s3_mount = S3Mount(
        bucket="s3-bucket",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )
    gcs_mount = GCSMount(
        bucket="gcs-bucket",
        access_id="access-id",
        secret_access_key="secret-key",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    s3_config = await s3_mount.build_in_container_mount_config(
        session,
        pattern,
        include_config_text=True,
    )
    gcs_config = await gcs_mount.build_in_container_mount_config(
        session,
        pattern,
        include_config_text=True,
    )

    assert isinstance(s3_config, RcloneMountConfig)
    assert isinstance(gcs_config, RcloneMountConfig)
    assert s3_config.remote_name == "sandbox_s3_12345678123456781234567812345678"
    assert gcs_config.remote_name == "sandbox_gcs_s3_12345678123456781234567812345678"
    assert s3_config.remote_name != gcs_config.remote_name


@pytest.mark.asyncio
async def test_s3_mount_direct_mountpoint_fields_override_pattern_options() -> None:
    pattern = MountpointMountPattern(
        options=MountpointMountPattern.MountpointOptions(
            prefix="pattern-prefix/",
            region="pattern-region",
            endpoint_url="https://pattern.example.test",
        )
    )
    mount = S3Mount(
        bucket="bucket",
        prefix="direct-prefix/",
        region="direct-region",
        endpoint_url="https://direct.example.test",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(),
        pattern,
        include_config_text=False,
    )

    assert isinstance(config, MountpointMountConfig)
    assert config.prefix == "direct-prefix/"
    assert config.region == "direct-region"
    assert config.endpoint_url == "https://direct.example.test"


@pytest.mark.asyncio
async def test_s3_mount_builds_prefixed_rclone_remote_path() -> None:
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern()
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="s3",
        mount_type="s3_mount",
    )
    mount = S3Mount(
        bucket="bucket",
        prefix="nested/prefix/",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(session_id=session_id),
        pattern,
        include_config_text=True,
    )

    assert isinstance(config, RcloneMountConfig)
    assert config.remote_name == remote_name
    assert config.remote_path == "bucket/nested/prefix/"


@pytest.mark.asyncio
async def test_s3_mount_rclone_config_includes_endpoint_and_region() -> None:
    """S3Mount must emit endpoint and region in the rclone config."""
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern()
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="s3",
        mount_type="s3_mount",
    )
    mount = S3Mount(
        bucket="my-bucket",
        access_key_id="ak",
        secret_access_key="sk",
        endpoint_url="http://localhost:9000",
        region="us-west-2",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(session_id=session_id),
        pattern,
        include_config_text=True,
    )

    assert isinstance(config, RcloneMountConfig)
    assert config.config_text == (
        f"[{remote_name}]\n"
        "type = s3\n"
        "provider = AWS\n"
        "endpoint = http://localhost:9000\n"
        "region = us-west-2\n"
        "env_auth = false\n"
        "access_key_id = ak\n"
        "secret_access_key = sk\n"
    )


@pytest.mark.asyncio
async def test_s3_mount_rclone_config_omits_endpoint_when_unset() -> None:
    """When endpoint_url and region are not set, rclone defaults to AWS."""
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern()
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="s3",
        mount_type="s3_mount",
    )
    mount = S3Mount(
        bucket="my-bucket",
        access_key_id="ak",
        secret_access_key="sk",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(session_id=session_id),
        pattern,
        include_config_text=True,
    )

    assert isinstance(config, RcloneMountConfig)
    assert config.config_text == (
        f"[{remote_name}]\n"
        "type = s3\n"
        "provider = AWS\n"
        "env_auth = false\n"
        "access_key_id = ak\n"
        "secret_access_key = sk\n"
    )


@pytest.mark.asyncio
async def test_s3_mount_rclone_config_uses_custom_provider() -> None:
    """S3Mount with s3_provider='Other' emits the custom provider in the rclone config,
    which is required for non-AWS S3-compatible services (MinIO, Ceph, etc.) that need
    path-style addressing instead of AWS virtual-hosted-style."""
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern()
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="s3",
        mount_type="s3_mount",
    )
    mount = S3Mount(
        bucket="my-bucket",
        access_key_id="ak",
        secret_access_key="sk",
        endpoint_url="http://localhost:9000",
        s3_provider="Other",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(session_id=session_id),
        pattern,
        include_config_text=True,
    )

    assert isinstance(config, RcloneMountConfig)
    assert config.config_text == (
        f"[{remote_name}]\n"
        "type = s3\n"
        "provider = Other\n"
        "endpoint = http://localhost:9000\n"
        "env_auth = false\n"
        "access_key_id = ak\n"
        "secret_access_key = sk\n"
    )


@pytest.mark.asyncio
async def test_r2_mount_builds_rclone_config_with_explicit_credentials() -> None:
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern()
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="r2",
        mount_type="r2_mount",
    )
    mount = R2Mount(
        bucket="bucket",
        account_id="abc123accountid",
        access_key_id="r2-access",
        secret_access_key="r2-secret",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(session_id=session_id),
        pattern,
        include_config_text=True,
    )

    assert isinstance(config, RcloneMountConfig)
    assert config.remote_name == remote_name
    assert config.remote_path == "bucket"
    assert config.config_text == (
        f"[{remote_name}]\n"
        "type = s3\n"
        "provider = Cloudflare\n"
        "endpoint = https://abc123accountid.r2.cloudflarestorage.com\n"
        "acl = private\n"
        "env_auth = false\n"
        "access_key_id = r2-access\n"
        "secret_access_key = r2-secret\n"
    )


@pytest.mark.asyncio
async def test_r2_mount_builds_env_auth_config_with_custom_domain() -> None:
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern()
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="r2",
        mount_type="r2_mount",
    )
    mount = R2Mount(
        bucket="bucket",
        account_id="abc123accountid",
        custom_domain="https://eu.r2.cloudflarestorage.com",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    config = await mount.build_in_container_mount_config(
        _MountConfigSession(session_id=session_id),
        pattern,
        include_config_text=True,
    )

    assert isinstance(config, RcloneMountConfig)
    assert config.remote_name == remote_name
    assert config.remote_path == "bucket"
    assert config.config_text == (
        f"[{remote_name}]\n"
        "type = s3\n"
        "provider = Cloudflare\n"
        "endpoint = https://eu.r2.cloudflarestorage.com\n"
        "acl = private\n"
        "env_auth = true\n"
    )


@pytest.mark.asyncio
async def test_r2_mount_merges_existing_rclone_config_section() -> None:
    session_id = uuid.uuid4()
    pattern = RcloneMountPattern(config_file_path=Path("rclone.conf"))
    remote_name = pattern.resolve_remote_name(
        session_id=session_id.hex,
        remote_kind="r2",
        mount_type="r2_mount",
    )
    session = _MountConfigSession(
        session_id=session_id,
        config_text=(f"[{remote_name}]\ntype = s3\nregion = auto\n\n[other]\ntype = memory\n"),
    )
    mount = R2Mount(
        bucket="bucket",
        account_id="abc123accountid",
        access_key_id="r2-access",
        secret_access_key="r2-secret",
        mount_strategy=InContainerMountStrategy(pattern=pattern),
    )

    config = await mount.build_in_container_mount_config(
        session,
        pattern,
        include_config_text=True,
    )

    assert isinstance(config, RcloneMountConfig)
    assert config.remote_name == remote_name
    assert config.config_text == (
        f"[{remote_name}]\n"
        "type = s3\n"
        "region = auto\n"
        "type = s3\n"
        "provider = Cloudflare\n"
        "endpoint = https://abc123accountid.r2.cloudflarestorage.com\n"
        "acl = private\n"
        "env_auth = false\n"
        "access_key_id = r2-access\n"
        "secret_access_key = r2-secret\n"
        "\n"
        "[other]\n"
        "type = memory\n"
    )


def test_r2_mount_rejects_mountpoint_pattern() -> None:
    with pytest.raises(MountConfigError, match="invalid mount_pattern type"):
        R2Mount(
            bucket="bucket",
            account_id="abc123accountid",
            mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
        )


@pytest.mark.asyncio
async def test_r2_mount_rejects_partial_credentials_for_both_strategies() -> None:
    in_container_mount = R2Mount(
        bucket="bucket",
        account_id="abc123accountid",
        access_key_id="r2-access",
        mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
    )
    with pytest.raises(
        MountConfigError,
        match="r2 credentials must include both access_key_id and secret_access_key",
    ):
        await in_container_mount.build_in_container_mount_config(
            _MountConfigSession(),
            RcloneMountPattern(),
            include_config_text=True,
        )

    docker_mount = R2Mount(
        bucket="bucket",
        account_id="abc123accountid",
        secret_access_key="r2-secret",
        mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
    )
    with pytest.raises(
        MountConfigError,
        match="r2 credentials must include both access_key_id and secret_access_key",
    ):
        docker_mount.build_docker_volume_driver_config(DockerVolumeMountStrategy(driver="rclone"))


@pytest.mark.asyncio
async def test_docker_volume_mount_apply_fails_on_non_docker_session() -> None:
    mount = S3Mount(
        bucket="bucket",
        mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
    )

    with pytest.raises(MountConfigError) as exc_info:
        await mount.apply(_MountConfigSession(), Path("/workspace/data"), Path("/ignored"))

    assert str(exc_info.value) == "docker-volume mounts are not supported by this sandbox backend"


def test_mount_requires_at_least_one_supported_strategy() -> None:
    with pytest.raises(
        MountConfigError,
        match="mount type must support at least one mount strategy",
    ):
        _NoStrategyMount()


@pytest.mark.asyncio
async def test_rclone_nfs_server_honors_read_only_runtime_config() -> None:
    session = _MountpointApplySession()
    pattern = RcloneMountPattern(mode="nfs")

    await pattern._start_rclone_server(
        session,
        config=RcloneMountConfig(
            remote_name="remote",
            remote_path="bucket",
            remote_kind="s3",
            mount_type="s3_mount",
            read_only=True,
        ),
        config_path=Path("/workspace/.sandbox-rclone-config/session/remote.conf"),
        nfs_addr="127.0.0.1:2049",
    )

    assert session.exec_calls == [
        [
            "sh",
            "-lc",
            "/usr/local/bin/rclone serve nfs --help >/dev/null 2>&1"
            " || rclone serve nfs --help >/dev/null 2>&1",
        ],
        [
            "sh",
            "-lc",
            "rclone serve nfs remote:bucket --addr 127.0.0.1:2049"
            " --config /workspace/.sandbox-rclone-config/session/remote.conf --read-only &",
        ],
    ]


@pytest.mark.asyncio
async def test_rclone_generated_config_is_written_owner_only() -> None:
    session_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    session = _GeneratedConfigApplySession(session_id=session_id)
    pattern = RcloneMountPattern()

    await pattern.apply(
        session,
        Path("/workspace/mnt"),
        RcloneMountConfig(
            remote_name="remote",
            remote_path="bucket",
            remote_kind="s3",
            mount_type="s3_mount",
            config_text="[remote]\ntype = s3\n",
        ),
    )

    assert session.write_calls == [
        (
            Path(".sandbox-rclone-config/12345678123456781234567812345678/remote.conf"),
            b"[remote]\ntype = s3\n",
        )
    ]
    assert session.exec_calls == [
        ["sh", "-lc", "command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone"],
        ["mkdir", "-p", "/workspace/mnt"],
        ["mkdir", "-p", "/workspace/.sandbox-rclone-config/12345678123456781234567812345678"],
        [
            "chmod",
            "0600",
            "/workspace/.sandbox-rclone-config/12345678123456781234567812345678/remote.conf",
        ],
        [
            "rclone",
            "mount",
            "remote:bucket",
            "/workspace/mnt",
            "--read-only",
            "--config",
            "/workspace/.sandbox-rclone-config/12345678123456781234567812345678/remote.conf",
            "--daemon",
        ],
    ]


@pytest.mark.asyncio
async def test_blobfuse_generated_config_is_written_owner_only() -> None:
    session_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    session = _GeneratedConfigApplySession(session_id=session_id)
    pattern = FuseMountPattern()

    await pattern.apply(
        session,
        Path("/workspace/mnt"),
        FuseMountConfig(
            account="acct",
            container="container",
            endpoint=None,
            identity_client_id=None,
            account_key="secret",
            mount_type="azure_blob_mount",
            read_only=True,
        ),
    )

    assert session.write_calls == [
        (
            Path(".sandbox-blobfuse-config/12345678123456781234567812345678/acct_container.yaml"),
            (
                b"allow-other: true\n"
                b"\n"
                b"logging:\n"
                b"  type: syslog\n"
                b"  level: log_debug\n"
                b"\n"
                b"components:\n"
                b"  - libfuse\n"
                b"  - block_cache\n"
                b"  - attr_cache\n"
                b"  - azstorage\n"
                b"\n"
                b"block_cache:\n"
                b"  block-size-mb: 16\n"
                b"  mem-size-mb: 50000\n"
                b"  path: /workspace/.sandbox-blobfuse-cache/"
                b"12345678123456781234567812345678/acct/container\n"
                b"  disk-size-mb: 50000\n"
                b"  disk-timeout-sec: 3600\n"
                b"\n"
                b"attr_cache:\n"
                b"  timeout-sec: 7200\n"
                b"\n"
                b"azstorage:\n"
                b"  type: block\n"
                b"  account-name: acct\n"
                b"  container: container\n"
                b"  endpoint: https://acct.blob.core.windows.net\n"
                b"  auth-type: key\n"
                b"  account-key: secret\n"
            ),
        )
    ]
    assert session.exec_calls == [
        ["sh", "-lc", "command -v blobfuse2 >/dev/null 2>&1"],
        ["mkdir", "-p", "/workspace/mnt"],
        [
            "mkdir",
            "-p",
            "/workspace/.sandbox-blobfuse-cache/12345678123456781234567812345678/acct/container",
        ],
        ["mkdir", "-p", "/workspace/.sandbox-blobfuse-config/12345678123456781234567812345678"],
        [
            "chmod",
            "0600",
            "/workspace/.sandbox-blobfuse-config/12345678123456781234567812345678/acct_container.yaml",
        ],
        [
            "blobfuse2",
            "mount",
            "--read-only",
            "--config-file",
            "/workspace/.sandbox-blobfuse-config/12345678123456781234567812345678/acct_container.yaml",
            "/workspace/mnt",
        ],
    ]


@pytest.mark.asyncio
async def test_blobfuse_cache_path_must_be_relative_to_workspace() -> None:
    with pytest.raises(MountConfigError) as exc_info:
        FuseMountPattern(cache_path=Path("/tmp/blobfuse-cache"))

    assert exc_info.value.message == "blobfuse cache_path must be relative to the workspace root"
    assert exc_info.value.context == {"cache_path": "/tmp/blobfuse-cache"}

    with pytest.raises(MountConfigError) as escape_exc_info:
        FuseMountPattern(cache_path=Path("../blobfuse-cache"))

    assert escape_exc_info.value.message == (
        "blobfuse cache_path must be relative to the workspace root"
    )
    assert escape_exc_info.value.context == {"cache_path": "../blobfuse-cache"}

    with pytest.raises(MountConfigError) as windows_exc_info:
        FuseMountPattern(cache_path=Path("C:\\blobfuse-cache"))

    assert windows_exc_info.value.message == (
        "blobfuse cache_path must be relative to the workspace root"
    )
    assert windows_exc_info.value.context == {"cache_path": "C:/blobfuse-cache"}


@pytest.mark.asyncio
async def test_blobfuse_cache_path_must_be_outside_mount_path() -> None:
    session_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    session = _GeneratedConfigApplySession(session_id=session_id)
    pattern = FuseMountPattern()

    with pytest.raises(MountConfigError) as exc_info:
        await pattern.apply(
            session,
            Path("/workspace"),
            FuseMountConfig(
                account="acct",
                container="container",
                endpoint=None,
                identity_client_id=None,
                account_key="secret",
                mount_type="azure_blob_mount",
                read_only=True,
            ),
        )

    assert exc_info.value.message == "blobfuse cache_path must be outside the mount path"
    assert exc_info.value.context == {
        "mount_path": "/workspace",
        "cache_path": (
            "/workspace/.sandbox-blobfuse-cache/12345678123456781234567812345678/acct/container"
        ),
    }
    assert session.exec_calls == [["sh", "-lc", "command -v blobfuse2 >/dev/null 2>&1"]]
    assert session.write_calls == []
