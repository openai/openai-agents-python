"""Manual replies advance only their original live native invocation."""

from __future__ import annotations

import asyncio
import pickle
from dataclasses import replace
from typing import Any, cast, get_args

import pytest
from pydantic import BaseModel, model_validator

from agents import (
    ActionRequested,
    Agent,
    FunctionTool,
    InteractionExpiredError,
    InteractionRequired,
    NativeError,
    NativeProvider,
    TurnStoppedError,
    function_tool,
)
from agents.observation import EventGap, Observation, SessionEnded
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import function_declarations


class LookupArgs(BaseModel):
    key: str


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-manual-test")


async def next_request(stream, tool):
    async for event in stream:
        request = tool.match(event)
        if request is not None:
            return request
    raise AssertionError("The submission ended before its manual request arrived")


def test_app_supplied_function_rejects_callback_policy():
    with pytest.raises(TypeError, match="require a callback"):
        FunctionTool(
            name="lookup",
            description="",
            input_type=LookupArgs,
            output_type=str,
            callback_timeout=1,
        )


async def test_callback_copy_preserves_its_declared_schema(responses):
    received: list[str] = []

    @function_tool
    def read_record(key: str) -> str:
        received.append(key)
        return key

    class Changed(BaseModel):
        key: int

    for field in ("input_type", "output_type"):
        with pytest.raises(TypeError, match="Declare a new callback"):
            copied = replace(read_record, **{field: Changed})
            await Agent(instructions="Read.", tools=[copied], provider=provider(responses)).run(
                "Read."
            )
    assert not responses.requests and not received

    renamed = replace(read_record, name="lookup", callback_timeout=2)
    responses.respond(function_call("lookup", {"key": "blue"}, "call-blue"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Read.", tools=[renamed], provider=provider(responses))
    async with timeout(10):
        assert (await agent.run("Read.")).final_output == "done"
    [declaration] = function_declarations(responses.requests[0])
    assert declaration["parameters"]["properties"]["key"]["type"] == "string"
    assert received == ["blue"] and renamed.callback_timeout == 2


async def test_parallel_manual_replies_keep_siblings_and_native_identity(responses):
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("lookup", {"key": "B"}, "call-B"),
    )
    responses.respond(assistant_output("both accepted"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        original = await session.submit("Look up A and B.")
        # The normal order does not require an observer before submission.
        first = session.interactions()
        second = session.interactions()
        requests = [await next_request(first, lookup), await next_request(first, lookup)]
        aliases = [await next_request(second, lookup), await next_request(second, lookup)]
        by_key = {request.arguments.key: request for request in requests}
        alias_by_key = {request.arguments.key: request for request in aliases}
        assert set(by_key) == set(alias_by_key) == {"A", "B"}
        snapshot = await session.pending_interactions()
        assert {r.call_id for r in snapshot} == {"call-A", "call-B"}
        [declaration] = function_declarations(responses.requests[0])
        assert declaration["name"] == "lookup"
        assert declaration["parameters"]["properties"]["key"]["type"] == "string"
        with pytest.raises(TypeError):
            pickle.dumps(requests[0])
        with pytest.raises(TypeError):
            type(requests[0]._grant)()
        for request in requests:
            assert (request.session_id, request.submission_id, request.turn_id) == (
                session.id,
                original.submission_id,
                original.turn_id,
            )
            assert request.call_id == "call-" + request.arguments.key
            assert request.call.tool == lookup.ref
            assert request.call.call_id == request.call_id
            assert (request.call.session_id, request.call.turn_id) == (session.id, original.turn_id)
            assert request.call.child_id is request.call.child_incarnation is None
            assert request.call.role is None
            assert request.call.model == responses.requests[0]["model"]
            assert request.call.route == "direct"
        competing = await asyncio.gather(
            by_key["A"].respond("answer-A"),
            alias_by_key["A"].respond("answer-A"),
            return_exceptions=True,
        )
        assert sum(value is None for value in competing) == 1
        assert sum(isinstance(value, InteractionExpiredError) for value in competing) == 1
        assert len(responses.requests) == 1
        still_pending = await next_request(session.interactions(), lookup)
        assert still_pending.call_id == "call-B"
        assert await still_pending.respond("answer-B") is None
        result = await (original).get_result()
        assert result.turn is original
        assert result.final_output == "both accepted"
        assert await (original).get_result() is result
        page = [item async for item in session.items()]
        turns = [item async for item in session.turns()]
        assert len(turns) == 1
        assert len([item for item in page if item.get("role") == "user"]) == 1
        outputs = [item for item in page if item["type"] == "function_call_output"]
        assert {item["call_id"] for item in outputs} == {"call-A", "call-B"}
        assert len(outputs) == 2
        assert {item["turn_id"] for item in outputs} == {original.turn_id}
        assert tool_output(responses.requests[1], "call-A") == "answer-A"
        assert tool_output(responses.requests[1], "call-B") == "answer-B"


async def test_manual_reply_checks_authority_before_validation_and_allows_retry(responses):
    validated: list[object] = []

    class Answer(BaseModel):
        value: int

        @model_validator(mode="before")
        @classmethod
        def note_validation(cls, value):
            validated.append(value)
            return value

    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=Answer
    )
    same_name = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=Answer
    )
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "private-argument"}, "reused-id"))
    responses.respond(assistant_output("first"))
    async with timeout(15), agent.session() as session:
        checkpoint = await session.save_state()
        async with agent.session(state=checkpoint) as foreign:
            assert foreign.id == session.id
            original = await session.submit("first")
            request = await next_request(session.interactions(), lookup)
            assert same_name.match(request) is None
            assert "private-argument" not in repr(request)
            assert validated == []
            with pytest.raises(TypeError, match="input must be"):
                await foreign.submit(cast(Any, request))
            assert validated == []
            with pytest.raises(ValueError, match="Only user messages"):
                await session.submit(cast(Any, {"type": "action_reply", "call_id": "reused-id"}))
            assert validated == []
            with pytest.raises(ValueError, match="failed validation"):
                await request.respond(cast(Any, {"value": "not-an-int"}))
            assert validated
            assert (await session.pending_interactions())[0].call_id == request.call_id
            assert await request.respond(cast(Any, {"value": 7})) is None
            assert (await (original).get_result()).final_output == "first"
            count = len(validated)
            with pytest.raises(InteractionExpiredError, match="expired|delivered"):
                await request.respond(cast(Any, {"value": 7}))
            assert len(validated) == count

            # A later native invocation may reuse the model's call ID, never its authority.
            responses.respond(function_call("lookup", {"key": "new"}, "reused-id"))
            responses.respond(assistant_output("second"))
            later = await session.submit("second")
            current = await next_request(session.interactions(), lookup)
            assert current.call_id == request.call_id
            assert current.turn_id != request.turn_id
            with pytest.raises(InteractionExpiredError, match="expired|delivered"):
                await request.respond(cast(Any, {"value": 7}))
            assert len(validated) == count
            assert await current.respond(cast(Any, {"value": 8})) is None
            assert (await (later).get_result()).final_output == "second"


@pytest.mark.parametrize("ending", ["cancel", "close"])
async def test_manual_requests_expire_on_cancel_or_close_without_result_validation(
    responses, ending
):
    validated: list[object] = []

    class Answer(BaseModel):
        value: str

        @model_validator(mode="before")
        @classmethod
        def note_validation(cls, value):
            validated.append(value)
            return value

    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=Answer
    )
    responses.respond(function_call("lookup", {"key": "held"}, "held-call"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        original = await session.submit("start")
        stream = session.interactions()
        request = await next_request(stream, lookup)
        await stream.aclose()
        if ending == "cancel":
            await original.cancel()
        else:
            await session._aclose()
        with pytest.raises(InteractionExpiredError, match="expired|closed|delivered"):
            await request.respond(cast(Any, {"value": "too late"}))
        assert validated == []
        with pytest.raises(TurnStoppedError):
            await (original).get_result()
        assert len(responses.requests) == 1


async def test_late_cursor_gets_live_action_despite_passive_gap_and_reader_close(responses):
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    chunks = [
        {"type": "response.output_text.delta", "item_id": "preface", "delta": "x"}
        for _ in range(300)
    ]
    preface = assistant_output("x" * 300)
    preface["item"]["id"] = "preface"
    responses.respond(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "id": "preface", "role": "assistant", "content": []},
        },
        *chunks,
        preface,
        function_call("lookup", {"key": "secret-value"}, "late-call"),
    )
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        handle = await session.submit("start")
        early = session.interactions()
        request = await next_request(early, lookup)
        observer = session.events()
        retained = await anext(observer)
        assert not isinstance(retained, ActionRequested)
        assert retained.sequence > 0
        await early.aclose()
        await observer.aclose()
        # A fresh snapshot reads the live call after the passive event gap.
        late = session.interactions()
        alias = await anext(late)
        assert isinstance(alias, ActionRequested)
        assert lookup.match(alias) is not None
        assert alias.call_id == request.call_id
        assert ActionRequested not in get_args(Observation)
        assert not hasattr(alias, "sequence")
        assert "secret-value" not in repr(alias)
        [snapshot] = await session.pending_interactions()
        assert snapshot.call_id == alias.call_id
        await late.aclose()
        assert await request.respond("accepted after cursor close") is None
        assert (await (handle).get_result()).final_output == "done"
        passive = [event async for event in handle.events(after=0)]
        assert any(isinstance(event, EventGap) for event in passive)
        assert all(not isinstance(event, ActionRequested) for event in passive)
        assert all(
            not hasattr(event, "arguments") and not hasattr(event, "respond") for event in passive
        )
        last = passive[-1]
        assert not isinstance(last, ActionRequested)
        sequence = last.sequence
        # Session-wide streams remain live after this turn's last event.
        later = session.events(after=sequence)
        await session._aclose()
        [closed] = [event async for event in later]
        assert isinstance(closed, SessionEnded)


async def test_oversized_utf8_manual_request_fails_owner_without_observer_or_validation(responses):
    validated: list[object] = []

    class LargeArgs(BaseModel):
        text: str

        @model_validator(mode="before")
        @classmethod
        def note_validation(cls, value):
            validated.append(value)
            return value

    lookup = FunctionTool(
        name="lookup", description="Read text", input_type=LargeArgs, output_type=str
    )
    text = "\N{GRINNING FACE}" * 20_000
    assert len(text) < 65_536 < len(text.encode("utf-8"))
    responses.respond(function_call("lookup", {"text": text}, "oversized"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        handle = await session.submit("start")
        with pytest.raises(NativeError, match="Manual tool arguments could not be delivered"):
            await (handle).get_result()
        assert validated == []
        assert len(responses.requests) == 1


async def test_invalid_manual_arguments_fail_owner_without_yielding_an_action(responses):
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": {"private": "value"}}, "invalid-input"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        handle = await session.submit("start")
        events = [event async for event in handle.events()]
        assert all(not isinstance(event, ActionRequested) for event in events)
        with pytest.raises(NativeError, match="Manual tool arguments could not be delivered"):
            await (handle).get_result()
        assert len(responses.requests) == 1


async def test_idle_replacement_switches_automatic_and_manual_dispatch(responses):
    calls: list[str] = []

    @function_tool
    async def lookup(key: str) -> str:
        calls.append(key)
        return f"automatic:{key}"

    manual = FunctionTool(
        name="manual_lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    for phase in ("first", "manual", "last"):
        responses.respond(
            function_call(
                "manual_lookup" if phase == "manual" else "lookup", {"key": phase}, f"call-{phase}"
            )
        )
        responses.respond(assistant_output(f"done:{phase}"))
    agent = Agent(instructions="Use lookup.", tools=[lookup, manual], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        first = await session.submit("first", tools=[lookup])
        assert (await (first).get_result()).final_output == "done:first"
        assert calls == ["first"]
        second = await session.submit("manual", tools=[manual])
        request = await next_request(session.interactions(), manual)
        assert request.arguments == LookupArgs(key="manual")
        assert calls == ["first"]
        assert await request.respond("manual:answer") is None
        assert (await (second).get_result()).final_output == "done:manual"
        last = await session.submit("last", tools=[lookup])
        assert (await (last).get_result()).final_output == "done:last"
        assert calls == ["first", "last"]
        assert tool_output(responses.requests[1], "call-first") == "automatic:first"
        assert tool_output(responses.requests[3], "call-manual") == "manual:answer"
        assert tool_output(responses.requests[5], "call-last") == "automatic:last"


@pytest.mark.parametrize("sync", [False, True])
async def test_manual_one_shot_handles_declared_tool_live(responses, sync):
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "live-call"))
    responses.respond(assistant_output("continued"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    actions = []

    async def answer(event):
        request = lookup.match(event)
        assert request is not None and request.arguments.key == "A"
        actions.append(request.call_id)
        return request.reply("answer")

    async with timeout(15):
        if sync:
            result = await asyncio.to_thread(agent.run_sync, "start", on_interaction=answer)
        else:
            result = await agent.run("start", on_interaction=answer)
        assert result.final_output == "continued"
        assert result.state is None
        assert actions == ["live-call"]
        assert tool_output(responses.requests[1], "live-call") == "answer"
        assert len(responses.requests) == 2


async def test_unsupported_manual_one_shot_rejects_before_provider_request(responses):
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    with pytest.raises(InteractionRequired, match="on_interaction") as failed:
        await agent.run("start")
    assert failed.value.state is None
    assert responses.requests == []


def test_unsupported_manual_sync_one_shot_rejects_before_startup():
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=NativeProvider(base_url="http://127.0.0.1:1/v1", api_key="synthetic-manual-test"),
        programmatic_tool_calling=True,
    )
    with pytest.raises(InteractionRequired, match="on_interaction") as failed:
        agent.run_sync("start")
    assert failed.value.state is None


@pytest.mark.parametrize("symbol", ["FunctionToolPolicy", "EnvironmentToolPolicy", "MCPToolPolicy"])
def test_unreleased_policy_wrappers_are_not_exported(symbol):
    import agents

    assert symbol not in agents.__all__
    with pytest.raises(AttributeError):
        getattr(agents, symbol)


async def test_caught_graceful_stop_after_request_wait_has_no_background_error(responses):
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    lookup = FunctionTool(
        name="lookup", description="Read a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "call-stop"))
    try:
        async with Agent(
            instructions="Read.", tools=[lookup], provider=provider(responses)
        ).session() as session:
            turn = await session.submit("Read A.")
            pending = await turn.wait_for_request_or_result()
            request = pending.requests[0]
            # Also discard an explicit passive result waiter before failure.
            waiting = asyncio.create_task(turn.get_result())
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            stopping = asyncio.create_task(turn.cancel(mode="graceful"))
            await asyncio.sleep(0)
            assert not stopping.done()
            await session.respond(request.reply("answer"))
            await stopping
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert errors == []
    finally:
        loop.set_exception_handler(previous)
