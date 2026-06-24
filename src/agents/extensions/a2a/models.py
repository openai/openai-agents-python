"""Wire models for the Agent-to-Agent (A2A) protocol.

These Pydantic models mirror the public A2A protocol
(https://a2aproject.github.io/A2A/) closely enough to interoperate with
peers from other frameworks. Field names are snake_case in Python and are
(de)serialized as camelCase on the wire via an alias generator, matching the
A2A JSON shape (``messageId``, ``contextId``, ``protocolVersion``, ...).

Always serialize with ``model_dump(by_alias=True, exclude_none=True)`` to emit
spec-compliant JSON.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

A2A_PROTOCOL_VERSION = "0.2.6"
"""The A2A protocol version advertised in the agent card."""


def _new_id() -> str:
    return uuid.uuid4().hex


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _A2AModel(BaseModel):
    """Base model that (de)serializes snake_case fields as camelCase.

    ``extra="allow"`` keeps unknown fields sent by other frameworks instead of
    rejecting them, which makes cross-framework interop forgiving.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
    )


# ---------------------------------------------------------------------------
# Message parts
# ---------------------------------------------------------------------------


class TextPart(_A2AModel):
    """A plain-text message part."""

    kind: Literal["text"] = "text"
    text: str
    metadata: dict[str, Any] | None = None


class DataPart(_A2AModel):
    """A structured-data message part."""

    kind: Literal["data"] = "data"
    data: dict[str, Any]
    metadata: dict[str, Any] | None = None


class FilePart(_A2AModel):
    """A file message part (carried by URI or inline bytes)."""

    kind: Literal["file"] = "file"
    file: dict[str, Any]
    metadata: dict[str, Any] | None = None


Part = Annotated[TextPart | FilePart | DataPart, Field(discriminator="kind")]
"""A single message part, discriminated by its ``kind``."""


# ---------------------------------------------------------------------------
# Messages, tasks, artifacts
# ---------------------------------------------------------------------------


class Message(_A2AModel):
    """A single turn exchanged between a user and an agent."""

    role: Literal["user", "agent"]
    parts: list[Part]
    message_id: str = Field(default_factory=_new_id)
    kind: Literal["message"] = "message"
    task_id: str | None = None
    context_id: str | None = None
    metadata: dict[str, Any] | None = None


class TaskState(str, Enum):
    """Lifecycle states of an A2A task."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    AUTH_REQUIRED = "auth-required"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class TaskStatus(_A2AModel):
    """The current status of a task."""

    state: TaskState
    message: Message | None = None
    timestamp: str = Field(default_factory=_utcnow_iso)


class Artifact(_A2AModel):
    """An output artifact produced by the agent while working a task."""

    artifact_id: str = Field(default_factory=_new_id)
    name: str | None = None
    parts: list[Part]
    metadata: dict[str, Any] | None = None


class Task(_A2AModel):
    """A unit of work tracked across its lifecycle."""

    id: str = Field(default_factory=_new_id)
    context_id: str = Field(default_factory=_new_id)
    status: TaskStatus
    kind: Literal["task"] = "task"
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Streaming update events
# ---------------------------------------------------------------------------


class TaskStatusUpdateEvent(_A2AModel):
    """Streaming event signalling a task status change."""

    task_id: str
    context_id: str
    status: TaskStatus
    kind: Literal["status-update"] = "status-update"
    final: bool = False
    metadata: dict[str, Any] | None = None


class TaskArtifactUpdateEvent(_A2AModel):
    """Streaming event carrying a (possibly partial) artifact."""

    task_id: str
    context_id: str
    artifact: Artifact
    kind: Literal["artifact-update"] = "artifact-update"
    append: bool = False
    last_chunk: bool = False
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Agent card
# ---------------------------------------------------------------------------


class AgentCapabilities(_A2AModel):
    """Optional capabilities advertised by an agent."""

    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False


class AgentProvider(_A2AModel):
    """The organization publishing an agent."""

    organization: str
    url: str | None = None


class AgentSkill(_A2AModel):
    """A discrete capability the agent exposes."""

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] | None = None
    input_modes: list[str] | None = None
    output_modes: list[str] | None = None


class AgentCard(_A2AModel):
    """The public description of an agent, served at the well-known path."""

    name: str
    description: str
    version: str
    url: str
    protocol_version: str = A2A_PROTOCOL_VERSION
    preferred_transport: str = "JSONRPC"
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    skills: list[AgentSkill] = Field(default_factory=list)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    provider: AgentProvider | None = None


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelopes
# ---------------------------------------------------------------------------


class MessageSendParams(_A2AModel):
    """Parameters for the ``message/send`` and ``message/stream`` methods."""

    message: Message
    metadata: dict[str, Any] | None = None


class JsonRpcRequest(_A2AModel):
    """A JSON-RPC 2.0 request envelope."""

    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    id: str | int | None = None
    params: dict[str, Any] | None = None


class JsonRpcError(_A2AModel):
    """A JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any | None = None


class JsonRpcSuccessResponse(_A2AModel):
    """A JSON-RPC 2.0 success response envelope."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: Any


class JsonRpcErrorResponse(_A2AModel):
    """A JSON-RPC 2.0 error response envelope."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    error: JsonRpcError


# Well-known JSON-RPC / A2A error codes.
JSON_RPC_PARSE_ERROR = -32700
JSON_RPC_INVALID_REQUEST = -32600
JSON_RPC_METHOD_NOT_FOUND = -32601
JSON_RPC_INVALID_PARAMS = -32602
JSON_RPC_INTERNAL_ERROR = -32603
A2A_TASK_NOT_FOUND = -32001


def text_from_message(message: Message) -> str:
    """Concatenate the text of every :class:`TextPart` in a message."""
    return "".join(part.text for part in message.parts if isinstance(part, TextPart))


def message_from_text(text: str, *, role: Literal["user", "agent"] = "agent") -> Message:
    """Build a single-text-part :class:`Message`."""
    return Message(role=role, parts=[TextPart(text=text)])
