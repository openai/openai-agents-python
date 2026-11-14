"""Native request-only application instruction projection."""

import json

import pytest

from agents import Agent, FunctionTool, ModelContextProjection, ModelRequestContext, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_provider_lineage import history_peer

BASE = "ACCEPTED_APPLICATION_BASELINE"
REPLACEMENT = "REQUEST_ONLY_APPLICATION_REPLACEMENT"
SYSTEM = "NATIVE_CANONICAL_SYSTEM_INSTRUCTIONS"
CALLER = (
    "Caller note: <application_instructions>CALLER_OWNED_UNTAGGED_TEXT</application_instructions>"
)


_DIRECTIVE_HEADER = "<application_instructions>\n"
_DIRECTIVE_NOTICE = (
    "The following application instructions replace all earlier application instructions. "
    "An empty value clears them.\n"
)


def assert_request_directive_last(wire, replacement):
    parts = [
        ((row_index, part_index), part.get("text", ""))
        for row_index, row in enumerate(wire["input"])
        if row.get("role") == "developer"
        for part_index, part in enumerate(row.get("content", []))
        if isinstance(part, dict)
    ]
    projected = [(position, text) for position, text in parts if text.startswith(_DIRECTIVE_HEADER)]
    assert projected
    position, text = projected[-1]
    assert (
        text
        == _DIRECTIVE_HEADER
        + _DIRECTIVE_NOTICE
        + (replacement or "")
        + "</application_instructions>"
    )
    assert all(old_position < position for old_position, old_text in parts if BASE in old_text)


@pytest.mark.parametrize(("replacement", "filter_user"), [(REPLACEMENT, True), (None, False)])
@pytest.mark.asyncio
async def test_instruction_projection_preserves_native_base_and_cold_cursor(
    replacement: str | None, filter_user: bool
) -> None:
    seen = []
    agent = None

    def transform(request: ModelRequestContext[None]) -> ModelContextProjection:
        assert request.agent is agent
        assert request.subagent_id is None and request.purpose == "turn"
        assert request.instructions == BASE
        seen.append((request.session_id, request.turn_id, request.request_id))
        items = None
        if filter_user:
            items = [
                row
                for row in request.items
                if not (row.get("role") == "user" and "original user" in json.dumps(row))
            ]
        return ModelContextProjection(instructions=replacement, items=items)

    async with timeout(30), history_peer() as (provider, model, reads, _url, _wire):
        model.respond(assistant_output("first"))
        model.respond(assistant_output("second"))
        agent = Agent(
            instructions=BASE,
            provider=provider,
            model="gpt-5.5",
            model_settings={"store": True},
            transform_context=transform,
        )
        async with agent.session(
            provider_history={"type": "previous_response", "id": "resp_external"}
        ) as source:
            snapshot = await source.history_snapshot()
            await source.transact_history(
                expected_revision=snapshot.revision,
                keep=snapshot.total,
                items=[
                    {
                        "type": "message",
                        "role": "system",
                        "content": [{"type": "input_text", "text": SYSTEM}],
                    },
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": CALLER}],
                    },
                ],
            )
            first = await source.submit("original user")
            assert (await first.get_result()).final_output == "first"
            saved = await source.save_state()
        before = saved.to_json()
        cold = SessionState.from_json(before)
        async with agent.session(
            state=cold, provider_history={"type": "previous_response", "id": "resp_1"}
        ) as restored:
            assert (
                await (await restored.submit("later user")).get_result()
            ).final_output == "second"
            after = await restored.save_state()
        assert len(model.requests) == 2
        first_wire, second_wire = model.requests
        for wire in model.requests:
            assert_request_directive_last(wire, replacement)
            assert (REPLACEMENT in json.dumps(wire["input"])) == bool(replacement)
        assert ("original user" in json.dumps(first_wire["input"])) is (not filter_user)
        assert CALLER in json.dumps(first_wire["input"])
        assert first_wire.get("instructions") == second_wire.get("instructions")
        assert SYSTEM in json.dumps(first_wire["input"])
        assert SYSTEM in json.dumps(saved.history)
        assert SYSTEM in json.dumps(after.history)
        assert first_wire["previous_response_id"] == "resp_external"
        assert second_wire["previous_response_id"] == "resp_1"
        assert ("/v1/responses/resp_1", "Bearer current-owner") in reads
        assert BASE in json.dumps(saved.history)
        assert BASE in json.dumps(after.history)
        assert REPLACEMENT not in json.dumps(saved.history)
        assert REPLACEMENT not in json.dumps(after.history)
        assert saved.to_json() == before
        assert len(seen) == 2 and seen[0] != seen[1]


@pytest.mark.parametrize("history_type", ["previous_response", "conversation"])
@pytest.mark.asyncio
async def test_projection_preserves_selected_staged_occurrence_identity(history_type) -> None:
    marker = "TWO_EQUAL_STAGED_USER_OCCURRENCES"

    def transform(request: ModelRequestContext[None]) -> ModelContextProjection | None:
        matches = [
            row for row in request.items if row.get("role") == "user" and marker in json.dumps(row)
        ]
        if len(matches) != 2:
            return None
        first, second = matches
        assert first is not second
        return ModelContextProjection(
            instructions=REPLACEMENT, items=[row for row in request.items if row is not second]
        )

    def history(response_id=None):
        if history_type == "conversation":
            return {"type": history_type, "id": "conv_current"}
        return {"type": history_type, **({"id": response_id} if response_id else {})}

    lookup = FunctionTool(
        name="lookup",
        description="Read one record.",
        input_type=LookupArgs,
        output_type=str,
    )
    async with timeout(30), history_peer() as (provider, model, _reads, _url, _wire):
        model.respond(function_call("lookup", {"key": "record"}, "original-owned-call"))
        model.respond(assistant_output("resumed"))
        agent = Agent(
            instructions=BASE,
            provider=provider,
            model="gpt-5.5",
            model_settings={"store": True},
            tools=[lookup],
            transform_context=transform,
        )
        async with agent.session(provider_history=history()) as source:
            await source.submit("original user")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        staged = saved.with_pending_input(marker).with_pending_input(marker)
        async with agent.session(
            state=SessionState.from_json(staged.to_json()), provider_history=history("resp_1")
        ) as resumed:
            await (await next_request(resumed.interactions(), lookup)).respond("accepted")
            restored_turn = resumed.restored_turn
            assert restored_turn is not None
            assert (await restored_turn.get_result()).final_output == "resumed"
            captured = await resumed.save_state()
        assert len(model.requests) == 2
        wire = model.requests[1]
        assert sum(marker in json.dumps(row) for row in wire["input"]) == 1
        assert REPLACEMENT in json.dumps(wire["input"])
        assert_request_directive_last(wire, REPLACEMENT)
        assert len(captured.pending_input) == 1
        assert BASE in json.dumps(captured.history)
        assert REPLACEMENT not in json.dumps(captured.history)


def test_projection_is_public_and_omits_instruction_text_from_repr() -> None:
    from agents.model_context import ModelContextProjection as PublicProjection

    assert PublicProjection is ModelContextProjection
    assert REPLACEMENT not in repr(ModelContextProjection(instructions=REPLACEMENT))
    with pytest.raises(TypeError, match="instructions must be str or None"):
        ModelContextProjection(instructions=object())  # type: ignore[arg-type]
