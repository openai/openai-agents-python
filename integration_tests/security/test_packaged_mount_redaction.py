from __future__ import annotations

import io
import logging
import traceback
import uuid
from pathlib import Path
from typing import Literal

import pytest

from agents.sandbox import Manifest
from agents.sandbox.entries import MountpointMountPattern
from agents.sandbox.entries.mounts.patterns import MountpointMountConfig
from agents.sandbox.errors import MountCommandError
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
        ):
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
        with pytest.raises(MountCommandError) as exc_info:
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
                    endpoint_url=None,
                    mount_type="s3_mount",
                    read_only=True,
                ),
            )

    error = exc_info.value
    serialized_observables = "\n".join(
        (
            str(error),
            repr(error),
            repr(error.__cause__),
            repr(error.__context__),
            repr(error.context),
            "".join(traceback.format_exception(error)),
            *(record.getMessage() for record in caplog.records),
            *(repr(record.args) for record in caplog.records),
            *(repr(record.__dict__) for record in caplog.records),
            *(
                "".join(traceback.format_exception(*record.exc_info))
                for record in caplog.records
                if record.exc_info is not None
            ),
            *(event.model_dump_json() for event in events),
            *(" ".join(command) for command in inner.exec_calls),
        )
    )
    assert "REDACTED" in serialized_observables
    for sentinel in sentinels:
        assert sentinel not in serialized_observables
