"""Bounded, non-streaming A2A 1.0 task calls. Requires the `a2a` extra."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

import httpx2
from a2a.types import a2a_pb2 as a2a
from google.protobuf.json_format import MessageToDict, ParseDict

__all__ = ["A2AError", "A2ATaskClient"]

_REQUEST_BYTES = 65_536
_RESPONSE_BYTES = 1_048_576


class A2AError(Exception):
    """A local call failure. A failed send may already have created a remote task."""

    def __init__(
        self,
        kind: Literal["request", "authorization", "timeout", "transport", "protocol", "remote"],
    ) -> None:
        self.kind = kind
        super().__init__(f"A2A {kind} error")


def _origin(value: str) -> tuple[str, str, int | None]:
    url = httpx2.URL(value)
    if (
        not url.host
        or url.userinfo
        or "?" in value
        or "#" in value
        or not (
            url.scheme == "https" or (url.scheme == "http" and url.host in {"127.0.0.1", "::1"})
        )
    ):
        raise ValueError("Use an approved HTTPS or loopback URL without credentials or parameters")

    return url.scheme, url.host, url.port


async def _fetch(url: str, token: Callable[[], str] | None, body: bytes | None = None) -> bytes:
    headers = {"Accept-Encoding": "identity", "A2A-Version": "1.0"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        try:
            bearer = token()
            if not bearer or not all(0x21 <= ord(c) <= 0x7E for c in bearer):
                raise ValueError
        except Exception:
            raise A2AError("authorization") from None
        headers["Authorization"] = f"Bearer {bearer}"

    # Keep cookies, redirects and ambient proxy settings out of both requests.
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=None) as client:
        async with client.stream(
            "GET" if body is None else "POST", url, content=body, headers=headers
        ) as response:
            if response.status_code in (401, 403):
                raise A2AError("authorization")
            if response.status_code != 200:
                raise A2AError("remote")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise A2AError("protocol")
            data = bytearray()
            async for chunk in response.aiter_raw():
                if len(data) + len(chunk) > _RESPONSE_BYTES:
                    raise A2AError("protocol")
                data.extend(chunk)
            return bytes(data)


@dataclass(frozen=True, kw_only=True)
class A2ATaskClient:
    """One trusted RPC URL and either stored card JSON or a trusted card URL.

    Token suppliers must be local, non-blocking and scoped to their respective URLs.
    Treat every field of the returned Task as private, untrusted peer data.
    """

    rpc_url: str
    card: bytes | None = field(default=None, repr=False)
    card_url: str | None = None
    rpc_token: Callable[[], str] | None = field(default=None, repr=False)
    card_token: Callable[[], str] | None = field(default=None, repr=False)
    timeout: float = 10.0

    def __post_init__(self) -> None:
        origin = _origin(self.rpc_url)
        if (self.card is None) == (self.card_url is None):
            raise ValueError("Provide exactly one of card and card_url")
        if self.card_url is not None:
            if _origin(self.card_url) != origin:
                raise ValueError("The card and RPC URLs must share an origin")
        elif self.card_token is not None:
            raise ValueError("card_token requires card_url")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be positive and finite")

    async def send_message(self, text: str, *, message_id: str) -> a2a.Task:
        """Send one user message. The caller owns the unique ID and any replay policy."""
        if not text or not message_id or len(text) + len(message_id) > _REQUEST_BYTES:
            raise A2AError("request")
        request = a2a.SendMessageRequest(
            message=a2a.Message(
                message_id=message_id, role=a2a.ROLE_USER, parts=[a2a.Part(text=text)]
            ),
            configuration=a2a.SendMessageConfiguration(
                accepted_output_modes=["text/plain"], history_length=0
            ),
        )
        return await self._call(request)

    async def refresh_task(self, task: a2a.Task) -> a2a.Task:
        """Read a task once using the same client and owner that received it."""
        task_id, context_id = task.id, task.context_id
        del task  # Keep the peer's artifacts and history out of failure frames.
        return await self.get_task(task_id=task_id, context_id=context_id)

    async def get_task(self, *, task_id: str, context_id: str) -> a2a.Task:
        """Read an existing task and require both identifiers to match."""
        if not task_id or not context_id or len(task_id) > _REQUEST_BYTES:
            raise A2AError("request")
        task = await self._call(a2a.GetTaskRequest(id=task_id, history_length=0))
        if task.id != task_id or task.context_id != context_id:
            del task
            raise A2AError("protocol")
        return task

    async def _call(self, request: a2a.SendMessageRequest | a2a.GetTaskRequest) -> a2a.Task:
        try:
            return await asyncio.wait_for(self._exchange(request), self.timeout)
        except asyncio.CancelledError as exc:
            raise exc.with_traceback(None) from None
        except asyncio.TimeoutError:
            failure = A2AError("timeout")
        except A2AError as exc:
            failure = A2AError(exc.kind)
        except httpx2.HTTPError:
            failure = A2AError("transport")
        except Exception:
            failure = A2AError("protocol")
        # Discard private dependency exceptions and their request/response frames.
        raise failure

    async def _exchange(self, request: a2a.SendMessageRequest | a2a.GetTaskRequest) -> a2a.Task:
        sending = isinstance(request, a2a.SendMessageRequest)
        rpc_id = uuid4().hex
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "SendMessage" if sending else "GetTask",
                "params": MessageToDict(request),
            },
            ensure_ascii=False,
        ).encode()
        if len(body) > _REQUEST_BYTES:
            raise A2AError("request")

        raw = self.card
        if raw is None:
            assert self.card_url is not None
            raw = await _fetch(self.card_url, self.card_token)
        if len(raw) > _RESPONSE_BYTES:
            raise A2AError("protocol")
        card = ParseDict(json.loads(raw), a2a.AgentCard())
        if not any(
            interface.url == self.rpc_url
            and interface.protocol_binding == "JSONRPC"
            and interface.protocol_version == "1.0"
            and not interface.tenant
            for interface in card.supported_interfaces
        ):
            raise A2AError("protocol")

        reply = json.loads(await _fetch(self.rpc_url, self.rpc_token, body))
        if reply.get("jsonrpc") != "2.0" or reply.get("id") != rpc_id:
            raise A2AError("protocol")
        if "error" in reply:
            raise A2AError("remote")
        result = reply["result"]
        task = (
            ParseDict(result, a2a.SendMessageResponse()).task
            if sending
            else ParseDict(result, a2a.Task())
        )
        if not task.id or not task.context_id or not task.status.state:
            raise A2AError("protocol")
        return task
