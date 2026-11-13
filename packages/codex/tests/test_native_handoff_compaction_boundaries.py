"""Native compaction has been preserving prepared-view boundaries."""

import json
from dataclasses import replace
from hashlib import sha256

import pytest

from agents import Agent, NativeProvider, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)


@pytest.mark.parametrize("cold", [False, True])
async def test_handoff_compaction_retires_removed_anchor_hot_and_cold(responses, cold):
    compacted = []

    def compact(value):
        visible = json.dumps(value.items)
        assert "old handoff projection" in visible
        assert "private source record" not in visible
        compacted.append("trusted replacement")
        return "canonical compacted replacement"

    destination = Agent(
        name="destination",
        instructions="Destination authority.",
        before_compact=compact,
    )

    def prepare(value):
        return replace(
            value,
            input=(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "old handoff projection"}],
                },
            ),
            previous=(),
            generated=(),
        )

    responses.respond(function_call("transfer_to_destination", {}, "accepted-transfer"))
    responses.respond(assistant_output("destination reached"))
    responses.respond(assistant_output("continued from compaction"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-retired-handoff-anchor"
    ) as provider:
        source = Agent(
            name="source",
            model="gpt-5.5",
            provider=provider,
            handoffs=[destination],
            instructions="Transfer to the destination.",
            prepare_handoff_input=prepare,
        )
        async with timeout(30), source.session() as session:
            assert (
                await (await session.submit("private source record")).get_result()
            ).final_output == "destination reached"
            await session.compact()
            assert compacted == ["trusted replacement"]
            assert len(responses.requests) == 2
            saved = await session.save_state()
            if not cold:
                assert (
                    await (await session.submit("continue")).get_result()
                ).final_output == "continued from compaction"
        if cold:
            async with (
                timeout(30),
                source.session(
                    state=SessionState.from_json(
                        saved.to_json(),
                        expected_sha256=sha256(saved.to_json().encode()).hexdigest(),
                    )
                ) as restored,
            ):
                assert (
                    await (await restored.submit("continue")).get_result()
                ).final_output == "continued from compaction"
    visible = json.dumps(responses.requests[2]["input"])
    assert "canonical compacted replacement" in visible
    assert "old handoff projection" not in visible
    assert "private source record" not in visible
    assert len(responses.requests) == 3


async def test_handoff_compaction_rejects_provider_disclosure(responses):
    destination = Agent(name="destination", instructions="Destination authority.")

    def prepare(value):
        return replace(
            value,
            input=(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "safe destination input"}],
                },
            ),
            previous=(),
            generated=(),
        )

    responses.respond(function_call("transfer_to_destination", {}, "transfer"))
    responses.respond(assistant_output("destination reached"))
    # An unsafe native provider compactor would consume this extra response.
    responses.respond(assistant_output("unexpected provider compaction"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-handoff-privacy"
    ) as provider:
        source = Agent(
            name="source",
            model="gpt-5.5",
            provider=provider,
            handoffs=[destination],
            instructions="Transfer to the destination.",
            prepare_handoff_input=prepare,
        )
        async with timeout(30), source.session() as session:
            assert (
                await (await session.submit("private source record")).get_result()
            ).final_output == "destination reached"
            with pytest.raises(
                Exception,
                match="Native compaction cannot combine with a filtered handoff presentation",
            ):
                await session.compact()
    assert len(responses.requests) == 2
    visible = json.dumps(responses.requests[1]["input"])
    assert "safe destination input" in visible
    assert "private source record" not in visible
