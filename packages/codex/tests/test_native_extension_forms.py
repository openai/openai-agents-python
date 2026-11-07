"""Actual OpenAI form methods preserve nested JSON through the native loop."""

import json
import traceback
from contextlib import aclosing
from copy import deepcopy
from uuid import uuid4

import pytest

from agents import Agent, ElicitationRequest, HttpMCP, InteractionExpiredError, InteractionRequired
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_scoped_policies import AskingPeer, provider, selected

METHODS = ["openai/form", "openai/elicitation/create"]
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "$defs": {"tag": {"type": "string", "minLength": 2}},
    "properties": {
        "profile": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
            "required": ["name", "enabled"],
            "additionalProperties": False,
        },
        "tags": {"type": "array", "items": {"$ref": "#/$defs/tag"}, "minItems": 1},
        "count": {"type": "integer", "minimum": 1},
        "note": {"type": ["string", "null"]},
        "format": {"type": "string", "format": "email"},
    },
    "required": ["profile", "tags", "count", "note"],
    "additionalProperties": False,
}
VALID = {
    "profile": {"name": "Sample", "enabled": True},
    "tags": ["alpha", "beta"],
    "count": 2,
    "note": None,
    # Extension formats are annotations; standard primitive rules stay separate.
    "format": "an annotation",
}


def app(responses, url):
    return Agent(
        instructions="Ask the peer",
        provider=provider(responses),
        tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")],
    )


@pytest.mark.parametrize(
    ("definition_name", "reference_name"),
    [("tag", "tag"), ("a/b", "a~1b"), ("a~b", "a~0b"), ("a b", "a%20b"), ("a%2Fb", "a%252Fb")],
)
@pytest.mark.parametrize("form_method", METHODS)
async def test_nested_extension_values_cross_native_transport(
    responses: ResponsesFixture, form_method, definition_name, reference_name
):
    schema = deepcopy(SCHEMA)
    schema["$defs"] = {definition_name: schema["$defs"]["tag"]}
    schema["properties"]["tags"]["items"]["$ref"] = f"#/$defs/{reference_name}"
    if form_method == "openai/form":
        del schema["$schema"]  # Absent draft means 2020-12.
    peer = AskingPeer(form_method=form_method, schema=schema)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("nested-form", "ordinary"))
        responses.respond(assistant_output("nested done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest)
            assert request.schema == schema
            assert request.call is None
            # Core supplies a Session token, not originating call/turn proof.
            assert peer.prompted.is_set() and len(responses.requests) == 1
            initialize = next(r for r in peer.requests if r.get("method") == "initialize")
            extensions = initialize["params"]["capabilities"]["extensions"]
            assert extensions["openai/form"] == {}
            assert extensions["openai/elicitation"]["form"] == {}
            detached = request.schema
            detached["properties"]["count"]["minimum"] = 0
            private = "synthetic-private-" + uuid4().hex
            for invalid in [
                {**VALID, "profile": {"name": private, "enabled": "true"}},
                {**VALID, "tags": ["x"]},
                {**VALID, "count": 0},
                {**VALID, "count": True},
                {**VALID, "count": "2"},
                {**VALID, "note": object()},
                {**VALID, "tags": ("alpha",)},
                {**VALID, "note": float("nan")},
                {**VALID, "note": float("inf")},
                {**VALID, "profile": {1: private}},
                {**VALID, "unknown": private},
            ]:
                with pytest.raises(ValueError, match="MCP form failed validation") as error:
                    await request.respond(invalid)
                assert error.value.__cause__ is None and error.value.__context__ is None
                assert private not in "".join(traceback.format_exception(error.value))
                assert not peer.answers
                assert await session.pending_interactions() == (request,)
            await request.respond(deepcopy(VALID))
            with pytest.raises(InteractionExpiredError):
                await request.respond(VALID)
            assert (await turn.get_result()).final_output == "nested done"
            assert len(peer.answers) == 1
            assert peer.answers[0]["result"]["content"] == VALID
            assert "alpha" in json.dumps(responses.requests[-1])
            assert not await session.pending_interactions()


BAD_SCHEMAS = [
    {"type": "object", "properties": {"unused": {"type": "string", "pattern": "^(a+)+$"}}},
    {"type": "object", "patternProperties": {"^(a+)+$": {"type": "string"}}},
    {
        "type": "object",
        "$defs": {"unused": {"type": "string", "pattern": "^(a+)+$"}},
    },
    {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
    {"type": "object", "$id": "https://example.invalid/base"},
    {"type": "object", "$dynamicRef": "#node"},
    {"type": "object", "$vocabulary": {"https://example.invalid/private": True}},
    {"type": "object", "properties": {"unused": {"$ref": "https://example.invalid/private"}}},
    {"type": "object", "properties": {"unused": {"$ref": "relative.json"}}},
    {"type": "object", "properties": {"unused": {"$ref": "#/$defs/missing"}}},
    {
        "type": "object",
        "$defs": {"cycle": {"$ref": "#/$defs/cycle"}},
        "properties": {"unused": {"$ref": "#/$defs/cycle"}},
    },
    {
        "type": "object",
        "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"type": "string"}},
        "properties": {"unused": {"$ref": "#/$defs/a"}},
    },
    {
        "type": "object",
        "$defs": {"a": {"default": {"$dynamicRef": "https://example.invalid/private"}}},
        "properties": {"unused": {"$ref": "#/$defs/a/default"}},
    },
    {"type": "object", "properties": {"unused": {"type": "private-invalid-type"}}},
]


# A Boolean target may share identity with an allowed root definition. The
# pointer must still name that definition directly, not its schema or metadata.
BAD_SCHEMAS.extend(
    pytest.param(
        {
            "type": "object",
            "$defs": {"boolean": value, "a": {keyword: value}},
            "properties": {"unused": {"$ref": f"#/$defs/a{separator}{keyword}"}},
        },
        id=f"nested-{value}-{keyword}-{separator}",
    )
    for value in (True, False)
    for keyword in ("items", "default")
    for separator in ("/", "%2F", "%2f")
)


@pytest.mark.parametrize("schema", BAD_SCHEMAS)
async def test_unsupported_extension_schema_fails_before_public_grant(
    responses: ResponsesFixture, schema, monkeypatch
):
    import socket

    # A peer always uses loopback. Reject any attempted lookup of schema hosts.
    real_getaddrinfo = socket.getaddrinfo
    lookups = []

    def local_only(host, *args, **kwargs):
        lookups.append(host)
        assert host in {"127.0.0.1", "localhost", "::1", None}
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_only)
    peer = AskingPeer(form_method="openai/elicitation/create", schema=schema)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("bad-schema", "ordinary"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            with pytest.raises(InteractionRequired, match="Unsupported MCP form schema") as error:
                await turn.get_result()
            assert error.value.__cause__ is None and error.value.__context__ is None
            assert "private-invalid-type" not in str(error.value)
            assert "private-invalid-type" not in "".join(traceback.format_exception(error.value))
            assert not await session.pending_interactions()
        await peer.answered.wait()
        assert len(peer.answers) == 1
        assert peer.answers[0]["result"]["action"] == "cancel"
    assert "example.invalid" not in lookups


@pytest.mark.parametrize("form_method", METHODS)
@pytest.mark.parametrize("startup", [False, True])
async def test_extension_headless_and_startup_refuse(
    responses: ResponsesFixture, form_method, startup
):
    peer = AskingPeer(form_method=form_method, schema=SCHEMA, startup=startup)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("headless-extension", "ordinary"))
        with pytest.raises(InteractionRequired):
            await app(responses, url).run("go")
        await peer.answered.wait()
        assert peer.prompted.is_set()
        assert len(peer.answers) == 1 and peer.answers[0]["result"]["action"] == "cancel"
        if startup:
            assert not responses.requests


@pytest.mark.parametrize("form_method", METHODS)
async def test_close_expires_pending_extension_request(responses: ResponsesFixture, form_method):
    peer = AskingPeer(form_method=form_method, schema=SCHEMA)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("close-extension", "ordinary"))
        async with app(responses, url).session() as session:
            await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
        await peer.answered.wait()
        assert peer.answers[0]["result"]["action"] == "cancel"
        with pytest.raises(InteractionExpiredError):
            await request.respond(VALID)


@pytest.mark.parametrize("form_method", METHODS)
async def test_one_shot_extension_reply_uses_existing_mapping_contract(
    responses: ResponsesFixture, form_method
):
    peer = AskingPeer(form_method=form_method, schema=SCHEMA)
    received = []
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("one-shot-extension", "ordinary"))
        responses.respond(assistant_output("extension replied"))

        def answer(request):
            received.append(request)
            return request.reply(VALID)

        result = await app(responses, url).run("go", on_interaction=answer)
        assert result.final_output == "extension replied"
        assert len(received) == 1
        assert len(peer.answers) == 1 and peer.answers[0]["result"]["content"] == VALID
        with pytest.raises(InteractionExpiredError):
            await received[0].respond(VALID)
