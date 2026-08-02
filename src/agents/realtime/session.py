from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Sequence
from functools import partial
from typing import Any, cast

from pydantic import BaseModel
from typing_extensions import assert_never

from .. import _debug
from .._tool_identity import (
    FunctionToolLookupKey,
    get_function_tool_lookup_key_for_tool,
    get_function_tool_namespace,
)
from ..agent import Agent
from ..exceptions import ToolInputGuardrailTripwireTriggered, UserError
from ..handoffs import Handoff
from ..items import ToolApprovalItem
from ..logger import (
    log_model_action_error,
    log_model_and_tool_action_warning,
    log_tool_action_error,
    logger,
)
from ..run_config import ToolErrorFormatterArgs
from ..run_context import RunContextWrapper, TContext
from ..tool import DEFAULT_APPROVAL_REJECTION_MESSAGE, FunctionTool, Tool, invoke_function_tool
from ..tool_context import ToolContext
from ..tool_guardrails import ToolInputGuardrailData
from ..util._approvals import evaluate_needs_approval_setting, parse_function_tool_arguments
from ..util._asyncio_tasks import gather_with_cancel
from ._tool_filtering import filter_enabled_tools
from ._tool_validation import validate_realtime_tool_names
from .agent import RealtimeAgent
from .config import RealtimeRunConfig, RealtimeSessionModelSettings, RealtimeUserInput
from .events import (
    RealtimeAgentEndEvent,
    RealtimeAgentStartEvent,
    RealtimeAudio,
    RealtimeAudioEnd,
    RealtimeAudioInterrupted,
    RealtimeError,
    RealtimeEventInfo,
    RealtimeGuardrailTripped,
    RealtimeHandoffEvent,
    RealtimeHistoryAdded,
    RealtimeHistoryUpdated,
    RealtimeInputAudioTimeoutTriggered,
    RealtimeRawModelEvent,
    RealtimeSessionEvent,
    RealtimeToolApprovalRequired,
    RealtimeToolEnd,
    RealtimeToolStart,
)
from .handoffs import collect_enabled_handoffs, filter_enabled_handoffs
from .items import (
    AssistantAudio,
    AssistantMessageItem,
    AssistantText,
    InputAudio,
    InputImage,
    InputText,
    RealtimeItem,
    UserMessageItem,
)
from .model import RealtimeModel, RealtimeModelConfig, RealtimeModelListener
from .model_events import (
    RealtimeModelEvent,
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelOutputTextDeltaEvent,
    RealtimeModelToolCallEvent,
    RealtimeModelUsageEvent,
)
from .model_inputs import (
    RealtimeModelSendAudio,
    RealtimeModelSendInterrupt,
    RealtimeModelSendSessionUpdate,
    RealtimeModelSendToolOutput,
    RealtimeModelSendUserInput,
)

REJECTION_MESSAGE = DEFAULT_APPROVAL_REJECTION_MESSAGE


class _RealtimeSessionClosedSentinel:
    pass


_REALTIME_SESSION_CLOSED_SENTINEL = _RealtimeSessionClosedSentinel()
_BACKGROUND_TASK_CANCEL_GRACE_SECONDS = 1.0


def _guardrail_diagnostic_extra(guardrail: Any) -> dict[str, object]:
    try:
        return {"guardrail_name": guardrail.get_name()}
    except Exception:
        try:
            guardrail_type = type(guardrail.guardrail_function)
            type_name = f"{guardrail_type.__module__}.{guardrail_type.__qualname__}"
        except Exception:
            type_name = "unknown"
        return {"guardrail_type": type_name}


def _serialize_tool_output(output: Any) -> str:
    """Serialize structured tool outputs to JSON when possible."""
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        try:
            output = output.model_dump(mode="json")
        except Exception:
            try:
                output = output.model_dump()
            except Exception:
                return str(output)
    elif dataclasses.is_dataclass(output) and not isinstance(output, type):
        try:
            output = dataclasses.asdict(output)
        except Exception:
            return str(output)
    try:
        return json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(output)


@dataclasses.dataclass
class _PendingToolOutput:
    tool_call: RealtimeModelToolCallEvent
    output: str
    start_response: bool
    tool_end_event: RealtimeToolEnd | None = None
    session_update: RealtimeModelSendSessionUpdate | None = None
    completion_task: asyncio.Task[None] | None = None
    response_request_id: str | None = None


@dataclasses.dataclass(frozen=True)
class _RealtimeDispatchSnapshot:
    agent: RealtimeAgent[Any]
    tools: tuple[Tool, ...]
    handoffs: tuple[Handoff[Any, RealtimeAgent[Any]], ...]


@dataclasses.dataclass
class _PendingToolCall:
    tool_call: RealtimeModelToolCallEvent
    agent: RealtimeAgent[Any]
    dispatch_snapshot: _RealtimeDispatchSnapshot
    function_tool: FunctionTool
    approval_item: ToolApprovalItem


class _PendingToolOutputSendError(RuntimeError):
    def __init__(self, call_id: str, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.call_id = call_id


class _GuardrailRecoveryAttemptError(RuntimeError):
    def __init__(self, interrupt_error: Exception, recovery_error: Exception) -> None:
        self.interrupt_error = interrupt_error
        self.recovery_error = recovery_error
        super().__init__(
            "Guardrail interrupt and recovery request both failed: "
            f"interrupt={interrupt_error!r}; recovery={recovery_error!r}"
        )


class RealtimeSession(RealtimeModelListener):
    """A connection to a realtime model. It streams events from the model to you, and allows you to
    send messages and audio to the model.

    Example:
        ```python
        runner = RealtimeRunner(agent)
        async with await runner.run() as session:
            # Send messages
            await session.send_message("Hello")
            await session.send_audio(audio_bytes)

            # Stream events
            async for event in session:
                if event.type == "audio":
                    # Handle audio event
                    pass
        ```
    """

    def __init__(
        self,
        model: RealtimeModel,
        agent: RealtimeAgent,
        context: TContext | None,
        model_config: RealtimeModelConfig | None = None,
        run_config: RealtimeRunConfig | None = None,
    ) -> None:
        """Initialize the session.

        Args:
            model: The model to use.
            agent: The current agent.
            context: The context object.
            model_config: Model configuration.
            run_config: Runtime configuration including guardrails.
        """
        self._model = model
        self._current_agent = agent
        self._context_wrapper = RunContextWrapper(context)
        self._event_info = RealtimeEventInfo(context=self._context_wrapper)
        self._history: list[RealtimeItem] = []
        self._model_config = model_config or {}
        self._run_config = run_config or {}
        initial_model_settings = self._model_config.get("initial_model_settings")
        run_config_settings = self._run_config.get("model_settings")
        self._base_model_settings: RealtimeSessionModelSettings = {
            **(run_config_settings or {}),
            **(initial_model_settings or {}),
        }
        self._event_queue: asyncio.Queue[RealtimeSessionEvent | _RealtimeSessionClosedSentinel] = (
            asyncio.Queue()
        )
        self._event_iterator_waiters = 0
        self._closing = False
        self._closed = False
        self._cleanup_task: asyncio.Task[None] | None = None
        self._stored_exception: BaseException | None = None
        self._pending_tool_calls: dict[str, _PendingToolCall] = {}
        self._active_tool_call_ids: set[str] = set()
        self._completed_tool_call_ids: set[str] = set()
        self._pending_tool_outputs: dict[str, _PendingToolOutput] = {}
        self._current_dispatch_snapshot: _RealtimeDispatchSnapshot | None = None

        # Guardrails state tracking
        self._interrupted_response_ids: set[str] = set()
        self._active_output_response_id: str | None = None
        self._response_agent_snapshots: dict[str, RealtimeAgent] = {}
        self._unscoped_response_agent_snapshot: RealtimeAgent | None = None
        self._item_transcripts: dict[str, str] = {}  # item_id -> accumulated transcript
        self._item_guardrail_run_counts: dict[str, int] = {}  # item_id -> run count
        self._pending_guardrail_recovery_ids: set[str] = set()
        self._pending_guardrail_recovery_order: deque[str] = deque()
        self._pending_response_request_order: deque[str] = deque()
        self._ordinary_response_request_counter = 0
        self._active_guardrail_recovery_response_ids: set[str] = set()
        self._legacy_guardrail_recovery_turn_active = False
        self._legacy_guardrail_recovery_response_id: str | None = None
        self._guardrail_recovery_request_counter = 0
        self._debounce_text_length = self._run_config.get("guardrails_settings", {}).get(
            "debounce_text_length", 100
        )

        self._guardrail_tasks: set[asyncio.Task[Any]] = set()
        self._guardrail_tasks_by_response_id: dict[str, set[asyncio.Task[Any]]] = {}
        self._responses_awaiting_guardrail_cleanup: set[str] = set()
        self._tool_call_tasks: set[asyncio.Task[Any]] = set()
        self._async_tool_calls: bool = bool(self._run_config.get("async_tool_calls", True))

    @property
    def model(self) -> RealtimeModel:
        """Access the underlying model for adding listeners or other direct interaction."""
        return self._model

    async def __aenter__(self) -> RealtimeSession:
        """Start the session by connecting to the model. After this, you will be able to stream
        events from the model and send messages and audio to the model.
        """
        model_config = self._model_config.copy()
        initial_model_settings = await self._get_updated_model_settings_from_agent(
            starting_settings=self._model_config.get("initial_model_settings", None),
            agent=self._current_agent,
        )
        model_config["initial_model_settings"] = initial_model_settings
        self._current_dispatch_snapshot = self._dispatch_snapshot_from_settings(
            self._current_agent,
            initial_model_settings,
        )

        # Add ourselves as a listener only after initial settings have been validated.
        self._model.add_listener(self)

        try:
            # Connect to the model.
            await self._model.connect(model_config)
        except BaseException:
            self._model.remove_listener(self)
            raise

        # Emit initial history update
        await self._put_event(
            RealtimeHistoryUpdated(
                history=self._history,
                info=self._event_info,
            )
        )

        return self

    async def enter(self) -> RealtimeSession:
        """Enter the async context manager. We strongly recommend using the async context manager
        pattern instead of this method. If you use this, you need to manually call `close()` when
        you are done.
        """
        return await self.__aenter__()

    async def __aexit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """End the session."""
        await self.close()

    async def __aiter__(self) -> AsyncIterator[RealtimeSessionEvent]:
        """Iterate over events from the session."""
        while True:
            if self._closed and self._event_queue.empty():
                return

            # Check if there's a stored exception to raise
            if self._stored_exception is not None:
                # Clean up resources before raising
                await self.close()
                raise self._stored_exception

            self._event_iterator_waiters += 1
            try:
                event = await self._event_queue.get()
            finally:
                self._event_iterator_waiters -= 1
            if event is _REALTIME_SESSION_CLOSED_SENTINEL:
                return
            yield cast(RealtimeSessionEvent, event)

    async def close(self) -> None:
        """Close the session."""
        if self._closed:
            self._wake_event_iterators()
            return

        cleanup_task = self._cleanup_task
        current_task = asyncio.current_task()
        if cleanup_task is not None and (
            current_task in self._guardrail_tasks or current_task in self._tool_call_tasks
        ):
            # Cleanup is already waiting for this tracked task, so waiting here would form a cycle.
            raise asyncio.CancelledError

        if cleanup_task is None:
            self._closing = True
            cleanup_task = asyncio.create_task(
                self._cleanup(),
                name="agents-realtime-session-cleanup",
            )
            self._cleanup_task = cleanup_task
            cleanup_task.add_done_callback(self._on_cleanup_task_done)

        await asyncio.shield(cleanup_task)

    async def send_message(self, message: RealtimeUserInput) -> None:
        """Send a message to the model."""
        await self._send_response_request(RealtimeModelSendUserInput(user_input=message))

    async def _send_response_request(
        self,
        event: RealtimeModelSendUserInput | RealtimeModelSendToolOutput,
        *,
        pending_tool_output: _PendingToolOutput | None = None,
    ) -> Awaitable[None] | None:
        response_create_id = (
            event.response_create_id if isinstance(event, RealtimeModelSendUserInput) else None
        )
        if response_create_id is None:
            self._ordinary_response_request_counter += 1
            request_id = f"ordinary_{self._ordinary_response_request_counter}"
        else:
            request_id = response_create_id
        self._pending_response_request_order.append(request_id)
        if pending_tool_output is not None:
            pending_tool_output.response_request_id = request_id
        try:
            if isinstance(event, RealtimeModelSendToolOutput):
                return await self._model._send_tool_output_with_completion(event)
            await self._model.send_event(event)
        except BaseException:
            self._discard_response_request(request_id)
            if pending_tool_output is not None:
                pending_tool_output.response_request_id = None
            raise
        return None

    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None:
        """Send a raw audio chunk to the model."""
        await self._model.send_event(RealtimeModelSendAudio(audio=audio, commit=commit))

    async def interrupt(self) -> None:
        """Interrupt the model."""
        await self._model.send_event(RealtimeModelSendInterrupt())

    async def update_agent(self, agent: RealtimeAgent) -> None:
        """Update the active agent for this session and apply its settings to the model."""
        updated_settings = await self._get_updated_model_settings_from_agent(
            starting_settings=None,
            agent=agent,
        )
        updated_snapshot = self._dispatch_snapshot_from_settings(agent, updated_settings)

        self._current_agent = agent
        self._current_dispatch_snapshot = updated_snapshot

        await self._model.send_event(
            RealtimeModelSendSessionUpdate(session_settings=updated_settings)
        )

    async def on_event(self, event: RealtimeModelEvent) -> None:
        if self._closing or self._closed:
            return

        if not await self._put_event(RealtimeRawModelEvent(data=event, info=self._event_info)):
            return
        if self._closing or self._closed:
            return

        if event.type == "error":
            self._discard_failed_guardrail_recovery(
                event.response_create_id,
                event.is_guardrail_recovery,
            )
            await self._put_event(RealtimeError(info=self._event_info, error=event.error))
        elif event.type == "function_call":
            agent_snapshot = self._current_agent
            dispatch_snapshot = self._current_dispatch_snapshot
            if dispatch_snapshot is not None and dispatch_snapshot.agent is not agent_snapshot:
                dispatch_snapshot = None
            if self._async_tool_calls:
                self._enqueue_tool_call_task(event, agent_snapshot, dispatch_snapshot)
            else:
                handle_kwargs: dict[str, Any] = {"agent_snapshot": agent_snapshot}
                if dispatch_snapshot is not None:
                    handle_kwargs["dispatch_snapshot"] = dispatch_snapshot
                await self._handle_tool_call(event, **handle_kwargs)
        elif event.type == "audio":
            if event.response_id not in self._interrupted_response_ids:
                await self._put_event(
                    RealtimeAudio(
                        info=self._event_info,
                        audio=event,
                        item_id=event.item_id,
                        content_index=event.content_index,
                    )
                )
        elif event.type == "audio_interrupted":
            await self._put_event(
                RealtimeAudioInterrupted(
                    info=self._event_info, item_id=event.item_id, content_index=event.content_index
                )
            )
        elif event.type == "audio_done":
            await self._put_event(
                RealtimeAudioEnd(
                    info=self._event_info, item_id=event.item_id, content_index=event.content_index
                )
            )
        elif event.type == "input_audio_transcription_completed":
            prev_len = len(self._history)
            self._history = RealtimeSession._get_new_history(self._history, event)
            # If a new user item was appended (no existing item),
            # emit history_added for incremental UIs.
            if len(self._history) > prev_len and len(self._history) > 0:
                new_item = self._history[-1]
                await self._put_event(RealtimeHistoryAdded(info=self._event_info, item=new_item))
            else:
                await self._put_event(
                    RealtimeHistoryUpdated(info=self._event_info, history=self._history)
                )
        elif event.type == "input_audio_timeout_triggered":
            await self._put_event(
                RealtimeInputAudioTimeoutTriggered(
                    info=self._event_info,
                )
            )
        elif event.type == "transcript_delta":
            item_id = event.item_id
            self._record_output_guardrail_delta(
                item_id,
                event.delta,
                event.response_id,
                is_audio_output=True,
            )
            self._history = self._get_new_history(
                self._history,
                AssistantMessageItem(
                    item_id=item_id,
                    content=[AssistantAudio(transcript=self._item_transcripts[item_id])],
                ),
            )
        elif event.type == "output_text_delta":
            assert isinstance(event, RealtimeModelOutputTextDeltaEvent)
            self._record_output_guardrail_delta(
                event.item_id,
                event.delta,
                event.response_id,
                is_audio_output=False,
            )
        elif event.type == "item_updated":
            is_new = not any(item.item_id == event.item.item_id for item in self._history)

            # Preserve previously known transcripts when updating existing items.
            # This prevents transcripts from disappearing when an item is later
            # retrieved without transcript fields populated.
            incoming_item = event.item
            existing_item = next(
                (i for i in self._history if i.item_id == incoming_item.item_id), None
            )

            if (
                existing_item is not None
                and existing_item.type == "message"
                and incoming_item.type == "message"
            ):
                try:
                    # Merge transcripts for matching content indices
                    existing_content = existing_item.content
                    new_content = []
                    for idx, entry in enumerate(incoming_item.content):
                        # Only attempt to preserve for audio-like content
                        if entry.type in ("audio", "input_audio"):
                            # Use tuple form when checking against multiple classes.
                            assert isinstance(entry, InputAudio | AssistantAudio)
                            # Determine if transcript is missing/empty on the incoming entry
                            entry_transcript = entry.transcript
                            if not entry_transcript:
                                preserved: str | None = None
                                # First prefer any transcript from the existing history item
                                if idx < len(existing_content):
                                    this_content = existing_content[idx]
                                    if isinstance(this_content, AssistantAudio) or isinstance(
                                        this_content, InputAudio
                                    ):
                                        preserved = this_content.transcript

                                # If still missing and this is an assistant item, fall back to
                                # accumulated transcript deltas tracked during the turn.
                                if not preserved and incoming_item.role == "assistant":
                                    preserved = self._item_transcripts.get(incoming_item.item_id)

                                if preserved:
                                    entry = entry.model_copy(update={"transcript": preserved})

                        new_content.append(entry)

                    if new_content:
                        incoming_item = incoming_item.model_copy(update={"content": new_content})
                except Exception as exc:
                    log_model_action_error(logger, "Error merging transcripts", exc)
                    pass

            self._history = self._get_new_history(self._history, incoming_item)
            if is_new:
                new_item = next(
                    item for item in self._history if item.item_id == event.item.item_id
                )
                await self._put_event(RealtimeHistoryAdded(info=self._event_info, item=new_item))
            else:
                await self._put_event(
                    RealtimeHistoryUpdated(info=self._event_info, history=self._history)
                )
        elif event.type == "item_deleted":
            deleted_id = event.item_id
            self._history = [item for item in self._history if item.item_id != deleted_id]
            await self._put_event(
                RealtimeHistoryUpdated(info=self._event_info, history=self._history)
            )
        elif event.type == "connection_status":
            pass
        elif event.type == "turn_started":
            if event.response_id is None:
                self._unscoped_response_agent_snapshot = self._current_agent
            else:
                self._response_agent_snapshots[event.response_id] = self._current_agent
            is_guardrail_recovery = self._consume_guardrail_recovery(
                event.response_create_id,
                event.is_guardrail_recovery,
            )
            if is_guardrail_recovery:
                if event.response_id is not None:
                    self._active_guardrail_recovery_response_ids.add(event.response_id)
                    self._legacy_guardrail_recovery_turn_active = False
                    self._legacy_guardrail_recovery_response_id = None
                else:
                    self._legacy_guardrail_recovery_turn_active = True
                    self._legacy_guardrail_recovery_response_id = None
            else:
                self._legacy_guardrail_recovery_turn_active = False
                self._legacy_guardrail_recovery_response_id = None
            await self._put_event(
                RealtimeAgentStartEvent(
                    agent=self._current_agent,
                    info=self._event_info,
                )
            )
        elif event.type == "usage":
            assert isinstance(event, RealtimeModelUsageEvent)
            self._context_wrapper.usage.add(event.usage)
        elif event.type == "turn_ended":
            response_id = event.response_id or self._active_output_response_id
            if response_id is not None:
                self._finish_response_guardrail_lifecycle(response_id)

            # Clear guardrail state for next turn
            self._active_output_response_id = None
            if event.response_id is None:
                self._response_agent_snapshots.clear()
            else:
                self._response_agent_snapshots.pop(event.response_id, None)
            self._unscoped_response_agent_snapshot = None
            self._item_transcripts.clear()
            self._item_guardrail_run_counts.clear()
            if event.response_id is None:
                self._active_guardrail_recovery_response_ids.clear()
            else:
                self._active_guardrail_recovery_response_ids.discard(event.response_id)
            self._legacy_guardrail_recovery_turn_active = False
            self._legacy_guardrail_recovery_response_id = None

            await self._put_event(
                RealtimeAgentEndEvent(
                    agent=self._current_agent,
                    info=self._event_info,
                )
            )
        elif event.type == "exception":
            self._discard_failed_guardrail_recovery(
                event.response_create_id,
                event.is_guardrail_recovery,
            )
            # Store the exception to be raised in __aiter__
            self._stored_exception = event.exception
        elif event.type == "other":
            pass
        elif event.type == "raw_server_event":
            pass
        else:
            assert_never(event)

    async def _put_event(self, event: RealtimeSessionEvent) -> bool:
        """Put an event into the queue."""
        if self._closing or self._closed:
            return False
        await self._event_queue.put(event)
        return True

    def _put_event_nowait(self, event: RealtimeSessionEvent) -> bool:
        """Put an event into the unbounded queue from a synchronous callback."""
        if self._closing or self._closed:
            return False
        self._event_queue.put_nowait(event)
        return True

    async def _function_needs_approval(
        self, function_tool: FunctionTool, tool_call: RealtimeModelToolCallEvent
    ) -> bool:
        """Evaluate a function tool's needs_approval setting with parsed args."""
        needs_setting = getattr(function_tool, "needs_approval", False)
        parsed_args: dict[str, Any] = {}
        if callable(needs_setting):
            parsed_args_result = parse_function_tool_arguments(tool_call.arguments)
            if parsed_args_result is None:
                return True
            parsed_args = parsed_args_result
        return await evaluate_needs_approval_setting(
            needs_setting,
            self._context_wrapper,
            parsed_args,
            tool_call.call_id,
            strict=False,
        )

    def _build_tool_approval_item(
        self,
        tool: FunctionTool,
        tool_call: RealtimeModelToolCallEvent,
        agent: RealtimeAgent,
        *,
        tool_lookup_key: FunctionToolLookupKey | None = None,
    ) -> ToolApprovalItem:
        """Create a ToolApprovalItem for approval tracking."""
        if tool_lookup_key is None:
            tool_lookup_key = get_function_tool_lookup_key_for_tool(tool)
        tool_namespace = get_function_tool_namespace(tool)
        raw_item = {
            "type": "function_call",
            "name": tool.name,
            "call_id": tool_call.call_id,
            "arguments": tool_call.arguments,
        }
        if tool_namespace is not None:
            raw_item["namespace"] = tool_namespace
        return ToolApprovalItem(
            agent=cast(Any, agent),
            raw_item=raw_item,
            tool_name=tool.name,
            tool_namespace=tool_namespace,
            tool_lookup_key=tool_lookup_key,
        )

    async def _maybe_request_tool_approval(
        self,
        tool_call: RealtimeModelToolCallEvent,
        *,
        function_tool: FunctionTool,
        agent: RealtimeAgent,
        dispatch_snapshot: _RealtimeDispatchSnapshot,
    ) -> bool | None | _PendingToolOutput:
        """Return approval status, pending output for guardrail rejection, or None when awaiting."""
        tool_lookup_key = get_function_tool_lookup_key_for_tool(function_tool)
        approval_item = self._build_tool_approval_item(
            function_tool,
            tool_call,
            agent,
            tool_lookup_key=tool_lookup_key,
        )

        needs_approval = await self._function_needs_approval(function_tool, tool_call)
        if self._closing or self._closed:
            return None
        if not needs_approval:
            return True

        approval_status = self._context_wrapper.get_approval_status(
            function_tool.name,
            tool_call.call_id,
            existing_pending=approval_item,
            tool_lookup_key=tool_lookup_key,
        )
        if approval_status is True:
            return True
        if approval_status is False:
            return False

        if self._pre_approval_tool_input_guardrails_enabled():
            rejected_message = await self._run_tool_input_guardrails(
                tool=function_tool,
                tool_call=tool_call,
                agent=agent,
            )
            if self._closing or self._closed:
                return None
            if rejected_message is not None:
                return self._build_realtime_tool_output(
                    tool=function_tool,
                    tool_call=tool_call,
                    agent=agent,
                    output=rejected_message,
                )

        if self._closing or self._closed:
            return None

        self._pending_tool_calls[tool_call.call_id] = _PendingToolCall(
            tool_call=tool_call,
            agent=agent,
            dispatch_snapshot=dispatch_snapshot,
            function_tool=function_tool,
            approval_item=approval_item,
        )
        await self._put_event(
            RealtimeToolApprovalRequired(
                agent=agent,
                tool=function_tool,
                call_id=tool_call.call_id,
                arguments=tool_call.arguments,
                info=self._event_info,
            )
        )
        return None

    def _pre_approval_tool_input_guardrails_enabled(self) -> bool:
        return (
            self._run_config.get("tool_execution", {}).get(
                "pre_approval_tool_input_guardrails", False
            )
            is True
        )

    async def _run_tool_input_guardrails(
        self,
        *,
        tool: FunctionTool,
        tool_call: RealtimeModelToolCallEvent,
        agent: RealtimeAgent,
    ) -> str | None:
        """Run function tool input guardrails and return rejection output when blocked."""
        guardrails = tool.tool_input_guardrails
        if isinstance(guardrails, str | bytes) or not isinstance(guardrails, Sequence):
            return None
        if not guardrails:
            return None

        tool_context = ToolContext(
            context=self._context_wrapper.context,
            usage=self._context_wrapper.usage,
            tool_name=tool_call.name,
            tool_call_id=tool_call.call_id,
            tool_arguments=tool_call.arguments,
            agent=agent,
        )
        for guardrail in guardrails:
            gr_out = await guardrail.run(
                ToolInputGuardrailData(context=tool_context, agent=cast(Agent[Any], agent))
            )
            if gr_out.behavior["type"] == "raise_exception":
                raise ToolInputGuardrailTripwireTriggered(guardrail=guardrail, output=gr_out)
            if gr_out.behavior["type"] == "reject_content":
                return gr_out.behavior["message"]
        return None

    def _build_realtime_tool_output(
        self,
        *,
        tool: FunctionTool,
        tool_call: RealtimeModelToolCallEvent,
        agent: RealtimeAgent,
        output: str,
    ) -> _PendingToolOutput:
        return _PendingToolOutput(
            tool_call=tool_call,
            output=output,
            start_response=True,
            tool_end_event=RealtimeToolEnd(
                info=self._event_info,
                tool=tool,
                output=output,
                agent=agent,
                arguments=tool_call.arguments,
            ),
        )

    async def _send_tool_rejection(
        self,
        event: RealtimeModelToolCallEvent,
        *,
        tool: FunctionTool,
        agent: RealtimeAgent,
    ) -> None:
        """Send a rejection response back to the model and emit an end event."""
        rejection_message = await self._resolve_approval_rejection_message(
            tool=tool,
            call_id=event.call_id,
        )
        await self._send_tool_output_completion(
            _PendingToolOutput(
                tool_call=event,
                output=rejection_message,
                start_response=True,
                tool_end_event=RealtimeToolEnd(
                    info=self._event_info,
                    tool=tool,
                    output=rejection_message,
                    agent=agent,
                    arguments=event.arguments,
                ),
            )
        )

    async def _send_tool_output_completion(self, pending_output: _PendingToolOutput) -> None:
        if self._closing or self._closed:
            return

        call_id = pending_output.tool_call.call_id
        self._pending_tool_outputs[call_id] = pending_output
        try:
            completion = await self._send_pending_tool_output(pending_output)
        except Exception as exc:
            if self._closing or self._closed:
                self._pending_tool_outputs.pop(call_id, None)
                return
            raise _PendingToolOutputSendError(call_id, exc) from exc
        if completion is not None:
            completion_task = asyncio.create_task(
                self._await_tool_output_completion(pending_output, completion)
            )
            pending_output.completion_task = completion_task
            self._tool_call_tasks.add(completion_task)
            completion_task.add_done_callback(self._on_tool_call_task_done)
            return
        await self._finalize_pending_tool_output(pending_output, mark_completed=False)

    async def _send_pending_tool_output(
        self,
        pending_output: _PendingToolOutput,
    ) -> Awaitable[None] | None:
        if self._closing or self._closed:
            return None
        if pending_output.session_update is not None:
            await self._model.send_event(pending_output.session_update)
        if self._closing or self._closed:
            return None
        tool_output_event = RealtimeModelSendToolOutput(
            tool_call=pending_output.tool_call,
            output=pending_output.output,
            start_response=pending_output.start_response,
        )
        if pending_output.start_response:
            return await self._send_response_request(
                tool_output_event,
                pending_tool_output=pending_output,
            )
        else:
            await self._model.send_event(tool_output_event)
        return None

    async def _await_tool_output_completion(
        self,
        pending_output: _PendingToolOutput,
        completion: Awaitable[None],
    ) -> None:
        try:
            await completion
        except asyncio.CancelledError:
            pending_output.completion_task = None
            raise
        except BaseException as exc:
            pending_output.completion_task = None
            if pending_output.response_request_id is not None:
                self._discard_response_request(pending_output.response_request_id)
                pending_output.response_request_id = None
            raise _PendingToolOutputSendError(pending_output.tool_call.call_id, exc) from exc
        await self._finalize_pending_tool_output(pending_output, mark_completed=True)

    async def _finalize_pending_tool_output(
        self,
        pending_output: _PendingToolOutput,
        *,
        mark_completed: bool,
    ) -> None:
        call_id = pending_output.tool_call.call_id
        if self._pending_tool_outputs.get(call_id) is pending_output:
            self._pending_tool_outputs.pop(call_id, None)
        pending_output.completion_task = None
        pending_output.response_request_id = None
        if self._closing or self._closed:
            return
        if mark_completed:
            self._completed_tool_call_ids.add(call_id)
        if pending_output.tool_end_event is not None:
            await self._put_event(pending_output.tool_end_event)

    async def _resolve_approval_rejection_message(self, *, tool: FunctionTool, call_id: str) -> str:
        """Resolve model-visible output text for approval rejections."""
        explicit_message = self._context_wrapper.get_rejection_message(
            tool.name,
            call_id,
            tool_lookup_key=get_function_tool_lookup_key_for_tool(tool),
        )
        if explicit_message is not None:
            return explicit_message

        formatter = self._run_config.get("tool_error_formatter")
        if formatter is None:
            return REJECTION_MESSAGE

        try:
            maybe_message = formatter(
                ToolErrorFormatterArgs(
                    kind="approval_rejected",
                    tool_type="function",
                    tool_name=tool.name,
                    call_id=call_id,
                    default_message=REJECTION_MESSAGE,
                    run_context=self._context_wrapper,
                )
            )
            message = await maybe_message if inspect.isawaitable(maybe_message) else maybe_message
        except Exception as exc:
            log_tool_action_error(logger, "Tool error formatter failed", exc)
            return REJECTION_MESSAGE

        if message is None:
            return REJECTION_MESSAGE

        if not isinstance(message, str):
            if _debug.DONT_LOG_TOOL_DATA:
                logger.error("Tool error formatter returned a non-string value")
            else:
                logger.error(
                    "Tool error formatter returned non-string for %s: %s",
                    tool.name,
                    type(message).__name__,
                )
            return REJECTION_MESSAGE

        return message

    async def approve_tool_call(self, call_id: str, *, always: bool = False) -> None:
        """Approve a pending tool call and resume execution."""
        if self._closing or self._closed:
            return

        pending = self._pending_tool_calls.pop(call_id, None)
        if pending is None:
            return

        if not self._begin_tool_call(call_id, from_pending_approval=True):
            return

        try:
            self._context_wrapper.approve_tool(pending.approval_item, always_approve=always)

            if self._async_tool_calls:
                self._enqueue_tool_call_task(
                    pending.tool_call,
                    pending.agent,
                    pending.dispatch_snapshot,
                    from_pending_approval=True,
                    call_id_reserved=True,
                )
            else:
                await self._handle_tool_call(
                    pending.tool_call,
                    agent_snapshot=pending.agent,
                    dispatch_snapshot=pending.dispatch_snapshot,
                    from_pending_approval=True,
                    call_id_reserved=True,
                )
        except Exception:
            if call_id in self._active_tool_call_ids:
                self._finish_tool_call(call_id, mark_completed=False)
            raise

    async def reject_tool_call(
        self,
        call_id: str,
        *,
        always: bool = False,
        rejection_message: str | None = None,
    ) -> None:
        """Reject a pending tool call and notify the model."""
        if self._closing or self._closed:
            return

        pending = self._pending_tool_calls.pop(call_id, None)
        if pending is None:
            return

        if not self._begin_tool_call(call_id, from_pending_approval=True):
            return

        mark_completed = False
        try:
            self._context_wrapper.reject_tool(
                pending.approval_item,
                always_reject=always,
                rejection_message=rejection_message,
            )
            await self._send_tool_rejection(
                pending.tool_call,
                tool=pending.function_tool,
                agent=pending.agent,
            )
            mark_completed = True
        finally:
            self._finish_tool_call(call_id, mark_completed=mark_completed)

    async def _handle_tool_call(
        self,
        event: RealtimeModelToolCallEvent,
        *,
        agent_snapshot: RealtimeAgent | None = None,
        dispatch_snapshot: _RealtimeDispatchSnapshot | None = None,
        from_pending_approval: bool = False,
        call_id_reserved: bool = False,
    ) -> None:
        """Handle a tool call event."""
        mark_completed = False
        if not call_id_reserved and not self._begin_tool_call(
            event.call_id, from_pending_approval=from_pending_approval
        ):
            return

        agent = dispatch_snapshot.agent if dispatch_snapshot is not None else agent_snapshot
        agent = agent or self._current_agent
        try:
            pending_output = self._pending_tool_outputs.get(event.call_id)
            if pending_output is not None:
                await self._send_tool_output_completion(pending_output)
                mark_completed = True
                return

            snapshot = await self._resolve_dispatch_snapshot(agent, dispatch_snapshot)
            snapshot = await self._filter_enabled_dispatch_snapshot(snapshot)
            if self._closing or self._closed:
                return
            tools = snapshot.tools
            handoffs = snapshot.handoffs
            validate_realtime_tool_names(tools, handoffs)
            function_map = {tool.name: tool for tool in tools if isinstance(tool, FunctionTool)}
            handoff_map = {handoff.tool_name: handoff for handoff in handoffs}

            if event.name in function_map:
                func_tool = function_map[event.name]
                approval_status = await self._maybe_request_tool_approval(
                    event,
                    function_tool=func_tool,
                    agent=agent,
                    dispatch_snapshot=snapshot,
                )
                if self._closing or self._closed:
                    return
                if isinstance(approval_status, _PendingToolOutput):
                    await self._send_tool_output_completion(approval_status)
                    mark_completed = True
                    return
                if approval_status is False:
                    await self._send_tool_rejection(event, tool=func_tool, agent=agent)
                    mark_completed = True
                    return
                if approval_status is None:
                    return

                rejected_message = await self._run_tool_input_guardrails(
                    tool=func_tool,
                    tool_call=event,
                    agent=agent,
                )
                if self._closing or self._closed:
                    return
                if rejected_message is not None:
                    await self._send_tool_output_completion(
                        self._build_realtime_tool_output(
                            tool=func_tool,
                            tool_call=event,
                            agent=agent,
                            output=rejected_message,
                        )
                    )
                    mark_completed = True
                    return

                await self._put_event(
                    RealtimeToolStart(
                        info=self._event_info,
                        tool=func_tool,
                        agent=agent,
                        arguments=event.arguments,
                    )
                )
                if self._closing or self._closed:
                    return

                tool_context = ToolContext(
                    context=self._context_wrapper.context,
                    usage=self._context_wrapper.usage,
                    tool_name=event.name,
                    tool_call_id=event.call_id,
                    tool_arguments=event.arguments,
                    agent=agent,
                )
                result = await invoke_function_tool(
                    function_tool=func_tool,
                    context=tool_context,
                    arguments=event.arguments,
                )
                if self._closing or self._closed:
                    return

                await self._send_tool_output_completion(
                    _PendingToolOutput(
                        tool_call=event,
                        output=_serialize_tool_output(result),
                        start_response=True,
                        tool_end_event=RealtimeToolEnd(
                            info=self._event_info,
                            tool=func_tool,
                            output=result,
                            agent=agent,
                            arguments=event.arguments,
                        ),
                    )
                )
                mark_completed = True
            elif event.name in handoff_map:
                handoff = handoff_map[event.name]
                tool_context = ToolContext(
                    context=self._context_wrapper.context,
                    usage=self._context_wrapper.usage,
                    tool_name=event.name,
                    tool_call_id=event.call_id,
                    tool_arguments=event.arguments,
                    agent=agent,
                )

                # Execute the handoff to get the new agent
                result = await handoff.on_invoke_handoff(self._context_wrapper, event.arguments)
                if self._closing or self._closed:
                    return
                if not isinstance(result, RealtimeAgent):
                    raise UserError(
                        f"Handoff {handoff.tool_name} returned invalid result: {type(result)}"
                    )

                # Store previous agent for event
                previous_agent = agent

                # Get updated model settings from new agent
                updated_settings = await self._get_updated_model_settings_from_agent(
                    starting_settings=None,
                    agent=result,
                )
                if self._closing or self._closed:
                    return
                updated_snapshot = self._dispatch_snapshot_from_settings(result, updated_settings)

                # Update current agent
                self._current_agent = result
                self._current_dispatch_snapshot = updated_snapshot

                # Send handoff event
                await self._put_event(
                    RealtimeHandoffEvent(
                        from_agent=previous_agent,
                        to_agent=self._current_agent,
                        info=self._event_info,
                    )
                )

                # Send the session update before the tool output that triggers a new response.
                transfer_message = handoff.get_transfer_message(result)
                await self._send_tool_output_completion(
                    _PendingToolOutput(
                        tool_call=event,
                        output=transfer_message,
                        start_response=True,
                        session_update=RealtimeModelSendSessionUpdate(
                            session_settings=updated_settings
                        ),
                    )
                )
                mark_completed = True
            else:
                error_message = f"Tool {event.name} not found"
                await self._send_tool_output_completion(
                    _PendingToolOutput(
                        tool_call=event,
                        output=error_message,
                        start_response=False,
                    )
                )
                mark_completed = True
                await self._put_event(
                    RealtimeError(
                        info=self._event_info,
                        error={"message": error_message},
                    )
                )
        finally:
            self._finish_tool_call(event.call_id, mark_completed=mark_completed)

    def _begin_tool_call(self, call_id: str, *, from_pending_approval: bool) -> bool:
        if self._closing or self._closed:
            return False
        pending_output = self._pending_tool_outputs.get(call_id)
        if (
            pending_output is not None
            and pending_output.completion_task is not None
            and not pending_output.completion_task.done()
        ):
            return False
        if call_id in self._active_tool_call_ids:
            return False
        if call_id in self._completed_tool_call_ids and pending_output is None:
            return False
        if not from_pending_approval and call_id in self._pending_tool_calls:
            return False
        self._active_tool_call_ids.add(call_id)
        return True

    def _finish_tool_call(self, call_id: str, *, mark_completed: bool) -> None:
        self._active_tool_call_ids.discard(call_id)
        if (
            mark_completed
            and call_id not in self._pending_tool_outputs
            and not self._closing
            and not self._closed
        ):
            self._completed_tool_call_ids.add(call_id)

    @classmethod
    def _get_new_history(
        cls,
        old_history: list[RealtimeItem],
        event: RealtimeModelInputAudioTranscriptionCompletedEvent | RealtimeItem,
    ) -> list[RealtimeItem]:
        if isinstance(event, RealtimeModelInputAudioTranscriptionCompletedEvent):
            new_history: list[RealtimeItem] = []
            existing_item_found = False
            for item in old_history:
                if item.item_id == event.item_id and item.type == "message" and item.role == "user":
                    content: list[InputText | InputAudio] = []
                    for entry in item.content:
                        if entry.type == "input_audio":
                            copied_entry = entry.model_copy(update={"transcript": event.transcript})
                            content.append(copied_entry)
                        else:
                            content.append(entry)  # type: ignore
                    new_history.append(
                        item.model_copy(update={"content": content, "status": "completed"})
                    )
                    existing_item_found = True
                else:
                    new_history.append(item)

            if existing_item_found is False:
                new_history.append(
                    UserMessageItem(
                        item_id=event.item_id, content=[InputText(text=event.transcript)]
                    )
                )
            return new_history

        # TODO (rm) Add support for audio storage config

        # If the item already exists, update it
        existing_index = next(
            (i for i, item in enumerate(old_history) if item.item_id == event.item_id), None
        )
        if existing_index is not None:
            new_history = old_history.copy()
            if event.type == "message" and event.content is not None and len(event.content) > 0:
                existing_item = old_history[existing_index]
                if existing_item.type == "message":
                    # Merge content preserving existing transcript/text when incoming entry is empty
                    if event.role == "assistant" and existing_item.role == "assistant":
                        assistant_existing_content = existing_item.content
                        assistant_incoming = event.content
                        assistant_new_content: list[AssistantText | AssistantAudio] = []
                        for idx, ac in enumerate(assistant_incoming):
                            if idx >= len(assistant_existing_content):
                                assistant_new_content.append(ac)
                                continue
                            assistant_current = assistant_existing_content[idx]
                            if ac.type == "audio":
                                if ac.transcript is None:
                                    assistant_new_content.append(assistant_current)
                                else:
                                    assistant_new_content.append(ac)
                            else:  # text
                                cur_text = (
                                    assistant_current.text
                                    if isinstance(assistant_current, AssistantText)
                                    else None
                                )
                                if cur_text is not None and ac.text is None:
                                    assistant_new_content.append(assistant_current)
                                else:
                                    assistant_new_content.append(ac)
                        updated_assistant = event.model_copy(
                            update={"content": assistant_new_content}
                        )
                        new_history[existing_index] = updated_assistant
                    elif event.role == "user" and existing_item.role == "user":
                        user_existing_content = existing_item.content
                        user_incoming = event.content

                        # Start from incoming content (prefer latest fields)
                        user_new_content: list[InputText | InputAudio | InputImage] = list(
                            user_incoming
                        )

                        # Merge by type with special handling for images and transcripts
                        def _image_url_str(val: object) -> str | None:
                            if isinstance(val, InputImage):
                                return val.image_url or None
                            return None

                        # 1) Preserve any existing images that are missing from the incoming payload
                        incoming_image_urls: set[str] = set()
                        for part in user_incoming:
                            if isinstance(part, InputImage):
                                u = _image_url_str(part)
                                if u:
                                    incoming_image_urls.add(u)

                        missing_images: list[InputImage] = []
                        for part in user_existing_content:
                            if isinstance(part, InputImage):
                                u = _image_url_str(part)
                                if u and u not in incoming_image_urls:
                                    missing_images.append(part)

                        # Insert missing images at the beginning to keep them visible and stable
                        if missing_images:
                            user_new_content = missing_images + user_new_content

                        # 2) For text/audio entries, preserve existing when incoming entry is empty
                        merged: list[InputText | InputAudio | InputImage] = []
                        for idx, uc in enumerate(user_new_content):
                            if uc.type == "input_audio":
                                # Attempt to preserve transcript if empty
                                transcript = getattr(uc, "transcript", None)
                                if transcript is None and idx < len(user_existing_content):
                                    prev = user_existing_content[idx]
                                    if isinstance(prev, InputAudio) and prev.transcript is not None:
                                        uc = uc.model_copy(update={"transcript": prev.transcript})
                                merged.append(uc)
                            elif uc.type == "input_text":
                                text = getattr(uc, "text", None)
                                if (text is None or text == "") and idx < len(
                                    user_existing_content
                                ):
                                    prev = user_existing_content[idx]
                                    if isinstance(prev, InputText) and prev.text:
                                        uc = uc.model_copy(update={"text": prev.text})
                                merged.append(uc)
                            else:
                                merged.append(uc)

                        updated_user = event.model_copy(update={"content": merged})
                        new_history[existing_index] = updated_user
                    elif event.role == "system" and existing_item.role == "system":
                        system_existing_content = existing_item.content
                        system_incoming = event.content
                        # Prefer existing non-empty text when incoming is empty
                        system_new_content: list[InputText] = []
                        for idx, sc in enumerate(system_incoming):
                            if idx >= len(system_existing_content):
                                system_new_content.append(sc)
                                continue
                            system_current = system_existing_content[idx]
                            cur_text = system_current.text
                            if cur_text is not None and sc.text is None:
                                system_new_content.append(system_current)
                            else:
                                system_new_content.append(sc)
                        updated_system = event.model_copy(update={"content": system_new_content})
                        new_history[existing_index] = updated_system
                    else:
                        # Role changed or mismatched; just replace
                        new_history[existing_index] = event
                else:
                    # If the existing item is not a message, just replace it.
                    new_history[existing_index] = event
            return new_history

        # Otherwise, insert it after the previous_item_id if that is set
        elif event.previous_item_id:
            # Insert the new item after the previous item
            previous_index = next(
                (i for i, item in enumerate(old_history) if item.item_id == event.previous_item_id),
                None,
            )
            if previous_index is not None:
                new_history = old_history.copy()
                new_history.insert(previous_index + 1, event)
                return new_history

        # Otherwise, add it to the end
        return old_history + [event]

    async def _run_output_guardrails(
        self,
        text: str,
        response_id: str,
        agent_snapshot: RealtimeAgent,
        is_guardrail_recovery: bool,
        is_audio_output: bool,
    ) -> bool:
        """Run output guardrails on the given text. Returns True if any guardrail was triggered."""
        if self._closing or self._closed:
            return False

        combined_guardrails = agent_snapshot.output_guardrails + self._run_config.get(
            "output_guardrails", []
        )
        seen_ids: set[int] = set()
        output_guardrails = []
        for guardrail in combined_guardrails:
            guardrail_id = id(guardrail)
            if guardrail_id not in seen_ids:
                output_guardrails.append(guardrail)
                seen_ids.add(guardrail_id)

        # If we've already interrupted this response, skip
        if not output_guardrails or response_id in self._interrupted_response_ids:
            return False

        triggered_results = []

        for guardrail in output_guardrails:
            try:
                result = await guardrail.run(
                    # TODO (rm) Remove this cast, it's wrong
                    self._context_wrapper,
                    cast(Agent[Any], agent_snapshot),
                    text,
                )
                if self._closing or self._closed:
                    return False
                if result.output.tripwire_triggered:
                    triggered_results.append(result)
            except Exception as exc:
                log_model_and_tool_action_warning(
                    logger,
                    "Output guardrail raised an exception; skipping it",
                    exc,
                    diagnostic_extra=partial(_guardrail_diagnostic_extra, guardrail),
                )
                continue

        if triggered_results:
            # Double-check: bail if already interrupted for this response
            if response_id in self._interrupted_response_ids or self._closing or self._closed:
                return False

            # Mark as interrupted immediately (before any awaits) to minimize race window
            self._interrupted_response_ids.add(response_id)

            # Emit guardrail tripped event
            if not await self._put_event(
                RealtimeGuardrailTripped(
                    guardrail_results=triggered_results,
                    message=text,
                    info=self._event_info,
                )
            ):
                return False

            interrupt_error: Exception | None = None
            try:
                # Cancel only while the response that produced this output remains active. Late
                # audio guardrails still stop buffered playback without cancelling a newer response.
                if self._is_guardrail_response_active(response_id):
                    await self._model.send_event(
                        RealtimeModelSendInterrupt(
                            force_response_cancel=True,
                            response_id=response_id,
                            cancel_response_only=not is_audio_output,
                        )
                    )
                elif is_audio_output:
                    await self._model.send_event(
                        RealtimeModelSendInterrupt(
                            response_id=response_id,
                            playback_only=True,
                        )
                    )
            except Exception as exc:
                interrupt_error = exc

            # Start one automatic recovery response for each ordinary response. If that recovery
            # response also trips a guardrail, reject it without extending the same recovery chain.
            recovery_error: Exception | None = None
            if not is_guardrail_recovery:
                if self._closing or self._closed:
                    if interrupt_error is not None:
                        raise interrupt_error
                    return False
                else:
                    guardrail_names = [result.guardrail.get_name() for result in triggered_results]
                    self._guardrail_recovery_request_counter += 1
                    response_create_id = (
                        f"guardrail_recovery_{self._guardrail_recovery_request_counter}"
                    )
                    self._pending_guardrail_recovery_ids.add(response_create_id)
                    self._pending_guardrail_recovery_order.append(response_create_id)
                    try:
                        await self._send_response_request(
                            RealtimeModelSendUserInput(
                                user_input=f"guardrail triggered: {', '.join(guardrail_names)}",
                                response_create_id=response_create_id,
                            )
                        )
                    except BaseException as exc:
                        self._discard_guardrail_recovery(response_create_id)
                        if not isinstance(exc, Exception):
                            raise
                        recovery_error = exc

            if interrupt_error is not None and recovery_error is not None:
                raise _GuardrailRecoveryAttemptError(
                    interrupt_error,
                    recovery_error,
                ) from interrupt_error
            if interrupt_error is not None:
                raise interrupt_error
            if recovery_error is not None:
                raise recovery_error

            return True

        return False

    def _discard_guardrail_recovery(self, response_create_id: str) -> None:
        self._pending_guardrail_recovery_ids.discard(response_create_id)
        try:
            self._pending_guardrail_recovery_order.remove(response_create_id)
        except ValueError:
            pass
        self._discard_response_request(response_create_id)

    def _discard_response_request(self, request_id: str) -> None:
        try:
            self._pending_response_request_order.remove(request_id)
        except ValueError:
            pass

    def _consume_pending_ordinary_response_requests(self) -> None:
        while (
            self._pending_response_request_order
            and self._pending_response_request_order[0] not in self._pending_guardrail_recovery_ids
        ):
            self._pending_response_request_order.popleft()

    def _discard_failed_guardrail_recovery(
        self,
        response_create_id: str | None,
        is_guardrail_recovery: bool | None,
    ) -> None:
        if response_create_id is not None:
            self._discard_guardrail_recovery(response_create_id)
            return

        if is_guardrail_recovery is False:
            return
        if is_guardrail_recovery is None and self._pending_response_request_order:
            request_id = self._pending_response_request_order[0]
            if request_id not in self._pending_guardrail_recovery_ids:
                self._pending_response_request_order.popleft()
                return
            self._discard_guardrail_recovery(request_id)
            return
        if not self._pending_guardrail_recovery_order:
            return

        self._discard_guardrail_recovery(self._pending_guardrail_recovery_order[0])

    def _consume_guardrail_recovery(
        self,
        response_create_id: str | None,
        is_guardrail_recovery: bool | None,
    ) -> bool:
        if response_create_id is not None:
            was_pending = response_create_id in self._pending_guardrail_recovery_ids
            if was_pending:
                self._discard_guardrail_recovery(response_create_id)
            return was_pending or is_guardrail_recovery is True

        if is_guardrail_recovery is False:
            self._consume_pending_ordinary_response_requests()
            return False
        if is_guardrail_recovery is True:
            if self._pending_guardrail_recovery_order:
                self._discard_guardrail_recovery(self._pending_guardrail_recovery_order[0])
            return True

        if self._pending_response_request_order:
            request_id = self._pending_response_request_order[0]
            if request_id not in self._pending_guardrail_recovery_ids:
                self._pending_response_request_order.popleft()
                return False
            self._discard_guardrail_recovery(request_id)
            return True

        if not self._pending_guardrail_recovery_order:
            return False

        self._discard_guardrail_recovery(self._pending_guardrail_recovery_order[0])
        return True

    def _is_guardrail_response_active(self, response_id: str) -> bool:
        return (
            not self._closing
            and not self._closed
            and self._active_output_response_id == response_id
        )

    def _record_output_guardrail_delta(
        self,
        item_id: str,
        delta: str,
        response_id: str,
        *,
        is_audio_output: bool,
    ) -> None:
        """Accumulate text or audio transcript deltas using the same guardrail debounce."""
        self._active_output_response_id = response_id
        agent_snapshot = self._response_agent_snapshots.get(response_id)
        if agent_snapshot is None:
            agent_snapshot = self._unscoped_response_agent_snapshot or self._current_agent
            self._response_agent_snapshots[response_id] = agent_snapshot
        if (
            self._legacy_guardrail_recovery_turn_active
            and self._legacy_guardrail_recovery_response_id is None
        ):
            self._legacy_guardrail_recovery_response_id = response_id
        is_guardrail_recovery = (
            response_id in self._active_guardrail_recovery_response_ids
            or self._legacy_guardrail_recovery_response_id == response_id
        )
        if item_id not in self._item_transcripts:
            self._item_transcripts[item_id] = ""
            self._item_guardrail_run_counts[item_id] = 0

        self._item_transcripts[item_id] += delta
        current_length = len(self._item_transcripts[item_id])
        next_run_threshold = (
            self._item_guardrail_run_counts[item_id] + 1
        ) * self._debounce_text_length
        if current_length >= next_run_threshold:
            self._item_guardrail_run_counts[item_id] += 1
            self._enqueue_guardrail_task(
                self._item_transcripts[item_id],
                response_id,
                agent_snapshot=agent_snapshot,
                is_guardrail_recovery=is_guardrail_recovery,
                is_audio_output=is_audio_output,
            )

    def _enqueue_guardrail_task(
        self,
        text: str,
        response_id: str,
        *,
        agent_snapshot: RealtimeAgent,
        is_guardrail_recovery: bool,
        is_audio_output: bool,
    ) -> None:
        # Runs the guardrails in a separate task to avoid blocking the main loop
        if self._closing or self._closed:
            return

        task = asyncio.create_task(
            self._run_output_guardrails(
                text,
                response_id,
                agent_snapshot,
                is_guardrail_recovery,
                is_audio_output,
            )
        )
        self._guardrail_tasks.add(task)
        self._guardrail_tasks_by_response_id.setdefault(response_id, set()).add(task)

        # Add callback to remove completed tasks and handle exceptions
        task.add_done_callback(partial(self._on_guardrail_task_done, response_id=response_id))

    def _on_guardrail_task_done(self, task: asyncio.Task[Any], *, response_id: str) -> None:
        """Handle completion of a guardrail task."""
        # Remove from tracking set
        self._guardrail_tasks.discard(task)
        response_tasks = self._guardrail_tasks_by_response_id.get(response_id)
        if response_tasks is not None:
            response_tasks.discard(task)
            if not response_tasks:
                self._guardrail_tasks_by_response_id.pop(response_id, None)

        should_retire_response_audio = (
            response_id in self._responses_awaiting_guardrail_cleanup
            and response_id not in self._guardrail_tasks_by_response_id
        )
        if should_retire_response_audio:
            self._responses_awaiting_guardrail_cleanup.discard(response_id)

        if self._closing or self._closed:
            self._consume_task_result(task)
            return

        if should_retire_response_audio:
            self._retire_response_audio(response_id)

        # Check for exceptions and propagate as events
        if not task.cancelled():
            exception = task.exception()
            if exception:
                # Create an exception event instead of raising
                self._put_event_nowait(
                    RealtimeError(
                        info=self._event_info,
                        error={"message": f"Guardrail task failed: {str(exception)}"},
                    )
                )

    def _finish_response_guardrail_lifecycle(self, response_id: str) -> None:
        if response_id in self._guardrail_tasks_by_response_id:
            self._responses_awaiting_guardrail_cleanup.add(response_id)
            return
        self._retire_response_audio(response_id)

    def _retire_response_audio(self, response_id: str) -> None:
        try:
            self._model._retire_response_audio(response_id)
        except Exception as exception:
            self._put_event_nowait(
                RealtimeError(
                    info=self._event_info,
                    error={"message": f"Response audio cleanup failed: {exception}"},
                )
            )
        finally:
            self._interrupted_response_ids.discard(response_id)

    def _enqueue_tool_call_task(
        self,
        event: RealtimeModelToolCallEvent,
        agent_snapshot: RealtimeAgent,
        dispatch_snapshot: _RealtimeDispatchSnapshot | None = None,
        *,
        from_pending_approval: bool = False,
        call_id_reserved: bool = False,
    ) -> None:
        """Run tool calls in the background to avoid blocking realtime transport."""
        if self._closing or self._closed:
            if call_id_reserved:
                self._finish_tool_call(event.call_id, mark_completed=False)
            return

        handle_kwargs: dict[str, Any] = {"agent_snapshot": agent_snapshot}
        if dispatch_snapshot is not None:
            handle_kwargs["dispatch_snapshot"] = dispatch_snapshot
        if from_pending_approval:
            handle_kwargs["from_pending_approval"] = True
        if call_id_reserved:
            handle_kwargs["call_id_reserved"] = True

        task = asyncio.create_task(self._handle_tool_call(event, **handle_kwargs))
        self._tool_call_tasks.add(task)
        task.add_done_callback(self._on_tool_call_task_done)

    def _on_tool_call_task_done(self, task: asyncio.Task[Any]) -> None:
        self._tool_call_tasks.discard(task)

        if self._closing or self._closed:
            self._consume_task_result(task)
            return

        if task.cancelled():
            return

        exception = task.exception()
        if exception is None:
            return

        if isinstance(exception, _PendingToolOutputSendError):
            if _debug.DONT_LOG_TOOL_DATA:
                logger.warning("Realtime tool output send failed; cached output will be retried")
            else:
                logger.warning(
                    "Realtime tool output send failed for call %s; cached output will be retried",
                    exception.call_id,
                    exc_info=exception,
                )
            self._put_event_nowait(
                RealtimeError(
                    info=self._event_info,
                    error={
                        "message": (
                            f"Tool output send failed; cached output will be retried: {exception}"
                        )
                    },
                )
            )
            return

        log_tool_action_error(logger, "Realtime tool call task failed", exception)

        if self._stored_exception is None:
            self._stored_exception = exception

        self._put_event_nowait(
            RealtimeError(
                info=self._event_info,
                error={"message": f"Tool call task failed: {exception}"},
            )
        )

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    def _on_cleanup_task_done(self, task: asyncio.Task[None]) -> None:
        if self._cleanup_task is task:
            self._cleanup_task = None
        self._consume_task_result(task)

    async def _cancel_background_tasks(self) -> None:
        tracked_tasks = self._guardrail_tasks | self._tool_call_tasks
        if not tracked_tasks:
            return

        for task in tracked_tasks:
            if not task.done():
                task.cancel()

        done, pending = await asyncio.wait(
            tracked_tasks,
            timeout=_BACKGROUND_TASK_CANCEL_GRACE_SECONDS,
        )

        self._guardrail_tasks.difference_update(done)
        self._tool_call_tasks.difference_update(done)
        for task in done:
            self._consume_task_result(task)

        if pending:
            logger.warning(
                "Realtime session cleanup timed out with %d background task(s) still stopping.",
                len(pending),
            )

    def _wake_event_iterators(self) -> None:
        for _ in range(self._event_iterator_waiters):
            self._event_queue.put_nowait(_REALTIME_SESSION_CLOSED_SENTINEL)

    def _clear_response_bookkeeping(self) -> None:
        self._interrupted_response_ids.clear()
        self._active_output_response_id = None
        self._response_agent_snapshots.clear()
        self._unscoped_response_agent_snapshot = None
        self._item_transcripts.clear()
        self._item_guardrail_run_counts.clear()
        self._pending_guardrail_recovery_ids.clear()
        self._pending_guardrail_recovery_order.clear()
        self._pending_response_request_order.clear()
        self._active_guardrail_recovery_response_ids.clear()
        self._legacy_guardrail_recovery_turn_active = False
        self._legacy_guardrail_recovery_response_id = None
        self._guardrail_tasks_by_response_id.clear()
        self._responses_awaiting_guardrail_cleanup.clear()

    async def _cleanup(self) -> None:
        """Clean up all resources and mark session as closed."""
        if self._closed:
            self._wake_event_iterators()
            return

        # Stop new model events before cleanup yields control.
        self._model.remove_listener(self)

        # Account for session-owned background work before closing its transport.
        await self._cancel_background_tasks()
        self._clear_response_bookkeeping()

        # Close the model connection
        await self._model.close()

        # Clear pending approval tracking
        self._pending_tool_calls.clear()
        self._pending_tool_outputs.clear()
        self._active_tool_call_ids.clear()
        self._completed_tool_call_ids.clear()

        # Mark as closed
        self._closed = True
        self._wake_event_iterators()

    def _dispatch_snapshot_from_settings(
        self,
        agent: RealtimeAgent[Any],
        settings: RealtimeSessionModelSettings,
    ) -> _RealtimeDispatchSnapshot:
        return _RealtimeDispatchSnapshot(
            agent=agent,
            tools=tuple(settings.get("tools", [])),
            handoffs=tuple(
                cast(list[Handoff[Any, RealtimeAgent[Any]]], settings.get("handoffs", []))
            ),
        )

    async def _resolve_dispatch_snapshot(
        self,
        agent: RealtimeAgent[Any],
        dispatch_snapshot: _RealtimeDispatchSnapshot | None,
    ) -> _RealtimeDispatchSnapshot:
        if dispatch_snapshot is not None:
            return dispatch_snapshot

        if (
            self._current_dispatch_snapshot is not None
            and self._current_dispatch_snapshot.agent is agent
        ):
            return self._current_dispatch_snapshot

        tools, handoffs = await gather_with_cancel(
            agent.get_all_tools(self._context_wrapper),
            self._get_handoffs(agent, self._context_wrapper),
        )
        return _RealtimeDispatchSnapshot(agent=agent, tools=tuple(tools), handoffs=tuple(handoffs))

    async def _filter_enabled_dispatch_snapshot(
        self,
        snapshot: _RealtimeDispatchSnapshot,
    ) -> _RealtimeDispatchSnapshot:
        tools, handoffs = await gather_with_cancel(
            filter_enabled_tools(snapshot.tools, self._context_wrapper, snapshot.agent),
            filter_enabled_handoffs(snapshot.handoffs, self._context_wrapper, snapshot.agent),
        )
        return _RealtimeDispatchSnapshot(
            agent=snapshot.agent,
            tools=tuple(tools),
            handoffs=tuple(cast(list[Handoff[Any, RealtimeAgent[Any]]], handoffs)),
        )

    async def _get_updated_model_settings_from_agent(
        self,
        starting_settings: RealtimeSessionModelSettings | None,
        agent: RealtimeAgent,
    ) -> RealtimeSessionModelSettings:
        # Start with the merged base settings from run and model configuration.
        updated_settings = self._base_model_settings.copy()

        if agent.prompt is not None:
            updated_settings["prompt"] = agent.prompt

        instructions, tools, handoffs = await gather_with_cancel(
            agent.get_system_prompt(self._context_wrapper),
            agent.get_all_tools(self._context_wrapper),
            self._get_handoffs(agent, self._context_wrapper),
        )
        updated_settings["instructions"] = instructions or ""
        updated_settings["tools"] = tools or []
        updated_settings["handoffs"] = handoffs or []

        # Apply starting settings (from model config) next
        if starting_settings:
            updated_settings.update(starting_settings)
            if "tools" in starting_settings:
                updated_settings["tools"] = await filter_enabled_tools(
                    updated_settings.get("tools") or [],
                    self._context_wrapper,
                    agent,
                )
            if "handoffs" in starting_settings:
                updated_settings["handoffs"] = await filter_enabled_handoffs(
                    updated_settings.get("handoffs") or [],
                    self._context_wrapper,
                    agent,
                )
        validate_realtime_tool_names(
            updated_settings.get("tools", []),
            updated_settings.get("handoffs", []),
        )

        disable_tracing = self._run_config.get("tracing_disabled", False)
        if disable_tracing:
            updated_settings["tracing"] = None

        return updated_settings

    @classmethod
    async def _get_handoffs(
        cls, agent: RealtimeAgent[Any], context_wrapper: RunContextWrapper[Any]
    ) -> list[Handoff[Any, RealtimeAgent[Any]]]:
        return await collect_enabled_handoffs(agent, context_wrapper)
