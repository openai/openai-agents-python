"""Registered Lite compaction through synthetic native HTTP and durable storage."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    LocalStorage,
    NativeError,
    TurnNeedsAttention,
    _native,
    function_tool,
)
from agents.compaction import CompactionContext
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call
from tests.test_native_codex_login import (
    uncompressed_model_requests as uncompressed_model_requests,
)
from tests.test_native_sign_in_with_chatgpt import (
    access_token,
    plan_agent,
    set_endpoint,
    synthetic_key as synthetic_key,
)

COMPACTION_MARKER = "You are performing a CONTEXT CHECKPOINT COMPACTION."
EFFECT = "synthetic-tool-effect-committed"


@pytest.mark.parametrize(
    ("phase", "code", "kind", "reason", "category"),
    [
        (
            "pre-turn",
            "subscription_sharing_usage_limit_exceeded",
            "usage_limit_reached",
            "quota_blocked",
            "usage_limit_exceeded",
        ),
        (
            "mid-turn",
            "rate_limit_exceeded",
            "rate_limit_error",
            "rate_limited",
            "rate_limit_exceeded",
        ),
    ],
    ids=["pre-turn-quota", "mid-turn-throttle"],
)
async def test_registered_auto_compaction_is_terminal_before_eof(
    monkeypatch, phase, code, kind, reason, category
):
    if phase == "pre-turn":
        create_engine = _native.create_engine

        def pre_turn_compaction(config_json: str, *args: Any, **kwargs: Any) -> Any:
            config = json.loads(config_json)
            # Seed the next turn above its threshold without post-turn compaction.
            config.update(
                model_auto_compact_token_limit=10_000,
                model_post_turn_compact_threshold_percent=0,
            )
            return create_engine(json.dumps(config), *args, **kwargs)

        monkeypatch.setattr(_native, "create_engine", pre_turn_compaction)

    token = access_token()
    effects: list[str] = []
    compactions: list[CompactionContext[None]] = []
    requests: list[tuple[str, str, bool, bool]] = []
    rejection_sent = asyncio.Event()
    release_eof = asyncio.Event()
    peer = ResponsesFixture()

    @function_tool
    def record_effect() -> str:
        """Record one synthetic application effect."""
        effects.append(EFFECT)
        return EFFECT

    async def high_usage(event):
        seed_request = 2 if phase == "pre-turn" else 1
        if event["type"] == "response.completed" and len(requests) == seed_request:
            event["response"]["usage"].update(
                input_tokens=260_000, output_tokens=1_000, total_tokens=261_000
            )

    peer.before_event = high_usage
    peer.respond(function_call("record_effect", {}, "effect-once"))
    if phase == "pre-turn":
        peer.respond(assistant_output("Seeded history."))

    async def handle(request: web.Request) -> web.StreamResponse:
        registered = (
            request.headers.get("Authorization") == f"Bearer {token}"
            and request.headers.get("x-openai-internal-codex-responses-lite") == "true"
            and all(
                header not in request.headers
                for header in ("ChatGPT-Account-Id", "OpenAI-Organization", "OpenAI-Project")
            )
        )
        route = request.method == "POST" and request.path == "/v1/responses"
        body = await request.json() if route else {}
        compact = COMPACTION_MARKER in json.dumps(body.get("input", []))
        requests.append((request.method, request.path, compact, registered))
        if not (route and registered):
            raise web.HTTPBadRequest(text="Unexpected route or auth")
        if not compact:
            return await peer.handle(request)

        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for event in (
            assistant_output("synthetic-rejected-summary"),
            {
                "type": "response.failed",
                "response": {
                    "id": "synthetic-compaction-rejected",
                    "status": "failed",
                    "error": {"code": code, "type": kind, "message": "Synthetic rejection"},
                },
            },
        ):
            await response.write(f"data: {json.dumps(event)}\n\n".encode())
        rejection_sent.set()
        await release_eof.wait()
        return response

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with timeout(25), TestServer(app, host="127.0.0.1") as server:
        set_endpoint(monkeypatch, str(server.make_url("/v1")))
        monkeypatch.setattr(
            "_agents_codex.session._CODEX_RESPONSES_BASE_URL", str(server.make_url("/saved-login"))
        )
        monkeypatch.setattr(
            "_agents_codex.session._CODEX_ACCOUNT_BASE_URL", str(server.make_url("/saved-account"))
        )
        monkeypatch.setenv("OPENAI_BASE_URL", str(server.make_url("/api-key")))
        agent = plan_agent(token, tools=[record_effect], before_compact=compactions.append)
        async with agent.session() as session:
            try:
                turn = await session.submit("Record one synthetic effect.")
                if phase == "pre-turn":
                    assert (await turn.get_result()).final_output == "Seeded history."
                    turn = await session.submit("Continue.")
                await rejection_sent.wait()
                # The peer cannot close until the caller observes the terminal result.
                async with timeout(5):
                    attention = await turn.wait_for_attention()
                    assert isinstance(attention, TurnNeedsAttention)
                    assert attention.reason == reason
                    assert isinstance(attention.error, NativeError)
                    assert attention.error.category == category
                    with pytest.raises(NativeError) as terminal:
                        await turn.get_result()
                assert not release_eof.is_set()
                assert terminal.value.category == category
                assert token not in str(terminal.value)
                [compaction] = compactions
                assert compaction.trigger == "auto"
                assert compaction.session_id == session.id
                assert compaction.turn_id == turn.turn_id
                assert EFFECT in json.dumps(compaction.items)
                context = json.dumps((await session.model_context()).items)
                saved = await session.save_state()
                assert EFFECT in context and EFFECT in saved.to_json()
                assert "synthetic-rejected-summary" not in context
                assert "synthetic-rejected-summary" not in saved.to_json()
                assert not any(row["type"] == "compacted" for row in saved.history)
            finally:
                release_eof.set()

    sampling_count = 2 if phase == "pre-turn" else 1
    assert requests == [("POST", "/v1/responses", False, True)] * sampling_count + [
        ("POST", "/v1/responses", True, True)
    ]
    assert effects == [EFFECT]


async def test_registered_auto_compaction_summary_survives_reopen(monkeypatch, tmp_path):
    token = access_token()
    summary = "synthetic-committed-auto-summary"
    effects: list[str] = []
    compactions: list[CompactionContext[None]] = []
    peer = ResponsesFixture()

    @function_tool
    def record_effect() -> str:
        """Record one synthetic application effect."""
        effects.append(EFFECT)
        return EFFECT

    async def high_usage(event):
        if event["type"] == "response.completed" and len(peer.requests) == 1:
            event["response"]["usage"].update(
                input_tokens=260_000, output_tokens=1_000, total_tokens=261_000
            )

    peer.before_event = high_usage
    peer.respond(function_call("record_effect", {}, "effect-once"))
    peer.respond(assistant_output(summary))
    peer.respond(assistant_output("Continued after compaction."))

    routes: list[str] = []

    async def handle(request):
        routes.append(f"{request.method} {request.path}")
        assert request.method == "POST" and request.path == "/v1/responses"
        assert request.headers.get("Authorization") == f"Bearer {token}"
        assert request.headers.get("x-openai-internal-codex-responses-lite") == "true"
        return await peer.handle(request)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with timeout(30), TestServer(app, host="127.0.0.1") as server:
        set_endpoint(monkeypatch, str(server.make_url("/v1")))
        agent = plan_agent(token, tools=[record_effect], before_compact=compactions.append)
        storage = LocalStorage(tmp_path / "auto-compaction")
        async with agent.session(storage=storage) as session:
            thread_id = session.id
            result = await (await session.submit("Record one synthetic effect.")).get_result()
            assert result.final_output == "Continued after compaction."
            assert summary in json.dumps((await session.model_context()).items)
            originals = (await session.history_snapshot()).items
            [summary_record] = [item for item in originals if summary in json.dumps(item)]

        async with agent.session(storage=storage, resume=thread_id) as reopened:
            assert summary in json.dumps((await reopened.model_context()).items)
            restored_originals = (await reopened.history_snapshot()).items
            assert [item for item in restored_originals if summary in json.dumps(item)] == [
                summary_record
            ]
            assert [
                item.get("output")
                for item in restored_originals
                if item.get("call_id") == "effect-once" and item["type"] == "function_call_output"
            ] == [EFFECT]

    assert routes == ["POST /v1/responses"] * 3
    assert [COMPACTION_MARKER in json.dumps(body["input"]) for body in peer.requests] == [
        False,
        True,
        False,
    ]
    assert summary in json.dumps(peer.requests[2]["input"])
    assert effects == [EFFECT]
    [compaction] = compactions
    assert compaction.trigger == "auto"
