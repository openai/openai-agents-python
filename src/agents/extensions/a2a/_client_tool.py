"""
A2A Client Tool — call external A2A agents from your OpenAI agent.

Wraps an A2A ``Client`` (or its configuration) as an OpenAI Agents SDK
``FunctionTool`` so that any A2A-compatible agent can be invoked like any
other tool in an agent's tool set.

Usage::

    from agents import Agent, Runner
    from agents.extensions.a2a import A2AClientTool

    research_agent = A2AClientTool.from_url(
        url="http://research-agent:8080",
        tool_name="research_agent",
        tool_description="Ask the research agent to find and summarize information.",
    )

    orchestrator = Agent(
        name="Orchestrator",
        tools=[research_agent],
    )
    result = await Runner.run(orchestrator, "Research quantum computing")
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from typing import TYPE_CHECKING, Any

from agents.exceptions import ModelBehaviorError
from agents.logger import logger
from agents.run_context import RunContextWrapper
from agents.tool import (
    FunctionTool,
    _build_handled_function_tool_error_handler,
    _build_wrapped_function_tool,
    _parse_function_tool_json_input,
)

if TYPE_CHECKING:
    from a2a.client.client import (
        Client as A2AClient,
        ClientConfig,
        ClientCallContext,
    )
    from a2a.types.a2a_pb2 import (  # type: ignore[import-untyped]
        AgentCard,
        Message,
        Part,
        SendMessageRequest,
        Task,
    )

# ---------------------------------------------------------------------------
# Default schema for the tool parameters
# ---------------------------------------------------------------------------

_A2A_TOOL_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": (
                "The message to send to the external agent. "
                "Be clear and specific about what you need."
            ),
        },
        "context_id": {
            "type": "string",
            "description": (
                "Optional. An existing conversation context ID to continue "
                "a previous conversation with this agent."
            ),
        },
    },
    "required": ["message"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# A2AClientTool
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class A2AClientTool:
    """Wraps an A2A remote agent as an OpenAI Agents SDK ``FunctionTool``.

    The tool can be constructed from an existing ``AgentCard`` (when you
    already have the card), or from a URL (the card is fetched at
    construction time via ``.well-known/agent-card.json``).

    Once constructed, pass it directly to ``Agent(tools=[...])``.

    Parameters
    ----------
    tool_name:
        The name exposed to the LLM for this tool.
    tool_description:
        A human-readable description helping the LLM decide when to call
        the external agent.
    agent_card:
        An A2A ``AgentCard`` describing the remote agent. Either
        ``agent_card`` or ``agent_card_url`` must be provided.
    agent_card_url:
        The base URL of the remote A2A agent. The ``AgentCard`` will be
        fetched from ``<url>/.well-known/agent-card.json``. Either
        ``agent_card`` or ``agent_card_url`` must be provided.
    client_config:
        Optional A2A ``ClientConfig``; a sensible default is used when
        omitted.
    timeout_seconds:
        Maximum time (in seconds) to wait for the remote agent to
        complete. Defaults to 300 (5 minutes). Set to ``None`` to
        disable.
    failure_error_function:
        Optional formatter for tool-level errors.
    """

    tool_name: str
    tool_description: str
    agent_card: AgentCard | None = None
    agent_card_url: str | None = None
    client_config: ClientConfig | None = None
    timeout_seconds: float | None = 300.0
    failure_error_function: Any = None  # ToolErrorFunction | None
    _client: A2AClient | None = dataclasses.field(default=None, repr=False)
    _httpx_client: Any = dataclasses.field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.agent_card is None and self.agent_card_url is None:
            raise ValueError(
                "Either 'agent_card' or 'agent_card_url' must be provided."
            )

    # ------------------------------------------------------------------
    # Factory constructors
    # ------------------------------------------------------------------

    @classmethod
    async def from_url(
        cls,
        *,
        url: str,
        tool_name: str,
        tool_description: str,
        tool_params_schema: dict[str, Any] | None = None,
        client_config: ClientConfig | None = None,
        timeout_seconds: float | None = 300.0,
    ) -> A2AClientTool:
        """Asynchronously create an ``A2AClientTool`` by fetching the
        ``AgentCard`` from the remote agent's well-known URL.

        Args:
            url: Base URL of the remote A2A agent.
            tool_name: Tool name exposed to the LLM.
            tool_description: Tool description for the LLM.
            client_config: Optional A2A ``ClientConfig``.
            timeout_seconds: Per-invocation timeout in seconds.

        Returns:
            A ready-to-use ``A2AClientTool`` instance.
        """
        from a2a.client.card_resolver import A2ACardResolver

        config = client_config
        if config is None:
            from a2a.client.client import ClientConfig

            config = ClientConfig()

        httpx_client = getattr(config, "httpx_client", None)
        own_httpx_client = httpx_client is None
        if own_httpx_client:
            import httpx

            httpx_client = httpx.AsyncClient()

        resolver = A2ACardResolver(httpx_client, url)
        card = await resolver.get_agent_card()

        instance = cls(
            tool_name=tool_name,
            tool_description=tool_description,
            agent_card=card,
            agent_card_url=url,
            client_config=config,
            timeout_seconds=timeout_seconds,
        )
        instance._client = await instance._get_or_create_client()
        if own_httpx_client:
            instance._httpx_client = httpx_client
        return instance

    @classmethod
    def from_card(
        cls,
        *,
        card: AgentCard,
        tool_name: str,
        tool_description: str,
        client_config: ClientConfig | None = None,
        timeout_seconds: float | None = 300.0,
    ) -> A2AClientTool:
        """Synchronously create an ``A2AClientTool`` from an existing
        ``AgentCard``.

        The underlying A2A ``Client`` is lazily created on first use.

        Args:
            card: An A2A ``AgentCard`` describing the remote agent.
            tool_name: Tool name exposed to the LLM.
            tool_description: Tool description for the LLM.
            client_config: Optional A2A ``ClientConfig``.
            timeout_seconds: Per-invocation timeout in seconds.

        Returns:
            A ready-to-use ``A2AClientTool`` instance.
        """
        return cls(
            tool_name=tool_name,
            tool_description=tool_description,
            agent_card=card,
            client_config=client_config,
            timeout_seconds=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # FunctionTool conversion — the core integration point
    # ------------------------------------------------------------------

    def as_function_tool(self) -> FunctionTool:
        """Return a ``FunctionTool`` that can be added to an ``Agent.tools``.

        The generated tool has a single ``message`` parameter (the text to
        send to the remote agent).
        """
        async def _invoke(ctx: RunContextWrapper[Any], input_json: str) -> Any:
            return await self._invoke_impl(ctx, input_json)

        tool = _build_wrapped_function_tool(
            name=self.tool_name,
            description=self.tool_description,
            params_json_schema=_A2A_TOOL_PARAMS_SCHEMA,
            invoke_tool_impl=_invoke,
            on_handled_error=_build_handled_function_tool_error_handler(
                span_message=f"Error calling A2A agent '{self.tool_name}'",
                span_message_for_json_decode_error="Error parsing arguments "
                f"for A2A agent '{self.tool_name}'",
                log_label="A2A",
            ),
            failure_error_function=self.failure_error_function,
            strict_json_schema=True,
        )
        return tool

    async def close(self) -> None:
        """Release resources held by this tool (client connections, etc.)."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                logger.debug("Error closing A2A client for '%s'", self.tool_name)
            self._client = None
        if self._httpx_client is not None:
            try:
                await self._httpx_client.aclose()
            except Exception:
                logger.debug("Error closing httpx client for '%s'", self.tool_name)
            self._httpx_client = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get_or_create_client(self) -> A2AClient:
        """Create (or return a cached) A2A ``Client``."""
        if self._client is not None:
            return self._client

        from a2a.client.client_factory import ClientFactory

        card = self.agent_card
        if card is None:
            raise RuntimeError("AgentCard not available — cannot create client.")

        factory = ClientFactory(self.client_config)
        self._client = factory.create(card)
        return self._client

    async def _invoke_impl(
        self, ctx: RunContextWrapper[Any], input_json: str
    ) -> str:
        """Execute the A2A call when the tool is invoked by the LLM."""
        from a2a.client.client import ClientCallContext
        from a2a.types.a2a_pb2 import Message, Part, SendMessageConfiguration, SendMessageRequest

        json_data = _parse_function_tool_json_input(
            tool_name=self.tool_name, input_json=input_json
        )

        user_text = json_data.get("message", input_json)
        context_id = json_data.get("context_id")

        # Build the A2A SendMessage request
        text_part = Part(text=str(user_text))
        text_part.media_type = "text/plain"

        message = Message(
            message_id=f"oai-msg-{uuid.uuid4().hex[:12]}",
            role=1,  # USER
            parts=[text_part],
        )
        if context_id:
            message.context_id = str(context_id)

        config = SendMessageConfiguration(
            accepted_output_modes=["text"],
        )

        request = SendMessageRequest(
            message=message,
            configuration=config,
        )

        # Send to the remote A2A agent
        client = await self._get_or_create_client()
        call_context = ClientCallContext()

        try:
            task = await self._send_and_wait(
                client, request, call_context, self.timeout_seconds
            )
        except ModelBehaviorError:
            raise
        except asyncio.TimeoutError:
            raise ModelBehaviorError(
                f"A2A agent '{self.tool_name}' timed out after "
                f"{self.timeout_seconds} seconds."
            )
        except Exception as exc:
            logger.warning(
                "A2A agent '%s' call failed: %s", self.tool_name, exc
            )
            raise ModelBehaviorError(
                f"A2A agent '{self.tool_name}' returned an error: {exc}"
            ) from exc

        # Extract text from the completed task
        return self._extract_task_result(task)

    async def _send_and_wait(
        self,
        client: A2AClient,
        request: SendMessageRequest,
        call_context: ClientCallContext,
        timeout_seconds: float | None,
    ) -> Task:
        """Send a message to the A2A agent and wait for the task to complete.

        Uses streaming if the client supports it; otherwise falls back to
        polling ``get_task``.
        """
        from a2a.types.a2a_pb2 import TaskState

        task: Task | None = None
        task_id: str | None = None

        timeout = timeout_seconds

        async def _consume() -> None:
            nonlocal task, task_id
            # send_message returns an AsyncIterator[StreamResponse]
            async for response in client.send_message(
                request, context=call_context
            ):
                kind = response.WhichOneof("response")
                if kind == "task":
                    task = response.task
                    task_id = task.id
                    if task.status.state in _TERMINAL_STATES:
                        return
                elif kind == "task_status_update_event":
                    ev = response.task_status_update_event
                    task_id = ev.task_id
                    if ev.status.state in _TERMINAL_STATES:
                        # Fetch the complete task to get artifacts
                        break

            # If we have a task_id but the task wasn't fully populated
            # by streaming, fetch it.
            if task_id and (
                task is None
                or task.status.state not in _TERMINAL_STATES
            ):
                from a2a.types.a2a_pb2 import GetTaskRequest

                task = await client.get_task(
                    GetTaskRequest(id=task_id), context=call_context
                )

        try:
            await asyncio.wait_for(_consume(), timeout=timeout)
        except asyncio.TimeoutError:
            # Try to cancel the remote task
            if task_id:
                from a2a.types.a2a_pb2 import CancelTaskRequest

                try:
                    await asyncio.wait_for(
                        client.cancel_task(
                            CancelTaskRequest(id=task_id),
                            context=call_context,
                        ),
                        timeout=10.0,
                    )
                except Exception:
                    logger.debug(
                        "Failed to cancel A2A task %s after timeout", task_id
                    )
            raise

        if task is None:
            raise ModelBehaviorError(
                f"A2A agent '{self.tool_name}' returned no task."
            )

        if task.status.state == TaskState.TASK_STATE_FAILED:
            error_text = self._extract_message_text(task.status.message)
            raise ModelBehaviorError(
                f"A2A agent '{self.tool_name}' task failed: {error_text}"
            )

        if task.status.state == TaskState.TASK_STATE_CANCELED:
            raise ModelBehaviorError(
                f"A2A agent '{self.tool_name}' task was canceled."
            )

        # TASK_STATE_REJECTED is terminal — treat as a tool error
        if task.status.state == TaskState.TASK_STATE_REJECTED:
            error_text = self._extract_message_text(task.status.message)
            raise ModelBehaviorError(
                f"A2A agent '{self.tool_name}' task was rejected: {error_text}"
            )

        return task

    def _extract_task_result(self, task: Task) -> str:
        """Extract a text result from a completed A2A Task."""
        parts: list[str] = []

        # 1. Extract from artifacts
        for artifact in task.artifacts:
            for part in artifact.parts:
                text = self._part_to_text(part)
                if text:
                    artifact_label = (
                        f"[{artifact.name}] " if artifact.name else ""
                    )
                    parts.append(f"{artifact_label}{text}")

        # 2. Extract from the last agent message in history
        if not parts:
            for msg in reversed(list(task.history)):
                if msg.role == 2:  # AGENT
                    for part in msg.parts:
                        text = self._part_to_text(part)
                        if text:
                            parts.append(text)
                    if parts:
                        break

        if parts:
            return "\n\n".join(parts)

        # 3. Fall back to status message
        return self._extract_message_text(task.status.message)

    @staticmethod
    def _part_to_text(part: Part) -> str:
        """Extract text content from an A2A Part."""
        kind = part.WhichOneof("content")
        if kind == "text":
            return part.text
        if kind == "url":
            return f"[URL: {part.url}]"
        if kind == "data":
            try:
                from google.protobuf import json_format

                return json_format.MessageToJson(part.data)
            except Exception:
                return str(part.data)
        return ""

    @staticmethod
    def _extract_message_text(message: Message | None) -> str:
        """Extract all text from a status/response Message."""
        if message is None:
            return ""
        texts: list[str] = []
        for part in message.parts:
            if part.WhichOneof("content") == "text":
                texts.append(part.text)
        return "\n".join(texts)


# ---------------------------------------------------------------------------
# Terminal states
# ---------------------------------------------------------------------------

_TERMINAL_STATES: frozenset[int] = frozenset({
    3,  # TASK_STATE_COMPLETED
    4,  # TASK_STATE_FAILED
    5,  # TASK_STATE_CANCELED
    7,  # TASK_STATE_REJECTED
})
