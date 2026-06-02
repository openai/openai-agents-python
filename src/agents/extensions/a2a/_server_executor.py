from __future__ import annotations

"""
A2A Server Agent — expose an OpenAI agent via the A2A protocol.

Implements the ``AgentExecutor`` interface from ``a2a-sdk`` so that any
OpenAI Agents SDK ``Agent`` can be served as a standard A2A endpoint.

Usage::

    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server import A2AServer
    from agents import Agent
    from agents.extensions.a2a import A2AServerAgent

    my_agent = Agent(name="Assistant", instructions="You are helpful.")

    executor = A2AServerAgent(agent=my_agent)
    handler = DefaultRequestHandler(executor=executor)
    server = A2AServer(handler=handler)

    await server.start(host="0.0.0.0", port=8080)
"""

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

from agents.logger import logger

from ._converter import (
    a2a_context_to_openai_input,
    openai_error_to_failed_task,
    openai_run_result_to_task,
    openai_stream_event_to_task_status,
)

if TYPE_CHECKING:
    from a2a.server.agent_execution.context import RequestContext  # type: ignore[import-untyped]
    from a2a.server.events.event_queue_v2 import EventQueue  # type: ignore[import-untyped]
    from a2a.types.a2a_pb2 import Task, TaskStatus  # type: ignore[import-untyped]

    from agents.agent import Agent
    from agents.run import RunConfig
    from agents.run_context import TContext


class A2AServerAgent:
    """Expose an OpenAI ``Agent`` as an A2A-compatible agent.

    Implements the ``AgentExecutor`` interface required by the ``a2a-sdk``
    server framework. The executor translates incoming A2A ``SendMessage``
    requests into ``Runner.run()`` calls on the wrapped OpenAI agent, and
    translates the results back into A2A task events.

    Parameters
    ----------
    agent:
        The OpenAI ``Agent`` to expose.
    run_config:
        Optional ``RunConfig`` applied to every ``Runner.run()`` invocation.
    max_turns:
        Maximum conversation turns per A2A task (defaults to 30).
    session_ttl_seconds:
        In-memory session entries are evicted after this many seconds of
        inactivity. Set to ``None`` to disable expiry. Default: 3600 (1 h).
    """

    def __init__(
        self,
        agent: Agent[TContext],
        *,
        run_config: RunConfig | None = None,
        max_turns: int | None = 30,
        session_ttl_seconds: float | None = 3600.0,
    ) -> None:
        # Attempt to inherit from AgentExecutor for protocol compliance,
        # but degrade gracefully when the ABC is not available at runtime.
        try:
            from a2a.server.agent_execution.agent_executor import AgentExecutor

            self.__class__ = type(
                self.__class__.__name__,
                (self.__class__, AgentExecutor),
                {},
            )
        except ImportError:
            pass

        self.agent = agent
        self.run_config = run_config
        self.max_turns = max_turns
        self._session_ttl = session_ttl_seconds

        # In-memory session store: context_id → (items, last_access_timestamp)
        self._sessions: dict[str, tuple[list[Any], float]] = {}
        # Running tasks for cancellation support: task_id → asyncio.Task
        self._running_tasks: dict[str, asyncio.Task[Any]] = {}

    # ------------------------------------------------------------------
    # AgentExecutor interface
    # ------------------------------------------------------------------

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Execute the agent for the given A2A request context.

        This method is called by the ``a2a-sdk`` server framework for each
        incoming ``SendMessage`` / ``SendStreamingMessage`` request.

        Args:
            context: The A2A request context containing the message and task.
            event_queue: Queue to publish ``Task``, ``TaskStatusUpdateEvent``,
                and ``TaskArtifactUpdateEvent`` messages.
        """
        task_id = context.task_id or f"oai-task-{uuid.uuid4().hex[:12]}"
        context_id = context.context_id

        # Register for cancellation support
        current_task = asyncio.current_task()
        if current_task is not None:
            self._running_tasks[task_id] = current_task

        try:
            # Retrieve or initialise the conversation session (with TTL eviction)
            input_items = self._get_session(context_id)

            # Append the current message
            new_items = a2a_context_to_openai_input(context)
            input_items.extend(new_items)

            from agents.run import Runner

            # Publish working status
            await self._publish_working(event_queue, task_id, context_id)

            if self.run_config is not None and getattr(
                self.run_config, "streaming_enabled", False
            ):
                # Streaming path
                streamed = Runner.run_streamed(
                    self.agent,
                    input=input_items,
                    max_turns=self.max_turns,
                    run_config=self.run_config,
                )
                async for event in streamed.stream_events():
                    status = openai_stream_event_to_task_status(
                        event, task_id=task_id, context_id=context_id
                    )
                    if status is not None:
                        await self._publish_status_update(
                            event_queue, task_id, status
                        )
                result = streamed
            else:
                # Non-streaming path
                result = await Runner.run(
                    self.agent,
                    input=input_items,
                    max_turns=self.max_turns,
                    run_config=self.run_config,
                )

            # Build the completed task
            task = openai_run_result_to_task(
                result,
                task_id=task_id,
                context_id=context_id,
            )

            # Persist conversation history for subsequent turns
            self._update_session(context_id, result)

            # Publish the completed task
            await self._publish_task(event_queue, task)

        except asyncio.CancelledError:
            # Task was cancelled by the framework
            failed_task = openai_error_to_failed_task(
                asyncio.CancelledError("Task was cancelled."),
                task_id=task_id,
                context_id=context_id,
            )
            await self._publish_task(event_queue, failed_task)
            raise

        except Exception as exc:
            logger.exception(
                "A2A executor failed for task %s: %s", task_id, exc
            )
            failed_task = openai_error_to_failed_task(
                exc, task_id=task_id, context_id=context_id
            )
            await self._publish_task(event_queue, failed_task)

        finally:
            self._running_tasks.pop(task_id, None)

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel an in-progress task.

        Args:
            context: The request context for the task to cancel.
            event_queue: Queue to publish the cancellation status.
        """
        task_id = context.task_id
        if task_id and task_id in self._running_tasks:
            self._running_tasks[task_id].cancel()
            del self._running_tasks[task_id]

        # Publish cancellation status
        from a2a.types.a2a_pb2 import Message, Part, TaskState, TaskStatus

        from google.protobuf.timestamp_pb2 import Timestamp

        timestamp = Timestamp()
        timestamp.GetCurrentTime()

        text_part = Part(text="Task cancelled by request.")
        text_part.media_type = "text/plain"

        status = TaskStatus(
            state=TaskState.TASK_STATE_CANCELED,
            message=Message(
                message_id=f"cancel-{uuid.uuid4().hex[:12]}",
                role=2,  # AGENT
                parts=[text_part],
            ),
            timestamp=timestamp,
        )

        await self._publish_status_update(event_queue, task_id or "", status)

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _get_session(self, context_id: str | None) -> list[Any]:
        """Retrieve persisted conversation items for a context with TTL eviction."""
        if not context_id:
            return []
        entry = self._sessions.get(context_id)
        if entry is None:
            return []

        items, last_access = entry
        if self._session_ttl is not None:
            if time.monotonic() - last_access > self._session_ttl:
                del self._sessions[context_id]
                return []
        # Update last-access timestamp
        self._sessions[context_id] = (items, time.monotonic())
        return list(items)

    def _update_session(
        self, context_id: str | None, result: Any
    ) -> None:
        """Persist new conversation items for future turns."""
        if not context_id or not self._session_ttl:
            return

        new_items = getattr(result, "new_items", [])
        if new_items:
            existing, _ = self._sessions.get(context_id, ([], time.monotonic()))
            existing.extend(new_items)
            self._sessions[context_id] = (existing, time.monotonic())

    # ------------------------------------------------------------------
    # Event publishing helpers
    # ------------------------------------------------------------------

    async def _publish_working(
        self,
        event_queue: EventQueue,
        task_id: str,
        context_id: str | None,
    ) -> None:
        """Publish a TASK_STATE_WORKING status update."""
        from a2a.types.a2a_pb2 import Message, Part, TaskState, TaskStatus

        from google.protobuf.timestamp_pb2 import Timestamp

        timestamp = Timestamp()
        timestamp.GetCurrentTime()

        text_part = Part(text="Agent is working on the task.")
        text_part.media_type = "text/plain"

        message = Message(
            message_id=f"working-{uuid.uuid4().hex[:12]}",
            role=2,
            parts=[text_part],
        )
        if context_id:
            message.context_id = context_id

        status = TaskStatus(
            state=TaskState.TASK_STATE_WORKING,
            message=message,
            timestamp=timestamp,
        )

        await self._publish_status_update(event_queue, task_id, status)

    async def _publish_status_update(
        self,
        event_queue: EventQueue,
        task_id: str,
        status: TaskStatus,
    ) -> None:
        """Enqueue a TaskStatusUpdateEvent."""
        from a2a.types.a2a_pb2 import TaskStatusUpdateEvent

        event = TaskStatusUpdateEvent(
            task_id=task_id,
            status=status,
        )
        await event_queue.enqueue_event(event)

    async def _publish_task(
        self, event_queue: EventQueue, task: Task
    ) -> None:
        """Enqueue a full Task object."""
        await event_queue.enqueue_event(task)
