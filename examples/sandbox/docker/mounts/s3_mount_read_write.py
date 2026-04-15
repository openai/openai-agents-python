from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from agents.sandbox.entries import (
    DockerVolumeMountStrategy,
    InContainerMountStrategy,
    MountpointMountPattern,
    RcloneMountPattern,
    S3Mount,
)
from examples.sandbox.docker.mounts.mount_smoke import (
    MountSmokeCase,
    require_env,
    run_mount_smoke_test,
)


def _mount_cases() -> list[MountSmokeCase]:
    bucket = require_env("S3_MOUNT_BUCKET")
    return [
        MountSmokeCase(
            name="docker_volume/rclone",
            mount_dir="s3-docker-volume-rclone",
            mount=S3Mount(
                bucket=bucket,
                access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                session_token=os.getenv("AWS_SESSION_TOKEN"),
                prefix=os.getenv("S3_MOUNT_PREFIX"),
                region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
                endpoint_url=os.getenv("S3_ENDPOINT_URL"),
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                read_only=False,
            ),
        ),
        MountSmokeCase(
            name="in_container/rclone",
            mount_dir="s3-in-container-rclone",
            mount=S3Mount(
                bucket=bucket,
                access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                session_token=os.getenv("AWS_SESSION_TOKEN"),
                prefix=os.getenv("S3_MOUNT_PREFIX"),
                region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
                endpoint_url=os.getenv("S3_ENDPOINT_URL"),
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                read_only=False,
            ),
        ),
        MountSmokeCase(
            name="in_container/mountpoint",
            mount_dir="s3-in-container-mountpoint",
            mount=S3Mount(
                bucket=bucket,
                access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                session_token=os.getenv("AWS_SESSION_TOKEN"),
                prefix=os.getenv("S3_MOUNT_PREFIX"),
                region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
                endpoint_url=os.getenv("S3_ENDPOINT_URL"),
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                read_only=False,
            ),
        ),
    ]


async def main() -> None:
    await run_mount_smoke_test(
        provider="s3",
        agent_name="S3 Mount Smoke Test",
        mount_cases=_mount_cases(),
    )


if __name__ == "__main__":
    asyncio.run(main())
