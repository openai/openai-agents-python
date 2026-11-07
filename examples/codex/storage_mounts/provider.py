"""Use provider storage with an application-owned native executor gateway.

The connect callable must target the supplied SandboxSession and own gateway
cleanup. This example does not install or deploy a gateway or storage driver.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast

from agents import Agent, Environment
from agents.sandbox import (
    DockerVolumeMountStrategy,
    Manifest,
    S3Mount,
    SandboxSession,
    render_storage_layout,
    sandbox_environment,
)


async def main() -> None:
    import docker

    from agents.classic.sandbox.sandboxes.docker import (
        DockerSandboxClient,
        DockerSandboxClientOptions,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--connect", required=True, help="trusted module:callable gateway connector"
    )
    parser.add_argument(
        "--image", required=True, help="preloaded image with native executor support"
    )
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args()
    module, name = args.connect.split(":", 1)
    connect = cast(
        Callable[[SandboxSession], AbstractAsyncContextManager[Environment[None]]],
        getattr(importlib.import_module(module), name),
    )
    manifest = Manifest(
        # Render guidance using Docker's effective workspace root.
        root="/workspace",
        entries={
            "data": S3Mount(
                bucket=args.bucket,
                access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                session_token=os.environ.get("AWS_SESSION_TOKEN"),
                read_only=True,
                description="Reference data; write results in the workspace.",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
        },
    )
    api = docker.from_env()
    try:
        source = sandbox_environment(
            DockerSandboxClient(api),
            manifest=manifest,
            options=DockerSandboxClientOptions(image=args.image),
            connect=connect,
        )
        agent = Agent(instructions="Inspect storage.\n" + render_storage_layout(manifest))
        result = await agent.run("List the files under /workspace/data.", environment=source)
        print(result.final_output)
    finally:
        api.close()


if __name__ == "__main__":
    asyncio.run(main())
