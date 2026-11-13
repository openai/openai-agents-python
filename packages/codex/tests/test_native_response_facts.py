"""Real native HTTP/WS facts use owned loopback peers and synthetic data only."""

from __future__ import annotations

import json
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeError, NativeProvider
from agents.observation import RawProviderEvent, ResponseFactReceived
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, text_response

PRIVATE = "SYNTHETIC_FACT_PRIVATE_CONTENT_9f0368"
TOKEN = {
    "token": "a",
    "bytes": [97],
    "logprob": -1,
    "top_logprobs": [{"token": PRIVATE, "bytes": [], "logprob": -2}],
}


def message(token: dict[str, Any] = TOKEN, count: int = 2) -> dict[str, Any]:
    item = assistant_output("a" * count)["item"]
    item["id"] = "item-facts"
    item["content"][0]["logprobs"] = [token] * count
    return item


@dataclass
class FactPeer:
    replies: deque[tuple[bool, bool]] = field(default_factory=deque)
    requests: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""
    disconnected: int = 0
    probability_frames: int = 2
    probability_token: dict[str, Any] | None = None
    response_id_suffix: str = ""
    upstream_id_suffix: str = ""

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)
        assert self.replies, "unexpected provider request"
        refused, filtered = self.replies.popleft()
        index = len(self.requests)
        response_id = f"response-{index}" + self.response_id_suffix
        token = self.probability_token or TOKEN
        item = (
            {
                "type": "message",
                "id": "item-refusal",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": PRIVATE}],
            }
            if refused
            else message(token, self.probability_frames)
        )
        terminal = {
            "id": response_id,
            "status": "incomplete" if filtered else "completed",
            "output": [item],
        }
        if filtered:
            terminal["incomplete_details"] = {"reason": "content_filter"}
        headers = {"x-request-id": f"upstream-{index}" + self.upstream_id_suffix}
        if body.get("stream") is False:
            return web.json_response(terminal, headers=headers)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", **headers})
        await response.prepare(request)
        events: list[dict[str, Any]] = [
            {"type": "response.created", "response": {"id": response_id}}
        ]
        if refused:
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": "item-refusal",
                        "role": "assistant",
                        "content": [],
                    },
                }
            )
            events.extend(
                [
                    {
                        "type": "response.refusal.delta",
                        "item_id": "item-refusal",
                        "content_index": 0,
                        "delta": PRIVATE,
                    },
                    {
                        "type": "response.refusal.done",
                        "item_id": "item-refusal",
                        "content_index": 0,
                        "refusal": PRIVATE,
                    },
                ]
            )
        elif not filtered:
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": "item-facts",
                        "role": "assistant",
                        "content": [],
                    },
                }
            )
            events.extend(
                {
                    "type": "response.output_text.delta",
                    "item_id": "item-facts",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "a",
                    "logprobs": [token],
                }
                for _ in range(self.probability_frames)
            )
            events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
        if not refused:
            events.append(
                {
                    "type": "response.incomplete" if filtered else "response.completed",
                    "response": terminal,
                }
            )
        try:
            for event in events:
                await response.write(f"data: {json.dumps(event)}\n\n".encode())
            await response.write_eof()
        except ConnectionResetError:
            self.disconnected += 1
        return response


@asynccontextmanager
async def fact_peer():
    fixture = FactPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    async with TestServer(app) as server:
        fixture.url = str(server.make_url("/v1"))
        yield fixture
    assert not fixture.replies


@pytest.mark.parametrize("mode", ["streaming", "buffered"])
async def test_probability_facts_identity_privacy_restore_and_no_prompt_replay(mode: str) -> None:
    async with fact_peer() as peer:
        peer.replies.extend([(False, False), (False, False), (False, False)])
        provider = NativeProvider(
            base_url=peer.url, api_key="synthetic-facts-key", response_mode=mode
        )
        agent = Agent(
            instructions="Answer.",
            model="gpt-5.2",
            provider=provider,
            model_settings={"top_logprobs": 0},
        )
        async with timeout(30), agent.session() as session:
            turn = await session.submit("answer")
            assert (await turn.get_result()).final_output == "aa"
            ordinary = [event async for event in turn.events()]
            trusted = [event async for event in turn.events(include_execution_details=True)]
            assert PRIVATE not in json.dumps([asdict(event) for event in ordinary])
            detailed = [
                event.record for event in trusted if isinstance(event, ResponseFactReceived)
            ]
            assert PRIVATE in json.dumps(detailed)
            recorded = [value async for value in session.turns()][0]
            facts = recorded["response_facts"]
            assert PRIVATE in json.dumps(facts)
            probability = [
                row["fact"] for row in facts if row["fact"]["kind"] == "text_probabilities"
            ]
            assert probability
            assert all(part["capture_complete"] for part in probability)
            assert any(part["tokens"][0]["top_logprobs"][0]["bytes"] == [] for part in probability)
            if mode == "streaming":
                assert [part["delta"] for part in probability if not part["snapshot"]] == ["a", "a"]
            response = recorded["responses"][0]
            assert response["response_id"] == "response-1"
            assert response["upstream_request_id"] == "upstream-1"
            assert facts[0]["upstream_request_id"] == "upstream-1"
            assert facts[0]["request_id"] not in (None, "upstream-1", "response-1")
            assert len({row["request_id"] for row in facts}) == 1
            state = await session.save_state()
            following = await session.submit("again")
            await following.get_result()
            assert PRIVATE not in json.dumps(peer.requests[-1])
        async with Agent(instructions="Answer.", provider=provider).session(
            state=state
        ) as restored:
            assert [value async for value in restored.turns()][0]["response_facts"] == facts
            await (await restored.submit("restored")).get_result()
            assert PRIVATE not in json.dumps(peer.requests[-1])
        assert all(request["top_logprobs"] == 0 for request in peer.requests)
        assert all(
            "message.output_text.logprobs" in request["include"] for request in peer.requests
        )
        assert all("reasoning.encrypted_content" in request["include"] for request in peer.requests)


@pytest.mark.parametrize("mode", ["streaming", "buffered"])
async def test_refusal_content_survives_failure_privately_without_a_fake_terminal(
    mode: str,
) -> None:
    async with fact_peer() as peer:
        peer.replies.extend([(True, False), (False, False)])
        provider = NativeProvider(
            base_url=peer.url, api_key="synthetic-facts-key", response_mode=mode
        )
        agent = Agent(instructions="Answer.", model="gpt-5.2", provider=provider)
        async with timeout(30), agent.session() as session:
            turn = await session.submit("answer")
            with pytest.raises(NativeError) as caught:
                await turn.get_result()
            assert caught.value.response_integrity_reason == "provider_refusal"
            assert PRIVATE not in str(caught.value)
            ordinary = [event async for event in turn.events()]
            trusted = [event async for event in turn.events(include_execution_details=True)]
            assert PRIVATE not in json.dumps([asdict(event) for event in ordinary])
            assert PRIVATE not in repr(trusted)
            assert PRIVATE in json.dumps(
                [event.record for event in trusted if isinstance(event, ResponseFactReceived)]
            )
            recorded = [value async for value in session.turns()][0]
            assert recorded["status"] == "failed"
            refusals = [
                row["fact"]
                for row in recorded["response_facts"]
                if row["fact"]["kind"] == "refusal"
            ]
            assert any(row["text"] == PRIVATE and row["capture_complete"] for row in refusals)
            if mode == "streaming":
                assert not recorded.get("responses")  # Peer sent no genuine terminal.
            else:
                assert (
                    recorded["responses"][0]["status"] == "completed"
                )  # Actual provider terminal, rejected locally.
            state = await session.save_state()
            await (await session.submit("continue")).get_result()
            assert PRIVATE not in json.dumps(peer.requests[-1])
        async with agent.session(state=state) as restored:
            assert [value async for value in restored.turns()][0]["response_facts"] == recorded[
                "response_facts"
            ]
        assert len(peer.requests) == 2


async def test_unsolicited_probabilities_are_not_retained_or_published() -> None:
    async with fact_peer() as peer:
        peer.replies.append((False, False))
        async with (
            timeout(30),
            Agent(
                instructions="Answer.",
                provider=NativeProvider(base_url=peer.url, api_key="synthetic-facts-key"),
            ).session() as session,
        ):
            turn = await session.submit("answer")
            await turn.get_result()
            events = [event async for event in turn.events(include_execution_details=True)]
            turns = [value async for value in session.turns()]
            assert all(
                row["fact"]["kind"] != "text_probabilities"
                for row in turns[0].get("response_facts", [])
            )
            assert PRIVATE not in json.dumps([asdict(event) for event in events])
            assert "top_logprobs" not in peer.requests[0]
            assert "message.output_text.logprobs" not in peer.requests[0]["include"]


@pytest.mark.parametrize("top_logprobs", [None, 0])
async def test_empty_probability_delta_and_replacement_snapshot_preserve_capture_flag(
    top_logprobs: int | None,
) -> None:
    from tests.test_native_provider_websockets import peer as websocket_peer

    frames = text_response("item-empty-probabilities", "a", "")
    frames[1]["logprobs"] = [TOKEN]
    frames[2]["logprobs"] = []
    frames[-1]["item"]["content"][0]["logprobs"] = []
    settings = {} if top_logprobs is None else {"top_logprobs": top_logprobs}
    async with timeout(30), websocket_peer() as peer:
        peer.respond(*frames)
        async with Agent(
            instructions="Answer.",
            model="gpt-5.2",
            provider=peer.provider(websocket=False),
            model_settings=settings,
        ).session(include_raw_provider_events=True, observation_retention="all") as session:
            turn = await session.submit("answer")
            assert (await turn.get_result()).final_output == "a"
            events = [event async for event in turn.events(include_execution_details=True)]
            recorded = [row async for row in session.turns()][0]
            probability = [
                row["fact"]
                for row in recorded.get("response_facts", [])
                if row["fact"]["kind"] == "text_probabilities"
            ]
            observed = [
                event.record["fact"]
                for event in events
                if isinstance(event, ResponseFactReceived)
                and event.record["fact"]["kind"] == "text_probabilities"
            ]
            assert observed == probability
            if top_logprobs is None:
                assert probability == []
            else:
                assert [
                    (part["snapshot"], part["delta"], part["tokens"]) for part in probability
                ] == [
                    (False, "a", [TOKEN]),
                    (False, "", []),
                    (True, None, []),
                ]
                assert all(part["capture_complete"] for part in probability)
                assert all(part["item_id"] == "item-empty-probabilities" for part in probability)
            raw = [event for event in events if isinstance(event, RawProviderEvent)]
            assert all(event.transport == "sse" for event in raw)
            assert [event.payload for event in raw[1:-1]] == frames
            assert PRIVATE in json.dumps([event.payload for event in raw])
        assert len(peer.requests) == 1
        request = peer.requests[0]
        if top_logprobs is None:
            assert "top_logprobs" not in request
            assert "message.output_text.logprobs" not in request["include"]
        else:
            assert request["top_logprobs"] == 0
            assert "message.output_text.logprobs" in request["include"]


async def test_websocket_probabilities_do_not_relabel_a_handshake_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_native_provider_websockets import peer as websocket_peer

    original = web.WebSocketResponse

    def response():
        socket = original()
        socket.headers["x-request-id"] = "SYNTHETIC_HANDSHAKE_ONLY"
        return socket

    monkeypatch.setattr(web, "WebSocketResponse", response)
    async with websocket_peer() as peer:
        done = assistant_output("aa")
        done["item"]["content"][0]["logprobs"] = [TOKEN]
        peer.respond(done)
        async with (
            timeout(30),
            Agent(
                instructions="Answer.",
                model="gpt-5.2",
                provider=peer.provider(),
                model_settings={"top_logprobs": 0},
            ).session() as session,
        ):
            await (await session.submit("answer")).get_result()
            recorded = [row async for row in session.turns()][0]
            assert "upstream_request_id" not in recorded["responses"][0]
            assert recorded["response_facts"]
            assert all("upstream_request_id" not in row for row in recorded["response_facts"])
            assert "SYNTHETIC_HANDSHAKE_ONLY" not in json.dumps(recorded)
            assert PRIVATE in json.dumps(recorded["response_facts"])
        assert peer.connections >= 1
        assert peer.requests[0]["top_logprobs"] == 0


async def test_native_cumulative_fact_budget_marks_a_whole_token_prefix() -> None:
    async with fact_peer() as peer:
        peer.probability_frames = 600
        peer.response_id_suffix = "\\\n🧪" * 1500
        peer.upstream_id_suffix = "r" * 2000
        peer.probability_token = {
            "token": "a",
            "bytes": [97],
            "logprob": -1,
            "top_logprobs": [{"token": "z" * 1200, "bytes": [], "logprob": -2}],
        }
        peer.replies.append((False, False))
        async with (
            timeout(60),
            Agent(
                instructions="Answer.",
                model="gpt-5.2",
                provider=NativeProvider(base_url=peer.url, api_key="synthetic-facts-key"),
                model_settings={"top_logprobs": 1},
            ).session() as session,
        ):
            await (await session.submit("answer")).get_result()
            recorded = [row async for row in session.turns()][0]
            facts = [row["fact"] for row in recorded["response_facts"]]
            encoded = sum(
                len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode())
                for record in recorded["response_facts"]
            )
            assert encoded <= 1_048_576
            probabilities = [fact for fact in facts if fact["kind"] == "text_probabilities"]
            assert any(not fact["capture_complete"] for fact in probabilities) or any(
                fact["kind"] == "capture_truncated" for fact in facts
            )
            assert probabilities
            assert all(
                token == peer.probability_token
                for fact in probabilities
                for token in fact["tokens"]
            )


async def test_content_filter_is_a_genuine_incomplete_fact_without_refusal_text() -> None:
    async with fact_peer() as peer:
        peer.replies.append((False, True))
        async with (
            timeout(30),
            Agent(
                instructions="Answer.",
                provider=NativeProvider(base_url=peer.url, api_key="synthetic-facts-key"),
            ).session() as session,
        ):
            with pytest.raises(NativeError):
                await (await session.submit("answer")).get_result()
            recorded = [row async for row in session.turns()][0]
            assert recorded["responses"][0]["status"] == "incomplete"
            assert recorded["responses"][0]["incomplete_reason"] == "content_filter"
            assert not recorded.get("response_facts")
