from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from agents.sandbox.entries import (
    AzureBlobMount,
    DockerVolumeMountStrategy,
    FuseMountPattern,
    InContainerMountStrategy,
    RcloneMountPattern,
)
from examples.sandbox.docker.mounts.mount_smoke import (
    MountSmokeCase,
    require_env,
    run_mount_smoke_test,
)


def _mount_cases() -> list[MountSmokeCase]:
    account = require_env("AZURE_STORAGE_ACCOUNT")
    container = require_env("AZURE_STORAGE_CONTAINER")
    endpoint = os.getenv("AZURE_STORAGE_ENDPOINT")
    identity_client_id = os.getenv("AZURE_CLIENT_ID")
    account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")

    return [
        MountSmokeCase(
            name="docker_volume/rclone",
            mount_dir="azure-docker-volume-rclone",
            mount=AzureBlobMount(
                account=account,
                container=container,
                endpoint=endpoint,
                identity_client_id=identity_client_id,
                account_key=account_key,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                read_only=False,
            ),
        ),
        MountSmokeCase(
            name="in_container/rclone",
            mount_dir="azure-in-container-rclone",
            mount=AzureBlobMount(
                account=account,
                container=container,
                endpoint=endpoint,
                identity_client_id=identity_client_id,
                account_key=account_key,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                read_only=False,
            ),
        ),
        MountSmokeCase(
            name="in_container/fuse",
            mount_dir="azure-in-container-fuse",
            mount=AzureBlobMount(
                account=account,
                container=container,
                endpoint=endpoint,
                identity_client_id=identity_client_id,
                account_key=account_key,
                mount_strategy=InContainerMountStrategy(pattern=FuseMountPattern()),
                read_only=False,
            ),
        ),
    ]


async def main() -> None:
    await run_mount_smoke_test(
        provider="azure",
        agent_name="Azure Blob Mount Smoke Test",
        mount_cases=_mount_cases(),
    )


if __name__ == "__main__":
    asyncio.run(main())
