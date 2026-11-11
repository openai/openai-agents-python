"""Public terminal functions use accepted native receipts and one durable result."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel, field_serializer, field_validator

from agents import (
    Agent,
    CompletionContext,
    ContinueTurn,
    FinishToolBatch,
    FunctionTool,
    NativeProvider,
    OutputValidationError,
    SessionState,
    StopTurn,
    ToolBatchCompletionContext,
    ToolOutput,
    ToolRef,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_manual_tools import LookupArgs, next_request


def provider(peer: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=peer.url, api_key="synthetic-terminal-tools")


def test_native_batch_completion_preserves_legacy_profile_fingerprints() -> None:
    from _agents_codex._agent_profiles import _capture_agent_graph
    from _agents_codex.session import _capture_profile_runtimes, _capture_tools
    from agents import _native

    def roster(callback: Any = None) -> list[dict[str, Any]]:
        target = Agent(instructions="Other", name="Other")
        root = Agent(
            instructions="Answer",
            name="Root",
            handoffs=[target],
            tool_batch_completion=callback,
        )
        _, declarations = _capture_profile_runtimes(
            _capture_agent_graph(root),
            _capture_tools(0, (), None),
            "Answer",
            None,
        )
        return declarations

    legacy = roster()
    assert all("tool_batch_completion" not in row for row in legacy)
    # Captured with the released pre-batch native fingerprint implementation.
    expected = "1ee377cb24dd5dd57ebf22fc21c356f7b11e274e747805d28edc2fe81122f12f"
    assert _native._agent_profiles_fingerprint(json.dumps(legacy), "agent_0") == expected
    explicit_false = [{**row, "tool_batch_completion": False} for row in legacy]
    assert _native._agent_profiles_fingerprint(json.dumps(explicit_false), "agent_0") == expected
    enabled = roster(lambda _: ContinueTurn(instructions="continue"))
    assert enabled[0]["tool_batch_completion"] is True
    assert "tool_batch_completion" not in enabled[1]
    assert _native._agent_profiles_fingerprint(json.dumps(enabled), "agent_0") != expected


async def test_native_batch_completion_uses_ordered_known_results_and_typed_final_value(
    responses: ResponsesFixture,
) -> None:
    second_finished = asyncio.Event()
    effects: list[str] = []
    proposals: list[tuple[str, ...]] = []

    class Final(BaseModel):
        failed: tuple[str, ...]

    @function_tool
    async def first() -> str:
        await second_finished.wait()
        effects.append("first")
        return "first result"

    @function_tool
    async def second() -> ToolOutput:
        effects.append("second")
        second_finished.set()
        return ToolOutput.error("explicit tool failure")

    async def complete(ctx: ToolBatchCompletionContext[Any]) -> FinishToolBatch[Final]:
        proposals.append(tuple(str(result.call.call_id) for result in ctx.results))
        return FinishToolBatch(
            Final(
                failed=tuple(
                    str(result.call.call_id) for result in ctx.results if not result.output.success
                )
            )
        )

    responses.respond(
        function_call("first", {}, "first-call"), function_call("second", {}, "second-call")
    )
    agent = Agent(
        instructions="Use both tools.",
        model="gpt-5.5",
        tools=[first, second],
        output_type=Final,
        provider=provider(responses),
        tool_batch_completion=complete,
    )
    async with timeout(30), agent.session() as session:
        completed = await (await session.submit("finish")).get_result()
        assert completed.final_output == Final(failed=("second-call",))
        assert completed.tool_call is None
        assert completed.tool_batch_call_ids == ("first-call", "second-call")
        history = (await session.save_state()).history
        terminals = [
            row["payload"]["tool_completion"]
            for row in history
            if row.get("type") == "event_msg" and row.get("payload", {}).get("tool_completion")
        ]
        assert len(terminals) == 1
        assert terminals[0]["batch_call_ids"] == ["first-call", "second-call"]
    assert effects == ["second", "first"]
    assert proposals == [("first-call", "second-call")]
    assert len(responses.requests) == 1


@pytest.mark.parametrize(
    "output",
    [
        ToolOutput(
            [{"type": "inputText", "text": "first"}, {"type": "inputText", "text": "second"}]
        ),
        ToolOutput.image("https://example.invalid/tool.png", detail="high"),
        ToolOutput.file(file_id="file-batch", filename="record.txt"),
    ],
    ids=["multipart-text", "image", "file"],
)
async def test_native_batch_completion_exposes_round_trippable_public_output(
    responses: ResponsesFixture,
    output: ToolOutput,
) -> None:
    observed: list[dict[str, Any]] = []

    @function_tool
    def lookup() -> ToolOutput:
        return output

    def complete(ctx: ToolBatchCompletionContext[Any]) -> FinishToolBatch[str]:
        assert len(ctx.results) == 1
        observed.append(ctx.results[0].output.to_native())
        return FinishToolBatch("complete")

    agent = Agent(
        instructions="Use lookup.",
        model="gpt-5.5",
        tools=[lookup],
        provider=provider(responses),
        tool_batch_completion=complete,
    )
    responses.respond(function_call("lookup", {}, "multipart-call"))
    async with timeout(30), agent.session() as session:
        completed = await (await session.submit("look up")).get_result()
        assert completed.final_output == "complete"
        assert completed.tool_batch_call_ids == ("multipart-call",)
    assert observed == [output.to_native()]
    assert len(responses.requests) == 1


async def test_native_batch_completion_yields_to_accepted_handoff(
    responses: ResponsesFixture,
) -> None:
    calls: list[str] = []
    proposals: list[ToolBatchCompletionContext[Any]] = []

    @function_tool
    def lookup() -> str:
        calls.append("lookup")
        return "lookup result"

    def complete(ctx: ToolBatchCompletionContext[Any]) -> FinishToolBatch[str]:
        proposals.append(ctx)
        return FinishToolBatch("source batch output")

    destination = Agent(name="Destination", instructions="Answer from the destination.")
    source = Agent(
        instructions="Look up the record and hand off.",
        model="gpt-5.5",
        tools=[lookup],
        handoffs=[destination],
        provider=provider(responses),
        tool_batch_completion=complete,
    )
    responses.respond(
        function_call("lookup", {}, "lookup"),
        function_call("transfer_to_destination", {}, "handoff"),
    )
    responses.respond(assistant_output("destination output"))
    async with timeout(30), source.session() as session:
        completed = await (await session.submit("look up and hand off")).get_result()
        assert completed.final_output == "destination output"
        assert await session.get_active_agent() is destination
    assert calls == ["lookup"]
    assert not proposals
    assert len(responses.requests) == 2


@pytest.mark.parametrize("success", [False, True])
async def test_native_batch_completion_uses_fresh_manual_reply_after_resume(
    responses: ResponsesFixture,
    success: bool,
) -> None:
    manual = FunctionTool(
        name="lookup",
        description="Look up a record.",
        input_type=LookupArgs,
        output_type=ToolOutput,
    )
    proposals: list[tuple[str | None, bool]] = []

    def complete(ctx: ToolBatchCompletionContext[Any]) -> FinishToolBatch[str]:
        proposals.extend((result.call.call_id, result.output.success) for result in ctx.results)
        return FinishToolBatch("lookup could not be completed")

    agent = Agent(
        instructions="Use lookup.",
        model="gpt-5.5",
        tools=[manual],
        provider=provider(responses),
        tool_batch_completion=complete,
    )
    responses.respond(function_call("lookup", {"key": "A"}, "lookup-pending"))
    async with timeout(30), agent.session() as source:
        await source.submit("look up A")
        await next_request(source.interactions(), manual)
        serialized = (await source.suspend()).to_json()
    async with timeout(30), agent.session(state=SessionState.from_json(serialized)) as restored:
        assert restored.restored_turn is not None
        request = await next_request(restored.interactions(), manual)
        await request.respond(
            ToolOutput.text("lookup succeeded") if success else ToolOutput.error("lookup failed")
        )
        completed = await restored.restored_turn.get_result()
        assert completed.final_output == "lookup could not be completed"
        assert completed.tool_call is None
        assert completed.tool_batch_call_ids == ("lookup-pending",)
    assert proposals == [("lookup-pending", success)]
    assert len(responses.requests) == 1


async def test_native_batch_completion_can_continue_without_replaying_tools(
    responses: ResponsesFixture,
) -> None:
    effects: list[str] = []
    proposals: list[tuple[str, ...]] = []

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "lookup result"

    def complete(ctx: ToolBatchCompletionContext[Any]) -> None:
        proposals.append(tuple(str(result.call.call_id) for result in ctx.results))

    responses.respond(function_call("lookup", {}, "lookup-call"))
    responses.respond(assistant_output("assistant result"))
    agent = Agent(
        instructions="Use lookup.",
        model="gpt-5.5",
        tools=[lookup],
        provider=provider(responses),
        tool_batch_completion=complete,
    )
    async with timeout(30), agent.session() as session:
        assert (
            await (await session.submit("finish")).get_result()
        ).final_output == "assistant result"
    assert effects == ["lookup"]
    assert proposals == [("lookup-call",)]
    assert len(responses.requests) == 2


@pytest.mark.parametrize("finish", [False, True])
async def test_default_off_and_one_request_terminal_result(
    responses: ResponsesFixture, finish: bool
) -> None:
    effects: list[str] = []

    @function_tool(finish_turn=finish)
    def done() -> str:
        effects.append("once")
        return "tool result"

    responses.respond(function_call("done", {}, "done-call"))
    if not finish:
        responses.respond(assistant_output("assistant result"))
    async with (
        timeout(30),
        Agent(
            instructions="Use done.", model="gpt-5.5", tools=[done], provider=provider(responses)
        ).session() as session,
    ):
        result = await (await session.submit("go")).get_result()
        assert result.final_output == ("tool result" if finish else "assistant result")
        if not finish:
            assert result.tool_call is None
        if finish:
            assert result.tool_call is not None
            assert result.tool_call.tool == ToolRef("done")
            assert result.tool_call.call_id == "done-call"
            assert result.tool_call.route == "direct"
            state = await session.save_state()
            terminal = [
                item["payload"]
                for item in state.history
                if item.get("type") == "event_msg"
                and item.get("payload", {}).get("tool_completion")
            ]
            assert len(terminal) == 1
            assert terminal[0]["tool_completion"]["final_output"] == "tool result"
            assert terminal[0].get("last_agent_message") is None
    assert effects == ["once"]
    assert "finish_turn" not in json.dumps(responses.requests[0]["tools"])
    assert len(responses.requests) == (1 if finish else 2)


async def test_structured_terminal_validates_serializes_and_calls_policy_once(
    responses: ResponsesFixture,
) -> None:
    counts = {"validation": 0, "serialization": 0, "policy": 0}

    class Final(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def validate_value(cls, value: int) -> int:
            counts["validation"] += 1
            return value

        @field_serializer("value")
        def serialize_value(self, value: int) -> int:
            counts["serialization"] += 1
            return value

    @function_tool(finish_turn=True, namespace="billing")
    def done() -> dict[str, int]:
        return {"value": 7}

    async def before_complete(ctx: CompletionContext[Any]) -> None:
        counts["policy"] += 1
        assert ctx.text is None and ctx.tool_call is not None
        assert ctx.tool_call.tool == ToolRef("done", "billing")
        assert isinstance(ctx.final_output, Final)
        ctx.final_output.value = 999  # Policy receives a detached observation.

    event = function_call("done", {}, "qualified-done")
    event["item"]["namespace"] = "billing"
    responses.respond(event)
    agent = Agent(
        instructions="Use done.",
        model="gpt-5.5",
        output_type=Final,
        tools=[done],
        provider=provider(responses),
        before_complete=before_complete,
    )
    async with timeout(30), agent.session() as session:
        completed = await (await session.submit("finish")).get_result()
        assert isinstance(completed.final_output, Final)
        assert completed.final_output.value == 7
        assert completed.tool_call is not None and completed.tool_call.tool == ToolRef(
            "done", "billing"
        )
    assert counts == {"validation": 1, "serialization": 1, "policy": 1}
    assert len(responses.requests) == 1


async def test_original_call_order_wins_after_reversed_callback_completion(
    responses: ResponsesFixture,
) -> None:
    second_finished = asyncio.Event()
    effects: list[str] = []

    @function_tool(finish_turn=True)
    async def first() -> str:
        await second_finished.wait()
        effects.append("first")
        return "first result"

    @function_tool(finish_turn=True)
    def second() -> str:
        effects.append("second")
        second_finished.set()
        return "second result"

    responses.respond(
        function_call("first", {}, "first-call"), function_call("second", {}, "second-call")
    )
    async with (
        timeout(30),
        Agent(
            instructions="Use both.",
            model="gpt-5.5",
            tools=[first, second],
            provider=provider(responses),
        ).session(max_function_concurrency=2) as session,
    ):
        completed = await (await session.submit("finish")).get_result()
        assert completed.final_output == "first result"
        assert completed.tool_call is not None and completed.tool_call.call_id == "first-call"
    assert effects == ["second", "first"]
    assert len(responses.requests) == 1


@pytest.mark.parametrize("decision", ["continue", "stop"])
async def test_completion_policy_owns_tool_proposals(
    responses: ResponsesFixture, decision: str
) -> None:
    calls: list[tuple[str | None, bool]] = []

    @function_tool(finish_turn=True)
    def done() -> str:
        return "tool candidate"

    def policy(ctx: CompletionContext[Any]) -> ContinueTurn | StopTurn | None:
        calls.append((ctx.text, ctx.tool_call is not None))
        if ctx.tool_call is not None:
            return (
                ContinueTurn("answer naturally")
                if decision == "continue"
                else StopTurn("policy stopped")
            )
        return None

    responses.respond(function_call("done", {}, "done"))
    if decision == "continue":
        responses.respond(assistant_output("natural"))
    async with (
        timeout(30),
        Agent(
            instructions="Use done.",
            model="gpt-5.5",
            tools=[done],
            provider=provider(responses),
            before_complete=policy,
        ).session() as session,
    ):
        handle = await session.submit("go")
        if decision == "stop":
            with pytest.raises(TurnStoppedError, match="policy"):
                await handle.get_result()
        else:
            result = await handle.get_result()
            assert result.final_output == "natural" and result.tool_call is None
    assert calls == (
        [(None, True), ("natural", False)] if decision == "continue" else [(None, True)]
    )
    assert len(responses.requests) == (2 if decision == "continue" else 1)


async def test_bad_terminal_output_preserves_owning_validation_error_without_repair(
    responses: ResponsesFixture,
) -> None:
    class Final(BaseModel):
        value: int

    @function_tool(finish_turn=True)
    def done() -> str:
        return '{"value":"invalid"}'

    responses.respond(function_call("done", {}, "done"))
    async with (
        timeout(30),
        Agent(
            instructions="Use done.",
            model="gpt-5.5",
            output_type=Final,
            tools=[done],
            provider=provider(responses),
        ).session() as session,
    ):
        with pytest.raises(OutputValidationError):
            await (await session.submit("go")).get_result()
    assert len(responses.requests) == 1


@pytest.mark.parametrize("remove_outcomes", [False, True])
async def test_fresh_manual_finisher_after_json_restore_needs_no_model_request(
    responses: ResponsesFixture, remove_outcomes: bool
) -> None:
    manual = FunctionTool(
        name="finish",
        description="Finish once.",
        input_type=LookupArgs,
        output_type=str,
        finish_turn=True,
    )
    responses.respond(function_call("finish", {"key": "A"}, "manual-finish"))
    agent = Agent(
        instructions="Use finish.", model="gpt-5.5", tools=[manual], provider=provider(responses)
    )
    async with timeout(30), agent.session() as source:
        await source.submit("finish")
        await next_request(source.interactions(), manual)
        payload = json.loads((await source.suspend()).to_json())
    if remove_outcomes:
        payload["native_continuation"].pop("tool_outcomes", None)
    state = SessionState.from_json(json.dumps(payload))
    async with timeout(30), agent.session(state=state) as restored:
        assert restored.restored_turn is not None
        request = await next_request(restored.interactions(), manual)
        await request.respond("restored result")
        result = await restored.restored_turn.get_result()
        assert result.final_output == "restored result"
        assert result.tool_call is not None and result.tool_call.call_id == "manual-finish"
    assert len(responses.requests) == 1
