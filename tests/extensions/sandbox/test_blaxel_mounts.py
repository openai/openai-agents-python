from __future__ import annotations

import shlex
from types import SimpleNamespace
from typing import Any

from agents.extensions.sandbox.blaxel.mounts import (
    BlaxelCloudBucketMountConfig,
    _mount_gcs,
    _mount_s3,
)

_INJECTION = "x; touch /tmp/pwned"


class _RecordingSession:
    """Minimal sandbox session that records the `sh -c` commands it is asked to run."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec(self, *args: Any, **kwargs: Any) -> Any:
        if len(args) >= 3 and args[0] == "sh" and args[1] == "-c":
            self.commands.append(args[2])
        return SimpleNamespace(exit_code=0, stdout=b"", stderr=b"")


async def test_s3_mount_options_are_shell_quoted() -> None:
    session = _RecordingSession()
    await _mount_s3(
        session,  # type: ignore[arg-type]
        BlaxelCloudBucketMountConfig(
            provider="s3",
            bucket="bucket",
            mount_path="/mnt/data",
            endpoint_url=f"http://{_INJECTION}",
        ),
    )
    cmd = next(c for c in session.commands if c.startswith("s3fs"))
    # The injected `; touch` must stay inside the -o option token, not become its own command.
    assert "touch" not in shlex.split(cmd)


async def test_gcs_mount_prefix_is_shell_quoted() -> None:
    session = _RecordingSession()
    await _mount_gcs(
        session,  # type: ignore[arg-type]
        BlaxelCloudBucketMountConfig(
            provider="gcs",
            bucket="bucket",
            mount_path="/mnt/data",
            prefix=_INJECTION,
        ),
    )
    cmd = next(c for c in session.commands if c.startswith("gcsfuse"))
    assert "touch" not in shlex.split(cmd)
