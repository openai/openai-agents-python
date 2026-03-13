from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest

from agents.sandbox import Manifest
from agents.sandbox.entries import (
    AzureBlobMount,
    GCSMount,
    MountpointMountPattern,
    RcloneMountPattern,
)
from agents.sandbox.entries.mounts.patterns import MountpointMountConfig, RcloneMountConfig
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult


class _MountConfigSession(BaseSandboxSession):
    def __init__(self, *, session_id: uuid.UUID | None = None, config_text: str = "") -> None:
        self.state = SandboxSessionState(
            session_id=session_id or uuid.uuid4(),
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self._config_text = config_text

    async def read(self, path: Path) -> io.BytesIO:
        _ = path
        return io.BytesIO(self._config_text.encode("utf-8"))

    async def shutdown(self) -> None:
        return None

    async def write(self, path: Path, data: io.IOBase) -> None:
        _ = (path, data)
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
        mount_pattern=pattern,
    )

    apply_config = await mount._build_mount_config(session, pattern, include_config_text=True)
    unmount_config = await mount._build_mount_config(session, pattern, include_config_text=False)

    assert isinstance(apply_config, RcloneMountConfig)
    assert apply_config.remote_name == remote_name
    assert apply_config.remote_path == "container"
    assert apply_config.config_text is not None
    assert "account = acct" in apply_config.config_text
    assert isinstance(unmount_config, RcloneMountConfig)
    assert unmount_config.remote_name == remote_name
    assert unmount_config.config_text is None


@pytest.mark.asyncio
async def test_gcs_mount_uses_runtime_endpoint_override_without_mutating_pattern_options() -> None:
    pattern = MountpointMountPattern()
    mount = GCSMount(bucket="bucket", mount_pattern=pattern)

    config = await mount._build_mount_config(
        _MountConfigSession(),
        pattern,
        include_config_text=False,
    )

    assert isinstance(config, MountpointMountConfig)
    assert config.endpoint_url == "https://storage.googleapis.com"
    assert pattern.options.endpoint_url is None
