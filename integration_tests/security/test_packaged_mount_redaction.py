from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path
from typing import Literal

import pytest

from agents.sandbox import Manifest
from agents.sandbox.entries import FuseMountPattern, MountpointMountPattern
from agents.sandbox.entries.mounts.patterns import FuseMountConfig, MountpointMountConfig
from agents.sandbox.errors import ErrorCode, MountCommandError
from agents.sandbox.session import (
    BaseSandboxSession,
    CallbackSink,
    Instrumentation,
    SandboxSession,
    SandboxSessionEvent,
    SandboxSessionState,
)
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, User
from integration_tests._contract_support import _redaction_observables

pytestmark = pytest.mark.security


class _SecuritySessionState(SandboxSessionState):
    type: Literal["integration_security"] = "integration_security"


class _FailingMountSession(BaseSandboxSession):
    def __init__(self, *, mount_stderr: bytes) -> None:
        self.state = _SecuritySessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self._mount_stderr = mount_stderr
        self.exec_calls: list[list[str]] = []

    async def read(self, path: Path, *, user: str | User | None = None) -> io.BytesIO:
        _ = (path, user)
        raise AssertionError("read() should not be called")

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        _ = (path, data, user)

    async def running(self) -> bool:
        return True

    async def shutdown(self) -> None:
        return None

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        command_strings = [str(part) for part in command]
        self.exec_calls.append(command_strings)
        if (
            len(command_strings) >= 3
            and command_strings[:2] == ["sh", "-lc"]
            and "mount-s3 " in command_strings[2]
            and "command -v " not in command_strings[2]
        ) or command_strings[:2] == ["blobfuse2", "mount"]:
            return ExecResult(exit_code=1, stdout=b"", stderr=self._mount_stderr)
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    async def persist_workspace(self) -> io.IOBase:
        raise AssertionError("persist_workspace() should not be called")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        raise AssertionError("hydrate_workspace() should not be called")


async def test_installed_distribution_redacts_mount_credentials_from_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinels = (
        "oaicred_access_42",
        "oaicred_secret_42",
        "oaicred_token_42",
        "oaicred_endpoint_42",
    )
    events: list[SandboxSessionEvent] = []
    inner = _FailingMountSession(
        mount_stderr=("mount failed: " + " ".join(sentinels)).encode(),
    )
    session = SandboxSession(
        inner,
        instrumentation=Instrumentation(
            sinks=[CallbackSink(lambda event, _session: events.append(event))]
        ),
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(
            MountCommandError,
            match="sandbox operation failed while using a protected mount configuration",
        ) as exc_info:
            await MountpointMountPattern().apply(
                session,
                Path("/workspace/remote"),
                MountpointMountConfig(
                    bucket="bucket",
                    access_key_id=sentinels[0],
                    secret_access_key=sentinels[1],
                    session_token=sentinels[2],
                    prefix=None,
                    region="us-east-1",
                    endpoint_url=f"https://user:{sentinels[3]}@example.test",
                    mount_type="s3_mount",
                    read_only=True,
                ),
            )

    error = exc_info.value
    serialized_observables = _redaction_observables(error, caplog.records)
    serialized_observables += "\n" + "\n".join(
        (
            *(event.model_dump_json() for event in events),
            *(" ".join(command) for command in inner.exec_calls),
        )
    )
    assert error.error_code is ErrorCode.MOUNT_FAILED
    assert error.op == "materialize"
    assert error.retryable is False
    assert error.context == {}
    for sentinel in sentinels:
        assert sentinel not in serialized_observables


async def test_installed_distribution_redacts_fuse_inline_authority_from_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "oaicred_fuse_endpoint_42"
    inner = _FailingMountSession(mount_stderr=b"mount failed")
    session = SandboxSession(inner)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(
            MountCommandError,
            match="sandbox operation failed while using a protected mount configuration",
        ) as exc_info:
            await FuseMountPattern().apply(
                session,
                Path("/workspace/remote"),
                FuseMountConfig(
                    account="account",
                    container="container",
                    endpoint=f"https://user:{sentinel}@example.test",
                    identity_client_id=None,
                    account_key=None,
                    mount_type="azure_blob_mount",
                    read_only=True,
                ),
            )

    error = exc_info.value
    assert error.error_code is ErrorCode.MOUNT_FAILED
    assert error.op == "materialize"
    assert error.retryable is False
    assert error.context == {}
    assert sentinel not in _redaction_observables(error, caplog.records)
