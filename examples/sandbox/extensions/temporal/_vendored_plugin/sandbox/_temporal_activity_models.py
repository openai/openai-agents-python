"""Pydantic models for Temporal sandbox activity arguments and results.

Using ``pydantic_data_converter`` on the Temporal client means these models are
serialized/deserialized automatically. Each activity receives a single typed
model instance rather than a positional arg list.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, SerializeAsAny, field_validator

from agents.sandbox import Manifest
from agents.sandbox.session.sandbox_client import BaseSandboxClientOptions
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import SnapshotBase, SnapshotSpecUnion
from agents.sandbox.types import User

# ---------------------------------------------------------------------------
# Shared base for all argument models that carry a session state field.
# ---------------------------------------------------------------------------


class _HasState(BaseModel):
    state: SerializeAsAny[SandboxSessionState]

    @field_validator("state", mode="before")
    @classmethod
    def _coerce_state(cls, value: object) -> SandboxSessionState:
        return SandboxSessionState.parse(value)


# ---------------------------------------------------------------------------
# Argument models (workflow -> activity)
# ---------------------------------------------------------------------------


class ExecArgs(_HasState):
    command: list[str]
    timeout: float | None = None
    shell: bool | list[str] = True
    user: str | User | None = None


class ReadArgs(_HasState):
    path: str


class WriteArgs(_HasState):
    path: str
    data: bytes


class RunningArgs(_HasState):
    pass


class PersistWorkspaceArgs(_HasState):
    pass


class HydrateWorkspaceArgs(_HasState):
    data: bytes


class PtyExecStartArgs(_HasState):
    command: list[str]
    timeout: float | None = None
    shell: bool | list[str] = True
    user: str | User | None = None
    tty: bool = False
    yield_time_s: float | None = None
    max_output_tokens: int | None = None


class PtyWriteStdinArgs(_HasState):
    session_id: int
    chars: str
    yield_time_s: float | None = None
    max_output_tokens: int | None = None


class StartArgs(_HasState):
    pass


class StopArgs(_HasState):
    pass


# ---------------------------------------------------------------------------
# Result models (activity -> workflow)
# ---------------------------------------------------------------------------


class ExecResult(BaseModel):
    stdout: bytes
    stderr: bytes
    exit_code: int


class PtyExecUpdateResult(BaseModel):
    process_id: int | None
    output: bytes
    exit_code: int | None
    original_token_count: int | None


class ReadResult(BaseModel):
    data: bytes


class RunningResult(BaseModel):
    is_running: bool


class PersistWorkspaceResult(BaseModel):
    data: bytes


class VoidResult(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Session lifecycle models (create / resume)
# ---------------------------------------------------------------------------


class CreateSessionArgs(BaseModel):
    snapshot_spec: SnapshotSpecUnion | SerializeAsAny[SnapshotBase] | None = None
    manifest: Manifest | None = None
    client_options: SerializeAsAny[BaseSandboxClientOptions] | None = None

    @field_validator("snapshot_spec", mode="before")
    @classmethod
    def _coerce_snapshot_spec(cls, value: object) -> SnapshotSpecUnion | SnapshotBase | None:
        if value is None or isinstance(value, SnapshotBase):
            return value
        # SnapshotBase subclasses always carry an `id` field;
        # SnapshotSpec subclasses do not.  Use that to distinguish
        # serialized SnapshotBase dicts from SnapshotSpecUnion dicts.
        if isinstance(value, dict) and "id" in value:
            return SnapshotBase.parse(value)
        return cast(SnapshotSpecUnion | None, value)

    @field_validator("client_options", mode="before")
    @classmethod
    def _coerce_client_options(cls, value: object) -> BaseSandboxClientOptions | None:
        if value is None:
            return None
        return BaseSandboxClientOptions.parse(value)


class ResumeSessionArgs(_HasState):
    pass


class SessionResult(_HasState):
    """Result of create/resume -- session state + capabilities."""

    supports_pty: bool
