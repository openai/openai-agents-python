import json
from dataclasses import replace

import pytest

from agents import Agent, HandoffInput, NativeProvider, SessionState
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)


@pytest.mark.parametrize(
    "case",
    [
        "removed-memory",
        "removed-json",
        "removed-receipt-json",
        "rewritten-initial-json",
        "trusted-recapture",
    ],
)
async def test_removing_handoff_presentation_cannot_disclose_canonical_source(responses, case):
    retained = "filtered source context"
    responses.respond(function_call("transfer_to_destination", {}, "source-handoff"))
    responses.respond(assistant_output("destination reached"))
    responses.respond(assistant_output("first follow-up complete"))
    responses.respond(assistant_output("cold follow-up complete"))

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        return replace(
            value,
            input=(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": retained}],
                },
            ),
            previous=(),
            generated=(),
        )

    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic-handoff-update",
        supports_websockets=False,
    ) as provider:
        destination = Agent(name="destination", instructions="Use the handoff context.")
        source = Agent(
            name="source",
            model="gpt-5.5",
            provider=provider,
            instructions="Transfer to the destination.",
            handoffs=[destination],
            prepare_handoff_input=prepare,
        )
        async with source.session() as session:
            result = await (await session.submit("private original")).get_result()
            assert result.final_output == "destination reached"
            await session.update(instructions="Use the updated instruction.")
            result = await (await session.submit("first follow-up")).get_result()
            assert result.final_output == "first follow-up complete"
            await session.update(instructions="Use the final instruction.")
            saved = await session.save_state()
        if case == "trusted-recapture":
            saved = saved._capture_for_restore()._capture_for_restore()
            async with source.session(state=saved) as restored:
                result = await (await restored.submit("cold follow-up")).get_result()
                assert result.final_output == "cold follow-up complete"
            cold = json.dumps(responses.requests[3]["input"])
            assert retained in cold and "private original" not in cold
            return
        if case != "removed-memory":
            saved = SessionState.from_json(saved.to_json())
        count = 0
        for history in (saved.history, *(child.history for child in saved.descendants)):
            for row in history:
                profile = (row.get("metadata") or {}).get("sdk_agent_profile", {})
                if "presentation" in profile:
                    del profile["presentation"]
                    count += 1
        assert count > 0
        if case in ("removed-receipt-json", "rewritten-initial-json"):
            for history in (
                saved.history,
                *(child.history for child in saved.descendants),
            ):
                for row in history:
                    metadata = row.get("metadata")
                    if isinstance(metadata, dict):
                        metadata.pop("sdk_agent_handoff", None)
        if case == "rewritten-initial-json":
            profiles = [
                row["metadata"]["sdk_agent_profile"]
                for row in saved.history
                if (row.get("metadata") or {}).get("sdk_agent_profile")
            ]
            assert len(profiles) >= 2
            profiles[0]["profile_id"] = profiles[1]["profile_id"]
            profiles[0]["model"] = profiles[1]["model"]
        try:
            async with source.session(state=saved) as restored:
                result = await (await restored.submit("cold follow-up")).get_result()
                assert result.final_output == "cold follow-up complete"
        except (ValueError, RuntimeError) as error:
            assert "authenticated state" in str(error) or (
                case == "rewritten-initial-json" and "captured root defaults" in str(error)
            )
            assert len(responses.requests) == 3
            return
        cold = json.dumps(responses.requests[3]["input"])
        assert "private original" not in cold, (
            "Removed presentation disclosed canonical source history"
        )
        pytest.fail("Removed presentation restored without independent authentication")


async def test_filtered_graph_without_handoff_keeps_ordinary_unverified_root_capture(responses):
    responses.respond(assistant_output("source continued"))
    responses.respond(assistant_output("source restored"))
    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic-handoff-root",
        supports_websockets=False,
    ) as provider:
        destination = Agent(name="destination", instructions="Destination.")
        source = Agent(
            name="source",
            instructions="Source.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[destination],
            prepare_handoff_input=lambda value: value,
        )
        async with source.session() as session:
            await (await session.submit("ordinary root input")).get_result()
            saved = SessionState.from_json((await session.save_state()).to_json())
        async with source.session(state=saved) as restored:
            result = await (await restored.submit("Continue.")).get_result()
            assert result.final_output == "source restored"
    assert len(responses.requests) == 2
    assert "ordinary root input" in json.dumps(responses.requests[1]["input"])


async def test_unrelated_configured_child_keeps_ordinary_unverified_root_capture(
    responses,
):
    responses.respond(function_call("nested_child", {"input": "child question"}, "root-child"))
    responses.respond(assistant_output("child answer"))
    responses.respond(assistant_output("parent answer"))
    responses.respond(assistant_output("parent restored"))
    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic-handoff-child",
        supports_websockets=False,
    ) as provider:
        child = Agent(
            name="independent child",
            instructions="Answer the child.",
            model="gpt-5.5",
            provider=provider,
        )
        destination = Agent(name="destination", instructions="Destination.")
        source = Agent(
            name="source",
            instructions="Source.",
            model="gpt-5.5",
            provider=provider,
            tools=[child.as_tool(tool_name="nested_child", tool_description="Run the child.")],
            handoffs=[destination],
            prepare_handoff_input=lambda value: value,
        )
        async with source.session() as session:
            result = await (await session.submit("ordinary root input")).get_result()
            assert result.final_output == "parent answer"
            saved = SessionState.from_json((await session.save_state()).to_json())
        async with source.session(state=saved) as restored:
            result = await (await restored.submit("Continue.")).get_result()
            assert result.final_output == "parent restored"
    assert len(responses.requests) == 4


@pytest.mark.parametrize("trusted", [False, True])
async def test_filtered_handoff_returning_to_root_requires_current_authority(responses, trusted):
    from hashlib import sha256

    responses.respond(function_call("transfer_to_destination", {}, "source-handoff"))
    responses.respond(function_call("transfer_to_source", {}, "return-handoff"))
    responses.respond(assistant_output("returned"))
    responses.respond(assistant_output("cold follow-up"))
    retained = "accepted return context"

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        return replace(
            value,
            input=(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": retained}],
                },
            ),
            previous=(),
            generated=(),
        )

    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic-cyclic-handoff",
        supports_websockets=False,
    ) as provider:
        destination = Agent(
            name="destination",
            instructions="Return to source.",
            handoffs=[lambda: source],
            prepare_handoff_input=prepare,
        )
        source = Agent(
            name="source",
            instructions="Transfer to destination.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[destination],
        )
        async with source.session() as session:
            result = await (await session.submit("private canonical source context")).get_result()
            assert result.final_output == "returned"
            assert await session.get_active_agent() is source
            saved = await session.save_state()
        raw = saved.to_json()
        if trusted:
            state = SessionState.from_json(raw, expected_sha256=sha256(raw.encode()).hexdigest())
            async with source.session(state=state) as restored:
                result = await (await restored.submit("Continue.")).get_result()
                assert result.final_output == "cold follow-up"
            request = json.dumps(responses.requests[3]["input"])
            assert retained in request and "private canonical source context" not in request
            return
        state = SessionState.from_json(raw)
        count = 0
        for row in state.history:
            metadata = row.get("metadata") or {}
            profile = metadata.get("sdk_agent_profile") or {}
            if "presentation" in profile:
                del profile["presentation"]
                count += 1
            metadata.pop("sdk_agent_handoff", None)
        assert count
        try:
            async with source.session(state=SessionState.from_json(state.to_json())) as restored:
                await (await restored.submit("Continue.")).get_result()
        except ValueError as error:
            assert "requires authenticated state" in str(error)
            assert len(responses.requests) == 3
        else:
            request = json.dumps(responses.requests[3]["input"])
            assert "private canonical source context" not in request
            pytest.fail("An unsigned return to the root bypassed current graph authority")
