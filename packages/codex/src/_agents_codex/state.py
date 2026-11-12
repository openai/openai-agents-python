"""Versioned native conversation checkpoints for application-owned storage."""

import importlib as importlib
import json as json
from dataclasses import dataclass as dataclass
from pathlib import Path as Path
from typing import Any as Any, Literal as Literal

from pydantic import (
    BaseModel as BaseModel,
    ConfigDict as ConfigDict,
    Field as Field,
    JsonValue,
    TypeAdapter as TypeAdapter,
    model_validator as model_validator,
)
from typing_extensions import NotRequired as NotRequired, TypedDict as TypedDict

from ._events import RequiredAction as RequiredAction, SessionItem as SessionItem
from ._public import preserve_module_identity as _preserve_module_identity
from .history import RecordedTurn as RecordedTurn
from .subagents import SubagentConfig as SubagentConfig
from .tools import ToolRef as ToolRef
from .usage import RunUsage as RunUsage, TokenUsage as TokenUsage


@dataclass(frozen=True)
class LocalStorage:
    """A trusted, application-owned native conversation directory.

    Pass to Agent.session(storage=...). Save Session.id and pass it as resume
    on a later open with this same profile. One native writer may own that
    conversation at a time. Current application tools, context and provider
    are required; no saved credential or Python task is restored.

    Code mode is rejected before opening because nested tool identities do not
    yet have durable intent and result receipts. Nonpersistent code mode remains
    available. Native local conversations cannot export portable SessionState. Missing
    receipts are unknown effects: reconcile them with your external system,
    never retry the tool automatically. This is not a checkpoint store.
    """

    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError("LocalStorage.path must be a pathlib.Path")
        object.__setattr__(self, "path", self.path.absolute())


@dataclass(frozen=True)
class CheckpointStatus:
    """Advice for a capture at the instant of inspection; it reserves no work."""

    status: Literal["ready", "not_ready", "unknown"]
    reasons: tuple[str, ...]


class ChecklistStep(BaseModel):
    """One retained step in native list order."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    step: str
    status: Literal["pending", "in_progress", "completed"]


class ChecklistState(BaseModel):
    """Accepted checklist data in a checkpoint, never an execution policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    thread_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    explanation: str | None = None
    plan: list[ChecklistStep]


class GoalState(BaseModel):
    """Retained goal data. Restoring it neither exposes goal tools nor resumes work."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    thread_id: str = Field(min_length=1)
    objective: str = Field(min_length=1, max_length=4000)
    status: Literal["paused", "complete"]
    token_budget: int | None = Field(default=None, gt=0, le=9223372036854775807)
    tokens_used: int = Field(default=0, ge=0, le=9223372036854775807)
    time_used_seconds: int = Field(default=0, ge=0, le=9223372036854775807)
    created_at: int | None = Field(default=None, ge=0, le=9223372036854775807)
    updated_at: int | None = Field(default=None, ge=0, le=9223372036854775807)


class NativeGoal(TypedDict):
    """An actual local Core goal, including Core-owned status and accounting.

    Keys and status values are the native goal wire fields. This inspection
    does not mutate or resume the goal.
    """

    threadId: str
    objective: str
    status: Literal["active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"]
    tokenBudget: NotRequired[int]
    tokensUsed: int
    timeUsedSeconds: int
    createdAt: int
    updatedAt: int


class CheckpointThread(BaseModel):
    """One descendant's persisted history and incoming spawn edge."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    thread_id: str
    parent_thread_id: str
    status: Literal["active", "closed"]
    closed_at: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any]
    history: list[dict[str, Any]]
    memory_mode: Literal["enabled", "disabled"] | None = None
    checklist: ChecklistState | None = None
    goal: GoalState | None = None
    code_mode: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_closed_at(self) -> "CheckpointThread":
        if self.status == "active" and self.closed_at is not None:
            raise ValueError("Active subagents cannot have a close timestamp")
        if self.checklist is not None and self.checklist.thread_id != self.thread_id:
            raise ValueError("Checkpoint checklist thread identity mismatch")
        if self.goal is not None and self.goal.thread_id != self.thread_id:
            raise ValueError("Checkpoint goal thread identity mismatch")
        return self


class CheckpointRecovery(TypedDict):
    """Work whose live execution or reply channels did not survive restoration."""

    interrupted_thread_ids: list[str]
    expired_actions: list[RequiredAction]


class _RootToolOrigin(BaseModel):
    """Native Code Mode origin for a nested root tool, never an effect receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    turn_id: str = Field(min_length=1)
    originating_item_id: str = Field(min_length=1)


class _StepMcpTool(BaseModel):
    """Transient native declaration identity, never a permission grant."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    server: str
    name: str


class _PendingManual(BaseModel):
    """Captured input only; restoration creates new native reply authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    call_id: str
    tool: str
    arguments: str
    approval_required: bool = False
    effective_arguments: str | None = None


class _ToolOutcomes(BaseModel):
    """Ordinary final sibling data; current native grants stay outside JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    response_observation_id: str = Field(min_length=1)
    call_ids: list[str]
    outcomes: list[tuple[str, bool]]
    held_call_ids: list[str] = Field(default_factory=list)


class _RequestRunBudgetCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: int = Field(ge=1, le=1)
    max_requests: int = Field(ge=0, le=2**53 - 1)
    admitted_requests: int = Field(ge=0, le=2**53 - 1)

    @model_validator(mode="after")
    def validate_admissions(self) -> "_RequestRunBudgetCheckpoint":
        if self.admitted_requests > self.max_requests:
            raise ValueError("Admitted requests cannot exceed the saved request limit")
        return self


class _ManualContinuation(BaseModel):
    """Native receipt order at a settled manual boundary, never live grants."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    turn_id: str
    seen_call_ids: list[str]
    turn_usage: TokenUsage
    reply_order: list[str]
    accepted_replies: dict[str, dict[str, Any]]
    pending_manual: list[_PendingManual]
    tool_outcomes: _ToolOutcomes | None = None
    output_schema: dict[str, Any] | None = None
    model_override: str | None = None


class ApplicationToolOutput(BaseModel):
    """Application data from one accepted native tool result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    submission_id: str
    turn_id: str
    call_id: str
    name: str
    namespace: str | None = None
    application_data: dict[str, JsonValue] = Field(repr=False)


class SessionState(BaseModel):
    """Conversation state, not live execution or a workspace backup.

    Versions 2 and 3 contain the original native conversation tree and
    optional settled manual continuation. Versions 4 and 5 add the SDK output
    contract to those idle and manual envelopes. A format 5 manual envelope
    may also contain an explicit operation subset. Its references must resolve
    against the destination's current opening declarations. No version
    contains live grants or acquires tools from saved names. Versions 6 and 7
    also retain native CodeMode values and terminal cell observations.
    Versions 8 and 9 have been adding request-only budget receipts to manual
    checkpoints without and with retained CodeMode state, respectively.

    Verify the checkpoint's integrity before restoring it. A strict Environment
    requires a matching saved executor policy digest. Local and remote
    Environments use strict write policy by default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    format_version: Literal[2, 3, 4, 5, 6, 7, 8, 9]
    codex_revision: str
    source_session_id: str
    history: list[dict[str, Any]]
    subagents: dict[str, SubagentConfig] = Field(default_factory=dict)
    root_metadata: dict[str, Any] | None = None
    root_memory_mode: Literal["enabled", "disabled"] | None = None
    root_checklist: ChecklistState | None = None
    root_goal: GoalState | None = None
    root_code_mode: dict[str, Any] | None = None
    descendants: list[CheckpointThread] = Field(default_factory=list)
    interrupted_thread_ids: list[str] = Field(default_factory=list)
    pending_actions: list[RequiredAction] = Field(default_factory=list)
    agent_options: dict[str, Any] = Field(default_factory=dict)
    recovery: CheckpointRecovery | None = None
    # Absent for startup history; [] is an intentionally empty managed catalog.
    managed_tool_specs: list[dict[str, Any]] | None = None
    root_tool_origins: dict[str, _RootToolOrigin] = Field(default_factory=dict)
    # None predates on_step; [] intentionally withholds every optional MCP tool.
    step_mcp_tools: list[_StepMcpTool] | None = None
    native_continuation: _ManualContinuation | None = None
    run_budget: _RequestRunBudgetCheckpoint | None = None
    run_usage: RunUsage | None = None
    # SDK contract identity is checked before importing native state or grants.
    output_version: Literal[1] | None = None
    output_fingerprint: str | None = None
    # Only a portable manual operation restriction. Resolve against the next
    # Session's actual opening declarations; this contains no binding or grant.
    operation_tools: list[ToolRef] | None = None
    # Passive receipts from separate tool-free correction sessions. They never
    # enter native history, model context, or continuation authority.
    repair_turns: list[RecordedTurn] = Field(default_factory=list)
    repair_items: list[SessionItem] = Field(default_factory=list)
    application_tool_outputs: list[ApplicationToolOutput] = Field(default_factory=list, repr=False)
    network_policy_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    executor_policy_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Additive SDK overlay. Older writable checkpoints omit it.
    executor_read_only: bool = False
    # Portable restores bind to the original Codex user and workspace.
    codex_login_binding: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_tree(self) -> "SessionState":
        if (self.format_version in (8, 9)) != (self.run_budget is not None):
            raise ValueError("Request budget requires a version 8 or 9 manual checkpoint")
        if (self.format_version in (3, 5, 7, 8, 9)) != (self.native_continuation is not None):
            raise ValueError("Settled manual continuation requires format_version 3, 5, 7, 8 or 9")
        if self.operation_tools is not None:
            if self.format_version not in (5, 7, 8, 9):
                raise ValueError("Operation tools require an SDK manual checkpoint")
            if len(set(self.operation_tools)) != len(self.operation_tools):
                raise ValueError("Operation tools must identify distinct declarations")
        if "_sdk_default_tools" in self.agent_options:
            # Persistent defaults belong to idle and manual SDK envelopes.
            if self.format_version not in (4, 5, 6, 7, 8, 9):
                raise ValueError("Saved defaults require an SDK checkpoint")
            defaults = TypeAdapter(list[ToolRef]).validate_json(
                json.dumps(self.agent_options["_sdk_default_tools"]), strict=True
            )
            if len(set(defaults)) != len(defaults):
                raise ValueError("Saved defaults must identify distinct declarations")
        if (self.output_version is None) != (self.output_fingerprint is None) or (
            self.format_version in (4, 5, 6, 7, 8, 9) and self.output_version is None
        ):
            raise ValueError("Checkpoint requires both output_version and output_fingerprint")
        if (
            self.root_code_mode is not None
            or any(child.code_mode is not None for child in self.descendants)
        ) and self.format_version not in (6, 7, 9):
            raise ValueError("Code-mode state requires a version 6, 7 or 9 checkpoint")
        if self.root_metadata is None:
            raise ValueError("Tree checkpoints require root metadata")
        if (
            self.root_checklist is not None
            and self.root_checklist.thread_id != self.source_session_id
        ):
            raise ValueError("Checkpoint checklist thread identity mismatch")
        if self.root_goal is not None and self.root_goal.thread_id != self.source_session_id:
            raise ValueError("Checkpoint goal thread identity mismatch")
        parents = {thread.thread_id: thread.parent_thread_id for thread in self.descendants}
        if len(parents) != len(self.descendants) or self.source_session_id in parents:
            raise ValueError("Checkpoint thread IDs must be unique")
        if set(self.interrupted_thread_ids) - {self.source_session_id, *parents}:
            raise ValueError("Interrupted threads must belong to the checkpoint tree")
        for thread_id in parents:
            seen = set()
            current = thread_id
            while current != self.source_session_id:
                if current in seen or current not in parents:
                    raise ValueError(
                        "Checkpoint descendants must form a tree rooted at the session"
                    )
                seen.add(current)
                current = parents[current]
        return self

    @property
    def instructions(self) -> str:
        """Read the latest accepted instructions from the native history.

        This checks the stored records, not the trustworthiness of their source.
        Use this value to restore instructions from storage your application
        trusts. Restoration still requires the current Agent to match them.
        """
        native = importlib.import_module("agents._native")
        if self.codex_revision != native.CODEX_REVISION:
            raise ValueError("Checkpoint belongs to an unsupported Codex revision")
        return str(native._read_session_instructions(json.dumps(self.history)))

    def to_json(self) -> str:
        """Serialize for storage; the payload may contain sensitive conversation data."""
        return self.model_dump_json(exclude_defaults=True)

    @classmethod
    def from_json(cls, value: str) -> "SessionState":
        """Validate a checkpoint read from application storage."""
        return cls.model_validate_json(value)


_preserve_module_identity(globals(), "agents.state")
