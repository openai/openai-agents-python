"""Standard MCP forms preserve raw schemas through the rebuilt native loop."""

import json
import socket
import traceback
from contextlib import aclosing
from copy import deepcopy
from uuid import uuid4

import pytest

from agents import Agent, ElicitationRequest, InteractionExpiredError, InteractionRequired
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    native_engine,
    native_options,
    responses as responses,
)
from tests.test_native_extension_forms import SCHEMA, VALID, app
from tests.test_native_mcp_stdio import assert_stopped, records, stdio
from tests.test_native_mcp_tools import mcp_call
from tests.test_native_scoped_policies import AskingPeer, provider, selected

ROOT_CONSTRAINT = {
    "type": "object",
    "properties": {"count": {"type": "integer"}, "email": {"type": "string", "format": "email"}},
    "allOf": [{"properties": {"count": {"minimum": 2}}, "required": ["count"]}],
    "additionalProperties": False,
}
ROOT_ONLY = {
    "type": "object",
    "allOf": [{"properties": {"count": {"type": "integer", "minimum": 2}}, "required": ["count"]}],
}


@pytest.mark.parametrize("mode", ["form", None], ids=["explicit", "legacy"])
async def test_standard_nested_values_and_metadata_cross_native_http(
    responses: ResponsesFixture, mode
):
    peer = AskingPeer(
        schema=SCHEMA,
        form_mode=mode,
        form_meta={"synthetic-preserved": {"sample": True}, "progressToken": 33},
    )
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("standard-nested", "ordinary"))
        responses.respond(assistant_output("standard nested done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest)
            assert request.schema == SCHEMA and peer.prompted.is_set()
            assert request._message["request"]["mode"] == "form"
            assert request._message["request"]["_meta"] == {"synthetic-preserved": {"sample": True}}
            assert request.call is None
            detached = request.schema
            detached["properties"]["count"]["minimum"] = 0
            private = "synthetic-private-" + uuid4().hex
            for invalid in (
                {**VALID, "profile": {"name": private, "enabled": "true"}},
                {**VALID, "tags": ["x"]},
                {**VALID, "count": 0},
                {**VALID, "count": True},
                {**VALID, "tags": ("alpha",)},
                {**VALID, "note": float("nan")},
            ):
                with pytest.raises(ValueError, match="MCP form failed validation") as error:
                    await request.respond(invalid)
                assert error.value.__cause__ is None and error.value.__context__ is None
                assert private not in "".join(traceback.format_exception(error.value))
                assert not peer.answers and await session.pending_interactions() == (request,)
            await request.respond(deepcopy(VALID))
            with pytest.raises(InteractionExpiredError):
                await request.respond(VALID)
            assert (await turn.get_result()).final_output == "standard nested done"
            assert len(peer.answers) == 1 and peer.answers[0]["result"]["content"] == VALID
            assert "alpha" in json.dumps(responses.requests[-1])
            assert not await session.pending_interactions()


@pytest.mark.parametrize(
    "schema", [ROOT_CONSTRAINT, ROOT_ONLY], ids=["primitive-looking", "root-only"]
)
async def test_root_constraints_survive_without_empty_autoaccept(
    responses: ResponsesFixture, schema
):
    peer = AskingPeer(schema=schema)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("root-constraint", "ordinary"))
        responses.respond(assistant_output("constraint done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest) and request.schema == schema
            assert not peer.answers
            for invalid in ({}, {"count": 1}, {"count": True}):
                with pytest.raises(ValueError, match="MCP form failed validation"):
                    await request.respond(invalid)
                assert not peer.answers and await session.pending_interactions() == (request,)
            # General schemas treat formats as annotations. Existing primitive
            # form tests separately retain strict email and type checks.
            valid = {"count": 2, "email": "a format annotation"}
            await request.respond(valid)
            assert (await turn.get_result()).final_output == "constraint done"
            assert len(peer.answers) == 1 and peer.answers[0]["result"]["content"] == valid


@pytest.mark.parametrize(
    "schema", [SCHEMA, ROOT_CONSTRAINT], ids=["nested-ref-array", "root-keyword"]
)
async def test_standard_raw_schema_and_reply_cross_native_stdio(
    responses: ResponsesFixture, tmp_path, schema
):
    schema_file = tmp_path / "synthetic-form.json"
    schema_file.write_text(json.dumps(schema))
    log = tmp_path / "stdio-log"
    valid = VALID if schema is SCHEMA else {"count": 2}
    responses.respond(mcp_call("search", "mcp__local", "stdio-form"))
    responses.respond(assistant_output("stdio form done"))
    async with timeout(25):
        agent = Agent(
            instructions="Ask the stdio peer",
            provider=provider(responses),
            tools=[stdio(log, "--form-schema", str(schema_file), approval_mode="preapproved")],
        )
        async with agent.session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest) and request.schema == schema
            invalid = {**valid, "count": 0}
            with pytest.raises(ValueError, match="MCP form failed validation"):
                await request.respond(invalid)
            assert not any(item.get("message", {}).get("id") == 873 for item in records(log))
            await request.respond(deepcopy(valid))
            assert (await turn.get_result()).final_output == "stdio form done"
            replies = [
                item["message"] for item in records(log) if item.get("message", {}).get("id") == 873
            ]
            assert len(replies) == 1 and replies[0]["result"]["content"] == valid
            assert "count" in json.dumps(responses.requests[-1])
        await assert_stopped(log)


async def test_legacy_primitive_keeps_strict_validation(responses: ResponsesFixture):
    schema = {"type": "object", "properties": {"email": {"type": "string", "format": "email"}}}
    peer = AskingPeer(schema=schema, form_mode=None)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("legacy-primitive", "ordinary"))
        responses.respond(assistant_output("primitive done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest)
            with pytest.raises(ValueError, match="form"):
                await request.respond({"email": "invalid email"})
            await request.respond({"email": "person@example.invalid"})
            assert (await turn.get_result()).final_output == "primitive done"
            assert not await session.pending_interactions()
            assert len(peer.answers) == 1
            assert peer.answers[0]["result"]["content"] == {"email": "person@example.invalid"}


@pytest.mark.parametrize("additional", [None, False, True], ids=["absent", "false", "true"])
async def test_explicit_native_policy_keeps_empty_primitive_autoaccept(
    responses: ResponsesFixture, tmp_path, additional
):
    schema = {"type": "object", "properties": {}}
    if additional is not None:
        schema["additionalProperties"] = additional
    peer = AskingPeer(schema=schema, form_mode=None)
    async with timeout(18), peer.serve() as url:
        options = native_options(tmp_path, responses)
        options["config"]["mcp_servers"] = {"records": {"url": url}}
        responses.respond(selected("empty-primitive", "ordinary"))
        responses.respond(assistant_output("empty done"))
        async with native_engine(options) as engine:
            thread = await engine.start_replaceable_thread(json.dumps(options), "[]")
            try:
                await thread._submit(
                    json.dumps(
                        {
                            "mode": "start_if_idle",
                            "input": [{"type": "text", "text": "go"}],
                        }
                    )
                )
                while True:
                    event = json.loads(await thread.next_event())
                    assert event is not None
                    assert event["msg"]["type"] not in {"error", "elicitation_request"}, event
                    if event["msg"]["type"] == "task_complete":
                        break
                assert len(peer.answers) == 1
                assert peer.answers[0]["result"]["action"] == "accept"
                assert peer.answers[0]["result"]["content"] == {}
                assert len(responses.requests) == 2
            finally:
                await thread.close()


@pytest.mark.parametrize(
    "meta",
    [{"codex_approval_kind": "tool_suggestion"}, {"codex_strict_auto_review": True}],
    ids=["privileged-approval", "strict-auto-review"],
)
async def test_raw_form_metadata_cannot_bypass_existing_review_policy(
    responses: ResponsesFixture, meta
):
    peer = AskingPeer(schema=ROOT_ONLY, form_meta=meta)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("guarded-raw-form", "ordinary"))
        responses.respond(assistant_output("guarded done"))
        async with app(responses, url).session() as session:
            assert (await (await session.submit("go")).get_result()).final_output == "guarded done"
            assert not await session.pending_interactions()
            assert len(peer.answers) == 1 and peer.answers[0]["result"]["action"] == "decline"
            assert not peer.answers[0]["result"].get("content")


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"value": {"$ref": "https://example.invalid/private"}}},
        {"type": "object", "patternProperties": {"^(a+)+$": {"type": "string"}}},
        {"type": "object", "$defs": {"cycle": {"$ref": "#/$defs/cycle"}}},
        {"type": "array", "items": {"type": "string"}},
        {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
        {"type": "object", "properties": {"value": {"type": "integer", "minimum": "private"}}},
    ],
    ids=[
        "remote-ref",
        "regex",
        "recursive-ref",
        "array-root",
        "other-draft",
        "malformed-primitive",
    ],
)
async def test_standard_general_limits_refuse_before_public_grant(
    responses: ResponsesFixture, schema, monkeypatch
):
    real_getaddrinfo = socket.getaddrinfo
    lookups = []

    def local_only(host, *args, **kwargs):
        lookups.append(host)
        assert host in {"127.0.0.1", "localhost", "::1", None}
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_only)
    peer = AskingPeer(schema=schema)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("invalid-raw-schema", "ordinary"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            with pytest.raises(InteractionRequired, match="Unsupported MCP form schema") as error:
                await turn.get_result()
            assert error.value.__cause__ is None and error.value.__context__ is None
            assert not await session.pending_interactions()
        await peer.answered.wait()
        assert len(peer.answers) == 1 and peer.answers[0]["result"]["action"] == "cancel"
    assert "example.invalid" not in lookups


@pytest.mark.parametrize("startup", [False, True])
async def test_standard_nested_headless_and_startup_keep_joined_refusal(
    responses: ResponsesFixture, startup
):
    peer = AskingPeer(schema=SCHEMA, startup=startup)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("headless-standard", "ordinary"))
        with pytest.raises(InteractionRequired):
            await app(responses, url).run("go")
        await peer.answered.wait()
        assert peer.prompted.is_set()
        assert len(peer.answers) == 1 and peer.answers[0]["result"]["action"] == "cancel"
        if startup:
            assert not responses.requests


async def test_standard_nested_close_expires_and_cancels_exact_request(responses: ResponsesFixture):
    peer = AskingPeer(schema=SCHEMA)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("close-standard", "ordinary"))
        async with app(responses, url).session() as session:
            await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
        await peer.answered.wait()
        assert len(peer.answers) == 1 and peer.answers[0]["result"]["action"] == "cancel"
        with pytest.raises(InteractionExpiredError):
            await request.respond(VALID)


async def test_object_valued_additional_properties_use_nested_validation(
    responses: ResponsesFixture,
):
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "additionalProperties": {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 2}},
            "required": ["name"],
            "additionalProperties": False,
        },
    }
    peer = AskingPeer(schema=schema)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("nested-extra", "ordinary"))
        responses.respond(assistant_output("nested extra done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest) and request.schema == schema
            with pytest.raises(ValueError, match="MCP form failed validation"):
                await request.respond({"count": 2, "tenant": {"name": "x"}})
            assert not peer.answers and await session.pending_interactions() == (request,)
            valid = {"count": 2, "tenant": {"name": "Sample"}}
            await request.respond(valid)
            assert (await turn.get_result()).final_output == "nested extra done"
            assert len(peer.answers) == 1 and peer.answers[0]["result"]["content"] == valid


@pytest.mark.parametrize(
    ("field", "invalid", "valid"),
    [
        ({"type": "array", "items": {"enum": ["x", "valid"], "minLength": 2}}, ["x"], ["valid"]),
        (
            {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"const": "x", "title": "Short", "minLength": 2},
                        {"const": "valid", "title": "Long"},
                    ]
                },
            },
            ["x"],
            ["valid"],
        ),
        (
            {
                "type": "string",
                "oneOf": [
                    {"const": "x", "title": "Short", "minLength": 2},
                    {"const": "valid", "title": "Long"},
                ],
            },
            "x",
            "valid",
        ),
        (
            {
                "type": "string",
                "oneOf": [
                    {"const": "x", "title": "First"},
                    {"const": "x", "title": "Second"},
                    {"const": "valid", "title": "Long"},
                ],
            },
            "x",
            "valid",
        ),
    ],
    ids=[
        "enum-item-constraint",
        "anyof-item-constraint",
        "oneof-choice-constraint",
        "oneof-overlap",
    ],
)
async def test_complete_select_constraints_leave_invalid_reply_pending(
    responses: ResponsesFixture, field, invalid, valid
):
    schema = {"type": "object", "properties": {"answer": field}, "required": ["answer"]}
    peer = AskingPeer(schema=schema)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("complete-select", "ordinary"))
        responses.respond(assistant_output("select done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest) and request.schema == schema
            with pytest.raises(ValueError, match="MCP form failed validation"):
                await request.respond({"answer": invalid})
            assert not peer.answers and await session.pending_interactions() == (request,)
            await request.respond({"answer": valid})
            assert (await turn.get_result()).final_output == "select done"
            assert len(peer.answers) == 1 and peer.answers[0]["result"]["content"] == {
                "answer": valid
            }


@pytest.mark.parametrize("raw", [False, True], ids=["typed", "raw"])
@pytest.mark.parametrize("kind", ["mcp_tool_call", "malformed"])
async def test_standard_approval_metadata_keeps_non_strict_guardian_policy(
    responses: ResponsesFixture, tmp_path, raw, kind
):
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    if raw:
        schema["additionalProperties"] = False
    peer = AskingPeer(
        schema=schema,
        form_meta={
            "codex_request_type": "approval_request",
            "codex_approval_kind": kind,
            "tool_name": "search",
            "tool_params": {},
        },
    )
    async with timeout(18), peer.serve() as url:
        options = native_options(tmp_path, responses)
        options["config"].update(approval_policy="on-request", approvals_reviewer="auto_review")
        options["config"]["mcp_servers"] = {"records": {"url": url}}
        responses.respond(selected("guarded-approval", "ordinary"))
        responses.respond(assistant_output("approval declined"))
        async with native_engine(options) as engine:
            thread = await engine.start_replaceable_thread(json.dumps(options), "[]")
            try:
                await thread._submit(
                    json.dumps({"mode": "start_if_idle", "input": [{"type": "text", "text": "go"}]})
                )
                while True:
                    event = json.loads(await thread.next_event())
                    assert event is not None
                    assert event["msg"]["type"] not in {"error", "elicitation_request"}, event
                    if event["msg"]["type"] == "task_complete":
                        break
                assert len(peer.answers) == 1 and peer.answers[0]["result"]["action"] == "decline"
                assert not peer.answers[0]["result"].get("content")
            finally:
                await thread.close()


@pytest.mark.parametrize(
    "form_method", ["elicitation/create", "openai/form", "openai/elicitation/create"]
)
async def test_explicit_open_primitive_form_preserves_fields_and_accepts_extra_json(
    responses: ResponsesFixture, form_method
):
    schema = {
        "type": "object",
        "title": "Synthetic primitive form",
        "properties": {
            "count": {"type": "integer", "minimum": 1, "title": "Count"},
            "email": {
                "type": "string",
                "format": "email",
                "description": "Synthetic mailbox",
            },
            "choice": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["count", "email", "choice"],
        "additionalProperties": True,
    }
    metadata = {"synthetic-preserved": {"flag": True}}
    peer = AskingPeer(schema=schema, form_method=form_method, form_meta=metadata)
    valid = {
        "count": 2,
        "email": "person@example.invalid",
        "choice": "a",
        "extra": {"items": [None, True, 2, "extra"], "flag": False},
    }
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("open-primitive-form", "ordinary"))
        responses.respond(assistant_output("open form done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest) and request.schema == schema
            assert request._message["request"]["_meta"] == metadata
            detached = dict(request.schema)
            detached["properties"]["count"]["minimum"] = 0
            detached["additionalProperties"] = False
            assert request.schema == schema
            private = "synthetic-private-" + uuid4().hex
            invalid = [
                {key: value for key, value in valid.items() if key != "count"},
                {**valid, "count": True},
                {**valid, "count": 0},
                {**valid, "choice": "c"},
                {**valid, "extra": {"private": private, "value": object()}},
                {**valid, "extra": ("not", "JSON")},
                {**valid, "extra": {"value": float("nan")}},
                {**valid, "extra": {1: "non-string key"}},
                {**valid, 1: "non-string root key"},
            ]
            if form_method == "elicitation/create":
                # Opening unknown fields must preserve standard primitive strictness.
                invalid += [
                    {**valid, "count": 2.0},
                    {**valid, "email": "not a mailbox"},
                ]
            for value in invalid:
                with pytest.raises(ValueError) as error:
                    await request.respond(value)
                assert error.value.__cause__ is None and error.value.__context__ is None
                assert private not in "".join(traceback.format_exception(error.value))
                assert not peer.answers and await session.pending_interactions() == (request,)
            await request.respond(deepcopy(valid))
            assert (await turn.get_result()).final_output == "open form done"
            assert len(peer.answers) == 1 and peer.answers[0]["result"]["content"] == valid
            with pytest.raises(InteractionExpiredError):
                await request.respond(valid)


@pytest.mark.parametrize("additional", [None, False], ids=["omitted", "false"])
async def test_primitive_omitted_or_false_additional_fields_keep_strict_validation(
    responses: ResponsesFixture, additional
):
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 1},
            "email": {"type": "string", "format": "email"},
        },
        "required": ["count", "email"],
    }
    if additional is not None:
        schema["additionalProperties"] = additional
    valid = {"count": 2, "email": "person@example.invalid"}
    peer = AskingPeer(schema=schema)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("closed-primitive-control", "ordinary"))
        responses.respond(assistant_output("control done"))
        async with app(responses, url).session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, ElicitationRequest) and request.schema == schema
            for invalid in [
                {**valid, "extra": {"valid": "JSON"}},
                {**valid, "count": 2.0},
                {**valid, "count": True},
                {**valid, "count": 0},
                {**valid, "email": "not a mailbox"},
            ]:
                with pytest.raises(ValueError):
                    await request.respond(invalid)
                assert not peer.answers and await session.pending_interactions() == (request,)
            await request.respond(valid)
            assert (await turn.get_result()).final_output == "control done"
            assert len(peer.answers) == 1 and peer.answers[0]["result"]["content"] == valid
