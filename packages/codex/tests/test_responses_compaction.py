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
