"""Call a remote A2A agent over HTTP.

``A2AClient`` fetches a peer's Agent Card and sends or streams messages using
the JSON-RPC surface. :meth:`A2AClient.as_tool` wraps the client as a
``FunctionTool`` so a local agent can delegate to a remote A2A peer, mirroring
the agents-as-tools pattern.

Usage::

    from agents.extensions.a2a import A2AClient

    async with A2AClient("http://localhost:8000/") as client:
        card = await client.get_agent_card()
        result = await client.send_message("What is the capital of France?")
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from agents.tool import FunctionTool, function_tool

from ._optional_imports import raise_optional_dependency_error
from .models import (
    AgentCard,
    JsonRpcRequest,
    Message,
    MessageSendParams,
    Task,
    TextPart,
    text_from_message,
)

try:
    import httpx
except ImportError as _e:
    raise_optional_dependency_error(
        "A2AClient",
        dependency_name="httpx",
        extra_name="a2a",
        cause=_e,
    )


class A2AError(Exception):
    """Raised when a remote A2A peer returns a JSON-RPC error."""


def _result_text(result: Task | Message) -> str:
    """Best-effort extraction of the agent's text from a send result."""
    if isinstance(result, Message):
        return text_from_message(result)
    if result.status.message is not None:
        text = text_from_message(result.status.message)
        if text:
            return text
    for artifact in result.artifacts:
        text = "".join(part.text for part in artifact.parts if isinstance(part, TextPart))
        if text:
            return text
    return ""


class A2AClient:
    """An async client for a remote A2A agent."""

    def __init__(
        self,
        base_url: str,
        *,
        httpx_client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        """Initialize the client.

        Args:
            base_url: The root URL the remote agent is served from.
            httpx_client: An existing client to reuse. If omitted, one is
                created and closed by this client.
            headers: Extra headers sent with every request (e.g. auth).
            timeout: Per-request timeout in seconds (used only when creating an
                internal client).
        """
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._headers = headers or {}
        self._client = httpx_client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = httpx_client is None

    def _well_known_url(self) -> str:
        return self.base_url + ".well-known/agent-card.json"

    def _rpc_url(self) -> str:
        return self.base_url

    def _coerce(self, message: str | Message, context_id: str | None) -> Message:
        if isinstance(message, str):
            return Message(role="user", parts=[TextPart(text=message)], context_id=context_id)
        if context_id is not None:
            message.context_id = context_id
        return message

    def _rpc_body(
        self, method: str, params: dict[str, Any], request_id: str | int
    ) -> dict[str, Any]:
        req = JsonRpcRequest(id=request_id, method=method, params=params)
        return req.model_dump(by_alias=True, exclude_none=True)

    def _parse_result(self, payload: dict[str, Any]) -> Task | Message:
        if "error" in payload and payload["error"] is not None:
            error = payload["error"]
            raise A2AError(f"{error.get('code')}: {error.get('message')}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise A2AError("Malformed A2A response: missing result")
        if result.get("kind") == "message":
            return Message.model_validate(result)
        return Task.model_validate(result)

    async def get_agent_card(self) -> AgentCard:
        """Fetch the remote agent's public card.

        Falls back to the legacy ``/.well-known/agent.json`` path on a 404 so
        0.2.x peers that only publish discovery there remain reachable.
        """
        resp = await self._client.get(self._well_known_url(), headers=self._headers)
        if resp.status_code == 404:
            legacy_url = self.base_url + ".well-known/agent.json"
            resp = await self._client.get(legacy_url, headers=self._headers)
        resp.raise_for_status()
        return AgentCard.model_validate(resp.json())

    async def send_message(
        self,
        message: str | Message,
        *,
        context_id: str | None = None,
        request_id: str | int = 1,
    ) -> Task | Message:
        """Send a message and return the resulting task (or message)."""
        msg = self._coerce(message, context_id)
        params = MessageSendParams(message=msg).model_dump(by_alias=True, exclude_none=True)
        body = self._rpc_body("message/send", params, request_id)
        resp = await self._client.post(self._rpc_url(), json=body, headers=self._headers)
        resp.raise_for_status()
        return self._parse_result(resp.json())

    async def stream_message(
        self,
        message: str | Message,
        *,
        context_id: str | None = None,
        request_id: str | int = 1,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream message updates, yielding each A2A result object."""
        msg = self._coerce(message, context_id)
        params = MessageSendParams(message=msg).model_dump(by_alias=True, exclude_none=True)
        body = self._rpc_body("message/stream", params, request_id)
        async with self._client.stream(
            "POST", self._rpc_url(), json=body, headers=self._headers
        ) as resp:
            resp.raise_for_status()
            # A peer that doesn't support streaming may answer with a plain
            # JSON-RPC body (HTTP 200) instead of an event stream. Surface its
            # error / result rather than silently dropping the non-SSE lines.
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                payload = json.loads(await resp.aread())
                if payload.get("error"):
                    error = payload["error"]
                    raise A2AError(f"{error.get('code')}: {error.get('message')}")
                result = payload.get("result")
                if isinstance(result, dict):
                    yield result
                return
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[len("data:") :].strip())
                if payload.get("error"):
                    error = payload["error"]
                    raise A2AError(f"{error.get('code')}: {error.get('message')}")
                result = payload.get("result")
                if isinstance(result, dict):
                    yield result

    async def get_task(self, task_id: str, *, request_id: str | int = 1) -> Task:
        """Fetch a previously created task by id."""
        body = self._rpc_body("tasks/get", {"id": task_id}, request_id)
        resp = await self._client.post(self._rpc_url(), json=body, headers=self._headers)
        resp.raise_for_status()
        result = self._parse_result(resp.json())
        if not isinstance(result, Task):
            raise A2AError("tasks/get did not return a task")
        return result

    def as_tool(
        self,
        *,
        tool_name: str = "call_remote_agent",
        tool_description: str | None = None,
    ) -> FunctionTool:
        """Expose this remote agent as a ``FunctionTool`` for a local agent."""
        client = self

        async def call_remote_agent(input: str) -> str:
            result = await client.send_message(input)
            return _result_text(result)

        return function_tool(
            call_remote_agent,
            name_override=tool_name,
            description_override=(
                tool_description
                or "Delegate a request to a remote A2A agent and return its text response."
            ),
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it was created internally."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> A2AClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
