from __future__ import annotations

import shlex

from agents.extensions.sandbox.blaxel.mounts import (
    BlaxelCloudBucketMountConfig,
    _mount_gcs,
    _mount_s3,
)
from agents.sandbox import ExecResult
from agents.testing import scripted_sandbox_session

_INJECTION = "x; touch /tmp/pwned"


def _successful_exec(_call: object) -> ExecResult:
    return ExecResult(exit_code=0, stdout=b"", stderr=b"")


async def test_s3_mount_options_are_shell_quoted() -> None:
    session = scripted_sandbox_session(
        [{"method": "exec", "responder": _successful_exec} for _ in range(3)]
    )
    await _mount_s3(
        session,
        BlaxelCloudBucketMountConfig(
            provider="s3",
            bucket="bucket",
            mount_path="/mnt/data",
            endpoint_url=f"http://{_INJECTION}",
        ),
    )
    commands = [call.args[2] for call in session.calls if call.args[:2] == ("sh", "-c")]
    cmd = next(command for command in commands if command.startswith("s3fs"))
    # The injected `; touch` must stay inside the -o option token, not become its own command.
    assert "touch" not in shlex.split(cmd)
    session.assert_complete()


async def test_gcs_mount_prefix_is_shell_quoted() -> None:
    session = scripted_sandbox_session(
        [{"method": "exec", "responder": _successful_exec} for _ in range(3)]
    )
    await _mount_gcs(
        session,
        BlaxelCloudBucketMountConfig(
            provider="gcs",
            bucket="bucket",
            mount_path="/mnt/data",
            prefix=_INJECTION,
        ),
    )
    commands = [call.args[2] for call in session.calls if call.args[:2] == ("sh", "-c")]
    cmd = next(command for command in commands if command.startswith("gcsfuse"))
    assert "touch" not in shlex.split(cmd)
    session.assert_complete()
