"""Paired native conversation and provider workspace receipts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar, cast

from .sandbox_memory import _MEMORY_OWNER_ATTRIBUTE, _SandboxMemoryOwner

if TYPE_CHECKING:
    from .executor import Environment
    from .state import SessionState


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _association(conversation: str, workspace: str) -> str:
    return hashlib.sha256((conversation + "\0" + workspace).encode()).hexdigest()


@dataclass(frozen=True)
class ManagedWorkspaceCheckpoint:
    """The checksum has been checking consistency, never authorizing external bytes."""

    conversation_json: str
    workspace_json: str
    association: str
    _capture_provenance: ClassVar[tuple[str, str, str] | None] = None

    def __post_init__(self) -> None:
        from .state import SessionState

        if not all(
            isinstance(value, str)
            for value in (self.conversation_json, self.workspace_json, self.association)
        ):
            raise TypeError("Managed checkpoint fields must be strings")
        if not hmac.compare_digest(
            self.association, _association(self.conversation_json, self.workspace_json)
        ):
            raise ValueError("Managed conversation/workspace association does not match")
        SessionState.from_json(self.conversation_json)
        workspace = json.loads(self.workspace_json)
        if (
            not isinstance(workspace, dict)
            or set(workspace) != {"backend_id", "provider_state"}
            or not isinstance(workspace["backend_id"], str)
            or not isinstance(workspace["provider_state"], dict)
        ):
            raise ValueError("Invalid managed provider receipt")
        if _json(workspace) != self.workspace_json:
            raise ValueError("Managed provider receipt must use its captured encoding")

    @property
    def conversation(self) -> SessionState:
        from .state import SessionState

        return SessionState.from_json(self.conversation_json)

    @property
    def workspace(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.workspace_json))

    def to_json(self) -> str:
        return _json(
            {
                "format": "native-managed-workspace-v1",
                "conversation": self.conversation_json,
                "workspace": self.workspace_json,
                "association": self.association,
            }
        )

    @classmethod
    def from_json(cls, source: str) -> ManagedWorkspaceCheckpoint:
        value = json.loads(source)
        if (
            not isinstance(value, dict)
            or set(value) != {"format", "conversation", "workspace", "association"}
            or value["format"] != "native-managed-workspace-v1"
        ):
            raise ValueError("Invalid managed checkpoint envelope")
        return cls(value["conversation"], value["workspace"], value["association"])

    @classmethod
    def _capture(cls, conversation: str, workspace: str) -> ManagedWorkspaceCheckpoint:
        return cls(conversation, workspace, _association(conversation, workspace))


def _capture_retired_workspace(conversation: str, workspace: str) -> ManagedWorkspaceCheckpoint:
    """Publish provenance only from the successful native retirement owner."""
    checkpoint = ManagedWorkspaceCheckpoint._capture(conversation, workspace)
    object.__setattr__(
        checkpoint,
        "_capture_provenance",
        (checkpoint.conversation_json, checkpoint.workspace_json, checkpoint.association),
    )
    return checkpoint


_WORKSPACE_INSTRUCTIONS_ATTRIBUTE = "_sdk_workspace_instructions"


class _ManagedWorkspaceSource(AbstractAsyncContextManager["Environment[Any]"]):
    """Existing source lifetime plus one explicitly requested retirement receipt."""

    def __init__(
        self,
        source: AbstractAsyncContextManager[Environment[Any]],
        *,
        backend_id: str,
        authorized_payload: dict[str, Any] | None,
        authorized_checkpoint: ManagedWorkspaceCheckpoint | None = None,
    ) -> None:
        self._source = source
        self.backend_id = backend_id
        self.authorized_receipt = (
            None
            if authorized_payload is None
            else _json({"backend_id": backend_id, "provider_state": authorized_payload})
        )
        self._authorized_association = (
            None
            if authorized_checkpoint is None
            else ManagedWorkspaceCheckpoint.from_json(authorized_checkpoint.to_json()).association
        )
        self.capture_requested = False
        self.receipt: str | None = None
        self.capture_task: asyncio.Task[ManagedWorkspaceCheckpoint] | None = None

    @property
    def _sdk_workspace_instructions(self) -> str | None:
        return cast(str | None, getattr(self._source, _WORKSPACE_INSTRUCTIONS_ATTRIBUTE, None))

    @property
    def _sdk_sandbox_memory(self) -> _SandboxMemoryOwner | None:
        return cast(
            _SandboxMemoryOwner | None, getattr(self._source, _MEMORY_OWNER_ATTRIBUTE, None)
        )

    def validate_resume(self, checkpoint: ManagedWorkspaceCheckpoint) -> ManagedWorkspaceCheckpoint:
        # Association validation precedes resource acquisition and native restore.
        provenance = checkpoint._capture_provenance
        captured = ManagedWorkspaceCheckpoint.from_json(checkpoint.to_json())
        if self.authorized_receipt is None or captured.workspace_json != self.authorized_receipt:
            raise ValueError("Managed resume requires matching current authorized provider state")
        if self._authorized_association is not None:
            authorized = self._authorized_association == captured.association
        else:
            authorized = provenance == (
                captured.conversation_json,
                captured.workspace_json,
                captured.association,
            )
        if not authorized:
            raise ValueError(
                "Managed resume requires trusted authorization of the whole checkpoint"
            )
        return captured

    def publish(self, provider_state: dict[str, Any]) -> None:
        self.receipt = _json({"backend_id": self.backend_id, "provider_state": provider_state})

    async def __aenter__(self) -> Environment[Any]:
        return await self._source.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return await self._source.__aexit__(exc_type, exc, traceback)


ManagedWorkspaceCheckpoint.__module__ = "agents.sandbox"
