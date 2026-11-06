"""Bind an authenticated WSS gateway route to a private Linux Docker job.

Provision the job with create_offline_job(image). Keep its full ID in the
application's trusted job record. Pin the Docker daemon in the service
configuration. On each connection, including restore, the route authenticates
the caller, looks up its job, and relays the exec-server protocol on these pipes::

    async with open_exec_stdio(job.container_id) as process:
        await relay_messages(websocket, process.stdin, process.stdout)

The gateway owns TLS, authentication and the relay. Join the relay tasks before
leaving the context. Keep the container across checkpoints and explicitly run
`docker rm -f <id>` at job end. Closing a CLI stream can leave descendants in
the container.

Use a trusted, preloaded image with /bin/sleep, /usr/local/bin/exec-server, a
writable workspace, and a compatible nested Linux sandbox. Keep the exec-server,
proxy and resolver inside the job. The application controls the image, daemon,
and route. Keep host sockets and inherited connections outside the job.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

_INSPECT = (
    "[{{json .Id}},{{json .State.Running}},"
    "{{json .HostConfig.NetworkMode}},{{json .NetworkSettings.Networks}}]"
)


def _docker(*args: str) -> bytes:
    result = subprocess.run(
        ["docker", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed")
    return result.stdout


def _full_id(value: str) -> str:
    if not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("Expected a full Docker container ID")
    return value


def _inspect(container_id: str) -> None:
    fields = _docker("inspect", "--type=container", "--format", _INSPECT, _full_id(container_id))
    actual_id, running, mode, networks = json.loads(fields)
    if actual_id != container_id:
        raise RuntimeError("Docker inspected a different container")
    if running is not True:
        raise RuntimeError("The Docker job is not running")
    if mode != "none" or not isinstance(networks, dict) or networks.keys() - {"none"}:
        raise RuntimeError("The Docker job requires network mode none and no other networks")


def create_offline_job(image: str) -> str:
    """Start an idle job and return the full ID after checking its network."""
    output = _docker(
        "create",
        "--pull=never",
        "--network",
        "none",
        "--entrypoint",
        "/bin/sleep",
        "--",
        image,
        "infinity",
    )
    container_id = _full_id(output.decode("ascii").strip())
    try:
        _docker("start", container_id)
        _inspect(container_id)
    except BaseException:
        with suppress(OSError, RuntimeError, subprocess.TimeoutExpired):
            _docker("rm", "-f", container_id)
        raise
    return container_id


@asynccontextmanager
async def open_exec_stdio(container_id: str) -> AsyncIterator[asyncio.subprocess.Process]:
    """Check the job again and open its exec-server's stdio for one connection."""
    inspection = asyncio.get_running_loop().run_in_executor(None, _inspect, container_id)
    try:
        await asyncio.shield(inspection)
    except asyncio.CancelledError:
        # Join the bounded Docker call even if the attachment is cancelled again.
        while not inspection.done():
            with suppress(asyncio.CancelledError):
                await asyncio.wait((inspection,))
        inspection.exception()
        raise
    process = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        container_id,
        "/usr/local/bin/exec-server",
        "stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=4 * 1024 * 1024,
    )
    assert process.stdin is not None
    try:
        yield process
    finally:
        process.stdin.close()
        try:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), 3)
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            cancelled = None
            while True:
                try:
                    await process.communicate()
                    break
                except asyncio.CancelledError as error:
                    cancelled = error
            if cancelled is not None:
                raise cancelled
