"""Native Session ownership behind the official A2A server executor."""

from __future__ import annotations

import asyncio
import secrets
from abc import abstractmethod
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from a2a.server.agent_execution import (
    AgentExecutor,
    RequestContext,
)
from a2a.server.cluster.task_store import VersionedTaskStore
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.tasks import InMemoryTaskStore, TaskManager
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from pydantic import TypeAdapter

from .._compat import cancel_once
from .._function_tools import FunctionTool
from .._lifecycle import _close_after_error, _error_contexts, _join
from .._trace_context import TraceContext, _trace_context
from ..agent import Agent
from ..executor import EnvironmentSource
from ..manual import (
    ActionRequested,
    _CapturedManual,
    _PreparedManualResult,
)
from ..observation import ToolFinished, ToolStarted
from ..schema import NativeOutput, PromptedOutput
from ..session import (
    TurnStoppedError,
    _ManualBoundaryLost,
)
from ..state import SessionState
from ..turn import PendingRequests
from ._client import _DEFAULT_LIMITS, A2ALimits, _limits
from ._provider import _json


def _bytes(task: p.Task) -> bytes:
    return task.SerializeToString(deterministic=True)


def _part(value: dict[str, Any]) -> p.Part:
    data = ParseDict(value, Value())
    if MessageToDict(data) != value:
        raise ValueError("A2A data cannot represent this JSON value exactly")
    return p.Part(data=data)


@dataclass(frozen=True, kw_only=True)
class NativeA2ABinding:
    """Trusted identity supplied by authenticated official server context."""

    principal: str
    task_id: str
    context_id: str


@dataclass(frozen=True, kw_only=True, repr=False)
class NativeA2ATask:
    """Private host record. Its snapshot and checkpoint are not model input.

    Atomically commit each record and desired snapshot. generation protects private
    writes and identifies that snapshot independently of official TaskVersion.
    Keep the desired snapshot after completion so publication can be retried safely.
    """

    binding: NativeA2ABinding
    generation: int
    status: Literal["running", "waiting", "completed", "failed", "canceled", "unknown"]
    snapshot: bytes
    checkpoint: str | None = None
    challenge: str | None = None

    def task(self) -> p.Task:
        """Return a fresh mutable protocol snapshot, never the stored object."""
        return p.Task.FromString(self.snapshot)

    def acknowledges(self, task: p.Task) -> bool:
        """Only the exact saved snapshot acknowledges this publication generation."""
        return _bytes(task) == self.snapshot


class NativeA2ALease(Protocol):
    """Host-issued current execution ownership, including across worker processes."""

    async def load(self) -> NativeA2ATask | None: ...

    async def commit(self, record: NativeA2ATask, *, expected_generation: int | None) -> None:
        """Conditionally commit private state AND desired snapshot in one transaction."""
        ...


class NativeA2AStore(VersionedTaskStore):
    """Application-owned private state and official public TaskStore in one boundary.

    own must keep one exclusive execution owner until its context exits. Before
    replacing an owner, stop and join its worker and official event consumer.
    Every operation checks current principal, task/context binding and ownership.
    Official save must implement TaskVersion CAS, reject stale publication over a
    newer/terminal snapshot, and acknowledge the current private generation only
    AFTER saving a task for which record.acknowledges(task) is true. Intermediate
    status/artifact writes must not acknowledge a different desired snapshot.
    A lost acknowledgment retains the desired snapshot for idempotent recovery.
    Reject unrelated framework terminal/follow-up failure writes over a saved
    boundary. A rejected answer must retain its pending challenge. When official
    cancellation bypasses an inactive executor, save must atomically retire a
    waiting checkpoint/challenge and adopt the authenticated canceled snapshot.

    recover is called with the previous worker AND its official handler stopped.
    The application owns that coordination, recovery scheduling and retention.
    This interface supplies no database, identity system or distributed scheduler.
    """

    @abstractmethod
    def own(self, binding: NativeA2ABinding) -> AbstractAsyncContextManager[NativeA2ALease]: ...


@dataclass(frozen=True, kw_only=True, repr=False)
class NativeA2AAgent:
    """Current host configuration, re-evaluated on every execution and resume.

    Peer and saved metadata never supply authority. Session owns Environment
    acquisition, policy matching and cleanup, including current Environment
    callbacks. Portable checkpoints with active RunBudget or root prepare_request
    and before_step callbacks are unsupported; use Agent/Session directly for
    those profiles. Return a fresh managed context manager on each factory call.
    """

    agent: Agent[Any, Any]
    context: object = None
    parent_trace: TraceContext | Mapping[str, str | None] | None = None
    environment: EnvironmentSource[Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent_trace", _trace_context(self.parent_trace))


@dataclass
class _Live:
    done: asyncio.Event = field(default_factory=asyncio.Event)
    session: Any = None
    opening: asyncio.Task[Any] | None = None
    canceled: bool = False


class _Projection:
    """Reuse official aggregation to save the exact snapshot before live delivery."""

    def __init__(self, context: RequestContext) -> None:
        self.context = context
        self.store = InMemoryTaskStore()
        self.manager = TaskManager(
            self.store, context.call_context, context.task_id, context.context_id, context.message
        )

    async def open(self) -> None:
        task = p.Task()
        if self.context.current_task is not None:
            task.CopyFrom(self.context.current_task)
        else:
            task.id = self.context.task_id or ""
            task.context_id = self.context.context_id or ""
            task.status.state = p.TASK_STATE_SUBMITTED
        message = self.context.message
        if message is not None:
            matches = [old for old in task.history if old.message_id == message.message_id]
            if any(old != message for old in matches):
                raise InvalidParamsError(message="Conflicting reuse of an existing message ID")
            if not matches:
                self.manager.update_with_message(message, task)
        await self.store.save(task, self.context.call_context)

    async def apply(self, events: list[Any]) -> bytes:
        for event in events:
            await self.manager.process(type(event).FromString(event.SerializeToString()))
        task = await self.manager.get_task()
        if task is None:
            raise RuntimeError("A2A task projection is unavailable")
        return _bytes(task)

    async def preview(self, events: list[Any]) -> tuple[_Projection, bytes]:
        current = await self.manager.get_task()
        if current is None:
            raise RuntimeError("A2A task projection is unavailable")
        candidate = _Projection(self.context)
        return candidate, await candidate.apply([current, *events])


def _status(
    binding: NativeA2ABinding, state: p.TaskState, message: p.Message | None = None
) -> p.TaskStatusUpdateEvent:
    status = p.TaskStatus(state=state)
    if message is not None:
        status.message.CopyFrom(message)
    return p.TaskStatusUpdateEvent(
        task_id=binding.task_id, context_id=binding.context_id, status=status
    )


def _message(binding: NativeA2ABinding, parts: list[p.Part]) -> p.Message:
    return p.Message(
        message_id=secrets.token_hex(16),
        role=p.ROLE_AGENT,
        task_id=binding.task_id,
        context_id=binding.context_id,
        parts=parts,
    )


class NativeA2AExecutor(AgentExecutor):
    """One native Session per task; only settled single root manual calls persist."""

    def __init__(
        self,
        *,
        agent_factory: Callable[[NativeA2ABinding, ServerCallContext], NativeA2AAgent],
        store: NativeA2AStore,
        limits: A2ALimits | Mapping[str, Any] = _DEFAULT_LIMITS,
    ) -> None:
        self._factory, self.store, self.limits = agent_factory, store, _limits(limits)
        self._live: dict[NativeA2ABinding, _Live] = {}

    @staticmethod
    def _binding(context: RequestContext) -> NativeA2ABinding:
        user = context.call_context.user
        if (
            context.call_context.tenant
            or not user.is_authenticated
            or not user.user_name
            or not context.task_id
            or not context.context_id
        ):
            raise InvalidParamsError(message="Authenticated task/context binding required")
        return NativeA2ABinding(
            principal=user.user_name, task_id=context.task_id, context_id=context.context_id
        )

    @staticmethod
    async def _load(lease: NativeA2ALease, binding: NativeA2ABinding) -> NativeA2ATask | None:
        record = await lease.load()
        if record is None:
            return None
        if record.binding != binding:
            raise PermissionError("A2A private state does not match the current binding")
        task = record.task()
        if task.id != binding.task_id or task.context_id != binding.context_id:
            raise PermissionError("A2A saved snapshot does not match the current binding")
        return record

    def _reply(
        self, context: RequestContext, record: NativeA2ATask, configured: NativeA2AAgent
    ) -> tuple[SessionState, _PreparedManualResult]:
        parts = context.message.parts if context.message is not None else []
        if len(parts) != 1 or not parts[0].HasField("data"):
            raise InvalidParamsError(message="Expected one bounded challenge answer")
        value = MessageToDict(parts[0].data)
        prepared = None
        try:
            _json(value, self.limits.message_bytes)
            if record.checkpoint is None:
                raise ValueError("Waiting task has no checkpoint")
            state = SessionState.from_json(record.checkpoint)
            continuation = state.native_continuation
            if continuation is None or len(continuation.pending_manual) != 1:
                raise ValueError("Expected one settled root manual request")
            pending = continuation.pending_manual[0]
            if (
                isinstance(value, dict)
                and set(value) == {"challenge", "generation", "result"}
                and value["challenge"] == record.challenge
                and type(value["generation"]) in (int, float)
                and value["generation"] == record.generation
            ):
                tools = [
                    tool
                    for tool in configured.agent.tools
                    if isinstance(tool, FunctionTool) and tool.name == pending.tool
                ]
                if len(tools) == 1:
                    captured = tools[0]._capture(configured.context)
                    if isinstance(captured, _CapturedManual):
                        checked = captured.output_adapter.validate_python(value["result"])
                        prepared = _PreparedManualResult(
                            captured.declaration,
                            pending.call_id,
                            captured._encode_result(checked, strict=True),
                        )
        except Exception:
            pass
        if prepared is None:
            raise InvalidParamsError(message="Invalid, stale or unsupported manual answer")
        return state, prepared

    async def _commit(
        self,
        lease: NativeA2ALease,
        record: NativeA2ATask | None,
        binding: NativeA2ABinding,
        projection: _Projection,
        events: list[Any],
        queue: EventQueue,
        **changes: Any,
    ) -> NativeA2ATask:
        for event in events:
            if isinstance(event, p.TaskStatusUpdateEvent) and event.status.HasField("message"):
                try:
                    _json(MessageToDict(event.status.message), self.limits.message_bytes)
                except ValueError:
                    if event.status.state not in {p.TASK_STATE_FAILED, p.TASK_STATE_CANCELED}:
                        raise
                    event.status.ClearField("message")
        candidate, snapshot = await projection.preview(events)
        if (
            len(snapshot) > self.limits.checkpoint_bytes
            and changes.get("status") in {"failed", "canceled"}
            and events[-1].status.HasField("message")
        ):
            events[-1].status.ClearField("message")
            candidate, snapshot = await projection.preview(events)
        if len(snapshot) > self.limits.checkpoint_bytes:
            raise ValueError("A2A snapshot exceeds configured byte limit")
        next_record = (
            NativeA2ATask(
                binding=binding,
                generation=0,
                status="running",
                snapshot=b"",
            )
            if record is None
            else record
        )
        next_record = replace(
            next_record,
            generation=next_record.generation + 1,
            snapshot=snapshot,
            **changes,
        )
        await lease.commit(
            next_record, expected_generation=record.generation if record is not None else None
        )
        projection.store, projection.manager = candidate.store, candidate.manager
        for event in events:
            await queue.enqueue_event(event)
        return next_record

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        binding = self._binding(context)
        if binding in self._live:
            raise InvalidParamsError(message="Task execution already has a local owner")
        live = _Live()
        self._live[binding] = live
        try:
            async with self.store.own(binding) as lease:
                record = await self._load(lease, binding)
                if context.message is not None:
                    _json(MessageToDict(context.message), self.limits.message_bytes)
                state, reply = None, None
                if record is None:
                    if context.current_task is not None:
                        raise InvalidParamsError(message="Native task state unavailable")
                    prompt = context.get_user_input()
                    if (
                        not prompt.strip()
                        or context.message is None
                        or any(not part.HasField("text") for part in context.message.parts)
                    ):
                        raise InvalidParamsError(
                            message="Only bounded nonempty text input is supported"
                        )
                elif record.status == "waiting":
                    prompt = None
                else:
                    raise InvalidParamsError(
                        message="Observe or recover this task; execution cannot be replayed"
                    )
                if live.canceled:
                    projection = _Projection(context)
                    await projection.open()
                    events: list[Any] = []
                    if record is None:
                        initial = await projection.manager.get_task()
                        events.append(initial)
                    events.append(_status(binding, p.TASK_STATE_CANCELED))
                    await self._commit(
                        lease,
                        record,
                        binding,
                        projection,
                        events,
                        event_queue,
                        status="canceled",
                        checkpoint=None,
                        challenge=None,
                    )
                    return
                configured = None
                try:
                    configured = self._factory(binding, context.call_context)
                except Exception:
                    pass
                if configured is None:
                    raise InvalidParamsError(message="Current native configuration is unavailable")
                projection = _Projection(context)
                await projection.open()
                if record is not None:
                    state, reply = self._reply(context, record, configured)
                try:
                    events = []
                    if record is None:
                        initial = await projection.manager.get_task()
                        events.append(initial)
                    events.append(_status(binding, p.TASK_STATE_WORKING))
                    record = await self._commit(
                        lease,
                        record,
                        binding,
                        projection,
                        events,
                        event_queue,
                        status="running",
                    )
                    # Keep Session entry, body and exit in one owned task. Caller
                    # cancellation joins it without consuming the caller's error.
                    work = asyncio.create_task(
                        self._run(
                            configured,
                            binding,
                            state,
                            reply,
                            prompt,
                            live,
                            lease,
                            record,
                            projection,
                            event_queue,
                        )
                    )
                    try:
                        record = await asyncio.shield(work)
                    except BaseException as error:
                        cancel_once(work)
                        await _close_after_error(error, lambda: _join(work))
                        raise
                except Exception:
                    # Never erase a committed waiting/completed publication on enqueue failure.
                    latest = await self._load(lease, binding)
                    if latest is None:
                        raise
                    if latest.status == "running":
                        await self._commit(
                            lease,
                            latest,
                            binding,
                            projection,
                            [
                                _status(
                                    binding,
                                    p.TASK_STATE_FAILED,
                                    _message(
                                        binding,
                                        [
                                            p.Part(
                                                text="Native task failed; effects need host review."
                                            )
                                        ],
                                    ),
                                )
                            ],
                            event_queue,
                            status="failed",
                            checkpoint=None,
                            challenge=None,
                        )
        finally:
            # Cancellation may reacquire the lease only after async owner exit.
            live.done.set()
            if self._live.get(binding) is live:
                self._live.pop(binding)

    async def _run(
        self,
        configured: NativeA2AAgent,
        binding: NativeA2ABinding,
        state: SessionState | None,
        reply: _PreparedManualResult | None,
        prompt: str | None,
        live: _Live,
        lease: NativeA2ALease,
        record: NativeA2ATask,
        projection: _Projection,
        queue: EventQueue,
    ) -> NativeA2ATask:
        output_type = configured.agent.output_type
        result, captured = None, None
        if not live.canceled:
            options: dict[str, Any] = {
                "context": configured.context,
                "state": state,
                "environment": configured.environment,
            }
            if configured.parent_trace is not None:
                options["parent_trace"] = configured.parent_trace
            live.opening = asyncio.current_task()
            try:
                async with configured.agent.session(**options) as session:
                    live.opening = None
                    live.session = session
                    try:
                        if not live.canceled:
                            turn = (
                                session.restored_turn
                                if state is not None
                                else await session.submit(prompt)
                            )
                            if turn is None:
                                raise ValueError("Expected pending native root turn")
                            if state is not None:
                                requests = await session.pending_interactions()
                                request = next(
                                    (
                                        request
                                        for request in requests
                                        if isinstance(request, ActionRequested)
                                        and request.subagent_id is None
                                        and reply is not None
                                        and request.name == reply.declaration.name
                                        and request.call_id == reply.call_id
                                    ),
                                    None,
                                )
                                if request is None or reply is None:
                                    raise ValueError(
                                        "Checkpoint does not match the current manual declaration"
                                    )
                                if not live.canceled:
                                    await session._respond(request, prepared=reply)
                            if not live.canceled:
                                progress = asyncio.create_task(
                                    self._progress(turn, binding, projection, queue)
                                )
                                try:
                                    outcome = await turn.wait_for_request_or_result()
                                    if isinstance(outcome, PendingRequests):
                                        if (
                                            len(outcome.requests) != 1
                                            or not isinstance(outcome.requests[0], ActionRequested)
                                            or outcome.requests[0].subagent_id is not None
                                        ):
                                            raise ValueError(
                                                "Only one settled root manual request is supported"
                                            )
                                        request = outcome.requests[0]
                                        arguments = (
                                            request.arguments.model_dump(mode="json")
                                            if hasattr(request.arguments, "model_dump")
                                            else request.arguments
                                        )
                                        _json(arguments, self.limits.message_bytes)
                                        if not live.canceled:

                                            def before_seal() -> None:
                                                live.session = None

                                            suspended_state = await session._suspend(
                                                before_seal=before_seal
                                            )
                                            continuation = suspended_state.native_continuation
                                            if (
                                                continuation is None
                                                or len(continuation.pending_manual) != 1
                                                or continuation.pending_manual[0].call_id
                                                != request.call_id
                                                or continuation.pending_manual[0].tool
                                                != request.name
                                            ):
                                                raise ValueError(
                                                    "Only one settled root manual "
                                                    "request is supported"
                                                )
                                            checkpoint = suspended_state.to_json()
                                            if (
                                                len(checkpoint.encode())
                                                > self.limits.checkpoint_bytes
                                            ):
                                                raise ValueError(
                                                    "Native checkpoint exceeds "
                                                    "configured byte limit"
                                                )
                                            captured = (
                                                checkpoint,
                                                request.name,
                                                arguments,
                                            )
                                    else:
                                        result = await turn.get_result()
                                except (TurnStoppedError, _ManualBoundaryLost):
                                    if not live.canceled:
                                        raise
                                finally:
                                    progress.cancel()
                                    await asyncio.gather(progress, return_exceptions=True)
                    finally:
                        live.session = None
            except asyncio.CancelledError as error:
                if not live.canceled:
                    raise
                # Entry rollback retains cleanup failures under cancellation.
                # An explicit CancelTask cannot turn those failures into success.
                for cleanup_error in _error_contexts(error.__context__):
                    if not isinstance(cleanup_error, asyncio.CancelledError):
                        raise cleanup_error from None
            finally:
                live.opening = None
        if live.canceled:
            return await self._commit(
                lease,
                record,
                binding,
                projection,
                [
                    _status(
                        binding,
                        p.TASK_STATE_CANCELED,
                        _message(
                            binding,
                            [p.Part(text="Native work stopped; effects were not rolled back.")],
                        ),
                    )
                ],
                queue,
                status="canceled",
                checkpoint=None,
                challenge=None,
            )
        if captured is not None:
            checkpoint, tool, arguments = captured
            generation = record.generation + 1
            challenge = secrets.token_urlsafe(32)
            proposal = {
                "challenge": challenge,
                "generation": generation,
                "tool": tool,
                "arguments": arguments,
            }
            return await self._commit(
                lease,
                record,
                binding,
                projection,
                [
                    _status(
                        binding, p.TASK_STATE_INPUT_REQUIRED, _message(binding, [_part(proposal)])
                    )
                ],
                queue,
                status="waiting",
                checkpoint=checkpoint,
                challenge=challenge,
            )
        if result is None:
            raise RuntimeError("Native task has no settled result")
        output = result.final_output
        if isinstance(output_type, (NativeOutput, PromptedOutput)):
            output_type = output_type.type_
        if output_type is not None and output_type is not str:
            output = TypeAdapter(output_type).dump_python(output, mode="json", warnings="error")
        _json(output, self.limits.message_bytes)
        parts = [p.Part(text=output)] if isinstance(output, str) else [_part({"result": output})]
        artifact = p.TaskArtifactUpdateEvent(
            task_id=binding.task_id,
            context_id=binding.context_id,
            artifact=p.Artifact(
                artifact_id=binding.task_id + ":final", name="Validated result", parts=parts
            ),
            last_chunk=True,
        )
        return await self._commit(
            lease,
            record,
            binding,
            projection,
            [artifact, _status(binding, p.TASK_STATE_COMPLETED)],
            queue,
            status="completed",
            checkpoint=None,
            challenge=None,
        )

    async def _progress(
        self, turn: Any, binding: NativeA2ABinding, projection: _Projection, queue: EventQueue
    ) -> None:
        count = 0
        async for event in turn.events():
            if isinstance(event, (ToolStarted, ToolFinished)):
                # Progress has no status message/artifact and cannot acknowledge a
                # durable boundary. Do not confuse tool finish with final output.
                update = _status(binding, p.TASK_STATE_WORKING)
                ParseDict(
                    {
                        "progress": "tool_finished"
                        if isinstance(event, ToolFinished)
                        else "tool_started"
                    },
                    update.metadata,
                )
                candidate, snapshot = await projection.preview([update])
                if len(snapshot) > self.limits.checkpoint_bytes:
                    return
                projection.store, projection.manager = candidate.store, candidate.manager
                await queue.enqueue_event(update)
                count += 1
                if count >= self.limits.progress_events:
                    return

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        binding = self._binding(context)
        live = self._live.get(binding)
        if live is not None:
            live.canceled = True
            try:
                opening = live.opening
                if opening is not None:
                    cancel_once(opening)
                session = live.session
                # Checkpoint ownership joins sealing; its preceding drain remains interruptible.
                if session is not None and session._checkpoint_task is None:
                    await session.cancel_all()
            finally:
                await live.done.wait()
        async with self.store.own(binding) as lease:
            record = await self._load(lease, binding)
            if record is None or record.status in {"completed", "failed", "unknown"}:
                raise InvalidParamsError(message="Task cannot confirm cancellation")
            if record.status == "canceled":
                return
            projection = _Projection(context)
            await projection.open()
            await self._commit(
                lease,
                record,
                binding,
                projection,
                [_status(binding, p.TASK_STATE_CANCELED)],
                event_queue,
                status="canceled",
                checkpoint=None,
                challenge=None,
            )

    async def recover(
        self, binding: NativeA2ABinding, context: ServerCallContext
    ) -> NativeA2ATask | None:
        """Republish a saved boundary after the old worker and handler have stopped.

        Running work remains unknown: this never calls the Agent factory.
        A save with a lost acknowledgment may be repeated under the same desired
        generation. The host's inherited save owns exact snapshot acknowledgment.
        """
        if (
            context.tenant
            or not context.user.is_authenticated
            or context.user.user_name != binding.principal
        ):
            raise PermissionError("A2A recovery requires the current task owner")
        async with self.store.own(binding) as lease:
            record = await self._load(lease, binding)
            if record is None:
                return None
            task = record.task()
            if record.status == "running" or (
                record.status == "unknown"
                and (
                    task.status.state != p.TASK_STATE_FAILED
                    or "agents.a2a.outcome" not in task.metadata
                    or task.metadata["agents.a2a.outcome"] != "unknown"
                )
            ):
                task.status.CopyFrom(p.TaskStatus(state=p.TASK_STATE_FAILED))
                task.metadata.update({"agents.a2a.outcome": "unknown"})
                snapshot = _bytes(task)
                if len(snapshot) > self.limits.checkpoint_bytes:
                    raise ValueError("A2A snapshot exceeds configured byte limit")
                record = replace(
                    record,
                    generation=record.generation + 1,
                    status="unknown",
                    snapshot=snapshot,
                    checkpoint=None,
                    challenge=None,
                )
                await lease.commit(record, expected_generation=record.generation - 1)
            current = await self.store.get(binding.task_id, context)
            from a2a.server.cluster.version import TaskVersion

            await self.store.save(
                record.task(),
                event=record.task(),
                prev=current.task if current else None,
                prev_version=current.version if current else TaskVersion.MISSING,
                context=context,
            )
            return record
