"""Accepted root URL completion belongs to the live Session and peer."""

import json
from contextlib import aclosing

from aiohttp import web

from agents import ElicitationRequest
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_scoped_policies import selected
from tests.test_native_url_completion import RequiredPeer, app


class RootLifetimePeer(RequiredPeer):
    def __init__(self):
        super().__init__(ids=("root-after-call",))

    async def handle(self, request):
        body = await request.json()
        if body.get("method") != "tools/call":
            return await super().handle(request)
        self.calls.append(body["params"])
        await self.connected.wait()
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        try:
            # POST SSE tags this request with the original outbound call.
            await self.send(
                stream,
                {
                    "id": 873,
                    "method": "elicitation/create",
                    "params": {
                        "mode": "url",
                        "elicitationId": self.ids[0],
                        "url": "https://example.invalid/never-open",
                        "message": "Synthetic root consent",
                    },
                },
            )
            self.prompted.set()
            await self.answered.wait()
            await self.send(
                stream,
                {
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": "original root result"}]},
                },
            )
            return stream
        finally:
            self.finished.set()

    async def notifications(self, request):
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        self.connected.set()
        # Completion comes from the same live MCP session, after the associated
        # POST stream and original turn have ended.
        await self.complete.wait()
        await self.send(
            stream,
            {
                "method": "notifications/elicitation/complete",
                "params": {"elicitationId": self.ids[0]},
            },
        )
        self.notification_sent.set()
        await self.closed.wait()
        return stream

    @staticmethod
    async def send(stream, value):
        await stream.write(
            ("event: message\ndata: " + json.dumps({"jsonrpc": "2.0", **value}) + "\n\n").encode()
        )


async def test_accepted_root_url_completion_survives_original_call_and_turn(
    responses: ResponsesFixture,
) -> None:
    peer = RootLifetimePeer()
    async with timeout(24), peer.serve() as url:
        responses.respond(selected("root-original-call", "ordinary"))
        responses.respond(assistant_output("original turn done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                question = await anext(cursor)
            assert isinstance(question, ElicitationRequest)
            assert question.elicitation_id == "root-after-call"
            assert question.subagent_id is None and question.turn_id is None
            await question.accept_url()
            assert (await turn.get_result()).final_output == "original turn done"
            await peer.finished.wait()
            assert "original root result" in json.dumps(responses.requests[-1])
            assert len(peer.calls) == len(peer.answers) == 1
            assert peer.calls[0]["_meta"]["callId"] == "root-original-call"
            assert peer.answers[0]["result"] == {"action": "accept"}
            assert not peer.notification_sent.is_set()
            # The original call and turn have ended; the exact peer is still live.
            peer.complete.set()
            await peer.notification_sent.wait()
            await question.wait_url_completion(timeout=8)
            assert len(peer.calls) == len(peer.answers) == 1
            assert not await session.pending_interactions()
