from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from typing_extensions import assert_never

from ..handoffs import Handoff
from ..run_context import RunContextWrapper, TContext
from ..tool import FunctionTool
from ..tool_context import ToolContext
from .agent import RealtimeAgent
from .config import RealtimeUserInput
from .events import (
    RealtimeAgentEndEvent,
    RealtimeAgentStartEvent,
    RealtimeAudio,
    RealtimeAudioEnd,
    RealtimeAudioInterrupted,
    RealtimeError,
    RealtimeEventInfo,
    RealtimeHistoryAdded,
    RealtimeHistoryUpdated,
    RealtimeRawModelEvent,
    RealtimeSessionEvent,
    RealtimeToolEnd,
    RealtimeToolStart,
)
from .items import InputAudio, InputText, RealtimeItem
from .model import RealtimeModel, RealtimeModelConfig, RealtimeModelListener
from .model_events import (
    RealtimeModelEvent,
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelToolCallEvent,
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
    ) -> None:
        """Initialize the session.

        Args:
            model: The model to use.
            agent: The current agent.
            context_wrapper: The context wrapper.
            event_info: Event info object.
            history: The conversation history.
            model_config: Model configuration.
        """
        self._model = model
        self._current_agent = agent
        self._context_wrapper = RunContextWrapper(context)
        self._event_info = RealtimeEventInfo(context=self._context_wrapper)
        self._history: list[RealtimeItem] = []
        self._model_config = model_config or {}
        self._event_queue: asyncio.Queue[RealtimeSessionEvent] = asyncio.Queue()
        self._closed = False

    async def __aenter__(self) -> RealtimeSession:
        """Start the session by connecting to the model. After this, you will be able to stream
        events from the model and send messages and audio to the model.
        """
        # Add ourselves as a listener
        self._model.add_listener(self)

        # Connect to the model
        await self._model.connect(self._model_config)

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
                event = await self._event_queue.get()
                yield event
            except asyncio.CancelledError:
                break

    async def close(self) -> None:
        """Close the session."""
        self._closed = True
        self._model.remove_listener(self)
        await self._model.close()

    async def send_message(self, message: RealtimeUserInput) -> None:
        """Send a message to the model."""
        await self._model.send_message(message)

    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None:
        """Send a raw audio chunk to the model."""
        await self._model.send_audio(audio, commit=commit)

    async def interrupt(self) -> None:
        """Interrupt the model."""
        await self._model.interrupt()

    async def on_event(self, event: RealtimeModelEvent) -> None:
        await self._put_event(RealtimeRawModelEvent(data=event, info=self._event_info))

        if event.type == "error":
            await self._put_event(RealtimeError(info=self._event_info, error=event.error))
        elif event.type == "function_call":
            await self._handle_tool_call(event)
        elif event.type == "audio":
            await self._put_event(RealtimeAudio(info=self._event_info, audio=event))
        elif event.type == "audio_interrupted":
            await self._put_event(RealtimeAudioInterrupted(info=self._event_info))
        elif event.type == "audio_done":
            await self._put_event(RealtimeAudioEnd(info=self._event_info))
        elif event.type == "conversation.item.input_audio_transcription.completed":
            self._history = RealtimeSession._get_new_history(self._history, event)
            await self._put_event(
                RealtimeHistoryUpdated(info=self._event_info, history=self._history)
            )
        elif event.type == "transcript_delta":
            # TODO (rm) Add guardrails
            pass
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
            await self._put_event(
                RealtimeAgentEndEvent(
                    agent=self._current_agent,
                    info=self._event_info,
                )
            )
        elif event.type == "other":
            pass
        else:
            assert_never(event)

    async def _put_event(self, event: RealtimeSessionEvent) -> None:
        """Put an event into the queue."""
        await self._event_queue.put(event)

    async def _handle_tool_call(self, event: RealtimeModelToolCallEvent) -> None:
        """Handle a tool call event."""
        all_tools = await self._current_agent.get_all_tools(self._context_wrapper)
        function_map = {tool.name: tool for tool in all_tools if isinstance(tool, FunctionTool)}
        handoff_map = {tool.name: tool for tool in all_tools if isinstance(tool, Handoff)}

        if event.name in function_map:
            await self._put_event(
                RealtimeToolStart(
                    info=self._event_info,
                    tool=function_map[event.name],
                    agent=self._current_agent,
                )
            )

            func_tool = function_map[event.name]
            tool_context = ToolContext.from_agent_context(self._context_wrapper, event.call_id)
            result = await func_tool.on_invoke_tool(tool_context, event.arguments)

            await self._model.send_tool_output(event, str(result), True)

            await self._put_event(
                RealtimeToolEnd(
                    info=self._event_info,
                    tool=func_tool,
                    output=result,
                    agent=self._current_agent,
                )
            )
        elif event.name in handoff_map:
            # TODO (rm) Add support for handoffs
            pass
        else:
            # TODO (rm) Add error handling
            pass

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
