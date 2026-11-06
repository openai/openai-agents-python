"""Core schema projection preserves declarations while Python enforces every constraint."""

import asyncio
from copy import deepcopy
from typing import Annotated

import pytest
from pydantic import BaseModel, Field, ValidationError

from agents import Agent, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_agent import function_declarations
from tests.test_native_provider_websockets import peer as ws_peer
from tests.test_sdk_session_transport import http_peer


class Owner(BaseModel):
    team: Annotated[str, Field(min_length=2, description="Team slug")]


class Record(BaseModel):
    owner: Owner
    label: Annotated[str, Field(min_length=4, description="Record label")]


RECORD = {"owner": {"team": "eu"}, "label": "atlas"}


def lookup_for(origin, minimum, effects, description="Read one record with a team owner."):
    @function_tool(description=description)
    async def lookup(
        key: Annotated[str, Field(min_length=minimum, description="Record key")],
        record: Record,
    ) -> str:
        effects.append((origin, key, record.owner.team))
        return f"{origin}:{key}:{record.label}"

    return lookup


def declaration(request):
    [tool] = function_declarations(request)
    assert tool["name"] == "lookup"
    return tool


def check_captured_schema(tool):
    """Check the pinned Core subset on an actual provider request."""
    schema = tool["parameters"]
    assert schema["properties"]["key"]["type"] == "string"
    assert schema["properties"]["key"]["description"] == "Record key"
    assert schema["properties"]["record"]["$ref"] == "#/$defs/Record"
    assert schema["$defs"]["Record"]["properties"]["owner"]["$ref"] == "#/$defs/Owner"
    assert set(schema["required"]) == {"key", "record"}


@pytest.mark.parametrize(
    ("route", "model"),
    [
        pytest.param("ordinary", "gpt-5.2", id="ordinary"),
        pytest.param("ordinary", None, id="http-lite"),
        pytest.param("compact", "gpt-5.2", id="compact"),
        pytest.param("recovery", "gpt-5.2", id="recovery"),
    ],
)
async def test_core_sampling_keeps_supported_schema_and_full_python_validation(route, model):
    effects = []
    lookup = lookup_for("one", 3, effects)
    async with timeout(30), http_peer(expected_model=model or "gpt-6.1-sol") as peer:
        agent = Agent(
            model=model,
            instructions="Use the record tool when asked.",
            tools=[lookup],
            provider=peer.provider(),
        )
        # Capture the supported schema from real native transport.
        peer.respond(assistant_output("ready"))
        async with agent.session() as first:
            assert (
                await (await first.submit("Remember atlas.")).get_result()
            ).final_output == "ready"
            original = declaration(peer.requests[-1])
            check_captured_schema(original)
            if route == "compact":
                peer.respond(assistant_output("Atlas remembered."))
                await first.compact()
                # Native summaries intentionally do not publish callable tools.
                assert peer.requests[-1]["tools"] == []
            elif route == "recovery":
                peer.respond(
                    function_call("lookup", {"key": "x", "record": RECORD}, "bad-ordinary")
                )
                with pytest.raises(ValidationError, match="at least 3 characters"):
                    await (await first.submit("Read the invalid key.")).get_result()
                assert effects == []
            before = len(peer.requests)
            peer.respond(function_call("lookup", {"key": "record-a", "record": RECORD}, "valid"))
            peer.respond(assistant_output("found"))
            assert (
                await (await first.submit("Read record-a.")).get_result()
            ).final_output == "found"
            actual = peer.requests[before:]

        assert effects == [("one", "record-a", "eu")]
        assert tool_output(actual[-1], "valid") == "one:record-a:atlas"
        assert len(actual) == 2
        for request in actual:
            assert declaration(request) == original


@pytest.mark.parametrize("model", ["gpt-5.2", None], ids=["http", "http-lite"])
async def test_core_checkpoint_restore_after_compaction_keeps_supported_schema(model):
    effects = []
    lookup = lookup_for("restored", 3, effects)
    async with timeout(30), http_peer(expected_model=model or "gpt-6.1-sol") as peer:
        agent = Agent(
            model=model,
            instructions="Use the record tool when asked.",
            tools=[lookup],
            provider=peer.provider(),
        )
        async with agent.session() as session:
            peer.respond(assistant_output("ready"))
            assert (
                await (await session.submit("Remember atlas.")).get_result()
            ).final_output == "ready"
            original = deepcopy(declaration(peer.requests[-1]))
            check_captured_schema(original)
            assert original["description"] == "Read one record with a team owner."

            peer.respond(assistant_output("Atlas remembered."))
            await session.compact()
            assert function_declarations(peer.requests[-1]) == []
            state = await session.save_state()
        assert effects == []
        assert len(peer.requests) == 2

        before_restore = len(peer.requests)
        async with agent.session(state=state) as restored:
            peer.respond(
                function_call("lookup", {"key": "record-a", "record": RECORD}, "restored-call")
            )
            peer.respond(assistant_output("found after restore"))
            assert (
                await (await restored.submit("Read record-a.")).get_result()
            ).final_output == "found after restore"

        actual = peer.requests[before_restore:]
        assert len(actual) == 2
        assert effects == [("restored", "record-a", "eu")]
        assert tool_output(actual[-1], "restored-call") == "restored:record-a:atlas"
        for request in actual:
            assert declaration(request) == original


async def test_websocket_lite_uses_current_opening_override_schema():
    effects = []
    first_tool = lookup_for("one", 3, effects)
    updated_tool = lookup_for(
        "one", 5, effects, description="Read a record with the new key format."
    )
    async with timeout(30), ws_peer() as peer:
        agent = Agent(
            model=None,
            instructions="Use the record tool when asked.",
            tools=[first_tool],
            provider=peer.provider(websocket=True),
        )
        async with agent.session() as session:
            peer.respond(assistant_output("ready"))
            assert (
                await (await session.submit("Remember atlas.")).get_result()
            ).final_output == "ready"
        async with agent.session(tools=[updated_tool]) as session:
            peer.respond(assistant_output("updated"))
            assert (
                await (await session.submit("Remember changed key requirements.")).get_result()
            ).final_output == "updated"

        assert effects == []
        assert not peer.http_requests
        assert peer.upgrades
        # Separate openings can warm their definitions in a nongenerating frame.
        # Compare reconstructed request rosters from their distinct connections.
        generating = [frame for frame in peer.frames if frame.get("generate") is not False]
        assert len(generating) == 2
        after_update = declaration(peer.requests[-1])
        # The first provider context may receive the original catalog in the
        # connection warmup; no update has happened at that point.
        before_update = declaration(peer.requests[0])
        check_captured_schema(after_update)
        check_captured_schema(before_update)
        assert before_update["description"] == "Read one record with a team owner."
        expected = deepcopy(before_update)
        expected["description"] = "Read a record with the new key format."
        assert after_update == expected


async def test_same_name_opening_overrides_stay_with_their_core_session():
    effects = []
    old = lookup_for("left-old", 3, effects)
    new = lookup_for("left-new", 9, effects)
    other = lookup_for("right", 6, effects)
    async with timeout(30), http_peer() as left, http_peer() as right:
        left_agent = Agent(
            model="gpt-5.2", instructions="Read records.", tools=[old], provider=left.provider()
        )
        right_agent = Agent(
            model="gpt-5.2", instructions="Read records.", tools=[other], provider=right.provider()
        )
        async with (
            left_agent.session(tools=[new]) as first,
            right_agent.session() as second,
        ):
            left.respond(assistant_output("left ready"))
            right.respond(assistant_output("right ready"))
            replies = await asyncio.gather(
                (await first.submit("left initial")).get_result(),
                (await second.submit("right initial")).get_result(),
            )
            assert [reply.final_output for reply in replies] == ["left ready", "right ready"]
            first_schema = declaration(left.requests[-1])
            other_schema = declaration(right.requests[-1])

            # 'sixkey' is valid for the untouched right Session and invalid for
            # the accepted left replacement. Effects prove validator/dispatch ownership.
            args = {"key": "sixkey", "record": RECORD}
            left.respond(function_call("lookup", args, "bad-left"))
            right.respond(function_call("lookup", args, "good-right"))
            right.respond(assistant_output("right found"))
            results = await asyncio.gather(
                (await first.submit("Read sixkey left.")).get_result(),
                (await second.submit("Read sixkey right.")).get_result(),
                return_exceptions=True,
            )
            assert isinstance(results[0], ValidationError)
            assert results[1].final_output == "right found"
            assert effects == [("right", "sixkey", "eu")]

            left.respond(
                function_call("lookup", {"key": "long-record", "record": RECORD}, "good-left")
            )
            left.respond(assistant_output("left found"))
            assert (
                await (await first.submit("Read long-record.")).get_result()
            ).final_output == "left found"
            assert effects == [("right", "sixkey", "eu"), ("left-new", "long-record", "eu")]
            assert tool_output(left.requests[-1], "good-left") == "left-new:long-record:atlas"
            assert tool_output(right.requests[-1], "good-right") == "right:sixkey:atlas"

        check_captured_schema(first_schema)
        check_captured_schema(other_schema)
        for request in left.requests[1:]:
            check_captured_schema(declaration(request))
        for request in right.requests[1:]:
            assert declaration(request) == other_schema
