"""Minimal realtime session implementation for voice agents."""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Awaitable
from typing import Any, Callable, Literal

from typing_extensions import TypeAlias, assert_never

from ..handoffs import Handoff
from ..run_context import RunContextWrapper
from ..tool import FunctionTool
from ..tool_context import ToolContext
from .agent import RealtimeAgent
from .config import APIKeyOrKeyFunc, RealtimeSessionConfig, RealtimeUserInput
from .events import (
    RealtimeAgentEndEvent,
    RealtimeAgentStartEvent,
    RealtimeAudio,
    RealtimeAudioEnd,
    RealtimeAudioInterrupted,
    RealtimeError,
    RealtimeEventInfo,
    RealtimeHandoffEvent,  # noqa: F401
    RealtimeHistoryAdded,
    RealtimeHistoryUpdated,
    RealtimeRawTransportEvent,
    RealtimeSessionEvent,
    RealtimeToolEnd,
    RealtimeToolStart,
)
from .items import InputAudio, InputText, RealtimeItem
from .openai_realtime import OpenAIRealtimeWebSocketTransport
from .transport import (
    RealtimeModelName,
    RealtimeSessionTransport,
    RealtimeTransportConnectionOptions,
    RealtimeTransportListener,
)
from .transport_events import (
    RealtimeTransportEvent,
    RealtimeTransportInputAudioTranscriptionCompletedEvent,
    RealtimeTransportToolCallEvent,
)


class RealtimeSessionListener(abc.ABC):
    """A listener for realtime session events."""

    @abc.abstractmethod
    async def on_event(self, event: RealtimeSessionEvent) -> None:
        """Called when an event is emitted by the realtime session."""
        pass


RealtimeSessionListenerFunc: TypeAlias = Callable[[RealtimeSessionEvent], Awaitable[None]]
"""A function that can be used as a listener for realtime session events."""


class _RealtimeFuncListener(RealtimeSessionListener):
    """A listener that wraps a function."""

    def __init__(self, func: RealtimeSessionListenerFunc) -> None:
        self._func = func

    async def on_event(self, event: RealtimeSessionEvent) -> None:
        """Call the wrapped function with the event."""
        await self._func(event)


class RealtimeSession(RealtimeTransportListener):
    """A `RealtimeSession` is the equivalent of `Runner` for realtime agents. It automatically
    handles multiple turns by maintaining a persistent connection with the underlying transport
    layer.

    The session manages the local history copy, executes tools, runs guardrails and facilitates
    handoffs between agents.

    Since this code runs on your server, it uses WebSockets by default. You can optionally create
    your own custom transport layer by implementing the `RealtimeSessionTransport` interface.
    """

    def __init__(
        self,
        starting_agent: RealtimeAgent,
        *,
        context: Any | None = None,
        transport: Literal["websocket"] | RealtimeSessionTransport = "websocket",
        api_key: APIKeyOrKeyFunc | None = None,
        model: RealtimeModelName | None = None,
        config: RealtimeSessionConfig | None = None,
        # TODO (rm) Add guardrail support
        # TODO (rm) Add tracing support
        # TODO (rm) Add history audio storage config
    ) -> None:
        """Initialize the realtime session.

        Args:
            starting_agent: The agent to start the session with.
            context: The context to use for the session.
            transport: The transport to use for the session. Defaults to using websockets.
            api_key: The API key to use for the session.
            model: The model to use. Must be a realtime model.
            config: Override parameters to use.
        """
        self._current_agent = starting_agent
        self._context_wrapper = RunContextWrapper(context)
        self._event_info = RealtimeEventInfo(context=self._context_wrapper)
        self._override_config = config
        self._history: list[RealtimeItem] = []
        self._model = model
        self._api_key = api_key

        self._listeners: list[RealtimeSessionListener] = []

        if transport == "websocket":
            self._transport: RealtimeSessionTransport = OpenAIRealtimeWebSocketTransport()
        else:
            self._transport = transport

    async def __aenter__(self) -> RealtimeSession:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.end()

    async def connect(self) -> None:
        """Start the session: connect to the model and start the connection."""
        self._transport.add_listener(self)

        config = await self.create_session_config(
            overrides=self._override_config,
        )

        options: RealtimeTransportConnectionOptions = {
            "initial_session_config": config,
        }

        if config.get("api_key") is not None:
            options["api_key"] = config["api_key"]
        elif self._api_key is not None:
            options["api_key"] = self._api_key

        if config.get("model") is not None:
            options["model"] = config["model"]
        elif self._model is not None:
            options["model"] = self._model

        await self._transport.connect(options)

        await self._emit_event(
            RealtimeHistoryUpdated(
                history=self._history,
                info=self._event_info,
            )
        )

    async def end(self) -> None:
        """End the session: disconnect from the model and close the connection."""
        pass

    def add_listener(self, listener: RealtimeSessionListener | RealtimeSessionListenerFunc) -> None:
        """Add a listener to the session."""
        if isinstance(listener, RealtimeSessionListener):
            self._listeners.append(listener)
        else:
            self._listeners.append(_RealtimeFuncListener(listener))

    def remove_listener(
        self, listener: RealtimeSessionListener | RealtimeSessionListenerFunc
    ) -> None:
        """Remove a listener from the session."""
        if isinstance(listener, RealtimeSessionListener):
            self._listeners.remove(listener)
        else:
            for x in self._listeners:
                if isinstance(x, _RealtimeFuncListener) and x._func == listener:
                    self._listeners.remove(x)
                    break

    async def create_session_config(
        self, overrides: RealtimeSessionConfig | None = None
    ) -> RealtimeSessionConfig:
        """Create the session config."""
        agent = self._current_agent
        instructions, tools = await asyncio.gather(
            agent.get_system_prompt(self._context_wrapper),
            agent.get_all_tools(self._context_wrapper),
        )
        config = RealtimeSessionConfig()

        if self._model is not None:
            config["model"] = self._model
        if instructions is not None:
            config["instructions"] = instructions
        if tools is not None:
            config["tools"] = [tool for tool in tools if isinstance(tool, FunctionTool)]

        if overrides:
            config.update(overrides)

        return config

    async def send_message(self, message: RealtimeUserInput) -> None:
        """Send a message to the model."""
        await self._transport.send_message(message)

    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None:
        """Send a raw audio chunk to the model."""
        await self._transport.send_audio(audio, commit=commit)

    async def interrupt(self) -> None:
        """Interrupt the model."""
        await self._transport.interrupt()

    async def on_event(self, event: RealtimeTransportEvent) -> None:
        """Called when an event is emitted by the realtime transport."""
        await self._emit_event(RealtimeRawTransportEvent(data=event, info=self._event_info))

        if event.type == "error":
            await self._emit_event(RealtimeError(info=self._event_info, error=event.error))
        elif event.type == "function_call":
            await self._handle_tool_call(event)
        elif event.type == "audio":
            await self._emit_event(RealtimeAudio(info=self._event_info, audio=event))
        elif event.type == "audio_interrupted":
            await self._emit_event(RealtimeAudioInterrupted(info=self._event_info))
        elif event.type == "audio_done":
            await self._emit_event(RealtimeAudioEnd(info=self._event_info))
        elif event.type == "conversation.item.input_audio_transcription.completed":
            self._history = self._get_new_history(self._history, event)
            await self._emit_event(
                RealtimeHistoryUpdated(info=self._event_info, history=self._history)
            )
        elif event.type == "transcript_delta":
            # TODO (rm) Add guardrails
            pass
        elif event.type == "item_updated":
            is_new = any(item.item_id == event.item.item_id for item in self._history)
            self._history = self._get_new_history(self._history, event.item)
            if is_new:
                new_item = next(
                    item for item in self._history if item.item_id == event.item.item_id
                )
                await self._emit_event(RealtimeHistoryAdded(info=self._event_info, item=new_item))
            else:
                await self._emit_event(
                    RealtimeHistoryUpdated(info=self._event_info, history=self._history)
                )
            pass
        elif event.type == "item_deleted":
            deleted_id = event.item_id
            self._history = [item for item in self._history if item.item_id != deleted_id]
            await self._emit_event(
                RealtimeHistoryUpdated(info=self._event_info, history=self._history)
            )
        elif event.type == "connection_status":
            pass
        elif event.type == "turn_started":
            await self._emit_event(
                RealtimeAgentStartEvent(
                    agent=self._current_agent,
                    info=self._event_info,
                )
            )
        elif event.type == "turn_ended":
            await self._emit_event(
                RealtimeAgentEndEvent(
                    agent=self._current_agent,
                    info=self._event_info,
                )
            )
        elif event.type == "other":
            pass
        else:
            assert_never(event)

    async def _emit_event(self, event: RealtimeSessionEvent) -> None:
        """Emit an event to the listeners."""
        await asyncio.gather(*[listener.on_event(event) for listener in self._listeners])

    async def _handle_tool_call(self, event: RealtimeTransportToolCallEvent) -> None:
        all_tools = await self._current_agent.get_all_tools(self._context_wrapper)
        function_map = {tool.name: tool for tool in all_tools if isinstance(tool, FunctionTool)}
        handoff_map = {tool.name: tool for tool in all_tools if isinstance(tool, Handoff)}

        if event.name in function_map:
            await self._emit_event(
                RealtimeToolStart(
                    info=self._event_info,
                    tool=function_map[event.name],
                    agent=self._current_agent,
                )
            )

            func_tool = function_map[event.name]
            tool_context = ToolContext.from_agent_context(self._context_wrapper, event.call_id)
            result = await func_tool.on_invoke_tool(tool_context, event.arguments)

            await self._transport.send_tool_output(event, str(result), True)

            await self._emit_event(
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

    def _get_new_history(
        self,
        old_history: list[RealtimeItem],
        event: RealtimeTransportInputAudioTranscriptionCompletedEvent | RealtimeItem,
    ) -> list[RealtimeItem]:
        # Merge transcript into placeholder input_audio message.
        if isinstance(event, RealtimeTransportInputAudioTranscriptionCompletedEvent):
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
        elif item.previous_item_id:
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
