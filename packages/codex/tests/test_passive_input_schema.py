"""Passive pending data has been retaining ordinary state authority boundaries."""
import json

import pytest

from agents import Agent, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_provider_lineage import history_peer


@pytest.mark.asyncio
async def test_passive_metadata_has_been_bound_and_removable_without_inventing_cursor():
    async with timeout(30), history_peer() as (provider, model, _reads, _url, _wire):
        model.respond(assistant_output("settled"))
        async with Agent(
            instructions="Answer.", provider=provider, model="gpt-5.5",
            model_settings={"store": True},
        ).session(provider_history={"type": "previous_response"}) as source:
            await (await source.submit("original")).get_result()
            idle = await source.save_state()
    ordinary = idle.to_json()
    assert idle.pending_input == []
    item = {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "queued"}]}
    payload = json.loads(ordinary)
    payload.setdefault("agent_options", {})["_sdk_pending_input"] = {"version": 1, "items": [item]}
    passive = SessionState.model_validate(payload)
    assert passive.native_continuation is None
    assert passive.pending_input == [item]
    detached = passive.pending_input
    detached[0]["content"][0]["text"] = "caller mutation"
    assert passive.pending_input == [item]
    cleared = passive.without_pending_input()
    assert cleared.pending_input == []
    assert cleared.model_dump(mode="json") == idle.model_dump(mode="json")
    assert idle.to_json() == ordinary
    for invalid in (
        {"version": True, "items": [item]},
        {"version": 1, "items": [item], "response_id": "invented"},
        {"version": 1, "items": []},
        {"version": 1, "items": [item] * 4097},
        {"version": 1, "items": [{"value": "x" * (8 * 1024 * 1024)}]},
    ):
        payload["agent_options"]["_sdk_pending_input"] = invalid
        with pytest.raises(ValueError, match="passive|Passive"):
            SessionState.model_validate(payload)
