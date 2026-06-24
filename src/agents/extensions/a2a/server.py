"""Publish an :class:`~agents.agent.Agent` as a spec-aligned A2A endpoint.

``A2AServer`` wraps an agent in a FastAPI application that serves the public
Agent Card and a JSON-RPC 2.0 surface (``message/send``, ``message/stream``,
``tasks/get``, ``tasks/cancel``) so peers from other frameworks can call it.

Usage::

    from agents import Agent
    from agents.extensions.a2a import A2AServer

    agent = Agent(name="Assistant", instructions="You are helpful.")
    server = A2AServer(agent, url="http://localhost:8000/")
    server.run(port=8000)  # requires the `uvicorn` package
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from agents.agent import Agent
from agents.items import ItemHelpers
from agents.run import Runner
from agents.stream_events import StreamEvent

from ._optional_imports import raise_optional_dependency_error
from .models import (
    A2A_TASK_NOT_FOUND,
    JSON_RPC_INVALID_REQUEST,
    JSON_RPC_METHOD_NOT_FOUND,
    JSON_RPC_PARSE_ERROR,
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    Artifact,
    JsonRpcError,
    JsonRpcErrorResponse,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    Message,
    MessageSendParams,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
    _new_id,
    message_from_text,
    text_from_message,
)

try:
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError as _e:
    raise_optional_dependency_error(
        "A2AServer",
        dependency_name="fastapi",
        extra_name="a2a",
        cause=_e,
    )

if TYPE_CHECKING:
    from pydantic import BaseModel


def _delta_text(event: StreamEvent) -> str | None:
    """Return the incremental output text from a streaming event, if any."""
    if getattr(event, "type", None) != "raw_response_event":
        return None
    data = getattr(event, "data", None)
    if getattr(data, "type", None) == "response.output_text.delta":
        return getattr(data, "delta", None)
    return None


class A2AServer:
    """Wrap an agent as a FastAPI app that speaks the A2A protocol."""

    def __init__(
        self,
        agent: Agent[Any],
        *,
        name: str | None = None,
        description: str | None = None,
        version: str = "0.1.0",
        url: str = "http://localhost:8000/",
        provider: AgentProvider | None = None,
        max_turns: int | None = None,
    ) -> None:
        """Initialize the server.

        Args:
            agent: The agent to expose.
            name: Public name for the agent card. Defaults to ``agent.name``.
            description: Public description. Defaults to the agent instructions
                when they are a plain string, else a generated description.
            version: Version string advertised in the agent card.
            url: The public URL this agent is served from (used in the card).
            provider: Optional publisher metadata.
            max_turns: Optional cap on agent turns per request. ``None`` uses
                the SDK default.
        """
        self.agent = agent
        self._name = name
        self._description = description
        self._version = version
        self._url = url
        self._provider = provider
        self._max_turns = max_turns
        self._tasks: dict[str, Task] = {}
        self.agent_card = self._build_agent_card()
        self.app = self._build_app()

    def _build_agent_card(self) -> AgentCard:
        instructions = self.agent.instructions
        description = self._description or (
            instructions
            if isinstance(instructions, str)
            else f"A2A endpoint for agent '{self.agent.name}'."
        )
        skill = AgentSkill(
            id=self.agent.name,
            name=self.agent.name,
            description=description,
        )
        return AgentCard(
            name=self._name or self.agent.name,
            description=description,
            version=self._version,
            url=self._url,
            capabilities=AgentCapabilities(streaming=True),
            skills=[skill],
            provider=self._provider,
        )

    def _run_kwargs(self) -> dict[str, Any]:
        return {} if self._max_turns is None else {"max_turns": self._max_turns}

    # -- HTTP app ----------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title=f"A2A · {self.agent.name}")

        @app.get("/.well-known/agent-card.json")
        async def _agent_card() -> Response:
            return JSONResponse(self.agent_card.model_dump(by_alias=True, exclude_none=True))

        # Older A2A clients look for the card at this legacy path.
        @app.get("/.well-known/agent.json")
        async def _agent_card_legacy() -> Response:
            return JSONResponse(self.agent_card.model_dump(by_alias=True, exclude_none=True))

        @app.post("/")
        async def _rpc(request: Request) -> Response:
            return await self._handle_rpc(request)

        return app

    async def _handle_rpc(self, request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return self._error(None, JSON_RPC_PARSE_ERROR, "Parse error")

        req_id = body.get("id") if isinstance(body, dict) else None
        try:
            req = JsonRpcRequest.model_validate(body)
        except Exception as e:
            return self._error(req_id, JSON_RPC_INVALID_REQUEST, "Invalid request", str(e))

        if req.method in ("message/send", "SendMessage"):
            return await self._on_message_send(req)
        if req.method in ("message/stream", "SendStreamingMessage"):
            return await self._on_message_stream(req)
        if req.method in ("tasks/get", "GetTask"):
            return self._on_tasks_get(req)
        if req.method in ("tasks/cancel", "CancelTask"):
            return self._on_tasks_cancel(req)
        return self._error(req.id, JSON_RPC_METHOD_NOT_FOUND, f"Method not found: {req.method}")

    # -- Method handlers ---------------------------------------------------

    async def _on_message_send(self, req: JsonRpcRequest) -> Response:
        try:
            params = MessageSendParams.model_validate(req.params or {})
        except Exception as e:
            return self._error(req.id, JSON_RPC_INVALID_REQUEST, "Invalid params", str(e))

        input_text = text_from_message(params.message)
        context_id = params.message.context_id or _new_id()
        try:
            result = await Runner.run(self.agent, input_text, **self._run_kwargs())
        except Exception as e:
            task = self._failed_task(context_id, params.message, str(e))
            self._tasks[task.id] = task
            return self._success(req.id, task)

        agent_text = ItemHelpers.text_message_outputs(result.new_items) or str(result.final_output)
        agent_msg = message_from_text(agent_text, role="agent")
        agent_msg.context_id = context_id
        task = Task(
            context_id=context_id,
            status=TaskStatus(state=TaskState.COMPLETED, message=agent_msg),
            artifacts=[Artifact(name="response", parts=[TextPart(text=agent_text)])],
            history=[params.message, agent_msg],
        )
        agent_msg.task_id = task.id
        self._tasks[task.id] = task
        return self._success(req.id, task)

    async def _on_message_stream(self, req: JsonRpcRequest) -> Response:
        try:
            params = MessageSendParams.model_validate(req.params or {})
        except Exception as e:
            return self._error(req.id, JSON_RPC_INVALID_REQUEST, "Invalid params", str(e))

        async def event_stream() -> AsyncIterator[str]:
            context_id = params.message.context_id or _new_id()
            task = Task(
                context_id=context_id,
                status=TaskStatus(state=TaskState.SUBMITTED),
                history=[params.message],
            )
            self._tasks[task.id] = task
            artifact_id = _new_id()

            yield self._sse(req.id, task)
            yield self._sse(
                req.id,
                TaskStatusUpdateEvent(
                    task_id=task.id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.WORKING),
                ),
            )

            collected = ""
            try:
                run = Runner.run_streamed(
                    self.agent, text_from_message(params.message), **self._run_kwargs()
                )
                async for event in run.stream_events():
                    # A concurrent tasks/cancel flips the shared task status; stop the
                    # in-flight run instead of completing it.
                    if task.status.state == TaskState.CANCELED:
                        run.cancel()
                        break
                    delta = _delta_text(event)
                    if not delta:
                        continue
                    collected += delta
                    yield self._sse(
                        req.id,
                        TaskArtifactUpdateEvent(
                            task_id=task.id,
                            context_id=context_id,
                            artifact=Artifact(
                                artifact_id=artifact_id,
                                name="response",
                                parts=[TextPart(text=delta)],
                            ),
                            append=True,
                        ),
                    )
                if task.status.state == TaskState.CANCELED:
                    yield self._sse(
                        req.id,
                        TaskStatusUpdateEvent(
                            task_id=task.id,
                            context_id=context_id,
                            status=task.status,
                            final=True,
                        ),
                    )
                    return
                # Mirror the non-streaming fallback: a run that ends on a tool result
                # has no message deltas, so fall back to the final output.
                final_text = (
                    collected
                    or ItemHelpers.text_message_outputs(run.new_items)
                    or str(run.final_output)
                )
            except Exception as e:
                task.status = TaskStatus(state=TaskState.FAILED, message=message_from_text(str(e)))
                yield self._sse(
                    req.id,
                    TaskStatusUpdateEvent(
                        task_id=task.id,
                        context_id=context_id,
                        status=task.status,
                        final=True,
                    ),
                )
                return

            if not collected and final_text:
                yield self._sse(
                    req.id,
                    TaskArtifactUpdateEvent(
                        task_id=task.id,
                        context_id=context_id,
                        artifact=Artifact(
                            artifact_id=artifact_id,
                            name="response",
                            parts=[TextPart(text=final_text)],
                        ),
                        last_chunk=True,
                    ),
                )

            agent_msg = message_from_text(final_text, role="agent")
            agent_msg.context_id = context_id
            agent_msg.task_id = task.id
            task.status = TaskStatus(state=TaskState.COMPLETED, message=agent_msg)
            task.history.append(agent_msg)
            yield self._sse(
                req.id,
                TaskStatusUpdateEvent(
                    task_id=task.id,
                    context_id=context_id,
                    status=task.status,
                    final=True,
                ),
            )

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    def _on_tasks_get(self, req: JsonRpcRequest) -> Response:
        task_id = (req.params or {}).get("id")
        task = self._tasks.get(task_id) if isinstance(task_id, str) else None
        if task is None:
            return self._error(req.id, A2A_TASK_NOT_FOUND, "Task not found")
        return self._success(req.id, task)

    def _on_tasks_cancel(self, req: JsonRpcRequest) -> Response:
        task_id = (req.params or {}).get("id")
        task = self._tasks.get(task_id) if isinstance(task_id, str) else None
        if task is None:
            return self._error(req.id, A2A_TASK_NOT_FOUND, "Task not found")
        task.status = TaskStatus(state=TaskState.CANCELED)
        return self._success(req.id, task)

    # -- Helpers -----------------------------------------------------------

    def _failed_task(self, context_id: str, user_message: Message, error: str) -> Task:
        return Task(
            context_id=context_id,
            status=TaskStatus(state=TaskState.FAILED, message=message_from_text(error)),
            history=[user_message],
        )

    def _success(self, req_id: str | int | None, result: BaseModel) -> Response:
        payload = JsonRpcSuccessResponse(
            id=req_id,
            result=result.model_dump(by_alias=True, exclude_none=True),
        )
        return JSONResponse(payload.model_dump(by_alias=True, exclude_none=True))

    def _error(
        self,
        req_id: str | int | None,
        code: int,
        message: str,
        data: Any | None = None,
    ) -> Response:
        payload = JsonRpcErrorResponse(
            id=req_id,
            error=JsonRpcError(code=code, message=message, data=data),
        )
        return JSONResponse(payload.model_dump(by_alias=True, exclude_none=True))

    def _sse(self, req_id: str | int | None, result: BaseModel) -> str:
        payload = JsonRpcSuccessResponse(
            id=req_id,
            result=result.model_dump(by_alias=True, exclude_none=True),
        )
        return f"data: {payload.model_dump_json(by_alias=True, exclude_none=True)}\n\n"

    def run(self, *, host: str = "127.0.0.1", port: int = 8000) -> None:
        """Serve the app with uvicorn (requires the ``uvicorn`` package)."""
        try:
            import uvicorn
        except ImportError as e:
            raise_optional_dependency_error(
                "A2AServer.run",
                dependency_name="uvicorn",
                extra_name="a2a",
                cause=e,
            )
        uvicorn.run(self.app, host=host, port=port)
