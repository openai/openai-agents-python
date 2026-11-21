from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from ..exceptions import (
    UserError,
    _data_redacted_error_boundary,
    _mark_error_data_redacted,
)
from ..items import TResponseInputItem
from .session import await_session_mutation

if TYPE_CHECKING:
    from ..agent import Agent
    from ..run_state import ContextDeserializer, ContextOverride, RunState


@dataclass(frozen=True)
class _ResumableSessionBinding:
    """A live checkpoint reference; serialized state alone cannot grant this authority."""

    session: ResumableSession
    revision: str

    def __deepcopy__(self, memo: dict[int, Any]) -> _ResumableSessionBinding:
        return self


class ResumableSessionStore(Protocol):
    """Application-owned, trusted storage for one complete checkpoint per session.

    Scope the store to the authenticated tenant. Both operations must address the
    same durable record. Configure storage timeouts in the application. The SDK
    does not authenticate serialized state or coordinate external side effects.
    """

    async def load(self, session_id: str) -> str | None:
        """Return the entire record, or ``None`` for a missing or expired ID."""
        ...

    async def save(self, session_id: str, value: str, *, expected: str | None) -> bool:
        """Atomically replace the record only if it equals ``expected``.

        ``expected=None`` creates a record only if the ID does not exist. Return
        whether the replacement committed. Never replace a missing record when
        ``expected`` is a string. Do not retry an uncertain write automatically.
        """
        ...


ResumableSessionErrorReason = Literal[
    "missing",
    "malformed",
    "incompatible",
    "unavailable",
    "conflict",
    "active",
    "paused",
    "invalid_state",
]


class ResumableSessionError(UserError):
    """An explicit checkpoint failure without stored prompts or tool payloads."""

    def __init__(self, reason: ResumableSessionErrorReason) -> None:
        self.reason: ResumableSessionErrorReason = reason
        messages: dict[ResumableSessionErrorReason, str] = {
            "missing": "The resumable session is missing or expired.",
            "malformed": "The resumable session checkpoint is malformed.",
            "incompatible": "The resumable session checkpoint version is unsupported.",
            "unavailable": "The resumable session store could not confirm the checkpoint.",
            "conflict": "The resumable session changed; reload it before continuing.",
            "active": (
                "The resumable session has an unfinished attempt with an unknown outcome. "
                "Reconcile external effects before retrying."
            ),
            "paused": "The resumable session requires its saved approval state, not a new prompt.",
            "invalid_state": (
                "The run state is not attached to this session's current approval checkpoint. "
                "Reload it through session.load_state(agent)."
            ),
        }
        super().__init__(messages[reason])
        _mark_error_data_redacted(self)


@lru_cache(maxsize=1)
def _history_adapter() -> TypeAdapter[list[TResponseInputItem]]:
    return TypeAdapter(list[TResponseInputItem])


class CodexChecklistStep(BaseModel):
    """One entry in the native checklist, in the order supplied by Codex."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    step: str
    status: Literal["pending", "in_progress", "completed"]


class CodexChecklist(BaseModel):
    """The most recent native checklist and the ephemeral thread and turn that issued it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    thread_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    plan: tuple[CodexChecklistStep, ...]
    explanation: str | None = None


class _Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1]
    revision: str = Field(min_length=1)
    status: Literal["ready", "paused", "active"]
    history: list[Any]
    run_state: str | None
    codex_checklist: CodexChecklist | None = Field(default=None, exclude_if=lambda v: v is None)

    @field_validator("history")
    @classmethod
    def validate_history(cls, value: list[Any]) -> list[Any]:
        # Validate the supported union without its coercion or loss of provider metadata.
        _history_adapter().validate_python(value)
        json.dumps(value, allow_nan=False)
        return value

    @model_validator(mode="after")
    def validate_status(self) -> _Checkpoint:
        if (self.status == "ready" and self.run_state is not None) or (
            self.status == "paused" and not self.run_state
        ):
            raise ValueError("Checkpoint status and pending run state do not agree.")
        return self

    @field_validator("run_state")
    @classmethod
    def validate_run_state(cls, value: str | None) -> str | None:
        if value is not None:
            from ..run_state import SUPPORTED_SCHEMA_VERSIONS

            state = json.loads(value)
            if not isinstance(state, dict) or "$schemaVersion" not in state:
                raise ValueError("Checkpoint run state is malformed.")
            if state["$schemaVersion"] not in SUPPORTED_SCHEMA_VERSIONS:
                raise PydanticCustomError("checkpoint_version", "Unsupported RunState version.")
        return value


def _parse_checkpoint(value: str) -> _Checkpoint:
    reason: ResumableSessionErrorReason = "malformed"
    checkpoint = None
    try:
        checkpoint = _Checkpoint.model_validate_json(value)
    except ValidationError as error:
        if any(
            (item["loc"] == ("version",) and item["type"] == "literal_error")
            or item["type"] == "checkpoint_version"
            for item in error.errors(include_input=False, include_context=False)
        ):
            reason = "incompatible"
    except Exception:
        pass
    if checkpoint is None:
        raise ResumableSessionError(reason)
    return checkpoint


class ResumableSession:
    """Save a conversation and its pending tool approvals under one ID.

    Start with ``await ResumableSession.create(store)``. On another worker, use
    ``await ResumableSession.resume(store, session_id)`` to load the session.
    For a new message, call ``Runner.run(agent, message, session=session)``.
    If ``session.is_paused``, load the state with ``await session.load_state(agent)``.
    Apply the user's decision and call ``Runner.run(agent, state)``. The state
    remembers which session to update. The same calls work with ``Runner.run_streamed``.
    Wait for the run to finish before acting on its final output or interruptions.
    Raw model stream events describe work in progress.

    The store receives conversation history, tool arguments and results, and the
    serialized RunState for a pending approval. Supply trusted tenant-scoped storage,
    rebuild the agent and its tools, and allow one writer per session. Keep secrets
    out of model inputs, tool outputs, and serializable context.

    If resume reports an active attempt, check whether its worker is still running.
    After worker loss, inspect the last checkpoint with ``store.load(session_id)``
    and reconcile any external effects before starting a new session.
    """

    def __init__(
        self,
        store: ResumableSessionStore,
        session_id: str,
        *,
        _checkpoint: _Checkpoint,
        _serialized: str,
    ) -> None:
        self.session_id = session_id
        self._store = store
        self._checkpoint = _checkpoint
        self._serialized = _serialized

    @property
    def codex_checklist(self) -> CodexChecklist | None:
        """Read the latest accepted native plan in this idle checkpoint without inference.

        Use `CodexProvider(config_overrides=["tools.update_plan.enabled=true"])`
        with this session. After the run finishes or pauses for approval, read
        `session.codex_checklist`. To reopen, use
        `session = await ResumableSession.resume(store, session_id)` first.

        Each accepted update replaces the whole list. An empty plan clears it.
        Invalid tool arguments leave it unchanged. None means no saved plan.
        History-only sessions cannot store this snapshot. Nested agent tools
        do not update their parent session. Use the plan for display only. It
        cannot authorize tools or resume a native thread. Older SDK versions
        cannot open checkpoints that contain a checklist.
        """
        if self._checkpoint.status == "active":
            raise ResumableSessionError("active")
        return self._checkpoint.codex_checklist

    @property
    def is_paused(self) -> bool:
        """Whether this loaded checkpoint contains a pending approval run."""
        return self._checkpoint.status == "paused"

    @classmethod
    @_data_redacted_error_boundary
    async def create(cls, store: ResumableSessionStore) -> ResumableSession:
        """Create and confirm a new empty session with an SDK-generated ID."""
        checkpoint = _Checkpoint(
            version=1, revision=uuid4().hex, status="ready", history=[], run_state=None
        )
        session = cls(store, uuid4().hex, _checkpoint=checkpoint, _serialized="")
        await session._replace(checkpoint, expected=None)
        return session

    @classmethod
    @_data_redacted_error_boundary
    async def resume(cls, store: ResumableSessionStore, session_id: str) -> ResumableSession:
        """Load an existing checkpoint, rejecting missing, invalid, or active state."""
        value = await cls._load(store, session_id)
        if value is None:
            raise ResumableSessionError("missing")
        checkpoint = _parse_checkpoint(value)
        if checkpoint.status == "active":
            raise ResumableSessionError("active")
        return cls(store, session_id, _checkpoint=checkpoint, _serialized=value)

    @staticmethod
    async def _load(store: ResumableSessionStore, session_id: str) -> str | None:
        reason: ResumableSessionErrorReason | None = None
        value = None
        try:
            value = await store.load(session_id)
        except ResumableSessionError as error:
            reason = error.reason
        except Exception:
            reason = "unavailable"
        if reason is not None:
            raise ResumableSessionError(reason)
        return value

    @_data_redacted_error_boundary
    async def load_state(
        self,
        agent: Agent[Any],
        *,
        context_override: ContextOverride | None = None,
        context_deserializer: ContextDeserializer | None = None,
        strict_context: bool = False,
    ) -> RunState[Any, Any]:
        """Load the run waiting for approval, using this worker's agent and tools.

        Match the user's decision to a call in ``state.get_interruptions()``. Call
        ``state.approve(call)`` or ``state.reject(call)``, then ``Runner.run(agent, state)``.
        The runner saves the outcome in this session. If you also pass ``session=``,
        pass the session that loaded the state. On another worker, resume by session
        ID and call ``load_state`` again to restore the connection to the store.
        """
        from ..run_state import RunState

        checkpoint = self._checkpoint
        if checkpoint.status != "paused" or checkpoint.run_state is None:
            raise ResumableSessionError("invalid_state")
        failed = False
        state = None
        try:
            state = await RunState.from_string(
                agent,
                checkpoint.run_state,
                context_override=context_override,
                context_deserializer=context_deserializer,
                strict_context=strict_context,
            )
            if not state.get_interruptions():
                failed = True
        except Exception:
            failed = True
        if failed or state is None:
            raise ResumableSessionError("invalid_state")
        state._resumable_binding = _ResumableSessionBinding(self, checkpoint.revision)
        return state

    def _validate_run(self, input: Any) -> None:
        from ..run_state import RunState

        if self._checkpoint.status == "active":
            raise ResumableSessionError("active")
        if self._checkpoint.status == "ready":
            if isinstance(input, RunState):
                raise ResumableSessionError("invalid_state")
            return
        if not isinstance(input, RunState):
            raise ResumableSessionError("paused")
        binding = input._resumable_binding
        if (
            not isinstance(binding, _ResumableSessionBinding)
            or binding.session is not self
            or binding.revision != self._checkpoint.revision
        ):
            raise ResumableSessionError("invalid_state")

    def _get_history(self) -> list[TResponseInputItem]:
        return copy.deepcopy(self._checkpoint.history)

    async def _begin_run(self, input: Any) -> None:
        self._validate_run(input)
        active = self._checkpoint.model_copy(update={"status": "active", "revision": uuid4().hex})
        await self._replace(active, expected=self._serialized)

    async def _save_checkpoint(
        self,
        history: list[TResponseInputItem],
        state: RunState[Any, Any] | None,
        *,
        codex_checklist: CodexChecklist | None = None,
    ) -> _ResumableSessionBinding:
        if self._checkpoint.status != "active":
            raise ResumableSessionError("conflict")
        checkpoint = None
        try:
            checkpoint = _Checkpoint(
                version=1,
                revision=uuid4().hex,
                status="paused" if state is not None else "ready",
                history=copy.deepcopy(history),
                codex_checklist=(
                    codex_checklist
                    if codex_checklist is not None
                    else self._checkpoint.codex_checklist
                ),
                run_state=state.to_string() if state is not None else None,
            )
        except Exception:
            pass
        if checkpoint is None:
            raise ResumableSessionError("invalid_state")
        await self._replace(checkpoint, expected=self._serialized)
        return _ResumableSessionBinding(self, checkpoint.revision)

    async def _replace(self, checkpoint: _Checkpoint, *, expected: str | None) -> None:
        value = None
        try:
            value = json.dumps(checkpoint.model_dump(), allow_nan=False, separators=(",", ":"))
        except Exception:
            pass
        if value is None:
            raise ResumableSessionError("malformed")
        committed = None
        try:
            committed = await await_session_mutation(
                self._store.save(self.session_id, value, expected=expected)
            )
        except Exception:
            pass
        if committed is not True:
            # A retry after a lost acknowledgment can report a conflict with our own write.
            stored = await self._load(self._store, self.session_id)
            if stored != value:
                if committed is None:
                    raise ResumableSessionError("unavailable")
                if expected is not None and stored is None:
                    raise ResumableSessionError("missing")
                raise ResumableSessionError("conflict")
        self._checkpoint = checkpoint
        self._serialized = value
