from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from ..errors import ErrorCode, OpName

EventPhase = Literal["start", "finish"]


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class EventPayloadPolicy(BaseModel):
    """Controls how much potentially sensitive/large data is included in events."""

    # Exec output can be noisy and sensitive; default off.
    include_exec_output: bool = Field(default=False)

    # When enabled, bound output sizes.
    max_stdout_chars: int = Field(default=8_000, ge=0)
    max_stderr_chars: int = Field(default=8_000, ge=0)

    # For write events, we only include a best-effort byte count (never file bytes).
    include_write_len: bool = Field(default=True)


class SandboxSessionEventBase(BaseModel):
    """Shared fields for all sandbox audit events."""

    version: int = Field(default=1)

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    ts: datetime = Field(default_factory=_utcnow)

    session_id: uuid.UUID
    seq: int

    op: OpName
    phase: EventPhase

    # Correlates start/finish records for an operation.
    # When SDK tracing is active, this is the SDK span id for the operation.
    span_id: str
    parent_span_id: str | None = None
    trace_id: str | None = None

    # Operation-specific metadata (paths, argv, timings, etc.)
    data: dict[str, object] = Field(default_factory=dict)


class SandboxSessionStartEvent(SandboxSessionEventBase):
    """The start event for an operation."""

    phase: Literal["start"] = Field(default="start")


class SandboxSessionFinishEvent(SandboxSessionEventBase):
    """The finish event for an operation."""

    phase: Literal["finish"] = Field(default="finish")

    ok: bool
    duration_ms: float

    error_code: ErrorCode | None = None
    error_type: str | None = None
    error_message: str | None = None

    # Optional exec outputs (truncated / opt-in via policy).
    stdout: str | None = None
    stderr: str | None = None

    # Raw exec outputs (bytes) for per-sink/per-op policy application.
    # These are excluded from serialization (JSONL / HTTP) by default.
    stdout_bytes: bytes | None = Field(default=None, exclude=True)
    stderr_bytes: bytes | None = Field(default=None, exclude=True)


# Discriminated union keyed by `phase`.
SandboxSessionEvent = Annotated[
    SandboxSessionStartEvent | SandboxSessionFinishEvent,
    Field(discriminator="phase"),
]
_SANDBOX_SESSION_EVENT_ADAPTER: TypeAdapter[SandboxSessionEvent] = TypeAdapter(SandboxSessionEvent)


def validate_sandbox_session_event(obj: object) -> SandboxSessionEvent:
    """Parse an event payload (e.g. from JSON) into the correct phase-specific model."""

    return _SANDBOX_SESSION_EVENT_ADAPTER.validate_python(obj)
