from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import httpx
import pytest
from openai import AsyncOpenAI

from agents import ResponsesCompaction, ResponsesCompactionContext
from agents.compaction import CompactionContext


def context() -> CompactionContext[None]:
    return CompactionContext(
        context=None,
        items=(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "original"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "candidate"}],
            },
        ),
        session_id="session",
        turn_id="turn",
        subagent_id=None,
        model="generation",
        trigger="auto",
    )


def result() -> dict:
    return {
        "id": "compact-result",
        "object": "response.compaction",
        "created_at": 1,
        "output": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "before"}],
            },
            {"type": "compaction", "id": "opaque", "encrypted_content": "opaque"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "after", "annotations": []}],
            },
        ],
        "usage": {
            "input_tokens": 4,
            "input_tokens_details": {"cached_tokens": 1},
            "output_tokens": 2,
            "total_tokens": 6,
        },
    }


async def test_borrowed_client_receives_full_input_and_preserves_the_complete_window() -> None:
    calls = []

    async def send(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=result())

    async with AsyncOpenAI(
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(send)),
    ) as client:
        factory = ResponsesCompaction(client=client, model="compactor")
        prepared = factory._context(
            context(), candidates=(context().items[1],), provider_response=None, model_omitted=False
        )
        assert isinstance(prepared, ResponsesCompactionContext)
        assert prepared.mode == "input" and prepared.model == "compactor"
        assert len(prepared.candidates) == 1
        with pytest.raises(FrozenInstanceError):
            prepared.model = "changed"
        original = factory._compact
        client.responses.compact = lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("replaced method")
        )
        assert factory._compact is original
        reports = []
        window = await factory._window(prepared, provider_response=None, observe=reports.append)
        assert calls == [{"model": "compactor", "input": list(context().items)}]
        assert [item.get("encrypted_content", item.get("role")) for item in window["items"]] == [
            "user",
            "opaque",
            "assistant",
        ]
        assert reports[0].input_tokens == 4 and reports[0].total_tokens == 6
        assert not client.is_closed()


@pytest.mark.parametrize("response_id", [7, False, "", " "])
async def test_malformed_response_id_fails_before_native_reply(response_id) -> None:
    async def send(request):
        return httpx.Response(200, json={**result(), "id": response_id})

    async with AsyncOpenAI(
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(send)),
    ) as client:
        factory = ResponsesCompaction(client=client)
        prepared = factory._context(
            context(), candidates=(), provider_response=None, model_omitted=False
        )
        reports = []
        with pytest.raises(ValueError, match="response ID"):
            await factory._window(prepared, provider_response=None, observe=reports.append)
        assert len(reports) == 1 and reports[0].total_tokens == 6


@pytest.mark.parametrize("mode", ["auto", "input", "previous_response_id"])
async def test_preflight_resolves_mode_model_and_requires_current_acknowledgment(mode) -> None:
    async with AsyncOpenAI(api_key="synthetic") as client:
        factory = ResponsesCompaction(client=client, mode=mode)
        if mode == "previous_response_id":
            with pytest.raises(ValueError, match="acknowledged"):
                factory._context(
                    context(), candidates=(), provider_response=None, model_omitted=False
                )
        else:
            assert (
                factory._context(
                    context(), candidates=(), provider_response=None, model_omitted=False
                ).mode
                == "input"
            )
        with pytest.raises(ValueError, match="explicit model|acknowledged"):
            factory._context(context(), candidates=(), provider_response=None, model_omitted=True)
        captured = factory._context(
            context(),
            candidates=(),
            provider_response={"response_id": "ordinary-current", "model": "actual-provider-model"},
            model_omitted=True,
        )
        assert captured.model == "actual-provider-model"
        assert captured.mode == ("input" if mode == "input" else "previous_response_id")


async def test_completed_malformed_result_reports_missing_usage_once_without_a_window() -> None:
    class Response:
        def model_dump(self, **kwargs):
            raise ValueError("malformed completed response")

    class Responses:
        async def compact(self, **kwargs):
            return Response()

    class Client:
        responses = Responses()

    factory = ResponsesCompaction(client=Client(), model="compact")
    prepared = factory._context(
        context(), candidates=(), provider_response=None, model_omitted=False
    )
    reports = []
    with pytest.raises(ValueError, match="malformed"):
        await factory._window(prepared, provider_response=None, observe=reports.append)
    assert reports == [None]


def test_handoff_capture_preserves_each_compaction_declaration():
    from types import SimpleNamespace

    from _agents_codex._agent_profiles import _capture_agent_graph
    from agents import Agent

    async def compact(**body):
        return result()

    client = SimpleNamespace(responses=SimpleNamespace(compact=compact))
    root_config = ResponsesCompaction(client=client, model="root-compactor")
    target_config = ResponsesCompaction(client=client, model="target-compactor")
    target = Agent(name="Target", instructions="Target.", compaction=target_config)
    root = Agent(instructions="Root.", handoffs=[target], compaction=root_config)
    profiles = _capture_agent_graph(root)
    assert profiles[0].declaration.compaction is root_config
    assert profiles[1].declaration.compaction is target_config


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_session_compaction_transforms_input_before_client_effects(cancel):
    import asyncio
    from types import SimpleNamespace

    import _agents_codex.session as implementation
    from agents import Agent

    active = True
    calls = []
    transforms = []

    async def transform(request):
        nonlocal active
        transforms.append((request.purpose, request.model, request.request_id))
        if cancel:
            active = False
        return [
            json.loads(json.dumps(item).replace("PRIVATE_SENTINEL", "redacted"))
            for item in request.items
        ]

    async def compact(**body):
        calls.append(body)
        return result()

    client = SimpleNamespace(responses=SimpleNamespace(compact=compact))
    agent = Agent(
        instructions="Answer.",
        transform_context=transform,
        compaction=ResponsesCompaction(client=client, model="compactor", mode="input"),
    )
    replies = []

    async def own(state, message, prepare, reply, reject):
        callback, request = prepare(lambda: active)
        reply(await callback(request))

    async def decide(state, message, callback, admitted, request):
        return await implementation._call_callback(callback, admitted, request)

    owner = SimpleNamespace(
        _profiles={},
        id="session",
        _context=None,
        _model_api_key=None,
        _native_options="{}",
        _native=SimpleNamespace(
            _reply_compaction=lambda *args, **kwargs: replies.append((args, kwargs))
        ),
        _agent_for_message=lambda message: agent,
        _run_owned_context_callback=own,
        _call_decision_callback=decide,
    )
    state = SimpleNamespace(run_usage=SimpleNamespace(report=lambda usage: None))
    message = {
        "step_id": 7,
        "thread": "session",
        "instance": None,
        "turn_id": "turn",
        "model": "generation",
        "trigger": "manual",
        "candidate_indexes": [0],
        "model_omitted": False,
        "items": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "PRIVATE_SENTINEL"}],
            }
        ],
    }
    work = implementation.Session._before_compact_body(
        owner, state, message, policy=False, tracing=None
    )
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await work
        assert calls == [] and replies == []
    else:
        await work
        assert len(calls) == 1
        assert "PRIVATE_SENTINEL" not in json.dumps(calls[0]["input"])
        assert "redacted" in json.dumps(calls[0]["input"])
        assert len(replies) == 1
    assert transforms == [("compaction", "compactor", "7")]
