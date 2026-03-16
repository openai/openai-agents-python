from __future__ import annotations

import abc
from typing import Generic, TypeVar

from ..codex_config import CodexConfig
from ..manifest import Manifest
from ..snapshot import SnapshotSpec
from .base_sandbox_session import BaseSandboxSession
from .dependencies import Dependencies
from .manager import Instrumentation
from .sandbox_session import SandboxSession
from .sandbox_session_state import SandboxSessionState

ClientOptionsT = TypeVar("ClientOptionsT")


class BaseSandboxClient(abc.ABC, Generic[ClientOptionsT]):
    backend_id: str
    supports_default_options: bool = False
    _dependencies: Dependencies | None = None

    def _resolve_dependencies(self) -> Dependencies | None:
        if self._dependencies is None:
            return None
        # Sessions get clones instead of the shared template so per-session factory caches and
        # owned resources do not leak across unrelated sandboxes.
        return self._dependencies.clone()

    def _wrap_session(
        self,
        inner: BaseSandboxSession,
        *,
        instrumentation: Instrumentation | None = None,
    ) -> SandboxSession:
        # Always return the instrumented wrapper so callers get consistent events and dependency
        # lifecycle handling regardless of which backend created the inner session.
        return SandboxSession(
            inner,
            instrumentation=instrumentation,
            dependencies=self._resolve_dependencies(),
        )

    @abc.abstractmethod
    async def create(
        self,
        *,
        snapshot: SnapshotSpec | None = None,
        manifest: Manifest | None = None,
        codex: bool | CodexConfig = False,
        options: ClientOptionsT,
    ) -> SandboxSession:
        """Create a new session.

        Args:
            snapshot: Snapshot spec used to create a snapshot instance for
                the session. If omitted, the session uses a no-op snapshot.
            manifest: Optional manifest to materialize into the workspace when
                the session starts.
            codex: Whether to provision Codex into the workspace, or a custom
                Codex provisioning config.
            options: Sandbox-specific settings. For example, Docker expects
                ``DockerSandboxClientOptions(image="...")``.
        Returns:
            A `SandboxSession` that can be entered with `async with` or closed explicitly with
            `await session.aclose()`.
        """

    @abc.abstractmethod
    async def delete(self, session: SandboxSession) -> SandboxSession:
        """Delete a session and release sandbox resources."""

    @abc.abstractmethod
    async def resume(
        self,
        state: SandboxSessionState,
        *,
        codex: bool | CodexConfig = False,
    ) -> SandboxSession:
        """Resume a session from a previously persisted `SandboxSessionState`.

        The returned session should hydrate its workspace from `state.snapshot`
        during `SandboxSession.start()`.
        """

    def serialize_session_state(self, state: SandboxSessionState) -> dict[str, object]:
        """Serialize backend-specific sandbox state into a JSON-compatible payload."""
        return state.model_dump(mode="json")

    @abc.abstractmethod
    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        """Deserialize backend-specific sandbox state from a JSON-compatible payload."""
