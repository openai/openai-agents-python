"""A completed selected turn has been retaining a usable passive input queue."""

import json

import pytest

from agents import Agent, FunctionTool, NativeError, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_provider_lineage import history_peer


def assert_queue_capture_unchanged(before: str, after: SessionState) -> None:
    expected = json.loads(before)
    actual = json.loads(after.to_json())
    prefix = expected["history"]
    assert actual["history"][: len(prefix)] == prefix
    settings = next(
        row
        for row in reversed(prefix)
        if row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "thread_settings_applied"
    )
    # Receipt refresh may publish the same effective settings, including the
    # exact provider cursor/window. No new message or turn event may appear.
    assert all(row == settings for row in actual["history"][len(prefix) :])
    actual["history"] = prefix
    expected["root_metadata"].pop("updated_at", None)
    actual["root_metadata"].pop("updated_at", None)
    assert actual == expected


@pytest.mark.parametrize("kind", ["previous_response", "conversation"])
@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.asyncio
async def test_terminal_pending_queue_has_been_reopening_after_success(kind, clear):
    marker = "EQUAL_PENDING_ORIGINAL_OCCURRENCE"
    filtered = []

    def transform(request):
        matches = [row for row in request.items if marker in json.dumps(row)]
        if len(matches) != 2 or filtered:
            return None
        first, second = matches
        assert first is not second
        filtered.append(True)
        return [row for row in request.items if row is not second]

    def history(response=None):
        if kind == "conversation":
            return {"type": kind, "id": "conv_current"}
        return {"type": kind, **({"id": response} if response else {})}

    lookup = FunctionTool(
        name="lookup",
        description="Read one owned record.",
        input_type=LookupArgs,
        output_type=str,
    )
    async with timeout(30), history_peer() as (provider, model, _reads, _url, _wire):
        model.respond(function_call("lookup", {"key": "record"}, "owned-initial"))
        model.respond(assistant_output("terminal"))
        model.respond(assistant_output("drained"))
        agent = Agent(
            instructions="Read and continue.",
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
            state=SessionState.from_json(staged.to_json()),
            provider_history=history("resp_1"),
        ) as resumed:
            action = await next_request(resumed.interactions(), lookup)
            await action.respond("accepted-result")
            assert (await resumed.restored_turn.get_result()).final_output == "terminal"
            passive = await resumed.save_state()
            assert len(passive.pending_input) == 1
            assert passive.native_continuation is None
            before = passive.to_json()
            refused = await resumed.submit("new data before terminal carryover")
            with pytest.raises(NativeError, match="empty fresh submission"):
                await refused.get_result()
            assert len(model.requests) == 2
            assert_queue_capture_unchanged(before, await resumed.save_state())
            with pytest.raises(RuntimeError, match="provider-managed history"):
                await resumed.compact(summary="must not consume pending data")
            assert_queue_capture_unchanged(before, await resumed.save_state())
            with pytest.raises(ValueError, match="manual continuation"):
                passive.with_pending_input("terminal append")
        assert len(model.requests) == 2
        assert sum(marker in json.dumps(row) for row in model.requests[1]["input"]) == 1
        state = SessionState.from_json(
            (passive.without_pending_input() if clear else passive).to_json()
        )
        async with agent.session(state=state, provider_history=history("resp_2")) as cold:
            assert cold.restored_turn is None
            assert (
                await (await cold.submit("ordinary new turn" if clear else "")).get_result()
            ).final_output == "drained"
            assert (await cold.save_state()).pending_input == []
        assert len(model.requests) == 3
        assert sum(marker in json.dumps(row) for row in model.requests[2]["input"]) == (
            0 if clear else 1
        )
        assert not any(row.get("call_id") == "owned-initial" for row in model.requests[2]["input"])
        assert model.requests[2].get(
            "previous_response_id", model.requests[2].get("conversation")
        ) == ("resp_2" if kind == "previous_response" else "conv_current")


@pytest.mark.asyncio
async def test_two_terminal_pending_cycles_have_been_retaining_each_new_mask():
    markers = ("FIRST_PASSIVE_CYCLE", "SECOND_PASSIVE_CYCLE")
    filtered = set()

    def transform(request):
        for marker in markers:
            matches = [row for row in request.items if marker in json.dumps(row)]
            if len(matches) == 2 and marker not in filtered:
                filtered.add(marker)
                return [row for row in request.items if row is not matches[1]]
        return None

    lookup = FunctionTool(
        name="lookup",
        description="Read each owned record.",
        input_type=LookupArgs,
        output_type=str,
    )
    async with timeout(30), history_peer() as (provider, model, _reads, _url, _wire):
        model.respond(function_call("lookup", {"key": "first"}, "cycle-one-call"))
        model.respond(assistant_output("first terminal"))
        model.respond(assistant_output("first drained"))
        model.respond(function_call("lookup", {"key": "second"}, "cycle-two-call"))
        model.respond(assistant_output("second terminal"))
        model.respond(assistant_output("second drained"))
        agent = Agent(
            instructions="Read each record.",
            provider=provider,
            model="gpt-5.5",
            model_settings={"store": True},
            tools=[lookup],
            transform_context=transform,
        )

        def history(identity=None):
            return {"type": "previous_response", **({"id": identity} if identity else {})}

        async with agent.session(provider_history=history()) as source:
            await source.submit("first cycle")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        first = saved.with_pending_input(markers[0]).with_pending_input(markers[0])
        async with agent.session(
            state=SessionState.from_json(first.to_json()), provider_history=history("resp_1")
        ) as active:
            await (await next_request(active.interactions(), lookup)).respond("first accepted")
            assert (await active.restored_turn.get_result()).final_output == "first terminal"
            assert len((await active.save_state()).pending_input) == 1
            assert (await (await active.submit("")).get_result()).final_output == "first drained"
            assert (await active.save_state()).pending_input == []
            await active.submit("second cycle")
            request = await next_request(active.interactions(), lookup)
            assert request.call_id == "cycle-two-call"
            second_saved = await active.suspend()
        second = second_saved.with_pending_input(markers[1]).with_pending_input(markers[1])
        async with agent.session(
            state=SessionState.from_json(second.to_json()), provider_history=history("resp_4")
        ) as active:
            await (await next_request(active.interactions(), lookup)).respond("second accepted")
            assert (await active.restored_turn.get_result()).final_output == "second terminal"
            assert len((await active.save_state()).pending_input) == 1
            assert (await (await active.submit("")).get_result()).final_output == "second drained"
            assert (await active.save_state()).pending_input == []
        assert len(model.requests) == 6
        for marker, terminal_index, drain_index in ((markers[0], 1, 2), (markers[1], 4, 5)):
            assert (
                sum(marker in json.dumps(row) for row in model.requests[terminal_index]["input"])
                == 1
            )
            assert (
                sum(marker in json.dumps(row) for row in model.requests[drain_index]["input"]) == 1
            )
        assert [row.get("previous_response_id") for row in model.requests[1:]] == [
            "resp_1",
            "resp_2",
            "resp_3",
            "resp_4",
            "resp_5",
        ]
        for index in (2, 5):
            assert not any(
                row.get("type") == "function_call_output" for row in model.requests[index]["input"]
            )
