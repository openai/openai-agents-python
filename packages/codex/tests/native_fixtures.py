"""Independent synthetic Responses protocol peer for actual native execution."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer


def assistant_output(text: str) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": f"msg_{uuid4().hex}",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def text_response(item_id: str, *chunks: str) -> list[dict[str, Any]]:
    finished = assistant_output("".join(chunks))
    finished["item"]["id"] = item_id
    return [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "id": item_id, "role": "assistant", "content": []},
        },
        *[
            {
                "type": "response.output_text.delta",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": chunk,
            }
            for chunk in chunks
        ],
        finished,
    ]


def function_call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


@dataclass
class ResponsesFixture:
    """Only the model is simulated; native HTTP, tools, events and storage run."""

    replies: deque[list[dict[str, Any]]] = field(default_factory=deque)
    requests: list[dict[str, Any]] = field(default_factory=list)
    request_headers: list[dict[str, str]] = field(default_factory=list)
    url: str = ""
    delta_gate: asyncio.Event | None = None
    before_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    progress_while_before_event: bool = False
    finite_http_response: bool = False

    def respond(self, *events: dict[str, Any]) -> None:
        self.replies.append(list(events))

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(await request.json())
        self.request_headers.append({key.lower(): value for key, value in request.headers.items()})
        if not self.replies:
            raise web.HTTPInternalServerError(text="Unexpected model request")
        response_id = f"resp_{len(self.requests)}"
        events = [
            {"type": "response.created", "response": {"id": response_id}},
            *self.replies.popleft(),
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "usage_metadata": {
                        "amount": "0.0007",
                        "metadata": {"fixture_response": response_id},
                    },
                    "usage": {
                        "input_tokens": 11,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 3,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": 14,
                        "fixture_marker": response_id,
                    },
                },
            },
        ]
        if self.finite_http_response:
            if self.before_event is not None or self.delta_gate is not None:
                raise ValueError("Finite HTTP responses cannot pause between events")
            body = bytearray()
            for event in events:
                body.extend(f"data: {json.dumps(event)}\n\n".encode())
                if event["type"] == "response.failed":
                    break
            # Content-Length makes HTTP completion coincide with the final SSE bytes.
            return web.Response(body=bytes(body), content_type="text/event-stream")
        # This is the same minimal event envelope used by Codex's core tests.
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for event in events:
            if self.before_event is not None:
                if self.progress_while_before_event:
                    pending = asyncio.ensure_future(self.before_event(event))
                    try:
                        while True:
                            done, _ = await asyncio.wait({pending}, timeout=1)
                            if done:
                                await pending
                                break
                            progress = {
                                "type": "response.in_progress",
                                "response": {"id": response_id},
                            }
                            await response.write(f"data: {json.dumps(progress)}\n\n".encode())
                    finally:
                        if not pending.done():
                            pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                else:
                    await self.before_event(event)
            await response.write(f"data: {json.dumps(event)}\n\n".encode())
            if event["type"] == "response.failed":
                break
            if event["type"] == "response.output_text.delta" and self.delta_gate is not None:
                await self.delta_gate.wait()
        await response.write_eof()
        return response


@pytest_asyncio.fixture(loop_scope="function")
async def responses():
    fixture = ResponsesFixture()
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    async with TestServer(app) as server:
        fixture.url = str(server.make_url("/v1"))
        yield fixture


@pytest.fixture
def core_owner() -> None:
    """Core owns every public Session. Kept for tests that instrument Core resources."""


def native_options(tmp_path: Path, responses: ResponsesFixture) -> dict[str, Any]:
    home = tmp_path / "codex-home"
    work = tmp_path / "work"
    home.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    return {
        "config": {
            "model": "gpt-5.1-codex",
            "model_provider": "fixture",
            "model_providers.fixture": {
                "name": "Local protocol fixture",
                "base_url": responses.url,
                "wire_api": "responses",
                "requires_openai_auth": False,
                "supports_websockets": False,
                "request_max_retries": 0,
                "stream_max_retries": 0,
                "stream_idle_timeout_ms": 5000,
            },
            "approval_policy": "never",
            "sandbox_mode": "danger-full-access",
            "web_search": "disabled",
            "analytics.enabled": False,
            "features.shell_snapshot": False,
            "features.code_mode": False,
        },
        "runtime": {
            "codex_home": str(home),
            "cwd": str(work),
            "strict_config": True,
            "loader": {
                "managed_config_path": str(home / "managed.toml"),
                "system_config_path": str(home / "system.toml"),
                "system_requirements_path": str(home / "requirements.toml"),
                "ignore_user_config": True,
                "ignore_project_config": True,
            },
        },
    }


def tool_output(request: dict[str, Any], call_id: str, kind: str = "function_call_output") -> str:
    outputs = [
        item["output"]
        for item in request["input"]
        if item.get("type") == kind and item.get("call_id") == call_id
    ]
    assert len(outputs) == 1
    output = outputs[0]
    if isinstance(output, str):
        return output
    return "".join(item.get("text", "") for item in output)


@asynccontextmanager
async def native_engine(options, *, checkpoint=False):
    """Acquire the actual binding directly for lower-level lifetime tests."""
    from agents import _native

    create = _native.create_checkpoint_engine if checkpoint else _native.create_engine
    engine = await create(json.dumps(options["config"]), json.dumps(options["runtime"]))
    try:
        yield engine
    finally:
        await engine.close()
