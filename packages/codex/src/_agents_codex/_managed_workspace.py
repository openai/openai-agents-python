"""Paired native conversation and provider workspace receipts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

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
    """Detached conversation/workspace pair; provider state confers no authority."""

    conversation_json: str
    workspace_json: str
    association: str

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


class _ManagedWorkspaceSource(AbstractAsyncContextManager["Environment[Any]"]):
    """Existing source lifetime plus one explicitly requested retirement receipt."""

    def __init__(
        self,
        source: AbstractAsyncContextManager[Environment[Any]],
        *,
        backend_id: str,
        authorized_payload: dict[str, Any] | None,
    ) -> None:
        self._source = source
        self.backend_id = backend_id
        self.authorized_receipt = (
            None
            if authorized_payload is None
            else _json({"backend_id": backend_id, "provider_state": authorized_payload})
        )
        self.capture_requested = False
        self.receipt: str | None = None
        self.capture_task: asyncio.Task[ManagedWorkspaceCheckpoint] | None = None

    def validate_resume(self, checkpoint: ManagedWorkspaceCheckpoint) -> None:
        # Association validation precedes resource acquisition and native restore.
        captured = ManagedWorkspaceCheckpoint.from_json(checkpoint.to_json())
        if self.authorized_receipt is None or captured.workspace_json != self.authorized_receipt:
            raise ValueError("Managed resume requires matching current authorized provider state")

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
