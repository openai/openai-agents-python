"""URL consent and completion cross the actual native MCP transport."""

import asyncio
import json
from contextlib import aclosing, asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, ElicitationRequest, HttpMCP, InteractionExpiredError
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_scoped_policies import AskingPeer, provider, selected


class CompletionPeer(AskingPeer):
    def __init__(self, *, early=False):
        super().__init__()
        self.early = early
        self.finish = asyncio.Event()
        self.before_accept = asyncio.Event()
        self.early_sent = asyncio.Event()
        self.completed = asyncio.Event()
        self.replay_answered = asyncio.Event()
        self.replay_answers = []

    async def handle(self, request):
        body = await request.json()
        if body.get("id") == 874 and "method" not in body:
            self.replay_answers.append(body)
            self.replay_answered.set()
            return web.Response(status=202)
        if body.get("method") != "tools/call":
            return await super().handle(request)
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)

        async def send(value):
            await stream.write(
                (
                    "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", **value}) + "\n\n"
                ).encode()
            )

        try:
            await send(
                {
                    "id": 873,
                    "method": "elicitation/create",
                    "params": {
                        "mode": "url",
                        "elicitationId": "synthetic-challenge-A",
                        "url": "https://example.invalid/never-open",
                        "message": "Synthetic consent",
                    },
                }
            )
            self.prompted.set()
            if self.early:
                await self.before_accept.wait()
                await send(
                    {
                        "method": "notifications/elicitation/complete",
                        "params": {"elicitationId": "synthetic-challenge-A"},
                    }
                )
                self.early_sent.set()
            await self.answered.wait()
            if not self.early:
                # A different ID on the same transport cannot resolve this request.
                await send(
                    {
                        "method": "notifications/elicitation/complete",
                        "params": {"elicitationId": "synthetic-challenge-B"},
                    }
                )
            await self.finish.wait()
            if not self.early:
                await send(
                    {
                        "method": "notifications/elicitation/complete",
                        "params": {"elicitationId": "synthetic-challenge-A"},
                    }
                )
                # Repeated completion has no additional effect.
                await send(
                    {
                        "method": "notifications/elicitation/complete",
                        "params": {"elicitationId": "synthetic-challenge-A"},
                    }
                )
            self.completed.set()
            await send(
                {
                    "id": 874,
                    "method": "elicitation/create",
                    "params": {
                        "mode": "url",
                        "elicitationId": "synthetic-challenge-A",
                        "url": "https://example.invalid/never-open",
                        "message": "Replayed interaction",
                    },
                }
            )
            await self.replay_answered.wait()
            await send(
                {
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": "synthetic action complete"}]},
                }
            )
            return stream
        finally:
            self.finished.set()


def app(responses, url):
    return Agent(
        instructions="",
        provider=provider(responses),
        tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")],
    )


@pytest.mark.parametrize("early", [False, True])
async def test_url_consent_waits_for_exact_peer_completion(responses: ResponsesFixture, early):
    peer = CompletionPeer(early=early)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("url-completion", "ordinary"))
        responses.respond(assistant_output("done"))
        try:
            async with app(responses, url).session() as session:
                turn = await session.submit("go")
                async with aclosing(session.interactions()) as cursor:
                    question = await anext(cursor)
                assert isinstance(question, ElicitationRequest)
                assert question.url == "https://example.invalid/never-open"
                assert question.elicitation_id == "synthetic-challenge-A"
                assert question.turn_id is None
                if early:
                    peer.before_accept.set()
                    await peer.early_sent.wait()
                await question.accept_url()
                await peer.answered.wait()
                assert peer.answers[0]["result"] == {"action": "accept"}
                with pytest.raises(InteractionExpiredError):
                    await question.accept_url()
                if not early:
                    abandoned = asyncio.create_task(question.wait_url_completion(timeout=8))
                    await asyncio.sleep(0)
                    abandoned.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await abandoned
                waiter = asyncio.create_task(question.wait_url_completion(timeout=8))
                if not early:
                    assert not waiter.done()
                peer.finish.set()
                await waiter
                assert (await turn.get_result()).final_output == "done"
                assert "synthetic action complete" in json.dumps(responses.requests[-1])
                assert len(peer.answers) == 1
                assert len(peer.replay_answers) == 1
                assert peer.replay_answers[0]["result"] == {"action": "cancel"}
                assert not await session.pending_interactions()
        finally:
            peer.finish.set()


async def test_url_completion_timeout_retires_the_issued_request(responses: ResponsesFixture):
    peer = CompletionPeer()
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("url-expiry", "ordinary"))
        responses.respond(assistant_output("done"))
        try:
            async with app(responses, url).session() as session:
                turn = await session.submit("go")
                async with aclosing(session.interactions()) as cursor:
                    question = await anext(cursor)
                await question.accept_url()
                with pytest.raises(TimeoutError):
                    await question.wait_url_completion(timeout=0.05)
                peer.finish.set()
                with pytest.raises(InteractionExpiredError):
                    await question.wait_url_completion(timeout=1)
                assert (await turn.get_result()).final_output == "done"
        finally:
            peer.finish.set()


async def test_url_callback_consent_uses_action_reply(responses: ResponsesFixture):
    peer = CompletionPeer(early=True)
    received = []

    def consent(question):
        received.append(question)
        peer.before_accept.set()
        peer.finish.set()
        return question.reply_url_accept()

    async with timeout(18), peer.serve() as url:
        responses.respond(selected("url-callback", "ordinary"))
        responses.respond(assistant_output("done"))
        result = await app(responses, url).run("go", on_interaction=consent)
        assert result.final_output == "done"
        assert len(received) == 1 and len(peer.answers) == 1
        assert peer.answers[0]["result"] == {"action": "accept"}


class RequiredPeer(AskingPeer):
    """A real tool error and independent MCP notification stream."""

    def __init__(self, ids=("required-A",), *, metas=None):
        super().__init__()
        self.ids = ids
        self.metas = metas
        self.notification_sent = asyncio.Event()
        self.connected = asyncio.Event()
        self.complete = asyncio.Event()
        self.closed = asyncio.Event()

    async def handle(self, request):
        body = await request.json()
        if body.get("method") == "initialize":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "required-peer", "version": "1"},
                    },
                },
                headers={"Mcp-Session-Id": "synthetic-required"},
            )
        if body.get("method") == "tools/call":
            await self.connected.wait()
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {
                        "code": -32042,
                        "message": "URL interaction required",
                        "data": {
                            "elicitations": [
                                {
                                    "mode": "url",
                                    "elicitationId": identity,
                                    "url": "https://example.invalid/never-open",
                                    "message": "Approve synthetic interaction",
                                    **(
                                        {"_meta": self.metas[index]}
                                        if self.metas is not None and self.metas[index] is not None
                                        else {}
                                    ),
                                }
                                for index, identity in enumerate(self.ids)
                            ]
                        },
                    },
                }
            )
        return await super().handle(request)

    async def notifications(self, request):
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        self.connected.set()
        await self.complete.wait()
        for identity in reversed(self.ids):
            value = {
                "jsonrpc": "2.0",
                "method": "notifications/elicitation/complete",
                "params": {"elicitationId": identity},
            }
            await stream.write(("event: message\ndata: " + json.dumps(value) + "\n\n").encode())
        self.notification_sent.set()
        await self.closed.wait()
        return stream

    @asynccontextmanager
    async def serve(self):
        app = web.Application()
        app.router.add_post("/mcp", self.handle)
        app.router.add_get("/mcp", self.notifications)
        server = TestServer(app)
        try:
            await server.start_server(shutdown_timeout=1)
            yield str(server.make_url("/mcp"))
        finally:
            self.closed.set()
            await server.close()


async def test_url_required_error_waits_without_an_invented_rpc_reply(responses: ResponsesFixture):
    peer = RequiredPeer()
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("url-required", "ordinary"))
        responses.respond(assistant_output("done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                question = await anext(cursor)
            assert question.elicitation_id == "required-A"
            await question.accept_url()
            assert len(responses.requests) == 1
            assert not peer.answers  # The tool error has no pending elicitation RPC.
            peer.complete.set()
            await question.wait_url_completion(timeout=8)
            assert (await turn.get_result()).final_output == "done"
            assert "server reported URL interaction complete" in json.dumps(responses.requests[-1])
            assert not peer.answers


async def test_close_retires_url_completion_waiters(responses: ResponsesFixture):
    peer = CompletionPeer()
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("url-close", "ordinary"))
        try:
            async with app(responses, url).session() as session:
                await session.submit("go")
                async with aclosing(session.interactions()) as cursor:
                    question = await anext(cursor)
                await question.accept_url()
                waiter = asyncio.create_task(question.wait_url_completion(timeout=8))
            with pytest.raises(InteractionExpiredError):
                await waiter
            with pytest.raises(InteractionExpiredError):
                await question.wait_url_completion(timeout=1)
        finally:
            peer.finish.set()


@pytest.mark.parametrize("decision", ["accept", "decline", "timeout"])
async def test_url_error_batch_is_registered_before_early_completion_and_retires_together(
    responses: ResponsesFixture, decision
):
    peer = RequiredPeer(ids=("required-A", "required-B"))
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("url-batch", "ordinary"))
        responses.respond(assistant_output("batch done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                first = await anext(cursor)
                # Even completion for B before its Python wrapper is read must be retained.
                peer.complete.set()
                await peer.notification_sent.wait()
                second = await anext(cursor)
            assert (first.elicitation_id, second.elicitation_id) == ("required-A", "required-B")
            if decision == "accept":
                await first.accept_url()
                assert len(responses.requests) == 1
                await second.accept_url()
                await asyncio.gather(
                    first.wait_url_completion(timeout=8), second.wait_url_completion(timeout=8)
                )
            elif decision == "decline":
                await second.decline()
                with pytest.raises(InteractionExpiredError):
                    await first.wait_url_completion(timeout=8)
                with pytest.raises(InteractionExpiredError):
                    await first.accept_url()
            else:
                with pytest.raises(TimeoutError):
                    await first.wait_url_completion(timeout=0.05)
                with pytest.raises(InteractionExpiredError):
                    await second.wait_url_completion(timeout=8)
            assert (await turn.get_result()).final_output == "batch done"
            assert not peer.answers
            assert not await session.pending_interactions()


async def test_duplicate_url_error_list_publishes_no_reply_authority(responses: ResponsesFixture):
    peer = RequiredPeer(ids=("same-id", "same-id"))
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("url-duplicate-batch", "ordinary"))
        responses.respond(assistant_output("rejected"))
        async with app(responses, url).session() as session:
            assert (await (await session.submit("go")).get_result()).final_output == "rejected"
            assert not await session.pending_interactions()
            assert not peer.answers


@pytest.mark.parametrize("marker", [True, None, 0, "false", [], {}])
@pytest.mark.parametrize("strict_index", [0, 1])
async def test_strict_url_error_member_rejects_whole_group_before_interaction(
    responses: ResponsesFixture, marker, strict_index
):
    metas = [None, None]
    metas[strict_index] = {"codex_strict_auto_review": marker}
    peer = RequiredPeer(ids=("group-A", "group-B"), metas=metas)
    observed = []

    def decline_unexpected(question):
        observed.append(question)
        return question.reply(None)

    async with timeout(18), peer.serve() as url:
        responses.respond(selected("strict-url-batch", "ordinary"))
        responses.respond(assistant_output("rejected"))
        result = await app(responses, url).run("go", on_interaction=decline_unexpected)
        assert result.final_output == "rejected"
        assert not observed, "a rejected group must publish no interaction"
        assert not peer.answers
        assert "server reported URL interaction complete" not in json.dumps(responses.requests[-1])


@pytest.mark.parametrize("markers", [(None, False), (False, None), (False, False), (None, None)])
async def test_false_or_absent_strict_metadata_keeps_url_group_completion(
    responses: ResponsesFixture, markers
):
    metas = [None if marker is None else {"codex_strict_auto_review": marker} for marker in markers]
    peer = RequiredPeer(ids=("group-A", "group-B"), metas=metas)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("non-strict-url-batch", "ordinary"))
        responses.respond(assistant_output("done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                first, second = await anext(cursor), await anext(cursor)
            await first.accept_url()
            await second.accept_url()
            peer.complete.set()
            await asyncio.gather(
                first.wait_url_completion(timeout=8), second.wait_url_completion(timeout=8)
            )
            assert (await turn.get_result()).final_output == "done"
            assert "server reported URL interaction complete" in json.dumps(responses.requests[-1])
            assert not await session.pending_interactions()
            assert not peer.answers
