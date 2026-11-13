"""The native one-shot runner can settle a supported failure with typed output."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    GuardrailResult,
    NativeError,
    NativeProvider,
    OutputGuardrail,
    OutputValidationError,
    PromptedOutput,
    RecoverRun,
    RunBudget,
    RunFailure,
    Session,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent_observation import provider


class Record(BaseModel):
    count: int


@pytest.mark.parametrize("persist", [True, False])
async def test_error_handler_returns_validated_output_and_controls_replay(responses, persist):
    responses.respond(assistant_output("bad output"))
    responses.respond(assistant_output('{"count":2}'))
    native = provider(responses)
    seen: list[RunFailure] = []

    def recover(failure: RunFailure) -> RecoverRun[dict[str, int]]:
        seen.append(failure)
        return RecoverRun({"count": 1}, include_in_history=persist)

    agent = Agent(
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
    )
    async with timeout(20):
        try:
            result = await agent.run(
                "first",
                error_handler=recover,
                return_state=True,
                return_history=True,
                return_items=True,
            )
            assert result.final_output == Record(count=1)
            assert result.state is not None and result.history is not None
            assert len(seen) == 1 and seen[0].kind == "invalid_output"
            assert isinstance(seen[0].error, OutputValidationError)
            assert seen[0].details.input == "first"
            assert len(seen[0].details.raw_responses) == 1
            assert any(
                item.get("role") == "user" and "first" in str(item.get("content"))
                for item in seen[0].details.history
            )
            assert (await result.turn.get_result()).final_output == Record(count=1)
            assert result.items is not None
            synthesized = [
                item.item
                for item in result.items
                if item.item["type"] == "message"
                and item.item["content"][0]["text"] == '{"count": 1}'
            ]
            assert len(synthesized) == int(persist)
            if synthesized:
                assert synthesized[0]["id"] in {item.get("id") for item in result.history}
            assert (
                any(
                    part.get("text") == '{"count": 1}'
                    for item in result.history
                    if item.get("role") == "assistant"
                    for part in item.get("content", [])
                )
                is persist
            )
            next_result = await agent.run("second", state=result.state)
            assert next_result.final_output == Record(count=2)
            assert (
                any(
                    part.get("text") == '{"count": 1}'
                    for item in responses.requests[1]["input"]
                    if item.get("role") == "assistant"
                    for part in item.get("content", [])
                )
                is persist
            )
        finally:
            await native.aclose()


async def test_request_limit_fallback_does_not_retry_or_repeat_effects(responses):
    calls: list[str] = []

    @function_tool
    def record() -> str:
        calls.append("record")
        return "done"

    responses.respond(function_call("record", {}, "first"))
    native = provider(responses)
    seen: list[str] = []

    async def recover(failure: RunFailure) -> RecoverRun[str]:
        seen.append(failure.kind)
        assert isinstance(failure.error, NativeError)
        assert failure.error.code == "run_budget_exceeded"
        assert failure.details.items
        return RecoverRun("Budget reached", include_in_history=False)

    agent = Agent(instructions="Record and answer.", provider=native, tools=[record])
    async with timeout(20):
        try:
            result = await agent.run(
                "begin", budget=RunBudget(max_requests=1), error_handler=recover
            )
            assert result.final_output == "Budget reached"
            assert seen == ["max_requests"] and calls == ["record"]
            assert len(responses.requests) == 1
        finally:
            await native.aclose()


async def test_refusal_fallback_and_unhandled_failure_keep_their_own_causes():
    from tests.test_native_response_facts import fact_peer

    async with timeout(20), fact_peer() as peer:
        peer.replies.extend([(True, False), (True, False)])
        native = NativeProvider(base_url=peer.url, api_key="synthetic-facts-key")
        agent = Agent(instructions="Answer.", model="gpt-5.2", provider=native)
        seen: list[RunFailure] = []

        def decline(failure: RunFailure) -> None:
            seen.append(failure)

        try:
            recovered = await agent.run("answer", error_handler=lambda _: RecoverRun("fallback"))
            assert recovered.final_output == "fallback"
            with pytest.raises(NativeError) as caught:
                await agent.run("answer again", error_handler=decline)
            assert len(seen) == 1 and seen[0].kind == "model_refusal"
            assert caught.value is seen[0].error
        finally:
            await native.aclose()


async def test_fallback_respects_existing_output_guardrails(responses):
    responses.respond(assistant_output("bad output"))
    native = provider(responses)
    called: list[RunFailure] = []

    def recover(failure: RunFailure) -> RecoverRun[Record]:
        called.append(failure)
        return RecoverRun(Record(count=3))

    agent = Agent(
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
        output_guardrails=[OutputGuardrail("final", lambda *_: GuardrailResult(True))],
    )
    try:
        async with timeout(20):
            with pytest.raises(OutputValidationError):
                await agent.run("first", error_handler=recover)
            assert not called
    finally:
        await native.aclose()


async def test_invalid_fallback_does_not_return_success(responses):
    responses.respond(assistant_output("bad output"))
    native = provider(responses)
    agent = Agent(
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
    )
    try:
        async with timeout(20):
            with pytest.raises(OutputValidationError) as caught:
                await agent.run("first", error_handler=lambda _: RecoverRun({"other": 3}))
            assert caught.value.raw_output == ""
            assert len(responses.requests) == 1
    finally:
        await native.aclose()


@pytest.mark.parametrize("cancel_source", ["caller", "policy"])
async def test_error_recovery_cancellation_keeps_canonical_history(
    responses, monkeypatch, cancel_source
):
    responses.respond(assistant_output("bad output"))
    native = provider(responses)
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    histories = []
    calls: list[RunFailure] = []
    original = Session._recover_one_shot

    async def observe_recovery(session, *args):
        before = await session.history_snapshot()
        try:
            return await original(session, *args)
        finally:
            after = await session.history_snapshot()
            histories.append((before, after))

    monkeypatch.setattr(Session, "_recover_one_shot", observe_recovery)

    async def recover(failure: RunFailure) -> RecoverRun[dict[str, int]]:
        calls.append(failure)
        entered.set()
        try:
            await release.wait()
            if cancel_source == "policy":
                raise asyncio.CancelledError("Policy cancelled recovery")
            return RecoverRun({"count": 73019})
        finally:
            finished.set()

    agent = Agent(
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
    )
    run = asyncio.create_task(agent.run("first", error_handler=recover))
    try:
        async with timeout(20):
            await entered.wait()
            if cancel_source == "caller":
                run.cancel()
            else:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await run
            assert finished.is_set() and run.cancelled()
            assert len(calls) == 1 and calls[0].kind == "invalid_output"
            assert len(histories) == 1
            before, after = histories[0]
            assert after.revision == before.revision
            assert after.items == before.items
            assert "73019" not in repr(after.items)
            assert len(responses.requests) == 1
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await native.aclose()
