"""Real wire ingress before consent; deterministic policy holds live in Core tests."""

import json
from contextlib import aclosing

import pytest
from aiohttp import web

from agents import ElicitationRequest
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_scoped_policies import selected
from tests.test_native_url_completion import RequiredPeer, app


class IngressPeer(RequiredPeer):
    """Return the call response and notifications on one existing GET stream."""

    def __init__(self, route):
        ids = ("ingress-A",) if route == "elicitation" else ("ingress-A", "ingress-B")
        super().__init__(ids=ids)
        self.route = route
        self.call_body = None

    async def handle(self, request):
        body = await request.json()
        if body.get("method") != "tools/call":
            return await super().handle(request)
        self.calls.append(body["params"])
        self.call_body = body
        self.call_entered.set()
        # The pending request receives its response over the common GET stream.
        return web.Response(status=202)

    async def notifications(self, request):
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        self.connected.set()
        await self.call_entered.wait()
        assert self.call_body is not None
        members = [
            {
                "mode": "url",
                "elicitationId": identity,
                "url": "https://example.invalid/never-open",
                "message": "Synthetic ingress consent",
            }
            for identity in self.ids
        ]
        if self.route == "elicitation":
            incoming = {"id": 873, "method": "elicitation/create", "params": members[0]}
        else:
            incoming = {
                "id": self.call_body["id"],
                "error": {
                    "code": -32042,
                    "message": "URL interaction required",
                    "data": {"elicitations": members},
                },
            }
        frames = [incoming] + [
            {
                "method": "notifications/elicitation/complete",
                "params": {"elicitationId": identity},
            }
            for identity in reversed(self.ids)
        ]
        # One write, no consent/public-request barrier between frames. The GET
        # stream forwards notifications after a response; POST SSE drains them.
        await stream.write(
            "".join(
                "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", **frame}) + "\n\n"
                for frame in frames
            ).encode()
        )
        self.notification_sent.set()
        if self.route == "elicitation":
            await self.answered.wait()
            await stream.write(
                (
                    "event: message\ndata: "
                    + json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": self.call_body["id"],
                            "result": {
                                "content": [{"type": "text", "text": "ingress action complete"}]
                            },
                        }
                    )
                    + "\n\n"
                ).encode()
            )
        await self.closed.wait()
        return stream


@pytest.mark.parametrize("route", ["elicitation", "required-list"])
async def test_same_wire_url_completion_precedes_public_consent_without_replay(
    responses: ResponsesFixture, route: str
) -> None:
    peer = IngressPeer(route)
    async with timeout(24), peer.serve() as url:
        responses.respond(selected("ingress-original-call", "ordinary"))
        responses.respond(assistant_output("done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            # Unlike the older early-completion tests, the peer never waits for
            # an issued/public request before sending its completion frames.
            await peer.notification_sent.wait()
            assert not peer.answers
            assert len(responses.requests) == 1
            async with aclosing(session.interactions()) as cursor:
                questions = [await anext(cursor) for _ in peer.ids]
            assert all(isinstance(question, ElicitationRequest) for question in questions)
            assert tuple(question.elicitation_id for question in questions) == peer.ids
            assert all(
                question.subagent_id is None and question.turn_id is None for question in questions
            )
            for question in questions:
                await question.accept_url()
            for question in questions:
                await question.wait_url_completion(timeout=8)
            assert (await turn.get_result()).final_output == "done"
            assert len(peer.calls) == 1
            assert peer.calls[0]["_meta"]["callId"] == "ingress-original-call"
            if route == "elicitation":
                assert len(peer.answers) == 1
                assert peer.answers[0]["id"] == 873
                assert peer.answers[0]["result"] == {"action": "accept"}
                assert "ingress action complete" in json.dumps(responses.requests[-1])
            else:
                assert not peer.answers
                assert "server reported URL interaction complete" in json.dumps(
                    responses.requests[-1]
                )
            assert not await session.pending_interactions()
