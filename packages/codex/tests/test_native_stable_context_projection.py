"""The accepted wire digest binds a stable view independently of canonical history."""

import json
from copy import deepcopy

import pytest

from agents import Agent, NativeError, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_provider_lineage import history_peer

ORIGINAL = "ORIGINAL_CANONICAL_USER"
PROJECTED = "STABLE_WIRE_USER_VIEW"
SUFFIX = "REQUEST_ONLY_TRAILING_NOTE"


def text(row):
    return "".join(
        part.get("text", "") for part in row.get("content", []) if isinstance(part, dict)
    )


def replace_canonical(value, replacement):
    if isinstance(value, dict):
        if value.get("role") == "user":
            for part in value.get("content", []):
                if isinstance(part, dict) and part.get("text") == ORIGINAL:
                    part["text"] = replacement
                    return 1
        return sum(replace_canonical(child, replacement) for child in value.values())
    if isinstance(value, list):
        return sum(replace_canonical(child, replacement) for child in value)
    return 0


@pytest.mark.parametrize("history_type", ["previous_response", "conversation"])
@pytest.mark.parametrize(
    "mode",
    [
        "plain",
        "suffix",
        "suffix-only",
        "changed-view",
        "canonical-view",
        "canonical-lookalike",
        "canonical-same-length",
        "foreign-origin",
        "duplicate-origin",
    ],
)
@pytest.mark.asyncio
async def test_stable_context_projection_keeps_native_and_wire_boundaries(history_type, mode):
    seen = []
    generation = 0

    def history(response_id=None):
        if history_type == "conversation":
            return {"type": history_type, "id": "conv_current"}
        return {"type": history_type, **({"id": response_id} if response_id else {})}

    def transform(request):
        rows = []
        for original in request.items:
            row = original
            if original.get("role") == "user" and text(original) == ORIGINAL:
                replacement = (
                    "DIFFERENT_SECOND_REQUESTED_VIEW"
                    if (generation and mode == "changed-view")
                    else PROJECTED
                )
                if generation and mode == "canonical-view":
                    replacement = ORIGINAL
                if mode != "suffix-only":
                    row = {**original, "content": [{"type": "input_text", "text": replacement}]}
                seen.append((deepcopy(original), deepcopy(row)))
                if generation and mode == "foreign-origin":
                    row = {**row, "id": "msg_unrelated_lookalike"}
                if generation and mode == "duplicate-origin":
                    rows.append(row)
            rows.append(row)
        if mode != "plain":
            rows.append(
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": SUFFIX}],
                }
            )
        return rows

    async with timeout(30), history_peer() as (provider, model, _reads, _url, _wire):
        for output in ("first", "second", "third"):
            model.respond(assistant_output(output))
        agent = Agent(
            instructions="Answer.",
            provider=provider,
            model="gpt-5.5",
            model_settings={"store": True},
            transform_context=transform,
        )
        async with agent.session(provider_history=history()) as source:
            assert (await (await source.submit(ORIGINAL)).get_result()).final_output == "first"
            saved = await source.save_state()
        original_json = saved.to_json()
        assert ORIGINAL in json.dumps(saved.history)
        assert PROJECTED not in json.dumps(saved.history)
        assert SUFFIX not in json.dumps(saved.history)
        payload = json.loads(original_json)
        if mode in ("canonical-lookalike", "canonical-same-length"):
            replacement = PROJECTED if mode == "canonical-lookalike" else "X" * len(ORIGINAL)
            assert (
                sum(
                    replace_canonical(row, replacement)
                    for row in payload["history"]
                    if row.get("type") == "response_item"
                )
                == 1
            )
        generation = 1
        rejected = mode not in ("plain", "suffix", "suffix-only", "canonical-view")
        async with agent.session(
            state=SessionState.from_json(json.dumps(payload)),
            provider_history=history("resp_1"),
        ) as restored:
            turn = await restored.submit("later")
            if rejected:
                with pytest.raises(NativeError, match="Provider history"):
                    await turn.get_result()
                assert len(model.requests) == 1
                assert saved.to_json() == original_json
                return
            assert (await turn.get_result()).final_output == "second"
            second = await restored.save_state()
        # Repeat the same stable projection over a second cold boundary. The
        # first output has now moved into the acknowledged input window.
        generation = 2
        async with agent.session(
            state=SessionState.from_json(second.to_json()),
            provider_history=history("resp_2"),
        ) as third:
            assert (await (await third.submit("latest")).get_result()).final_output == "third"
            final = await third.save_state()
        assert len(model.requests) == 3
        assert saved.to_json() == original_json
        assert ORIGINAL in json.dumps(final.history)
        assert PROJECTED not in json.dumps(final.history)
        assert SUFFIX not in json.dumps(final.history)
        assert len(seen) == 3
        for index, (original, projected) in enumerate(seen):
            assert text(original) == ORIGINAL
            canonical_view = mode == "suffix-only" or (index and mode == "canonical-view")
            assert text(projected) == (ORIGINAL if canonical_view else PROJECTED)
        for original, _projected in seen[1:]:
            assert original == seen[0][0]
        first_user = ORIGINAL if mode == "suffix-only" else PROJECTED
        assert first_user in json.dumps(model.requests[0]["input"])
        for index, request in enumerate(model.requests):
            rows = request["input"]
            if mode != "plain":
                assert text(rows[-1]) == SUFFIX
                assert sum(text(row) == SUFFIX for row in rows) == 1
            if index:
                assert PROJECTED not in json.dumps(rows)
                assert ORIGINAL not in json.dumps(rows)
                new_user = "later" if index == 1 else "latest"
                assert [text(row) for row in rows if row.get("role") == "user"] == [new_user]
                if mode != "plain":
                    user_position = next(i for i, row in enumerate(rows) if text(row) == new_user)
                    assert user_position < len(rows) - 1
            if history_type == "previous_response" and index:
                assert request["previous_response_id"] == f"resp_{index}"
            if history_type == "conversation":
                assert request["conversation"] == "conv_current"

        # Each callback's unowned developer row is a new request-only occurrence.
        # Mirror the physical append order, including each accepted response.
        markers = {first_user, "later", "latest", SUFFIX, "first", "second", "third"}
        ledger = []
        for request, response in zip(model.requests, ("first", "second", "third"), strict=True):
            ledger.extend(text(row) for row in request["input"] if text(row) in markers)
            ledger.append(response)
        expected = []
        for user, response in ((first_user, "first"), ("later", "second"), ("latest", "third")):
            expected.append(user)
            if mode != "plain":
                expected.append(SUFFIX)
            expected.append(response)
        assert ledger == expected
