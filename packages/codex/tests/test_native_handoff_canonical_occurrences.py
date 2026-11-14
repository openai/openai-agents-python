import json
from dataclasses import replace
from hashlib import sha256

import pytest

from agents import Agent, HandoffInput, LocalStorage, NativeProvider, SessionState, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


@pytest.mark.parametrize("same_id", [False, True], ids=["distinct-ids", "shared-id"])
@pytest.mark.parametrize(
    "canonical_contract", [False, True], ids=["model-only-control", "v0-canonical-contract"]
)
async def test_handoff_generated_occurrence_filter_distinguishes_canonical_storage(
    responses, canonical_contract, same_id, tmp_path
):
    old_id = "msg_shared_occurrence" if same_id else "msg_old_occurrence"
    new_id = "msg_shared_occurrence" if same_id else "msg_new_occurrence"
    effects = []
    seen = []
    storage = LocalStorage(tmp_path / "canonical-store")

    @function_tool
    def historical_effect() -> str:
        effects.append("once")
        return "OLD_EFFECT_RECEIPT"

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        seen.append(value)
        assert old_id in json.dumps(value.previous)
        assert new_id in json.dumps(value.generated)
        filtered = tuple(item for item in value.generated if item.get("type") != "message")
        return replace(
            value, generated=filtered, recorded_generated=filtered if canonical_contract else None
        )

    old = assistant_output("REPEATED")
    old["item"]["id"] = old_id
    new = assistant_output("REPEATED")
    new["item"]["id"] = new_id
    responses.respond(old, function_call("historical_effect", {}, "old_effect"))
    responses.respond(new, function_call("transfer_to_b", {}, "handoff"))
    responses.respond(assistant_output("done"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-handoff-canonical"
    ) as provider:
        target = Agent(name="B", instructions="destination")
        source = Agent(
            name="A",
            instructions="source",
            provider=provider,
            model="gpt-5.5",
            tools=[historical_effect],
            handoffs=[target],
            prepare_handoff_input=prepare,
        )
        async with timeout(30), source.session(storage=storage) as session:
            snapshot = await session.history_snapshot()
            await session.transact_history(
                expected_revision=snapshot.revision,
                keep=snapshot.total,
                items=[
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "OLD_KEEP"}],
                    }
                ],
            )
            assert (await (await session.submit("NEW_INPUT")).get_result()).final_output == "done"
            canonical = (await session.history_snapshot()).items
            target_input = responses.requests[2]["input"]

            def ids(items):
                return [item.get("id") for item in items if item.get("type") == "message"]

            assert ids(target_input).count(old_id) == 1
            if not same_id:
                assert new_id not in ids(target_input)
            assert ids(canonical).count(old_id) == (1 if canonical_contract or not same_id else 2)
            assert "OLD_EFFECT_RECEIPT" in json.dumps(canonical)
            assert effects == ["once"]
            if not same_id:
                assert (new_id in ids(canonical)) is not canonical_contract
            saved = await session.save_state()
        wire = saved.to_json()
        saved = SessionState.from_json(wire, expected_sha256=sha256(wire.encode()).hexdigest())
        responses.respond(assistant_output("continued"))
        async with source.session(state=saved) as restored:
            assert await restored.get_active_agent() is target
            snapshot = await restored.history_snapshot()
            assert ids(snapshot.items).count(old_id) == (
                1 if canonical_contract or not same_id else 2
            )
            if not same_id:
                assert (new_id in ids(snapshot.items)) is not canonical_contract
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "continued"
            assert effects == ["once"]
    assert len(seen) == 1
    assert len(responses.requests) == 4


@pytest.mark.parametrize("selection", ["detached", "previous-group", "tool-omission"])
async def test_canonical_handoff_requires_current_unchanged_occurrences(responses, selection):
    from copy import deepcopy

    from agents import NativeError

    effects = []

    @function_tool
    def historical_effect() -> str:
        effects.append("once")
        return "OLD_EFFECT_RECEIPT"

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        if selection == "detached":
            recorded = tuple(deepcopy(value.generated))
        elif selection == "previous-group":
            recorded = (*value.previous[:1], *value.generated)
        else:
            recorded = tuple(
                item for item in value.generated if item.get("type") != "function_call"
            )
        return replace(value, recorded_generated=recorded)

    old = assistant_output("REPEATED")
    old["item"]["id"] = "msg_old_occurrence"
    new = assistant_output("REPEATED")
    new["item"]["id"] = "msg_new_occurrence"
    responses.respond(old, function_call("historical_effect", {}, "old_effect"))
    responses.respond(new, function_call("transfer_to_b", {}, "handoff"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-owned-handoff"
    ) as provider:
        target = Agent(name="B", instructions="destination")
        source = Agent(
            name="A",
            instructions="source",
            provider=provider,
            model="gpt-5.5",
            tools=[historical_effect],
            handoffs=[target],
            prepare_handoff_input=prepare,
        )
        async with timeout(30), source.session() as session:
            with pytest.raises((NativeError, ValueError), match="Canonical handoff"):
                await (await session.submit("NEW_INPUT")).get_result()
            assert await session.get_active_agent() is source
            canonical = json.dumps((await session.history_snapshot()).items)
            assert "msg_new_occurrence" in canonical
            assert "msg_old_occurrence" in canonical
            assert "OLD_EFFECT_RECEIPT" in canonical
    assert effects == ["once"]
    assert len(responses.requests) == 2
