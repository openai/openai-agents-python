"""Translate the Agents sandbox operations to an owned Vercel service client.

Execution always uses the captured runtime session. A named Vercel handle can
resume implicitly, which must not preserve the Agents workspace-ready flag.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from vercel.oidc.credentials import get_credentials
from vercel.sandbox import (
    Sandbox,
    SandboxClient,
    SandboxCredentials,
    SandboxInvalidHandleError,
    SandboxPathNotFoundError,
    SandboxResources,
    SandboxRuntimeSession,
    SandboxServiceOptions,
    SandboxStatus,
    SandboxTerminalStateError,
    SnapshotSource,
)

from ._network_policy import NetworkPolicy, to_provider

DEFAULT_VERCEL_WAIT_FOR_RUNNING_TIMEOUT_S = 45.0

# The released runtime images use Amazon Linux, including the S3 mount tools.
_RUNTIME_IMAGES = {
    "node22": "vercel/sandbox/node:al-22",
    "node24": "vercel/sandbox/node:al-24",
    "node26": "vercel/sandbox/node:al-26",
    "python3.13": "vercel/sandbox/python:al-3.13.1",
}


@dataclass
class _CommandResult:
    exit_code: int
    _stdout: str
    _stderr: str

    async def stdout(self) -> str:
        return self._stdout

    async def stderr(self) -> str:
        return self._stderr


@dataclass
class _SnapshotReference:
    snapshot_id: str


class ProviderSandbox:
    """Own one provider transport and one exact execution session."""

    def __init__(self, client: SandboxClient, sandbox: Sandbox) -> None:
        session = sandbox.current_session
        if session is None:
            raise SandboxInvalidHandleError("Sandbox has no execution session")
        self.client = client
        self._sandbox = sandbox
        self._session: SandboxRuntimeSession = session

    @property
    def sandbox_id(self) -> str:
        return self._session.id

    @property
    def sandbox_name(self) -> str:
        return self._sandbox.name

    @property
    def status(self) -> SandboxStatus | None:
        return self._session.status

    @staticmethod
    def _client(token: str | None, project_id: str | None, team_id: str | None) -> SandboxClient:
        resolved = get_credentials(token=token, project_id=project_id, team_id=team_id)

        async def credentials() -> SandboxCredentials:
            return SandboxCredentials(
                token=resolved.token, project_id=resolved.project_id, team_id=resolved.team_id
            )

        return SandboxClient.create(options=SandboxServiceOptions(credentials_factory=credentials))

    @classmethod
    async def create(
        cls,
        *,
        token: str | None = None,
        project_id: str | None = None,
        team_id: str | None = None,
        source: SnapshotSource | None = None,
        ports: list[int] | None = None,
        timeout: int | None = None,
        resources: SandboxResources | None = None,
        runtime: str | None = None,
        interactive: bool = False,
        env: dict[str, str] | None = None,
        network_policy: NetworkPolicy | None = None,
    ) -> ProviderSandbox:
        # The provider's controller now owns interactive connections; creation
        # no longer needs the legacy flag. Keep accepting it for existing state.
        image = _RUNTIME_IMAGES.get(runtime or "node24")
        if image is None:
            raise ValueError(f"Unsupported Vercel runtime: {runtime!r}")
        client = cls._client(token, project_id, team_id)
        sandbox = None
        name = "agents-" + uuid.uuid4().hex
        try:
            sandbox = await asyncio.wait_for(
                client.create_sandbox(
                    name=name,
                    image=image,
                    source=source,
                    ports=ports,
                    execution_time_limit=None
                    if timeout is None
                    else timedelta(milliseconds=timeout),
                    resources=resources,
                    persistent=False,
                    env=env,
                    network_policy=to_provider(network_policy),
                ),
                timeout=DEFAULT_VERCEL_WAIT_FOR_RUNNING_TIMEOUT_S,
            )
            return cls(client, sandbox)
        except BaseException as error:
            if isinstance(error, SandboxTerminalStateError) and isinstance(error.sandbox, Sandbox):
                sandbox = error.sandbox
            try:
                if sandbox is None:
                    sandbox = await client.get_sandbox(name=name)
                if sandbox is not None:
                    try:
                        await sandbox.stop()
                    finally:
                        await sandbox.destroy()
            except (Exception, asyncio.CancelledError):
                # Best-effort cleanup must not replace the original failure or cancellation.
                pass
            try:
                await client.aclose()
            except (Exception, asyncio.CancelledError):
                # Best-effort cleanup must not replace the original failure or cancellation.
                pass
            raise

    @classmethod
    async def get(
        cls,
        *,
        sandbox_id: str,
        sandbox_name: str | None = None,
        token: str | None = None,
        project_id: str | None = None,
        team_id: str | None = None,
    ) -> ProviderSandbox:
        client = cls._client(token, project_id, team_id)
        try:
            sandbox = await client.get_sandbox(name=sandbox_name or sandbox_id)
            result = cls(client, sandbox)
            if result.sandbox_id != sandbox_id:
                raise SandboxInvalidHandleError("Sandbox execution session has changed")
            return result
        except BaseException:
            try:
                await client.aclose()
            except (Exception, asyncio.CancelledError):
                # Best-effort cleanup must not replace the original failure or cancellation.
                pass
            raise

    async def refresh(self) -> None:
        await self._session.refresh()

    async def wait_for_status(self, status: SandboxStatus, *, timeout: float) -> None:
        async def wait() -> None:
            while self.status != status:
                await asyncio.sleep(0.5)
                await self.refresh()

        await asyncio.wait_for(wait(), timeout=timeout)

    async def stop(self, *, blocking: bool = False) -> None:
        await self._session.stop()
        # Check the current identity before deleting this Agents-owned name.
        # A replacement already present at lookup belongs to another owner.
        self._sandbox = await self.client.get_sandbox(name=self.sandbox_name)
        if self._sandbox.current_session_id == self.sandbox_id:
            await self._sandbox.destroy()

    async def run_command(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sudo: bool = False,
    ) -> _CommandResult:
        result = await self._session.run_process(
            command, args, cwd=cwd, env=env, sudo=sudo, capture_output=True, check=False
        )
        return _CommandResult(result.returncode, result.stdout or "", result.stderr or "")

    def domain(self, port: int) -> str:
        for route in self._sandbox.routes:
            if route.port == port and not route.system:
                return route.url
        raise ValueError(f"No route for port {port}")

    async def read_file(self, path: str) -> bytes | None:
        try:
            return await self._session.fs.read_bytes(path)
        except SandboxPathNotFoundError:
            return None

    async def iter_file(self, path: str, *, chunk_size: int) -> AsyncIterator[bytes]:
        async def chunks() -> AsyncIterator[bytes]:
            async with self._session.fs.open(path, "rb") as reader:
                while data := await reader.read(chunk_size):
                    yield data

        return chunks()

    async def write_files(self, files: list[dict[str, object]]) -> None:
        async with self._session.fs.batch() as batch:
            for file in files:
                batch.write_bytes(cast(str, file["path"]), cast(bytes, file["content"]))

    async def snapshot(self, *, expiration: int | None = None) -> _SnapshotReference:
        result = await self._session.snapshot(
            expiration=None if expiration is None else timedelta(milliseconds=expiration)
        )
        return _SnapshotReference(result.id)
