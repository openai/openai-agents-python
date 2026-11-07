"""Forms cross the real native MCP transport before any SDK validation."""

import asyncio
import json
import traceback
from contextlib import aclosing
from uuid import uuid4

import pytest

from agents import Agent, ElicitationRequest, HttpMCP, InteractionExpiredError, InteractionRequired
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_scoped_policies import AskingPeer, provider, selected

SCHEMA = {
    "type": "object",
    "properties": {
        "display-name": {"type": "string", "minLength": 2, "maxLength": 7},
        "count": {"type": "integer", "minimum": 1, "maximum": 3},
        "price": {"type": "number", "minimum": 0.5, "maximum": 2.5},
        "choice": {"type": "string", "enum": ["alpha", "beta"]},
        "enabled": {"type": "boolean"},
        "email": {"type": "string", "format": "email"},
        "uri": {"type": "string", "format": "uri"},
        "date": {"type": "string", "format": "date"},
        "timestamp": {"type": "string", "format": "date-time"},
        "named": {"type": "string", "enum": ["a", "b"], "enumNames": ["Alpha", "Beta"]},
        "titled": {
            "type": "string",
            "oneOf": [{"const": "a", "title": "Alpha"}, {"const": "b", "title": "Beta"}],
        },
        "multiple": {
            "type": "array",
            "items": {"type": "string", "enum": ["a", "b", "c"]},
            "minItems": 1,
            "maxItems": 2,
        },
        "titled-multiple": {
            "type": "array",
            "items": {"anyOf": [{"const": "a", "title": "Alpha"}, {"const": "b", "title": "Beta"}]},
            "minItems": 1,
            "maxItems": 2,
        },
    },
    "required": ["display-name", "count", "choice"],
}
VALID = {
    "display-name": "A name",
    "count": 2,
    "price": 1.5,
    "choice": "beta",
    "enabled": True,
    "email": "person@example.invalid",
    "uri": "urn:example:case-7",
    "date": "2024-02-29",
    "timestamp": "2024-02-29T23:42:00Z",
    "named": "b",
    "titled": "a",
    "multiple": ["a", "c"],
    "titled-multiple": ["a", "b"],
}


@pytest.mark.parametrize("form_mode", ["form", "openai/form", "openaiForm"])
@pytest.mark.parametrize("reply", ["respond", "decline"])
async def test_real_primitive_form_constraints_and_correction(
    responses: ResponsesFixture, form_mode: str, reply: str
):
    peer = AskingPeer(form_mode=form_mode, schema=SCHEMA)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("form", "ordinary"))
        responses.respond(assistant_output("form done"))
        agent = Agent(
            instructions="Request information from the peer",
            provider=provider(responses),
            tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")],
        )
        async with agent.session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest) and peer.prompted.is_set()
            assert request.call is None
            assert (request.submission_id, request.turn_id) == (turn.submission_id, turn.turn_id)
            # Standard and OpenAI forms must preserve constraints through Core.
            actual = request.schema
            for name, constraints in SCHEMA["properties"].items():
                assert all(actual["properties"][name][k] == v for k, v in constraints.items())
            assert set(actual["required"]) == set(SCHEMA["required"])
            private_answer = "synthetic-private-" + uuid4().hex
            for key, value in [
                ("display-name", private_answer),
                ("display-name", "x"),
                ("count", 0),
                ("count", 4),
                ("count", True),
                ("price", 0.4),
                ("price", 2.6),
                ("choice", "gamma"),
                ("enabled", "true"),
                ("email", private_answer),
                ("email", "not two@addresses@example.invalid"),
                ("uri", "relative/path"),
                ("uri", "https://example.invalid/bad percent%GG"),
                ("uri", "https://example.invalid/<private>"),
                ("date", "2023-02-29"),
                ("date", "20240229"),
                ("timestamp", "2024-02-29"),
                ("timestamp", "2024-02-30T00:00:00Z"),
                ("named", "Beta"),
                ("titled", "Alpha"),
                ("multiple", []),
                ("multiple", ["a", "b", "c"]),
                ("multiple", ["Alpha"]),
                ("multiple", ["a", 2]),
                ("multiple", "a"),
                ("titled-multiple", ["Beta"]),
                ("titled-multiple", ["foreign"]),
                ("unknown", "value"),
            ]:
                with pytest.raises(ValueError, match="form") as invalid:
                    await request.respond({**VALID, key: value})
                assert private_answer not in str(invalid.value)
                assert private_answer not in "".join(traceback.format_exception(invalid.value))
                assert invalid.value.__cause__ is None
                assert invalid.value.__context__ is None
                assert not peer.answers
                assert (await session.pending_interactions()) == (request,)
            without_alias = {key: value for key, value in VALID.items() if key != "display-name"}
            with pytest.raises(ValueError, match="form"):
                await request.respond({**without_alias, "display_name": "A name"})
            if reply == "respond":
                await request.respond(VALID)
            else:
                await request.decline()
            with pytest.raises(InteractionExpiredError, match="expired|answered"):
                await request.decline()
            assert (await turn.get_result()).final_output == "form done"
            assert len(peer.answers) == 1
            result = peer.answers[0]["result"]
            assert result["action"] == ("accept" if reply == "respond" else "decline")
            if reply == "respond":
                assert result["content"] == VALID
            assert not await session.pending_interactions()


@pytest.mark.parametrize("reply", ["decline", "close"])
async def test_real_url_verification_has_only_live_decline(responses: ResponsesFixture, reply: str):
    peer = AskingPeer(unsupported=True)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("url", "ordinary"))
        responses.respond(assistant_output("url done"))
        agent = Agent(
            instructions="Ask",
            provider=provider(responses),
            tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")],
        )
        async with agent.session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest)
            assert request.submission_id is None and request.turn_id is None
            assert "https://example.invalid/never-visit" in request.description
            assert "synthetic-unsupported" in request.description
            assert request.schema == {}
            assert "never-visit" not in repr(request)
            with pytest.raises(ValueError, match="URL verification has no form"):
                await request.respond({"confirmed": True})
            assert (await session.pending_interactions()) == (request,)
            assert not peer.answers
            if reply == "decline":
                await request.decline()
                assert (await turn.get_result()).final_output == "url done"
                assert len(peer.answers) == 1
                assert peer.answers[0]["result"]["action"] == "decline"
                assert not peer.answers[0]["result"].get("content")
                with pytest.raises(InteractionExpiredError, match="expired|answered"):
                    await request.decline()
        if reply == "close":
            # Close joins local work; wait for the peer to record its cancellation.
            await peer.answered.wait()
            assert peer.answers == [{"id": 873, "jsonrpc": "2.0", "result": {"action": "cancel"}}]
            assert len(responses.requests) == 1
            with pytest.raises((ValueError, RuntimeError), match="expired|closed"):
                await request.decline()


@pytest.mark.parametrize("startup", [False, True])
async def test_url_headless_or_startup_has_no_grant(responses: ResponsesFixture, startup: bool):
    peer = AskingPeer(startup=startup, unsupported=True)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("url", "ordinary"))
        agent = Agent(
            instructions="Ask",
            provider=provider(responses),
            tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")],
        )
        with pytest.raises(InteractionRequired):
            await agent.run("go")
        assert peer.prompted.is_set()
        await peer.answered.wait()
        assert peer.answers == [{"id": 873, "jsonrpc": "2.0", "result": {"action": "cancel"}}]
        if startup:
            assert not peer.calls
            assert not responses.requests
        else:
            assert len(responses.requests) == 1


@pytest.mark.parametrize("mode", ["openai/form", "openaiForm"])
@pytest.mark.parametrize(
    "field",
    [
        {"type": "object", "properties": {"name": {"type": "string"}}},
        {"type": "array", "items": {"type": "string"}},
        {"$ref": "#/$defs/value"},
    ],
    ids=["object", "array", "ref"],
)
async def test_standard_method_rejects_nested_extension_mode_aliases(
    responses: ResponsesFixture, mode: str, field: dict
):
    from tests.test_native_root_environment_approval import Observe

    schema = {"type": "object", "properties": {"answer": field}, "required": ["answer"]}
    if "$ref" in field:
        schema["$defs"] = {"value": {"type": "string"}}
    peer = AskingPeer(schema=schema, form_mode=mode)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("unsupported", "ordinary"))
        responses.respond(assistant_output("form unavailable"))
        agent = Agent(
            instructions="Ask",
            provider=provider(responses),
            tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")],
        )
        async with agent.session() as session:
            observed = Observe(session._native)
            session._native = observed
            turn = await session.submit("go")
            assert (await turn.get_result()).final_output == "form unavailable"
            assert peer.prompted.is_set()
            assert len(peer.answers) == 1
            error = peer.answers[0]["error"]
            assert error["code"] == -32602
            assert error["message"] == "invalid MCP elicitation parameters"
            assert not any(e["msg"]["type"] == "elicitation_request" for e in observed.events)
            assert not await session.pending_interactions()


@pytest.mark.parametrize("reply", ["decline", "expire", "unread", "stopping", "idle"])
async def test_background_url_has_no_turn_authority(responses: ResponsesFixture, reply: str):
    from asyncio import Event

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from agents import function_tool
    from tests.native_fixtures import function_call
    from tests.test_native_mcp_tools import Peer

    class BackgroundPeer(Peer):
        def __init__(self):
            super().__init__(names=("search",))
            self.connected = Event()
            self.send_url = Event()
            self.answered = Event()
            self.answers = []

        async def handle(self, request):
            body = await request.json()
            if body.get("id") == 873 and "method" not in body:
                self.answers.append(body)
                self.answered.set()
                return web.Response(status=202)
            response = await super().handle(request)
            if body.get("method") == "initialize":
                response.headers["Mcp-Session-Id"] = "url-background-session"
            return response

        async def background(self, request):
            stream = web.StreamResponse(
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
            )
            await stream.prepare(request)
            self.connected.set()
            await self.send_url.wait()
            await stream.write(
                b'event: message\ndata: {"jsonrpc":"2.0","id":873,'
                b'"method":"elicitation/create","params":{"mode":"url",'
                b'"message":"Delayed original notification",'
                b'"url":"https://example.invalid/never-visit",'
                b'"elicitationId":"synthetic-delayed"}}\n\n'
            )
            await self.answered.wait()
            return stream

    peer = BackgroundPeer()
    entered, release = Event(), Event()
    retiring, retired = Event(), Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        try:
            await release.wait()
            return "successor effect"
        finally:
            if reply == "stopping":
                retiring.set()
                await retired.wait()

    app = web.Application()
    app.router.add_post("/mcp", peer.handle)
    app.router.add_get("/mcp", peer.background)
    async with timeout(25), TestServer(app) as server:
        responses.respond(selected("original-call", "ordinary"))
        responses.respond(assistant_output("original done"))
        responses.respond(function_call("hold", {}, "successor-call"))
        responses.respond(assistant_output("successor done"))
        agent = Agent(
            instructions="Read, then wait",
            provider=provider(responses),
            tools=[
                HttpMCP(
                    name="records",
                    url=str(server.make_url("/mcp")),
                    tools=["search"],
                    approval_mode="preapproved",
                ),
                hold,
            ],
        )
        async with agent.session() as session:
            original = await session.submit("Read")
            assert (await original.get_result()).final_output == "original done"
            await peer.connected.wait()
            if reply == "idle":
                peer.send_url.set()
                # The server must receive a real negative reply while the
                # Session is idle, before another turn or Session.close.
                await peer.answered.wait()
                assert len(peer.answers) == 1
                assert peer.answers[0]["result"]["action"] == "decline"
                assert not peer.answers[0]["result"].get("content")
                assert not await session.pending_interactions()
            successor = await session.submit("Wait")
            await entered.wait()
            if reply == "stopping":
                stopping = asyncio.create_task(successor.cancel())
                try:
                    await retiring.wait()
                    peer.send_url.set()
                    await peer.answered.wait()
                    assert len(peer.answers) == 1
                    assert peer.answers[0]["result"]["action"] == "decline"
                    assert not peer.answers[0]["result"].get("content")
                    assert not await session.pending_interactions()
                finally:
                    retired.set()
                    await stopping
                return
            if reply == "unread":
                peer.send_url.set()
                while not await session.pending_interactions():
                    await asyncio.sleep(0)
            elif reply != "idle":
                peer.send_url.set()
                async with aclosing(session.interactions()) as cursor:
                    question = await anext(cursor)
                assert isinstance(question, ElicitationRequest)
                assert question.session_id == session.id
                assert question.turn_id is None and question.submission_id is None
                assert question.schema == {}
                with pytest.raises(ValueError, match="URL verification has no form"):
                    await question.respond({"confirmed": True})
                if reply == "decline":
                    await question.decline()
                    await peer.answered.wait()
                    assert peer.answers[0]["result"]["action"] == "decline"
                    assert not peer.answers[0]["result"].get("content")
                    with pytest.raises(InteractionExpiredError, match="expired|answered"):
                        await question.decline()
            release.set()
            assert (await successor.get_result()).final_output == "successor done"
            if reply in {"expire", "unread"}:
                assert not await session.pending_interactions()
                if reply == "expire":
                    with pytest.raises(InteractionExpiredError, match="expired|answered"):
                        await question.decline()
                await peer.answered.wait()
                assert len(peer.answers) == 1
                assert peer.answers[0]["result"]["action"] == "decline"
                assert not peer.answers[0]["result"].get("content")


async def test_one_shot_url_reaches_handler_as_negative_only_request(
    responses: ResponsesFixture,
):
    received = []
    peer = AskingPeer(unsupported=True)
    async with timeout(12), peer.serve() as url:
        responses.respond(selected("url-oneshot", "ordinary"))
        responses.respond(assistant_output("url declined"))
        agent = Agent(
            instructions="Ask",
            provider=provider(responses),
            tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")],
        )

        async def decline(request):
            received.append(request)
            assert isinstance(request, ElicitationRequest)
            assert request.submission_id is None and request.turn_id is None
            assert request.schema == {}
            with pytest.raises(RuntimeError, match="A callback must return its reply"):
                await request.respond({"confirmed": True})
            return request.reply(None)

        result = await agent.run("go", on_interaction=decline)
        assert result.final_output == "url declined"
        assert len(received) == 1 and len(peer.answers) == 1
        assert peer.answers[0]["result"]["action"] == "decline"
        assert not peer.answers[0]["result"].get("content")
        with pytest.raises((ValueError, RuntimeError), match="expired|closed|answered"):
            await received[0].decline()


@pytest.mark.parametrize("timing", ["successor", "idle", "stopping"])
@pytest.mark.parametrize(
    ("form_method", "nested"),
    [
        ("elicitation/create", False),
        ("openai/form", False),
        ("openai/elicitation/create", False),
        ("elicitation/create", True),
    ],
)
async def test_unowned_root_form_has_no_successor_authority(
    responses: ResponsesFixture, timing: str, form_method: str, nested: bool
):
    from asyncio import FIRST_COMPLETED, Event, create_task, gather, wait

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from agents import function_tool
    from tests.native_fixtures import function_call
    from tests.test_native_mcp_tools import Peer

    class BackgroundPeer(Peer):
        def __init__(self):
            super().__init__(names=("search",))
            self.connected = Event()
            self.send_form = Event()
            self.form_sent = Event()
            self.answered = Event()
            self.answers = []

        async def handle(self, request):
            body = await request.json()
            if body.get("id") == 873 and "method" not in body:
                self.answers.append(body)
                self.answered.set()
                return web.Response(status=202)
            response = await super().handle(request)
            if body.get("method") == "initialize":
                response.headers["Mcp-Session-Id"] = "form-background-session"
            return response

        async def background(self, request):
            stream = web.StreamResponse(
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
            )
            await stream.prepare(request)
            self.connected.set()
            await self.send_form.wait()
            prompt = {
                "jsonrpc": "2.0",
                "id": 873,
                "method": form_method,
                "params": {
                    "mode": "form",
                    "message": "Delayed original notification",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "array", "items": {"type": "string"}}
                            if nested
                            else {"type": "string"}
                        },
                    },
                },
            }
            await stream.write(("event: message\ndata: " + json.dumps(prompt) + "\n\n").encode())
            self.form_sent.set()
            await self.answered.wait()
            return stream

    peer = BackgroundPeer()
    entered, release = Event(), Event()
    retiring, retired = Event(), Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        try:
            await release.wait()
            return "successor effect"
        finally:
            if timing == "stopping":
                retiring.set()
                await retired.wait()

    app = web.Application()
    app.router.add_post("/mcp", peer.handle)
    app.router.add_get("/mcp", peer.background)
    async with timeout(25), TestServer(app) as server:
        responses.respond(selected("original-call", "ordinary"))
        responses.respond(assistant_output("original done"))
        responses.respond(function_call("hold", {}, "successor-call"))
        responses.respond(assistant_output("successor done"))
        agent = Agent(
            instructions="Read, then wait",
            provider=provider(responses),
            tools=[
                HttpMCP(
                    name="records",
                    url=str(server.make_url("/mcp")),
                    tools=["search"],
                    approval_mode="preapproved",
                ),
                hold,
            ],
        )
        async with agent.session() as session:
            original = await session.submit("Read")
            assert (await original.get_result()).final_output == "original done"
            await peer.connected.wait()
            if timing == "idle":
                peer.send_form.set()
                await peer.form_sent.wait()
                answered = create_task(peer.answered.wait())
                try:
                    done, _ = await wait((answered,), timeout=5)
                    assert answered in done, (
                        "Idle Session received no native decline after the peer sent "
                        "a form and the original completed; no successor has started"
                    )
                finally:
                    answered.cancel()
                    await gather(answered, return_exceptions=True)
                assert len(responses.requests) == 2
                assert len(peer.answers) == 1
                assert peer.answers[0]["result"]["action"] == "decline"
                assert not peer.answers[0]["result"].get("content")
                assert not await session.pending_interactions()
            successor = await session.submit("Wait")
            await entered.wait()
            if timing == "stopping":
                stopping = create_task(successor.cancel())
                try:
                    await retiring.wait()
                    peer.send_form.set()
                    await peer.answered.wait()
                    assert peer.answers[0]["result"]["action"] == "decline"
                    assert not await session.pending_interactions()
                finally:
                    retired.set()
                    await stopping
                return
            if timing == "successor":
                async with aclosing(session.interactions()) as cursor:
                    granted = create_task(anext(cursor))
                    answered = create_task(peer.answered.wait())
                    try:
                        peer.send_form.set()
                        done, _ = await wait((granted, answered), return_when=FIRST_COMPLETED)
                        if granted in done:
                            request = granted.result()
                            pytest.fail(
                                "Delayed form was exposed while successor ran only Python: "
                                f"{type(request).__name__} with "
                                f"submission_id={request.submission_id!r}, "
                                f"turn_id={request.turn_id!r}; "
                                f"successor={successor.submission_id!r}/{successor.turn_id!r}"
                            )
                        assert answered in done and peer.answered.is_set()
                    finally:
                        granted.cancel()
                        answered.cancel()
                        await gather(granted, answered, return_exceptions=True)
            assert len(peer.answers) == 1
            assert peer.answers[0]["result"]["action"] == "decline"
            assert not peer.answers[0]["result"].get("content")
            assert not await session.pending_interactions()
            release.set()
            assert (await successor.get_result()).final_output == "successor done"
