from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import Any, cast

from typing_extensions import assert_never

from ..agent import Agent
from ..exceptions import ModelBehaviorError, UserError
from ..handoffs import Handoff
from ..run_context import RunContextWrapper, TContext
from ..tool import FunctionTool
from ..tool_context import ToolContext
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
    RealtimeRawModelEvent,
    RealtimeSessionEvent,
    RealtimeToolEnd,
    RealtimeToolStart,
)
from .handoffs import realtime_handoff
from .items import InputAudio, InputText, RealtimeItem
from .model import RealtimeModel, RealtimeModelConfig, RealtimeModelListener
from .model_events import (
    RealtimeModelEvent,
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelToolCallEvent,
)
from .model_inputs import (
    RealtimeModelSendAudio,
    RealtimeModelSendInterrupt,
    RealtimeModelSendSessionUpdate,
    RealtimeModelSendToolOutput,
    RealtimeModelSendUserInput,
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
        self._event_queue: asyncio.Queue[RealtimeSessionEvent] = asyncio.Queue()
        self._closed = False
        self._stored_exception: Exception | None = None

        # Guardrails state tracking
        self._interrupted_by_guardrail = False
        self._item_transcripts: dict[str, str] = {}  # item_id -> accumulated transcript
        self._item_guardrail_run_counts: dict[str, int] = {}  # item_id -> run count
        self._debounce_text_length = self._run_config.get("guardrails_settings", {}).get(
            "debounce_text_length", 100
        )

        self._guardrail_tasks: set[asyncio.Task[Any]] = set()

    @property
    def model(self) -> RealtimeModel:
        """Access the underlying model for adding listeners or other direct interaction."""
        return self._model

    async def __aenter__(self) -> RealtimeSession:
        """Start the session by connecting to the model. After this, you will be able to stream
        events from the model and send messages and audio to the model.
        """
        # Add ourselves as a listener
        self._model.add_listener(self)

        model_config = self._model_config.copy()
        model_config["initial_model_settings"] = await self._get_updated_model_settings_from_agent(
            starting_settings=self._model_config.get("initial_model_settings", None),
            agent=self._current_agent,
        )

        # Connect to the model
        await self._model.connect(model_config)

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
        while not self._closed:
            try:
                # Check if there's a stored exception to raise
                if self._stored_exception is not None:
                    # Clean up resources before raising
                    await self._cleanup()
                    raise self._stored_exception

                event = await self._event_queue.get()
                yield event
            except asyncio.CancelledError:
                break

    async def close(self) -> None:
        """Close the session."""
        await self._cleanup()

    async def send_message(self, message: RealtimeUserInput) -> None:
        """Send a message to the model."""
        await self._model.send_event(RealtimeModelSendUserInput(user_input=message))

    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None:
        """Send a raw audio chunk to the model."""
        await self._model.send_event(RealtimeModelSendAudio(audio=audio, commit=commit))

    async def interrupt(self) -> None:
        """Interrupt the model."""
        await self._model.send_event(RealtimeModelSendInterrupt())

    async def update_agent(self, agent: RealtimeAgent) -> None:
        """Update the active agent for this session and apply its settings to the model."""
        self._current_agent = agent

        updated_settings = await self._get_updated_model_settings_from_agent(
            starting_settings=None,
            agent=self._current_agent,
        )

        await self._model.send_event(
            RealtimeModelSendSessionUpdate(session_settings=updated_settings)
        )

    async def on_event(self, event: RealtimeModelEvent) -> None:
        await self._put_event(RealtimeRawModelEvent(data=event, info=self._event_info))

        if event.type == "error":
            await self._put_event(RealtimeError(info=self._event_info, error=event.error))
        elif event.type == "function_call":
            await self._handle_tool_call(event)
        elif event.type == "audio":
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
            self._history = RealtimeSession._get_new_history(self._history, event)
            await self._put_event(
                RealtimeHistoryUpdated(info=self._event_info, history=self._history)
            )
        elif event.type == "transcript_delta":
            # Accumulate transcript text for guardrail debouncing per item_id
            item_id = event.item_id
            if item_id not in self._item_transcripts:
                self._item_transcripts[item_id] = ""
                self._item_guardrail_run_counts[item_id] = 0

            self._item_transcripts[item_id] += event.delta

            # Check if we should run guardrails based on debounce threshold
            current_length = len(self._item_transcripts[item_id])
            threshold = self._debounce_text_length
            next_run_threshold = (self._item_guardrail_run_counts[item_id] + 1) * threshold

            if current_length >= next_run_threshold:
                self._item_guardrail_run_counts[item_id] += 1
                self._enqueue_guardrail_task(self._item_transcripts[item_id])
        elif event.type == "item_updated":
            is_new = not any(item.item_id == event.item.item_id for item in self._history)
            self._history = self._get_new_history(self._history, event.item)
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
            await self._put_event(
                RealtimeAgentStartEvent(
                    agent=self._current_agent,
                    info=self._event_info,
                )
            )
        elif event.type == "turn_ended":
            # Clear guardrail state for next turn
            self._item_transcripts.clear()
            self._item_guardrail_run_counts.clear()
            self._interrupted_by_guardrail = False

            await self._put_event(
                RealtimeAgentEndEvent(
                    agent=self._current_agent,
                    info=self._event_info,
                )
            )
        elif event.type == "exception":
            # Store the exception to be raised in __aiter__
            self._stored_exception = event.exception
        elif event.type == "other":
            pass
        elif event.type == "raw_server_event":
            pass
        else:
            assert_never(event)

    async def _put_event(self, event: RealtimeSessionEvent) -> None:
        """Put an event into the queue."""
        await self._event_queue.put(event)

    async def _handle_tool_call(self, event: RealtimeModelToolCallEvent) -> None:
        """Handle a tool call event."""
        tools, handoffs = await asyncio.gather(
            self._current_agent.get_all_tools(self._context_wrapper),
            self._get_handoffs(self._current_agent, self._context_wrapper),
        )
        function_map = {tool.name: tool for tool in tools if isinstance(tool, FunctionTool)}
        handoff_map = {handoff.tool_name: handoff for handoff in handoffs}

        if event.name in function_map:
            await self._put_event(
                RealtimeToolStart(
                    info=self._event_info,
                    tool=function_map[event.name],
                    agent=self._current_agent,
                )
            )

            func_tool = function_map[event.name]
            tool_context = ToolContext(
                context=self._context_wrapper.context,
                usage=self._context_wrapper.usage,
                tool_name=event.name,
                tool_call_id=event.call_id,
            )
            result = await func_tool.on_invoke_tool(tool_context, event.arguments)

            await self._model.send_event(
                RealtimeModelSendToolOutput(
                    tool_call=event, output=str(result), start_response=True
                )
            )

            await self._put_event(
                RealtimeToolEnd(
                    info=self._event_info,
                    tool=func_tool,
                    output=result,
                    agent=self._current_agent,
                )
            )
        elif event.name in handoff_map:
            handoff = handoff_map[event.name]
            tool_context = ToolContext(
                context=self._context_wrapper.context,
                usage=self._context_wrapper.usage,
                tool_name=event.name,
                tool_call_id=event.call_id,
            )

            # Execute the handoff to get the new agent
            result = await handoff.on_invoke_handoff(self._context_wrapper, event.arguments)
            if not isinstance(result, RealtimeAgent):
                raise UserError(
                    f"Handoff {handoff.tool_name} returned invalid result: {type(result)}"
                )

            # Store previous agent for event
            previous_agent = self._current_agent

            # Update current agent
            self._current_agent = result

            # Get updated model settings from new agent
            updated_settings = await self._get_updated_model_settings_from_agent(
                starting_settings=None,
                agent=self._current_agent,
            )

            # Send handoff event
            await self._put_event(
                RealtimeHandoffEvent(
                    from_agent=previous_agent,
                    to_agent=self._current_agent,
                    info=self._event_info,
                )
            )

            # First, send the session update so the model receives the new instructions
            await self._model.send_event(
                RealtimeModelSendSessionUpdate(session_settings=updated_settings)
            )

            # Then send tool output to complete the handoff (this triggers a new response)
            transfer_message = handoff.get_transfer_message(result)
            await self._model.send_event(
                RealtimeModelSendToolOutput(
                    tool_call=event,
                    output=transfer_message,
                    start_response=True,
                )
            )
        else:
            raise ModelBehaviorError(f"Tool {event.name} not found")

    @classmethod
    def _get_new_history(
        cls,
        old_history: list[RealtimeItem],
        event: RealtimeModelInputAudioTranscriptionCompletedEvent | RealtimeItem,
    ) -> list[RealtimeItem]:
        # Merge transcript into placeholder input_audio message.
        if isinstance(event, RealtimeModelInputAudioTranscriptionCompletedEvent):
            new_history: list[RealtimeItem] = []
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
                else:
                    new_history.append(item)
            return new_history

        # Otherwise it's just a new item
        # TODO (rm) Add support for audio storage config

        # If the item already exists, update it
        existing_index = next(
            (i for i, item in enumerate(old_history) if item.item_id == event.item_id), None
        )
        if existing_index is not None:
            new_history = old_history.copy()
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

    async def _run_output_guardrails(self, text: str) -> bool:
        """Run output guardrails on the given text. Returns True if any guardrail was triggered."""
        combined_guardrails = self._current_agent.output_guardrails + self._run_config.get(
            "output_guardrails", []
        )
        seen_ids: set[int] = set()
        output_guardrails = []
        for guardrail in combined_guardrails:
            guardrail_id = id(guardrail)
            if guardrail_id not in seen_ids:
                output_guardrails.append(guardrail)
                seen_ids.add(guardrail_id)

        if not output_guardrails or self._interrupted_by_guardrail:
            return False

        triggered_results = []

        for guardrail in output_guardrails:
            try:
                result = await guardrail.run(
                    # TODO (rm) Remove this cast, it's wrong
                    self._context_wrapper,
                    cast(Agent[Any], self._current_agent),
                    text,
                )
                if result.output.tripwire_triggered:
                    triggered_results.append(result)
            except Exception:
                # Continue with other guardrails if one fails
                continue

        if triggered_results:
            # Mark as interrupted to prevent multiple interrupts
            self._interrupted_by_guardrail = True

            # Emit guardrail tripped event
            await self._put_event(
                RealtimeGuardrailTripped(
                    guardrail_results=triggered_results,
                    message=text,
                    info=self._event_info,
                )
            )

            # Interrupt the model
            await self._model.send_event(RealtimeModelSendInterrupt())

            # Send guardrail triggered message
            guardrail_names = [result.guardrail.get_name() for result in triggered_results]
            await self._model.send_event(
                RealtimeModelSendUserInput(
                    user_input=f"guardrail triggered: {', '.join(guardrail_names)}"
                )
            )

            return True

        return False

    def _enqueue_guardrail_task(self, text: str) -> None:
        # Runs the guardrails in a separate task to avoid blocking the main loop

        task = asyncio.create_task(self._run_output_guardrails(text))
        self._guardrail_tasks.add(task)

        # Add callback to remove completed tasks and handle exceptions
        task.add_done_callback(self._on_guardrail_task_done)

    def _on_guardrail_task_done(self, task: asyncio.Task[Any]) -> None:
        """Handle completion of a guardrail task."""
        # Remove from tracking set
        self._guardrail_tasks.discard(task)

        # Check for exceptions and propagate as events
        if not task.cancelled():
            exception = task.exception()
            if exception:
                # Create an exception event instead of raising
                asyncio.create_task(
                    self._put_event(
                        RealtimeError(
                            info=self._event_info,
                            error={"message": f"Guardrail task failed: {str(exception)}"},
                        )
                    )
                )

    def _cleanup_guardrail_tasks(self) -> None:
        for task in self._guardrail_tasks:
            if not task.done():
                task.cancel()
        self._guardrail_tasks.clear()

    async def _cleanup(self) -> None:
        """Clean up all resources and mark session as closed."""
        # Cancel and cleanup guardrail tasks
        self._cleanup_guardrail_tasks()

        # Remove ourselves as a listener
        self._model.remove_listener(self)

        # Close the model connection
        await self._model.close()

        # Mark as closed
        self._closed = True

    async def _get_updated_model_settings_from_agent(
        self,
        starting_settings: RealtimeSessionModelSettings | None,
        agent: RealtimeAgent,
    ) -> RealtimeSessionModelSettings:
        # Start with run config model settings as base
        run_config_settings = self._run_config.get("model_settings", {})
        updated_settings: RealtimeSessionModelSettings = run_config_settings.copy()
        # Apply starting settings (from model config) next
        if starting_settings:
            updated_settings.update(starting_settings)

        instructions, tools, handoffs = await asyncio.gather(
            agent.get_system_prompt(self._context_wrapper),
            agent.get_all_tools(self._context_wrapper),
            self._get_handoffs(agent, self._context_wrapper),
        )
        updated_settings["instructions"] = instructions or ""
        updated_settings["tools"] = tools or []
        updated_settings["handoffs"] = handoffs or []

        disable_tracing = self._run_config.get("tracing_disabled", False)
        if disable_tracing:
            updated_settings["tracing"] = None

        return updated_settings

    @classmethod
    async def _get_handoffs(
        cls, agent: RealtimeAgent[Any], context_wrapper: RunContextWrapper[Any]
    ) -> list[Handoff[Any, RealtimeAgent[Any]]]:
        handoffs: list[Handoff[Any, RealtimeAgent[Any]]] = []
        for handoff_item in agent.handoffs:
            if isinstance(handoff_item, Handoff):
                handoffs.append(handoff_item)
            elif isinstance(handoff_item, RealtimeAgent):
                handoffs.append(realtime_handoff(handoff_item))

        async def _check_handoff_enabled(handoff_obj: Handoff[Any, RealtimeAgent[Any]]) -> bool:
            attr = handoff_obj.is_enabled
            if isinstance(attr, bool):
                return attr
            res = attr(context_wrapper, agent)
            if inspect.isawaitable(res):
                return await res
            return res

        results = await asyncio.gather(*(_check_handoff_enabled(h) for h in handoffs))
        enabled = [h for h, ok in zip(handoffs, results) if ok]
        return enabled
