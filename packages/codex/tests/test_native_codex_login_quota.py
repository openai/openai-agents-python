"""Quota and throttling outcomes from the saved-login Responses endpoint."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeError, NativeProvider, TurnNeedsAttention
from agents.rate_limits import ApiRateLimits
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_codex_login import (
    ACCOUNT,
    codex_agent,
    saved_login,
    uncompressed_model_requests as uncompressed_model_requests,
)


@pytest.mark.parametrize(
    ("error", "status", "reason", "category"),
    [
        (
            {"type": "usage_limit_reached", "plan_type": "pro", "resets_at": 2_000_000_000},
            429,
            "quota_blocked",
            "usage_limit_exceeded",
        ),
        (
            {"type": "rate_limit_error", "code": "rate_limit_exceeded"},
            429,
            "rate_limited",
            "rate_limit_exceeded",
        ),
        (
            {"type": "rate_limit_error", "code": "slow_down"},
            503,
            "rate_limited",
            "rate_limit_exceeded",
        ),
        (
            {"type": "insufficient_quota", "code": "insufficient_quota"},
            None,
            "quota_blocked",
            "usage_limit_exceeded",
        ),
        (
            {"type": "rate_limit_error", "code": "rate_limit_exceeded"},
            None,
            "rate_limited",
            "rate_limit_exceeded",
        ),
    ],
    ids=["plan-quota", "request-throttle", "slow-down", "stream-quota", "stream-throttle"],
)
async def test_saved_login_limit_returns_attention_without_replaying_the_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: dict[str, str | int],
    status: int | None,
    reason: str,
    category: str,
) -> None:
    peer = ResponsesFixture()
    rejected_headers: list[tuple[str, str]] = []
    stream_release = asyncio.Event()
    accept = False
    streamed = status is None

    async def handle(request: web.Request) -> web.StreamResponse:
        if accept:
            return await peer.handle(request)
        await request.read()
        rejected_headers.append(
            (request.headers["Authorization"], request.headers["ChatGPT-Account-Id"])
        )
        rejection = {**error, "message": "Synthetic provider rejection"}
        if status is None:
            payload = {
                "type": "response.failed",
                "response": {
                    "id": f"resp_rejected_{len(rejected_headers)}",
                    "status": "failed",
                    "error": {
                        **rejection,
                        "headers": {"Retry-After": "Wed, 01 Jan 2020 00:00:00 GMT"},
                    },
                },
            }
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(f"data: {json.dumps(payload)}\n\n".encode())
            await stream_release.wait()
            return response
        return web.json_response(
            {"error": rejection},
            status=status,
            headers={"Retry-After": "3600"},
        )

    app = web.Application()
    app.router.add_post("/responses", handle)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    if streamed:
        native = importlib.import_module("agents._native")
        create_engine = native.create_engine

        def with_short_stream_timeout(config_json: str, *args: Any, **kwargs: Any) -> Any:
            config = json.loads(config_json)
            config[f"model_providers.{config['model_provider']}"]["stream_idle_timeout_ms"] = 1000
            return create_engine(json.dumps(config), *args, **kwargs)

        monkeypatch.setattr(native, "create_engine", with_short_stream_timeout)
    async with timeout(20), TestServer(app) as server:
        monkeypatch.setattr(
            "_agents_codex.session._CODEX_RESPONSES_BASE_URL", str(server.make_url("/"))
        )
        agent = codex_agent(saved_login(tmp_path / "login"))
        async with agent.session() as session:
            turn = await session.submit("Reply.")
            try:
                attention = await turn.wait_for_attention()
            finally:
                stream_release.set()
            assert isinstance(attention, TurnNeedsAttention)
            assert attention.reason == reason
            assert isinstance(attention.error, NativeError)
            assert attention.error.category == category
            retry_advice = (
                ApiRateLimits(retry_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
                if streamed
                else ApiRateLimits(retry_after_seconds=3600)
            )
            assert attention.rate_limits == session.rate_limits() == retry_advice
            assert rejected_headers == [("Bearer saved-token", ACCOUNT)]

            accept = True
            peer.respond(assistant_output("done"))
            next_turn = await session.submit("Reply again.")
            assert (await next_turn.get_result()).final_output == "done"
            assert len(peer.requests) == 1


async def test_api_key_can_retry_streamed_throttling(responses: ResponsesFixture) -> None:
    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "id": "resp_1",
                "status": "failed",
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "Synthetic provider rejection",
                    "headers": {"Retry-After": "0"},
                },
            },
        }
    )
    responses.respond(assistant_output("done"))
    provider = NativeProvider(
        api_key="synthetic-throttle-key", base_url=responses.url, supports_websockets=False
    )
    agent = Agent(instructions="Reply.", model="gpt-5.1-codex", provider=provider)
    try:
        async with timeout(20), agent.session() as session:
            turn = await session.submit("Reply.")
            assert (await turn.get_result()).final_output == "done"
            assert len(responses.requests) == 2
            assert session.rate_limits() == ApiRateLimits(retry_after_seconds=0)
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    ("saved", "code"),
    [(True, "server_is_overloaded"), (False, "slow_down")],
    ids=["saved-login-overload", "api-key-slow-down"],
)
async def test_http_503_retry_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, saved: bool, code: str
) -> None:
    peer = ResponsesFixture()
    peer.respond(assistant_output("recovered"))
    attempts = 0

    async def handle(request: web.Request) -> web.StreamResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await request.read()
            return web.json_response(
                {"error": {"code": code, "message": "Synthetic provider rejection"}},
                status=503,
                headers={"Retry-After": "0"},
            )
        return await peer.handle(request)

    app = web.Application()
    app.router.add_post("/responses", handle)
    async with timeout(20), TestServer(app) as server:
        endpoint = str(server.make_url("/"))
        if saved:
            monkeypatch.setattr("_agents_codex.session._CODEX_RESPONSES_BASE_URL", endpoint)
            agent = codex_agent(saved_login(tmp_path / "login"))
        else:
            provider = NativeProvider(
                api_key="synthetic-throttle-key", base_url=endpoint, supports_websockets=False
            )
            agent = Agent(instructions="Reply.", model="gpt-5.1-codex", provider=provider)
        assert agent.provider is not None
        async with agent.provider, agent.session() as session:
            turn = await session.submit("Reply.")
            assert (await turn.get_result()).final_output == "recovered"
            assert attempts == 2
