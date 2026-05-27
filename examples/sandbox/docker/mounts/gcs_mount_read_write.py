from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from agents.sandbox.entries import (
    DockerVolumeMountStrategy,
    GCSMount,
    InContainerMountStrategy,
    MountpointMountPattern,
    RcloneMountPattern,
)
from examples.sandbox.docker.mounts.mount_smoke import (
    MountSmokeCase,
    require_env,
    run_mount_smoke_test,
)


def _mount_cases() -> list[MountSmokeCase]:
    bucket = require_env("GCS_MOUNT_BUCKET")
    access_id = os.getenv("GCS_ACCESS_ID")
    secret_access_key = os.getenv("GCS_SECRET_ACCESS_KEY")
    prefix = os.getenv("GCS_MOUNT_PREFIX")
    region = os.getenv("GCS_REGION")
    endpoint_url = os.getenv("GCS_ENDPOINT_URL")
    service_account_file = os.getenv("GCS_SERVICE_ACCOUNT_FILE")
    service_account_credentials = os.getenv("GCS_SERVICE_ACCOUNT_CREDENTIALS")
    access_token = os.getenv("GCS_ACCESS_TOKEN")

    return [
        MountSmokeCase(
            name="docker_volume/rclone",
            mount_dir="gcs-docker-volume-rclone",
            mount=GCSMount(
                bucket=bucket,
                access_id=access_id,
                secret_access_key=secret_access_key,
                prefix=prefix,
                region=region,
                endpoint_url=endpoint_url,
                service_account_file=service_account_file,
                service_account_credentials=service_account_credentials,
                access_token=access_token,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                read_only=False,
            ),
        ),
        MountSmokeCase(
            name="in_container/rclone",
            mount_dir="gcs-in-container-rclone",
            mount=GCSMount(
                bucket=bucket,
                access_id=access_id,
                secret_access_key=secret_access_key,
                prefix=prefix,
                region=region,
                endpoint_url=endpoint_url,
                service_account_file=service_account_file,
                service_account_credentials=service_account_credentials,
                access_token=access_token,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                read_only=False,
            ),
        ),
        MountSmokeCase(
            name="in_container/mountpoint",
            mount_dir="gcs-in-container-mountpoint",
            mount=GCSMount(
                bucket=bucket,
                access_id=access_id,
                secret_access_key=secret_access_key,
                prefix=prefix,
                region=region,
                endpoint_url=endpoint_url,
                service_account_file=service_account_file,
                service_account_credentials=service_account_credentials,
                access_token=access_token,
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                read_only=False,
            ),
        ),
    ]


async def main() -> None:
    await run_mount_smoke_test(
        provider="gcs",
        agent_name="GCS Mount Smoke Test",
        mount_cases=_mount_cases(),
    )


if __name__ == "__main__":
    asyncio.run(main())
