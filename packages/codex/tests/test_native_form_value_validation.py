"""Form answers preserve decimal values and enforce the primitive format profile."""

from contextlib import aclosing

import pytest

from agents import ElicitationRequest
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_extension_forms import app
from tests.test_native_scoped_policies import AskingPeer, selected


@pytest.mark.parametrize(
    "method", ["elicitation/create", "openai/form", "openai/elicitation/create"]
)
async def test_decimal_multiples_remain_exact_and_correctable(responses: ResponsesFixture, method):
    schema = {
        "type": "object",
        "$defs": {
            "tiny": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "number",
                "multipleOf": 1e-30,
            }
        },
        "properties": {
            "amount": {"type": "number", "multipleOf": 0.1, "minimum": -1, "maximum": 1},
            "tiny": {"$ref": "#/$defs/tiny"},
            "count": {"type": "integer", "multipleOf": 2},
            "annotation": {"type": "string", "multipleOf": 0.1},
        },
        "required": ["amount", "tiny", "count", "annotation"],
        "additionalProperties": False,
    }
    valid = {"amount": 0.3, "tiny": 3e-30, "count": 2, "annotation": "not numeric"}
    peer = AskingPeer(schema=schema, form_method=method)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("decimal-form", "ordinary"))
        responses.respond(assistant_output("done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest)
            assert request.schema == schema
            for invalid in [
                {**valid, "amount": 0.31},
                {**valid, "amount": 0.30000000000000004},
                {**valid, "amount": 1.1},
                {**valid, "amount": True},
                {**valid, "amount": "0.3"},
                {**valid, "tiny": 3.000000000000001e-30},
                {**valid, "count": 3},
            ]:
                with pytest.raises(ValueError, match="MCP form failed validation"):
                    request.reply(invalid)
                with pytest.raises(ValueError, match="MCP form failed validation"):
                    await request.respond(invalid)
                assert not peer.answers
                assert await session.pending_interactions() == (request,)
            request.reply(valid)
            request.reply({**valid, "amount": -0.3})
            assert not peer.answers
            await request.respond(valid)
            assert (await turn.get_result()).final_output == "done"
            assert peer.answers[0]["result"]["content"] == valid
            assert not await session.pending_interactions()


FORMAT_SCHEMA = {
    "type": "object",
    "properties": {
        "uri": {"type": "string", "format": "uri"},
        "datetime": {"type": "string", "format": "date-time"},
        "email": {"type": "string", "format": "email"},
    },
    "required": ["uri", "datetime", "email"],
}
FORMAT_VALID = {
    "uri": "https://example.invalid:443/path?key=value#fragment",
    "datetime": "2024-02-29T23:42:00+01:30",
    "email": "person@example.invalid",
}


async def test_primitive_formats_reject_invalid_components_without_consuming_request(
    responses: ResponsesFixture,
):
    peer = AskingPeer(schema=FORMAT_SCHEMA)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("primitive-form", "ordinary"))
        responses.respond(assistant_output("done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest)
            invalid_uris = [
                "https://example.invalid:abc/path",
                "https://user@@example.invalid/path",
                "https://example.invalid/path[part]",
                "https://2001:db8::1/path",
                "https://example.invalid/%zz",
                "relative/path",
            ]
            invalid_times = [
                "2024-02-29T24:00:00Z",
                "2024-02-29T25:00:00Z",
                "2024-02-29T12:00:00+24:00",
                "2024-02-29T12:00:00+01:60",
                "2024-02-29T23:59:60Z",
            ]
            for key, values in [("uri", invalid_uris), ("datetime", invalid_times)]:
                for invalid in values:
                    answer = {**FORMAT_VALID, key: invalid}
                    with pytest.raises(ValueError, match="MCP form failed validation"):
                        request.reply(answer)
                    with pytest.raises(ValueError, match="MCP form failed validation"):
                        await request.respond(answer)
                    assert not peer.answers
                    assert await session.pending_interactions() == (request,)
            for uri in [
                "urn:example:case-7",
                "custom://example.invalid:999999/path",
                "custom://example.invalid:/path",
                "https://user:password@example.invalid/path",
                "https://[2001:db8::1]:443/path",
                "https://example.invalid/path%5Bpart%5D",
            ]:
                request.reply({**FORMAT_VALID, "uri": uri})
                assert not peer.answers
            # Preserve the existing mailbox grammar and lowercase temporal spelling.
            answer = {
                **FORMAT_VALID,
                "uri": "urn:example:case-7",
                "datetime": "2024-02-29t23:42:00z",
                "email": "person(comment)@example.invalid",
            }
            await request.respond(answer)
            assert (await turn.get_result()).final_output == "done"
            assert peer.answers[0]["result"]["content"] == answer
            assert not await session.pending_interactions()


async def test_general_form_formats_stay_annotations(responses: ResponsesFixture):
    peer = AskingPeer(schema={**FORMAT_SCHEMA, "allOf": [{}]})
    answer = {"uri": "relative/path", "datetime": "2024-02-29T24:00:00Z", "email": "annotation"}
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("annotation-form", "ordinary"))
        responses.respond(assistant_output("done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest)
            await request.respond(answer)
            assert (await turn.get_result()).final_output == "done"
            assert peer.answers[0]["result"]["content"] == answer
