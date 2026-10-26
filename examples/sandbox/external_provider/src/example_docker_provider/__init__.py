"""A separately packaged client that delegates execution to the SDK's Docker backend."""

from typing import Literal

from pydantic import ConfigDict, Field

from agents.sandbox import Manifest
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions
from agents.sandbox.session import (
    BaseSandboxClient,
    BaseSandboxClientOptions,
    SandboxSession,
    SandboxSessionState,
)
from agents.sandbox.snapshot import SnapshotBase, SnapshotSpec

__all__ = ["ExampleDockerClient", "ExampleDockerOptions"]


class ExampleDockerOptions(BaseSandboxClientOptions):
    """Options registered when the application imports this package."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["example_docker"] = "example_docker"
    image: str = Field(min_length=1)


class ExampleDockerClient(BaseSandboxClient[ExampleDockerOptions]):
    """Adapt package-specific options while retaining Docker sessions and state.

    This is a concrete external package, not a new execution engine. The application
    owns the Docker connection supplied to the delegate and must close that connection.
    """

    # Backend identity describes the session runtime, not the distribution name.
    backend_id = "docker"

    def __init__(self, delegate: DockerSandboxClient) -> None:
        self.delegate = delegate

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: ExampleDockerOptions,
    ) -> SandboxSession:
        if not isinstance(options, ExampleDockerOptions):
            raise TypeError(
                "ExampleDockerClient requires an ExampleDockerOptions instance; "
                "use DockerSandboxClient directly for Docker options"
            )
        return await self.delegate.create(
            snapshot=snapshot,
            manifest=manifest,
            options=DockerSandboxClientOptions(image=options.image),
        )

    async def delete(self, session: SandboxSession) -> SandboxSession:
        return await self.delegate.delete(session)

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        return await self.delegate.resume(state)

    def serialize_session_state(self, state: SandboxSessionState) -> dict[str, object]:
        return self.delegate.serialize_session_state(state)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return self.delegate.deserialize_session_state(payload)
