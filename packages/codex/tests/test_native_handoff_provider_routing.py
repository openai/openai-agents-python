from __future__ import annotations

import json

import pytest

from agents import Agent
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_provider_websockets import peer


@pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
async def test_native_handoff_provider_scope_survives_portable_restore(websocket: bool) -> None:
    async with timeout(30), peer() as source, peer() as target:
        source.respond(function_call("transfer_to_destination", {}, "source-transfer"))
        target.respond(assistant_output("first destination"))
        target.respond(assistant_output("restored destination"))
        target.respond(assistant_output("forked destination"))
        source_provider = source.provider(websocket=websocket, key="synthetic-source-profile")
        target_provider = target.provider(websocket=websocket, key="synthetic-target-profile")
        destination = Agent(
            name="Destination",
            model="gpt-5.5",
            instructions="Destination provider only.",
            provider=target_provider,
        )
        agent = Agent(
            model="gpt-5.5",
            instructions="Source provider only.",
            provider=source_provider,
            handoffs=[destination],
        )
        async with agent.session() as session:
            result = await (await session.submit("Transfer.")).get_result()
            assert result.final_output == "first destination"
            saved = (await session.save_state()).to_json()
            forked = (await session.fork()).to_json()

        for state, answer in [
            (saved, "restored destination"),
            (forked, "forked destination"),
        ]:
            assert "synthetic-source-profile" not in state
            assert "synthetic-target-profile" not in state
            async with agent.session(state=SessionState.from_json(state)) as restored:
                result = await (await restored.submit("Continue.")).get_result()
                assert result.final_output == answer

        assert len(source.requests) == 1
        assert len(target.requests) == 3
        for server, credential in [
            (source, "synthetic-source-profile"),
            (target, "synthetic-target-profile"),
        ]:
            headers = [*server.upgrades, *server.request_headers]
            assert headers
            assert {header["authorization"] for header in headers} == {f"Bearer {credential}"}
        for request in target.requests:
            assert request.get("previous_response_id") is None
            assert "Destination provider only." in json.dumps(request["input"])
        assert any(
            item.get("call_id") == "source-transfer" and item["type"] == "function_call_output"
            for item in target.requests[0]["input"]
        )


async def test_native_compaction_uses_the_active_handoff_provider() -> None:
    async with timeout(30), peer() as source, peer() as target:
        source.respond(function_call("transfer_to_destination", {}, "source-transfer"))
        target.respond(assistant_output("first destination"))
        target.respond(assistant_output("destination summary"))
        destination = Agent(
            name="Destination",
            instructions="Destination only.",
            provider=target.provider(websocket=False, key="synthetic-compact-target"),
        )
        agent = Agent(
            instructions="Source only.",
            provider=source.provider(websocket=False, key="synthetic-compact-source"),
            handoffs=[destination],
        )
        async with agent.session() as session:
            assert (
                await (await session.submit("Transfer.")).get_result()
            ).final_output == "first destination"
            await session.compact()

        assert len(source.requests) == 1
        assert len(target.requests) == 2
        assert target.request_headers[1]["authorization"] == "Bearer synthetic-compact-target"
        assert "first destination" in json.dumps(target.requests[1]["input"])
