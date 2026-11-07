"""Passive HTTP observations and canonical history across malformed input."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeError, NativeProvider
from agents.observation import OutputTextDelta
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent_observation import text_response


async def assert_ingress_outcome(
    peer: ResponsesFixture, expected_deltas: list[str], *, allow_transient_delta: bool = False
) -> None:
    """Check observations, both history surfaces and the next request's input."""
    provider = NativeProvider(base_url=peer.url, api_key="synthetic-ingress-key")
    async with timeout(30), Agent(instructions="Answer briefly.", provider=provider).session() as s:
        first = await s.submit("first")
        result = await (first).get_result()
        assert result.final_output == "authoritative answer"
        assert result.thread_usage is not None
        assert result.thread_usage.last_token_usage.input_tokens == 11
        assert result.thread_usage.last_token_usage.output_tokens == 3
        assert result.thread_usage.total_token_usage.total_tokens == 14
        assert len(peer.requests) == 1
        deltas = [e.delta async for e in first.events() if isinstance(e, OutputTextDelta)]
        if allow_transient_delta:
            # Core omits malformed events and wire item IDs from its parsed
            # stream. Passive text need not authenticate that lost metadata.
            deltas = [delta for delta in deltas if delta != "stale-not-allowed"]
        assert deltas == expected_deltas
        histories = [
            json.dumps([entry async for entry in s.items()], ensure_ascii=False),
            json.dumps((await s.save_state()).history, ensure_ascii=False),
        ]
        for history in histories:
            assert "authoritative answer" in history
            for transient in [*expected_deltas, "stale-not-allowed"]:
                assert transient not in history

        second = await s.submit("follow up")
        followup = await (second).get_result()
        assert followup.final_output == "follow-up answer"
        assert followup.thread_usage is not None
        assert followup.thread_usage.last_token_usage.total_tokens == 14
        assert followup.thread_usage.total_token_usage.total_tokens == 28
        assert len(peer.requests) == 2
        replay = json.dumps(peer.requests[1]["input"], ensure_ascii=False)
        assert "authoritative answer" in replay
        assert "stale-not-allowed" not in replay
        for live in expected_deltas:
            assert live not in replay


@pytest.mark.parametrize(
    ("added_id", "delta_id"),
    [(None, "wire-id"), ("wire-id", None), (None, None)],
    ids=["added-missing", "delta-missing", "both-missing"],
)
async def test_missing_wire_id_preserves_live_text_and_canonical_history(
    responses: ResponsesFixture, added_id: str | None, delta_id: str | None
) -> None:
    added, delta, done = text_response("wire-id", "live-while-active")
    done["item"]["content"][0]["text"] = "authoritative answer"
    if added_id is None:
        del added["item"]["id"]
    if delta_id is None:
        del delta["item_id"]
    responses.respond(added, delta, done)
    responses.respond(assistant_output("follow-up answer"))
    await assert_ingress_outcome(responses, ["live-while-active"])


@pytest.mark.parametrize("item_event", ["response.output_item.added", "response.output_item.done"])
async def test_malformed_item_keeps_transient_text_out_of_canonical_history(
    responses: ResponsesFixture, item_event: str
) -> None:
    added, delta, _ = text_response("wire-id", "live-before-reset")
    fresh, fresh_delta, done = text_response("fresh-id", "live-after-reset")
    done["item"]["content"][0]["text"] = "authoritative answer"
    responses.respond(
        added,
        delta,
        # A Message without content is rejected by the public ResponseItem type.
        {"type": item_event, "item": {"type": "message", "role": "assistant", "id": "broken"}},
        {"type": "response.output_text.delta", "item_id": "wire-id", "delta": "stale-not-allowed"},
        fresh,
        fresh_delta,
        done,
    )
    responses.respond(assistant_output("follow-up answer"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-ingress-key")
    async with timeout(30), Agent(instructions="Answer briefly.", provider=provider).session() as s:
        first = await s.submit("first")
        error = "^Native model response ended without complete supported output$"
        with pytest.raises(NativeError, match=error):
            await first.get_result()
        assert len(responses.requests) == 1
        deltas = [e.delta async for e in first.events() if isinstance(e, OutputTextDelta)]
        assert deltas == ["live-before-reset"]
        histories = [
            json.dumps([entry async for entry in s.items()], ensure_ascii=False),
            json.dumps((await s.save_state()).history, ensure_ascii=False),
        ]
        rejected = [
            "live-before-reset",
            "live-after-reset",
            "stale-not-allowed",
            "authoritative answer",
            "broken",
        ]
        for history in histories:
            assert all(text not in history for text in rejected)
        # Waiting again preserves the same failed outcome without a retry.
        with pytest.raises(NativeError, match=error):
            await first.get_result()
        assert len(responses.requests) == 1
        second = await s.submit("follow up")
        followup = await second.get_result()
        assert second.turn_id != first.turn_id
        assert followup.final_output == "follow-up answer"
        assert followup.thread_usage is not None
        assert followup.thread_usage.last_token_usage.total_tokens == 14
        assert followup.thread_usage.total_token_usage.total_tokens == 14
        assert len(responses.requests) == 2
        replay = json.dumps(responses.requests[1]["input"], ensure_ascii=False)
        assert all(text not in replay for text in rejected)


def raw_data(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\r\n\r\n".encode()


def raw_completed() -> bytes:
    return raw_data(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "usage": {
                    "input_tokens": 11,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 3,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 14,
                },
            },
        }
    )


@pytest.mark.parametrize(
    "invalid_data",
    [b'{"unrelated":true}', b'{"type":17}', b'{"type":', b"null"],
    ids=["missing-type", "non-string-type", "invalid-json", "non-object-envelope"],
)
async def test_malformed_raw_event_keeps_transient_text_out_of_canonical_history(
    invalid_data: bytes,
) -> None:
    # ResponsesFixture intentionally requires dicts with a type for its hooks.
    # Only this first response needs raw wire bytes; follow-up uses that fixture.
    peer = ResponsesFixture()
    peer.respond(assistant_output("follow-up answer"))
    added, delta, _ = text_response("wire-id", "live-before-reset")
    fresh, fresh_delta, done = text_response("fresh-id", "live-after-reset")
    done["item"]["content"][0]["text"] = "authoritative answer"

    async def handle(request: web.Request) -> web.StreamResponse:
        if peer.requests:
            return await peer.handle(request)
        peer.requests.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for event in [
            {"type": "response.created", "response": {"id": "resp_1"}},
            added,
            delta,
        ]:
            await response.write(raw_data(event))
        await response.write(b"data: " + invalid_data + b"\r\n\r\n")
        for event in [
            {
                "type": "response.output_text.delta",
                "item_id": "wire-id",
                "delta": "stale-not-allowed",
            },
            fresh,
            fresh_delta,
            done,
        ]:
            await response.write(raw_data(event))
        await response.write(raw_completed())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with TestServer(app) as server:
        peer.url = str(server.make_url("/v1"))
        await assert_ingress_outcome(
            peer, ["live-before-reset", "live-after-reset"], allow_transient_delta=True
        )


async def test_fragmented_crlf_multiline_sse_preserves_unicode_and_canonical_history() -> None:
    peer = ResponsesFixture()
    peer.respond(assistant_output("follow-up answer"))
    added, _, done = text_response("wire-id", "unused")
    done["item"]["content"][0]["text"] = "authoritative answer"

    async def handle(request: web.Request) -> web.StreamResponse:
        if peer.requests:
            return await peer.handle(request)
        peer.requests.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(raw_data({"type": "response.created", "response": {"id": "resp_1"}}))
        await response.write(raw_data(added))
        await response.write(
            raw_data(
                {
                    "type": "response.output_text.delta",
                    "item_id": "different",
                    "delta": "stale-not-allowed",
                }
            )
        )
        # Two data lines join at legal JSON whitespace. Split CRLF and both
        # multibyte code points across separate HTTP body writes.
        frame = (
            ": comment ignored by SSE\r\n"
            "event: response.output_text.delta\r\n"
            "id: sse-id-1\r\n"
            "retry: 1200\r\n"
            'data: {"type":"response.output_text.delta",\r\n'
            'data: "item_id":"wire-id","delta":"café🐈"}\r\n\r\n'
        ).encode()
        cuts = [
            frame.index(b"\r\n") + 1,
            frame.index("é".encode()) + 1,
            frame.index("🐈".encode()) + 2,
        ]
        start = 0
        for end in [*cuts, len(frame)]:
            await response.write(frame[start:end])
            start = end
            await asyncio.sleep(0)
        await response.write(raw_data(done))
        await response.write(raw_completed())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with TestServer(app) as server:
        peer.url = str(server.make_url("/v1"))
        await assert_ingress_outcome(peer, ["café🐈"], allow_transient_delta=True)
