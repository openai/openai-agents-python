"""Smoke-test an Amazon S3 Files file-system mount in Docker.

Required:

    S3_FILES_FILE_SYSTEM_ID=fs-...

Common optional settings:

    S3_FILES_MOUNT_TARGET_IP=10.0.0.123
    AWS_REGION=us-east-1
    S3_FILES_ACCESS_POINT=fsap-...
    S3_FILES_SUBPATH=/path/in/file-system

Example:

    S3_FILES_FILE_SYSTEM_ID=fs-... \
    S3_FILES_MOUNT_TARGET_IP=10.0.0.123 \
    AWS_REGION=us-east-1 \
    uv run python examples/sandbox/docker/mounts/s3_files_mount_read_write.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from agents.sandbox.entries import (
    InContainerMountStrategy,
    S3FilesMount,
    S3FilesMountPattern,
)
from examples.sandbox.docker.mounts.mount_smoke import (
    MountSmokeCase,
    require_env,
    run_mount_smoke_test,
)


def _mount_cases() -> list[MountSmokeCase]:
    file_system_id = require_env("S3_FILES_FILE_SYSTEM_ID")
    return [
        MountSmokeCase(
            name="in_container/s3files",
            mount_dir="s3-files-in-container",
            mount=S3FilesMount(
                file_system_id=file_system_id,
                subpath=os.getenv("S3_FILES_SUBPATH"),
                mount_target_ip=os.getenv("S3_FILES_MOUNT_TARGET_IP"),
                access_point=os.getenv("S3_FILES_ACCESS_POINT"),
                region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
                mount_strategy=InContainerMountStrategy(pattern=S3FilesMountPattern()),
                read_only=False,
            ),
        )
    ]


async def main() -> None:
    await run_mount_smoke_test(
        provider="s3-files",
        agent_name="S3 Files Mount Smoke Test",
        mount_cases=_mount_cases(),
    )


if __name__ == "__main__":
    asyncio.run(main())
