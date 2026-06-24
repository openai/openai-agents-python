"""Serve an :class:`~agents.agent.Agent` over HTTP.

``AgentServer`` wraps an agent in a FastAPI application with invoke, streaming,
and thread (session) endpoints, so going from a local agent to a deployable
service does not require hand-writing the web layer.

Usage::

    from agents import Agent
    from agents.extensions.memory import SQLiteSession  # or any Session
    from agents.extensions.server import AgentServer

    agent = Agent(name="Assistant", instructions="You are helpful.")
    server = AgentServer(agent, session_factory=lambda tid: SQLiteSession(tid))
    server.run(port=8000)  # requires the `uvicorn` package
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from pydantic_core import to_jsonable_python

from agents.agent import Agent
from agents.items import ItemHelpers, MessageOutputItem
from agents.memory.session import Session
from agents.run import Runner
from agents.stream_events import StreamEvent

from ._optional_imports import raise_optional_dependency_error

try:
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError as _e:
    raise_optional_dependency_error(
        "AgentServer",
        dependency_name="fastapi",
        extra_name="server",
        cause=_e,
    )

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _event_to_dict(event: StreamEvent) -> dict[str, Any] | None:
    """Serialize a stream event into a small JSON-friendly dict (or skip it)."""
    if event.type == "raw_response_event":
        data = event.data
        if getattr(data, "type", None) == "response.output_text.delta":
            return {"type": "text_delta", "delta": getattr(data, "delta", "")}
        return None
    if event.type == "run_item_stream_event":
        item = event.item
        payload: dict[str, Any] = {"type": event.name, "item_type": getattr(item, "type", None)}
        if event.name == "message_output_created" and isinstance(item, MessageOutputItem):
            payload["text"] = ItemHelpers.text_message_output(item)
        return payload
    if event.type == "agent_updated_stream_event":
        return {"type": "agent_updated", "agent": event.new_agent.name}
    return None


def _serialize_output(output: Any) -> Any:
    """JSON-compatible view of a final output.

    Preserves structured outputs (primitives, lists/dicts, Pydantic models) as
    JSON data rather than stringifying them, using a JSON-mode encoder so values
    like datetimes serialize correctly. Falls back to ``str`` only for values
    that are not JSON-serializable.
    """
    if isinstance(output, str):
        return output
    try:
        return to_jsonable_python(output)
    except Exception:
        return str(output)


class AgentServer:
    """Wrap an agent as a FastAPI app with invoke / stream / thread endpoints."""

    def __init__(
        self,
        agent: Agent[Any],
        *,
        session_factory: Callable[[str], Session] | None = None,
        api_key: str | None = None,
        max_turns: int | None = None,
        title: str | None = None,
    ) -> None:
        """Initialize the server.

        Args:
            agent: The agent to serve.
            session_factory: Builds a [`Session`][agents.memory.session.Session]
                for a given ``thread_id``. When omitted, requests run statelessly
                and the thread endpoints are disabled.
            api_key: If set, requests must present it via an ``Authorization:
                Bearer`` or ``X-API-Key`` header.
            max_turns: Optional cap on agent turns per request. ``None`` uses the
                SDK default.
            title: FastAPI app title. Defaults to the agent name.
        """
        self.agent = agent
        self._session_factory = session_factory
        self._api_key = api_key
        self._max_turns = max_turns
        self._sessions: dict[str, Session] = {}
        self.app = self._build_app(title or f"AgentServer · {agent.name}")

    # -- Internals ---------------------------------------------------------

    def _run_kwargs(self) -> dict[str, Any]:
        return {} if self._max_turns is None else {"max_turns": self._max_turns}

    def _require_auth(self, request: Request) -> None:
        if self._api_key is None:
            return
        header = request.headers.get("authorization", "")
        presented = header[len("Bearer ") :] if header.startswith("Bearer ") else None
        presented = presented or request.headers.get("x-api-key")
        if presented != self._api_key:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    def _get_session(self, thread_id: str) -> Session:
        if self._session_factory is None:
            raise HTTPException(status_code=400, detail="Threads are not enabled on this server")
        session = self._sessions.get(thread_id)
        if session is None:
            session = self._session_factory(thread_id)
            self._sessions[thread_id] = session
        return session

    def _resolve_thread(self, body: dict[str, Any]) -> tuple[str | None, Session | None]:
        """Resolve a request's ``thread_id`` to a session, rejecting bad types.

        A missing ``thread_id`` runs statelessly. A non-string ``thread_id`` is a
        client error (422) rather than being silently dropped to a stateless run,
        which would otherwise echo the id and mislead the caller into thinking the
        turn was persisted.
        """
        thread_id = body.get("thread_id")
        if thread_id is None:
            return None, None
        if not isinstance(thread_id, str):
            raise HTTPException(status_code=422, detail="'thread_id' must be a string")
        return thread_id, self._get_session(thread_id)

    # -- App ---------------------------------------------------------------

    def _build_app(self, title: str) -> FastAPI:
        app = FastAPI(title=title)

        @app.get("/health")
        async def _health() -> Response:
            return JSONResponse({"status": "ok", "agent": self.agent.name})

        @app.post("/invoke")
        async def _invoke(request: Request) -> Response:
            self._require_auth(request)
            body = await self._json_body(request)
            user_input = body.get("input")
            if not isinstance(user_input, str):
                raise HTTPException(status_code=422, detail="'input' must be a string")
            thread_id, session = self._resolve_thread(body)
            result = await Runner.run(self.agent, user_input, session=session, **self._run_kwargs())
            return JSONResponse(
                {"output": _serialize_output(result.final_output), "thread_id": thread_id}
            )

        @app.post("/stream")
        async def _stream(request: Request) -> Response:
            self._require_auth(request)
            body = await self._json_body(request)
            user_input = body.get("input")
            if not isinstance(user_input, str):
                raise HTTPException(status_code=422, detail="'input' must be a string")
            thread_id, session = self._resolve_thread(body)

            async def event_stream() -> AsyncIterator[str]:
                run = Runner.run_streamed(
                    self.agent, user_input, session=session, **self._run_kwargs()
                )
                async for event in run.stream_events():
                    payload = _event_to_dict(event)
                    if payload is not None:
                        yield f"data: {json.dumps(payload)}\n\n"
                done = {"type": "done", "output": _serialize_output(run.final_output)}
                yield f"data: {json.dumps(done)}\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        @app.get("/threads/{thread_id}")
        async def _get_thread(thread_id: str, request: Request) -> Response:
            self._require_auth(request)
            session = self._get_session(thread_id)
            items = await session.get_items()
            return JSONResponse({"thread_id": thread_id, "items": items})

        @app.delete("/threads/{thread_id}")
        async def _delete_thread(thread_id: str, request: Request) -> Response:
            self._require_auth(request)
            session = self._get_session(thread_id)
            await session.clear_session()
            self._sessions.pop(thread_id, None)
            return JSONResponse({"thread_id": thread_id, "status": "cleared"})

        return app

    async def _json_body(self, request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from e
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="Request body must be a JSON object")
        return body

    def run(self, *, host: str = "127.0.0.1", port: int = 8000) -> None:
        """Serve the app with uvicorn (requires the ``uvicorn`` package).

        Refuses to bind a non-loopback host when no ``api_key`` is configured, to
        avoid accidentally exposing an unauthenticated agent on the network.
        """
        if self._api_key is None and host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"Refusing to bind unauthenticated AgentServer to non-loopback host {host!r}. "
                "Set api_key=... or bind to a loopback host."
            )
        try:
            import uvicorn
        except ImportError as e:
            raise_optional_dependency_error(
                "AgentServer.run",
                dependency_name="uvicorn",
                extra_name="server",
                cause=e,
            )
        uvicorn.run(self.app, host=host, port=port)
