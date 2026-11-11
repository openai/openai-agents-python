"""Registered Lite compaction through synthetic native HTTP and durable storage."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    CustomStorage,
    LocalStorage,
    NativeError,
    NativeProvider,
    SteeringReceipt,
    TurnNeedsAttention,
    _native,
    function_tool,
)
from agents.compaction import CompactionContext
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.native_storage_fixtures import MemorySessionStore
from tests.test_native_codex_login import (
    uncompressed_model_requests as uncompressed_model_requests,
)
from tests.test_native_sign_in_with_chatgpt import (
    MODEL,
    access_token,
    plan_agent,
    set_endpoint,
    synthetic_key as synthetic_key,
)

COMPACTION_MARKER = "You are performing a CONTEXT CHECKPOINT COMPACTION."
EFFECT = "synthetic-tool-effect-committed"


def set_compaction_thresholds(monkeypatch, *, post_turn_percent=0):
    create_engine = _native.create_engine

    def configure_compaction(config_json: str, *args: Any, **kwargs: Any) -> Any:
        config = json.loads(config_json)
        config.update(
            model_auto_compact_token_limit=10_000,
            model_post_turn_compact_threshold_percent=post_turn_percent,
        )
        return create_engine(json.dumps(config), *args, **kwargs)

    monkeypatch.setattr(_native, "create_engine", configure_compaction)


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
        set_compaction_thresholds(monkeypatch)

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


@pytest.mark.parametrize("mode", ["registered-auto", "registered-manual", "api-key-manual"])
async def test_compaction_context_overflow_retry_policy(monkeypatch, responses, mode):
    automatic = mode == "registered-auto"
    registered = mode != "api-key-manual"
    if automatic:
        set_compaction_thresholds(monkeypatch)
    token = access_token() if registered else "synthetic-compaction-api-key"
    original_input = "Preserve this original input."
    original_answer = "Seeded history."
    rejected_summary = "synthetic-rejected-overflow-summary"
    effects = []

    @function_tool
    def record_effect() -> str:
        """Record one synthetic application effect."""
        effects.append(EFFECT)
        return EFFECT

    async def high_usage(event):
        if event["type"] == "response.completed" and len(responses.requests) == 2:
            event["response"]["usage"].update(
                input_tokens=260_000, output_tokens=1_000, total_tokens=261_000
            )

    if automatic:
        responses.before_event = high_usage
    responses.respond(function_call("record_effect", {}, "effect-once"))
    responses.respond(assistant_output(original_answer))
    responses.respond(
        assistant_output(rejected_summary),
        {
            "type": "response.failed",
            "response": {
                "id": "resp_3",
                "status": "failed",
                "error": {
                    "code": "context_length_exceeded",
                    "type": "invalid_request_error",
                    "message": "Synthetic context window overflow.",
                },
            },
        },
    )
    retry_summary = "Synthetic successful retry summary."
    responses.respond(assistant_output(retry_summary))
    responses.respond(assistant_output("Unexpected continuation."))

    if registered:
        set_endpoint(monkeypatch, responses.url)
        agent = plan_agent(token, tools=[record_effect])
    else:
        agent = Agent(
            instructions="Follow the coding request.",
            model=MODEL,
            provider=NativeProvider(base_url=responses.url, api_key=token),
            tools=[record_effect],
        )
    async with timeout(25), agent.session() as session:
        seed = await session.submit(original_input)
        assert (await seed.get_result()).final_output == original_answer
        terminal = None
        try:
            if automatic:
                followup = await session.submit("Continue.")
                await followup.get_result()
            else:
                await session.compact()
        except NativeError as error:
            terminal = error

        expected_requests = [False, False, True] if registered else [False, False, True, True]
        assert [
            COMPACTION_MARKER in json.dumps(row["input"]) for row in responses.requests
        ] == expected_requests
        prompt = responses.requests[2]["input"]
        assert len(prompt) > 1
        assert original_input in json.dumps(prompt)
        assert original_answer in json.dumps(prompt)

        context = json.dumps((await session.model_context()).items)
        saved = await session.save_state()
        assert rejected_summary not in context
        assert rejected_summary not in saved.to_json()
        if registered:
            assert terminal is not None
            assert terminal.category == "context_length_exceeded"
            for value in (original_input, original_answer, EFFECT):
                assert value in context and value in saved.to_json()
            assert not any(row["type"] == "compacted" for row in saved.history)
        else:
            assert terminal is None
            assert retry_summary in context
            assert any(row["type"] == "compacted" for row in saved.history)

    assert all(
        headers.get("authorization") == f"Bearer {token}" for headers in responses.request_headers
    )
    if registered:
        assert all(
            headers.get("x-openai-internal-codex-responses-lite") == "true"
            for headers in responses.request_headers
        )
    assert effects == [EFFECT]


@pytest.mark.parametrize("failure", ["append", "flush"])
async def test_registered_compaction_abort_reports_accepted_steer_write_failure(
    monkeypatch, failure
):
    set_compaction_thresholds(monkeypatch)
    token = access_token()
    summary = "synthetic-retirement-summary"
    original = "synthetic-original-before-retirement"
    steer = "synthetic-steer-retirement-write"
    checkpoint_pending = threading.Event()
    release_checkpoint = threading.Event()
    interrupt_entered = asyncio.Event()
    terminal_events = []

    class FailSteerWrite(MemorySessionStore):
        checkpoint_committed = False
        steer_writes = 0
        failed = False

        def commit(self, session_id, writer, revision, write_id, payload):
            items = json.loads(payload)["items"]
            if self.steer_writes == 0 and steer in json.dumps(items):
                self.steer_writes += 1
                assert checkpoint_pending.is_set() and release_checkpoint.is_set()
                if failure == "append":
                    self.failed = True
                    raise OSError("synthetic steering append rejected")
            super().commit(session_id, writer, revision, write_id, payload)
            if not checkpoint_pending.is_set() and any(
                row["type"] == "compacted" and summary in json.dumps(row) for row in items
            ):
                self.checkpoint_committed = True

        def flush(self, session_id, writer):
            if self.checkpoint_committed and not checkpoint_pending.is_set():
                checkpoint_pending.set()
                if not release_checkpoint.wait(10):
                    raise TimeoutError("synthetic checkpoint flush was not released")
            if failure == "flush" and self.steer_writes and not self.failed:
                self.failed = True
                raise OSError("synthetic steering flush acknowledgment lost")
            super().flush(session_id, writer)

    peer = ResponsesFixture()
    peer.respond(assistant_output("Seeded history."))
    peer.respond(assistant_output(summary))

    async def high_usage(event):
        if event["type"] == "response.completed" and len(peer.requests) == 1:
            event["response"]["usage"].update(
                input_tokens=260_000, output_tokens=1_000, total_tokens=261_000
            )

    peer.before_event = high_usage

    async def handle(request):
        assert request.method == "POST" and request.path == "/v1/responses"
        assert request.headers.get("Authorization") == f"Bearer {token}"
        assert request.headers.get("x-openai-internal-codex-responses-lite") == "true"
        return await peer.handle(request)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with timeout(30), TestServer(app, host="127.0.0.1") as server:
        set_endpoint(monkeypatch, str(server.make_url("/v1")))
        agent = plan_agent(token)
        store = FailSteerWrite()
        storage = CustomStorage(store)
        async with agent.session(storage=storage) as session:
            thread_id = session.id
            assert (await (await session.submit("Seed history.")).get_result()).final_output == (
                "Seeded history."
            )
            native = session._native

            class ObserveRetirement:
                def __getattr__(self, name):
                    return getattr(native, name)

                async def _interrupt_session(self, *args, **kwargs):
                    interrupt_entered.set()
                    return await native._interrupt_session(*args, **kwargs)

                async def _next_session_event(self):
                    raw = await native._next_session_event()
                    event = json.loads(raw)
                    if event is not None and event["msg"]["type"] in {
                        "error",
                        "turn_aborted",
                        "task_complete",
                        "turn_complete",
                    }:
                        terminal_events.append(event)
                    return raw

            session._native = ObserveRetirement()
            turn = await session.submit(original)
            try:
                assert await asyncio.to_thread(checkpoint_pending.wait, 6)
                assert turn.turn_id is not None
                accepted = await turn.steer_exact(steer, expected_turn_id=turn.turn_id)
                assert isinstance(accepted, SteeringReceipt)
                assert accepted.turn_id == turn.turn_id
                cancelling = asyncio.create_task(turn.cancel())
                await interrupt_entered.wait()
                assert not cancelling.done()
            finally:
                release_checkpoint.set()
            await cancelling
            assert store.failed
            reports = [event["msg"] for event in terminal_events if event["id"] == turn.turn_id]
            [aborted] = [event for event in reports if event["type"] != "error"]
            assert aborted["type"] == "turn_aborted" and aborted["reason"] == "interrupted"
            error = aborted.get("error")
            assert error is not None, "accepted steer write failure settled as a clean abort"
            assert "Canonical thread persistence failed" in error["message"]
            [reported] = [event for event in reports if event["type"] == "error"]
            assert reported["message"] == error["message"]

        async with agent.session(storage=storage, resume=thread_id) as reopened:
            restored = json.dumps((await reopened.history_snapshot()).items)
            assert restored.count(summary) == restored.count(original) == 1
            assert restored.count(steer) == (1 if failure == "flush" else 0)
            if failure == "flush":
                assert restored.index(summary) < restored.index(original) < restored.index(steer)
    assert len(peer.requests) == 2


async def test_registered_blank_post_turn_summary_preserves_history(monkeypatch, tmp_path):
    set_compaction_thresholds(monkeypatch, post_turn_percent=1)
    token = access_token()
    answer = "Synthetic answer before post-turn compaction."
    peer = ResponsesFixture()
    peer.respond(assistant_output(answer))
    peer.respond(assistant_output(" \n\t "))

    async def high_usage(event):
        if event["type"] == "response.completed" and len(peer.requests) == 1:
            event["response"]["usage"].update(
                input_tokens=260_000, output_tokens=1_000, total_tokens=261_000
            )

    peer.before_event = high_usage

    async def handle(request):
        assert request.method == "POST" and request.path == "/v1/responses"
        assert request.headers.get("Authorization") == f"Bearer {token}"
        assert request.headers.get("x-openai-internal-codex-responses-lite") == "true"
        return await peer.handle(request)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with timeout(25), TestServer(app, host="127.0.0.1") as server:
        set_endpoint(monkeypatch, str(server.make_url("/v1")))
        agent = plan_agent(token)
        storage = LocalStorage(tmp_path / "blank-post-turn")
        async with agent.session(storage=storage) as session:
            thread_id = session.id
            result = await (await session.submit("Give a short answer.")).get_result()
            assert result.final_output == answer
            assert answer in json.dumps((await session.model_context()).items)

        async with agent.session(storage=storage, resume=thread_id) as reopened:
            assert answer in json.dumps((await reopened.model_context()).items)

    assert [COMPACTION_MARKER in json.dumps(body["input"]) for body in peer.requests] == [
        False,
        True,
    ]


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
