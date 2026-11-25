"""Experimental per-attempt callbacks for an application-approved Responses HTTP client."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import httpx2
from openai import AsyncOpenAI, OpenAIError

from .exceptions import UserError
from .logger import logger
from .models.openai_provider import OpenAIProvider
from .models.openai_responses import OpenAIResponsesModel
from .run_internal.plugin_cancellation import check_cancelled

if TYPE_CHECKING:
    from .agent import Agent
    from .models.interface import Model

__all__ = ["HTTPHooks", "RequestEdit", "RequestInfo", "ResponseInfo", "responses_provider"]


class AbortError(UserError, OpenAIError):
    """The client skips OpenAIError retries. The Runner recognizes AbortError by name."""


Verbosity = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class RequestInfo:
    """Nonsecret fields for a single HTTP attempt, including retries."""

    attempt_id: str
    model: str
    stream: bool
    verbosity: Verbosity | None


@dataclass(frozen=True, kw_only=True)
class RequestEdit:
    """Optional replacements. None keeps the original field."""

    verbosity: Verbosity | None = None
    client_request_id: str | None = None


@dataclass(frozen=True)
class ResponseInfo:
    """The final request fields and HTTP status, before body consumption."""

    request: RequestInfo
    status: int


@dataclass(frozen=True, kw_only=True)
class HTTPHooks:
    """Require the application's dedicated HTTP route on each model turn."""

    on_request: (
        Callable[[RequestInfo], RequestEdit | None | Awaitable[RequestEdit | None]] | None
    ) = None
    on_response: Callable[[ResponseInfo], None | Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        for handler in (self.on_request, self.on_response):
            if handler is not None and not callable(handler):
                raise UserError("Invalid HTTP handler.")


def validate_http(agent: Agent[Any], model: Model) -> None:
    """Check the effective client even when another provider resolved the model."""
    client = model._client if type(model) is OpenAIResponsesModel else None
    http = client._client if type(client) is AsyncOpenAI else None
    transport = http._transport if type(http) is httpx2.AsyncClient else None
    if isinstance(transport, _Transport):
        if (
            agent is not transport.agent
            or client is not transport.client
            or type(model) is not OpenAIResponsesModel
            or model.model != transport.model
            or not transport.selection_matches()
        ):
            raise UserError(
                "Plugin HTTP requires the original approved agent, selection, model and client."
            )
    elif agent.plugins:
        for plugin in agent.plugins:
            if plugin.http is not None:
                raise UserError(
                    f"Plugin {plugin.name} requires the approved Responses HTTP client."
                )


def _encode(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


class _Transport(httpx2.AsyncHTTPTransport):
    def __init__(self, agent: Agent[Any], model: str, endpoint: httpx2.URL):
        super().__init__(trust_env=False)
        self.agent = agent
        self.model = model
        self.endpoint = endpoint
        self.plugins = tuple(agent.plugins)
        self.hooks = tuple(p.http for p in self.plugins if p.http is not None)
        self.client: AsyncOpenAI | None = None

    def selection_matches(self) -> bool:
        return tuple(map(id, self.plugins)) == tuple(map(id, self.agent.plugins))

    async def _prepare(self, request: httpx2.Request) -> tuple[httpx2.Request, RequestInfo]:
        headers = request.headers.copy()
        extensions = dict(request.extensions)
        if (
            request.method != "POST"
            or request.url != self.endpoint
            or headers.get_list("host") != [self.endpoint.netloc.decode()]
            or {"target", "sni_hostname"}.intersection(extensions)
            or headers.get("content-type") != "application/json"
            or "content-encoding" in headers
            or "transfer-encoding" in headers
            or self.client is None
            or headers.get_list("authorization") != [f"Bearer {self.client.api_key}"]
        ):
            raise ValueError
        content = request.content
        body = json.loads(content)
        stream = body.get("stream", False)
        verbosity = body.get("text", {}).get("verbosity")
        if (
            body.get("model") != self.model
            or type(stream) is not bool
            or verbosity not in (None, "low", "medium", "high")
        ):
            raise ValueError
        info = RequestInfo(uuid4().hex, self.model, stream, verbosity)
        for hook in self.hooks:
            if hook.on_request is None:
                continue
            result = hook.on_request(info)
            edit = await result if inspect.isawaitable(result) else result
            if edit is None:
                continue
            if type(edit) is not RequestEdit:
                raise ValueError
            if edit.verbosity is not None:
                if edit.verbosity not in ("low", "medium", "high"):
                    raise ValueError
                info = replace(info, verbosity=edit.verbosity)
            if edit.client_request_id is not None:
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", edit.client_request_id):
                    raise ValueError
                headers["x-client-request-id"] = edit.client_request_id
        if verbosity != info.verbosity:
            if _encode(body) != content or any(
                key in headers for key in ("content-digest", "repr-digest", "digest", "content-md5")
            ):
                raise ValueError
            body.setdefault("text", {})["verbosity"] = info.verbosity
            content = _encode(body)
            headers.pop("content-length", None)
        await check_cancelled()
        if self.client is None or not self.selection_matches():
            raise ValueError
        return httpx2.Request(
            "POST", self.endpoint, content=content, headers=headers, extensions=extensions
        ), info

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            prepared = await self._prepare(request)
        except Exception:
            prepared = None
        if prepared is None:
            # OpenAIError prevents client retries and leaves callback details out of the error.
            raise AbortError("Plugin HTTP request refused.")
        outgoing, info = prepared
        response = await super().handle_async_request(outgoing)
        try:
            event = ResponseInfo(info, response.status_code)
            for hook in self.hooks:
                if hook.on_response is None:
                    continue
                try:
                    value = hook.on_response(event)
                    if inspect.isawaitable(value):
                        await value
                except Exception:
                    logger.warning("Plugin HTTP response observer failed.")
        except BaseException:
            await response.aclose()
            raise
        return response


@asynccontextmanager
async def responses_provider(
    agent: Agent[Any], *, base_url: str, api_key: str, model: str
) -> AsyncIterator[OpenAIProvider]:
    """Approve one agent, plugin selection, model and destination for this client."""
    url = httpx2.URL(base_url.rstrip("/") + "/responses")
    if (
        not url.host
        or url.userinfo
        or "?" in base_url
        or "#" in base_url
        or not (
            url.scheme == "https" or (url.scheme == "http" and url.host in {"127.0.0.1", "::1"})
        )
    ):
        raise UserError("Use HTTPS or literal loopback without URL credentials, query or fragment.")
    for plugin in agent.plugins:
        if plugin.http is not None and not isinstance(plugin.http, HTTPHooks):
            raise UserError(f"Plugin {plugin.name} requires HTTPHooks.")
    transport = _Transport(agent, model, url)
    async with httpx2.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False
    ) as http:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http)
        transport.client = client
        provider = OpenAIProvider(
            openai_client=client, use_responses=True, use_responses_websocket=False
        )
        try:
            yield provider
        finally:
            transport.client = None
            await provider.aclose()
