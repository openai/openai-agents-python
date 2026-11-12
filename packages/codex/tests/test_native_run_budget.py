"""Exercise run budgets through the native engine and a synthetic Responses peer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import BaseModel

from agents import (
    ActionReply,
    Agent,
    CapabilityError,
    CustomStorage,
    FunctionTool,
    Interaction,
    InteractionRequired,
    LocalStorage,
    NativeError,
    NativeProvider,
    PreparedRequest,
    PromptedOutput,
    RunBudget,
    Turn,
    function_tool,
)
from agents.usage import RunBudget as UsageRunBudget, TokenUsage
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.native_storage_fixtures import MemorySessionStore
from tests.test_native_agent import function_declarations
from tests.test_native_agent_observation import provider
from tests.test_native_manual_tools import next_request
from tests.test_review_ii_steering_tail import ProjectContinuation


@pytest.mark.parametrize("max_requests", [1, 2])
async def test_budget_follows_live_managed_approval(responses, max_requests):
    effects: list[str] = []

    @function_tool(needs_approval=True)
    def record() -> str:
        effects.append("accepted")
        return "recorded"

    responses.respond(function_call("record", {}, "approved-effect"))
    if max_requests == 2:
        responses.respond(assistant_output("Done"))
    agent = Agent(instructions="Record and answer.", provider=provider(responses), tools=[record])
    async with timeout(20), agent.session() as session:
        turn = await session.submit(
            "begin", budget=RunBudget(max_requests=max_requests, max_tokens=1000)
        )
        request = await next_request(session.interactions(), record)
        assert effects == []
        await request.approve()
        if max_requests == 1:
            with pytest.raises(NativeError) as stopped:
                await turn.get_result()
            assert stopped.value.code == "run_budget_exceeded"
        else:
            assert (await turn.get_result()).final_output == "Done"
        assert effects == ["accepted"]
    assert len(responses.requests) == max_requests


@pytest.mark.parametrize("backend", ["local", "custom"])
async def test_settled_store_reopen_starts_a_fresh_budget(responses, tmp_path: Path, backend):
    storage = (
        LocalStorage(tmp_path / "profile")
        if backend == "local"
        else CustomStorage(MemorySessionStore())
    )
    responses.respond(assistant_output("First"))
    responses.respond(assistant_output("Second"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(20):
        async with agent.session(storage=storage) as session:
            identity = session.id
            first = await session.submit("first", budget=RunBudget(max_requests=1))
            assert (await first.get_result()).final_output == "First"
            with pytest.raises(CapabilityError, match="Persistent storage"):
                await session.save_state()
        async with agent.session(storage=storage, resume=identity) as reopened:
            assert reopened.id == identity
            second = await reopened.submit("second", budget=RunBudget(max_requests=1))
            assert (await second.get_result()).final_output == "Second"
    assert len(responses.requests) == 2
    assert "First" in json.dumps(responses.requests[1]["input"])


class LookupArgs(BaseModel):
    key: str


def lookup_tool() -> FunctionTool[LookupArgs, str, Any]:
    return FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )


async def require_follow_up(event: dict[str, Any]) -> None:
    if event["type"] == "response.completed" and event["response"]["id"] == "resp_1":
        event["response"]["end_turn"] = False


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"max_requests": -1},
        {"max_requests": True},
        {"max_tokens": 2**53},
        {"max_tokens": 1.5},
        {"max_estimated_cost_micros": float("inf")},
        {"max_requests": 1, "missing_usage": "ignore"},
        {"max_requests": 1, "estimate_cost_micros": 2},
    ],
)
def test_run_budget_public_type_and_validation(options: dict[str, Any]) -> None:
    assert RunBudget is UsageRunBudget
    assert RunBudget.__module__ == "agents.usage"
    with pytest.raises((ValueError, TypeError), match="RunBudget"):
        RunBudget(**options)


@pytest.mark.parametrize(
    "budget",
    [
        RunBudget(max_requests=1),
        RunBudget(max_tokens=14),
        RunBudget(
            max_estimated_cost_micros=14, estimate_cost_micros=lambda usage: usage.total_tokens
        ),
    ],
)
async def test_limit_at_equality_blocks_next_sampling_before_selector(
    responses: ResponsesFixture, budget: RunBudget
) -> None:
    steps: list[int] = []

    def before_step(turn: Turn[str], *, step_index: int) -> None:
        steps.append(step_index)

    responses.before_event = require_follow_up
    responses.respond(assistant_output("Need another sample"))
    responses.respond(assistant_output("This request must be blocked"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(15):
        with pytest.raises(NativeError) as stopped:
            await agent.run("answer", budget=budget, before_step=before_step)
    assert stopped.value.code == "run_budget_exceeded"
    assert steps == [0]
    assert len(responses.requests) == 1


async def test_request_budget_gates_the_next_prepared_request(
    responses: ResponsesFixture,
) -> None:
    preparations: list[int] = []

    def prepare(
        turn: Turn[Any], request: PreparedRequest, *, preparation_index: int, recovery_attempt: int
    ) -> None:
        preparations.append(preparation_index)

    responses.before_event = require_follow_up
    responses.respond(assistant_output("Need another sample"))
    responses.respond(assistant_output("This request must be blocked"))
    agent = Agent(
        instructions="Answer.",
        model="gpt-5.5",
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-budget-key", supports_websockets=False
        ),
    )
    async with timeout(15):
        with pytest.raises(NativeError) as stopped:
            await agent.run("answer", budget=RunBudget(max_requests=1), prepare_request=prepare)
    assert stopped.value.code == "run_budget_exceeded"
    assert preparations == [0]
    assert len(responses.requests) == 1


@pytest.mark.parametrize(
    ("usage", "missing_usage", "metric", "expected"),
    [
        (0, "stop", "tokens", None),
        (None, "stop", "tokens", "run_budget_usage_unavailable"),
        (None, "continue", "tokens", None),
        (-1, "stop", "tokens", "run_budget_usage_unavailable"),
        (None, "stop", "requests", None),
        (0, "stop", "cost", None),
        (None, "stop", "cost", "run_budget_usage_unavailable"),
        (None, "continue", "cost", None),
        (-1, "continue", "cost", None),
        (1, "stop", "cost", "run_budget_usage_unavailable"),
        (1, "continue", "cost", None),
    ],
)
async def test_reported_zero_and_missing_values_at_next_sample(
    responses: ResponsesFixture,
    usage: int | None,
    missing_usage: Literal["stop", "continue"],
    metric: str,
    expected: str | None,
) -> None:
    estimates: list[TokenUsage] = []

    def estimate(reported: TokenUsage) -> int | None:
        estimates.append(reported)
        return 0 if reported.total_tokens == 0 else None

    async def report(event: dict[str, Any]) -> None:
        if event["type"] != "response.completed" or event["response"]["id"] != "resp_1":
            return
        if usage is None:
            event["response"].pop("usage")
        else:
            event["response"]["usage"] = {
                "input_tokens": max(usage, 0),
                "output_tokens": 0,
                "total_tokens": usage,
            }

    responses.before_event = report
    responses.respond(function_call("lookup", {"key": "A"}, "manual"))
    responses.respond(assistant_output("Done"))
    lookup = lookup_tool()
    budget = RunBudget(
        max_requests=2 if metric == "requests" else None,
        max_tokens=1 if metric == "tokens" else None,
        max_estimated_cost_micros=1 if metric == "cost" else None,
        estimate_cost_micros=estimate,
        missing_usage=missing_usage,
    )
    agent = Agent(instructions="Look up the key.", provider=provider(responses), tools=[lookup])
    async with timeout(15), agent.session() as session:
        turn = await session.submit("look up A", budget=budget)
        request = await next_request(session.interactions(), lookup)
        await request.respond("A result")
        if expected is None:
            assert (await turn.get_result()).final_output == "Done"
        else:
            with pytest.raises(NativeError) as stopped:
                await turn.get_result()
            assert stopped.value.code == expected
    assert len(responses.requests) == (2 if expected is None else 1)
    assert estimates == (
        [TokenUsage(usage, 0, 0, 0, 0, usage)]
        if metric == "cost" and usage is not None and usage >= 0
        else []
    )


@pytest.mark.parametrize("compaction_usage_missing", [True, False])
async def test_missing_usage_stops_before_estimating_later_completions(
    responses: ResponsesFixture, compaction_usage_missing: bool
) -> None:
    estimates: list[int] = []

    @function_tool
    async def lookup(key: str) -> str:
        return "Known result"

    def estimate(usage: TokenUsage) -> None:
        estimates.append(usage.total_tokens)

    async def report(event: dict[str, Any]) -> None:
        if event["type"] != "response.completed":
            return
        response = event["response"]
        if response["id"] == "resp_1":
            response["usage"] = {
                "input_tokens": 249_999,
                "output_tokens": 1,
                "total_tokens": 250_000,
            }
        elif response["id"] == "resp_2" and compaction_usage_missing:
            response.pop("usage")

    responses.before_event = report
    responses.respond(function_call("lookup", {"key": "A"}, "auto"))
    responses.respond(assistant_output("Compacted summary"))
    responses.respond(assistant_output("This request must be blocked"))
    agent = Agent(
        instructions="Look up the key.",
        model="gpt-5.2",
        provider=provider(responses),
        tools=[lookup],
    )
    async with timeout(15):
        with pytest.raises(NativeError) as stopped:
            await agent.run(
                "look up A",
                budget=RunBudget(max_estimated_cost_micros=100, estimate_cost_micros=estimate),
            )
    assert stopped.value.code == "run_budget_usage_unavailable"
    assert len(responses.requests) == 2
    assert function_declarations(responses.requests[1]) == []
    assert estimates == ([] if compaction_usage_missing else [250_000])


@pytest.mark.parametrize("value", [-1, 2**53, True, 1.5, "private estimator return", "raise"])
async def test_invalid_estimator_stops_with_sanitized_error(
    responses: ResponsesFixture, value: Any
) -> None:
    def estimate(usage: TokenUsage) -> int:
        if value == "raise":
            raise RuntimeError("private estimator failure")
        return cast(int, value)

    responses.before_event = require_follow_up
    responses.respond(assistant_output("Need another sample"))
    responses.respond(assistant_output("This request must be blocked"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(15):
        with pytest.raises(NativeError) as stopped:
            await agent.run(
                "answer",
                budget=RunBudget(
                    max_estimated_cost_micros=100,
                    estimate_cost_micros=estimate,
                    missing_usage="continue",
                ),
            )
    assert stopped.value.code == "run_budget_invalid_estimate"
    assert "private estimator" not in str(stopped.value)
    assert stopped.value.__context__ is stopped.value.__cause__ is None
    assert len(responses.requests) == 1


async def test_final_overshoot_can_save_idle_and_start_a_fresh_budget(
    responses: ResponsesFixture,
) -> None:
    responses.respond(assistant_output("First"))
    responses.respond(assistant_output("Second"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    budget = RunBudget(max_tokens=1, max_requests=1)
    async with timeout(15):
        first = await agent.run("first", budget=budget, return_state=True)
        assert first.final_output == "First"
        assert first.state is not None
        assert first.state.native_continuation is None
        assert (
            first.thread_usage is not None
            and first.thread_usage.last_token_usage.total_tokens == 14
        )
        second = await asyncio.to_thread(agent.run_sync, "second", state=first.state, budget=budget)
        assert second.final_output == "Second"
    assert len(responses.requests) == 2


async def test_zero_limit_blocks_first_request_and_callback(responses: ResponsesFixture) -> None:
    steps: list[int] = []

    def before_step(turn: Turn[str], *, step_index: int) -> None:
        steps.append(step_index)

    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(15):
        with pytest.raises(NativeError) as stopped:
            await agent.run("answer", before_step=before_step, budget=RunBudget(max_requests=0))
    assert stopped.value.code == "run_budget_exceeded"
    assert steps == []
    assert not responses.requests


@pytest.mark.parametrize("manual_selected", [True, False])
async def test_budgeted_idle_restore_uses_saved_default_selection(
    responses: ResponsesFixture, manual_selected: bool
) -> None:
    lookup = lookup_tool()
    agent = Agent(instructions="Answer.", provider=provider(responses), tools=[lookup])
    budget = RunBudget(max_requests=1, max_estimated_cost_micros=1000)
    async with timeout(15):
        async with agent.session() as session:
            await session.update(tools=[lookup.ref] if manual_selected else [])
            saved = await session.save_state()
        if manual_selected:
            with pytest.raises(CapabilityError, match="cannot be captured"):
                await agent.run("answer", state=saved, budget=budget)
            assert not responses.requests
        else:
            responses.respond(assistant_output("Done"))
            result = await agent.run("answer", state=saved, budget=budget)
            assert result.final_output == "Done"
            assert len(responses.requests) == 1
            assert function_declarations(responses.requests[0]) == []


async def test_budget_keeps_live_manual_boundary_but_refuses_capture(
    responses: ResponsesFixture,
) -> None:
    lookup = lookup_tool()
    agent = Agent(instructions="Look up the key.", provider=provider(responses), tools=[lookup])
    budget = RunBudget(max_requests=1, max_estimated_cost_micros=1000)
    async with timeout(20):
        with pytest.raises(CapabilityError, match="cannot be captured"):
            await agent.run("no automatic capture", budget=budget)
        assert not responses.requests
        responses.respond(function_call("lookup", {"key": "A"}, "manual"))
        async with agent.session() as session:
            turn = await session.submit("look up A", budget=budget)
            request = await next_request(session.interactions(), lookup)
            assert (await session.checkpoint_status(kind="manual_wait")).status == "not_ready"
            with pytest.raises(CapabilityError, match="Estimated-cost"):
                await session.suspend()
            await request.respond("result from the live application")
            with pytest.raises(NativeError) as stopped:
                await turn.get_result()
            assert stopped.value.code == "run_budget_exceeded"
            assert len(responses.requests) == 1
            responses.respond(assistant_output("Fresh"))
            fresh = await session.submit("fresh root", budget=budget)
            assert (await fresh.get_result()).final_output == "Fresh"
    assert len(responses.requests) == 2


async def test_budget_survives_a_one_shot_live_interaction(responses: ResponsesFixture) -> None:
    lookup = lookup_tool()
    answers: list[str] = []

    def answer(event: Interaction) -> ActionReply[str]:
        request = lookup.match(event)
        assert request is not None
        answers.append(request.arguments.key)
        return request.reply("live answer")

    responses.respond(function_call("lookup", {"key": "A"}, "manual"))
    agent = Agent(instructions="Look up the key.", provider=provider(responses), tools=[lookup])
    async with timeout(15):
        with pytest.raises(NativeError) as stopped:
            await agent.run("look up A", budget=RunBudget(max_requests=1), on_interaction=answer)
    assert stopped.value.code == "run_budget_exceeded"
    assert answers == ["A"]
    assert len(responses.requests) == 1


async def test_budget_cannot_be_added_to_a_cold_active_checkpoint(
    responses: ResponsesFixture,
) -> None:
    lookup = lookup_tool()
    agent = Agent(instructions="Look up the key.", provider=provider(responses), tools=[lookup])
    responses.respond(function_call("lookup", {"key": "A"}, "manual"))
    async with timeout(20):
        async with agent.session() as session:
            await session.submit("look up A")
            await next_request(session.interactions(), lookup)
            saved = await session.suspend()
        with pytest.raises(CapabilityError, match="restoring an active checkpoint"):
            await agent.run(None, state=saved, budget=RunBudget(max_requests=2))
    assert len(responses.requests) == 1


async def test_retained_steering_uses_the_same_budget(responses: ResponsesFixture) -> None:
    responses.respond(assistant_output("First native turn"))
    responses.respond(assistant_output("This request must be blocked"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(15), agent.session() as session:
        # Reuse the projection for Core's frozen steering receipt. The second
        # native turn must still pass the real selector and reach the same limit.
        session._native = ProjectContinuation(session._native)
        first = await session.submit("original", budget=RunBudget(max_requests=1))
        with pytest.raises(NativeError) as stopped:
            await first.get_result()
        assert stopped.value.code == "run_budget_exceeded"
    assert len(responses.requests) == 1


@pytest.mark.parametrize("valid_repair", [False, True])
async def test_prompted_repair_shares_budget_without_root_selector_or_tools(
    responses: ResponsesFixture, valid_repair: bool
) -> None:
    steps: list[int] = []
    lookup = lookup_tool()

    def before_step(
        turn: Turn[dict[str, int]], *, step_index: int
    ) -> list[FunctionTool[LookupArgs, str, Any]]:
        steps.append(step_index)
        return [lookup]

    responses.respond(assistant_output("invalid original"))
    responses.respond(assistant_output('{"count": 2}' if valid_repair else "invalid repair"))
    responses.respond(assistant_output("This request must be blocked"))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        output_type=PromptedOutput(dict[str, int], max_retries=2),
        tools=[lookup],
    )
    async with timeout(15):
        if valid_repair:
            result = await agent.run(
                "answer", budget=RunBudget(max_requests=2), before_step=before_step
            )
            assert result.final_output == {"count": 2}
            assert len(result.repair_attempts) == 1
        else:
            with pytest.raises(NativeError) as stopped:
                await agent.run("answer", budget=RunBudget(max_requests=2), before_step=before_step)
            assert stopped.value.code == "run_budget_exceeded"
    assert steps == [0]
    assert len(responses.requests) == 2
    assert any(tool["name"] == "lookup" for tool in function_declarations(responses.requests[0]))
    assert function_declarations(responses.requests[1]) == []


@pytest.mark.parametrize(
    ("code", "expected_requests"),
    [("run_budget_exceeded", 0), ("run_budget_usage_unavailable", 1)],
)
async def test_budget_stop_is_a_supervisory_outcome(
    responses: ResponsesFixture, code: str, expected_requests: int
) -> None:
    budget = RunBudget(max_requests=0)
    if code == "run_budget_usage_unavailable":

        async def omit_usage(event: dict[str, Any]) -> None:
            await require_follow_up(event)
            if event["type"] == "response.completed":
                event["response"].pop("usage", None)

        responses.before_event = omit_usage
        responses.respond(assistant_output("Need another sample"))
        responses.respond(assistant_output("This request must be blocked"))
        budget = RunBudget(max_tokens=14)
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(15), agent.session() as session:
        turn = await session.submit("Answer", budget=budget)
        outcome = await turn.wait_for_attention()
        from agents import TurnNeedsAttention

        assert isinstance(outcome, TurnNeedsAttention)
        assert outcome.reason == "budget_exceeded"
        assert isinstance(outcome.error, NativeError)
        assert outcome.error.code == code
        with pytest.raises(NativeError) as original:
            await turn.get_result()
        assert original.value is outcome.error
        assert len(responses.requests) == expected_requests


@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("max_requests", [1, 2])
async def test_request_budget_survives_portable_same_turn_restart(
    responses, approval, max_requests
):
    effects = []

    @function_tool
    def accepted() -> str:
        effects.append("sibling")
        return "recorded sibling"

    @function_tool(needs_approval=True)
    def approved() -> str:
        effects.append("approved")
        return "recorded approval"

    lookup = lookup_tool()
    pending = approved if approval else lookup
    arguments = {} if approval else {"key": "A"}
    responses.respond(
        function_call("accepted", {}, "sibling"), function_call(pending.name, arguments, "pending")
    )
    if max_requests == 2:
        responses.respond(assistant_output("Done"))
    agent = Agent(
        instructions="Use tools and answer.",
        provider=provider(responses),
        tools=[accepted, pending],
    )
    limits = RunBudget(max_requests=max_requests)
    async with timeout(20):
        async with agent.session() as source:
            await source.submit("begin", budget=limits)
            await next_request(source.interactions(), pending)
            saved = await source.suspend()
            assert (await source.suspend()).to_json() == saved.to_json()
        assert saved.format_version == 8
        assert saved.run_budget is not None
        assert saved.run_budget.admitted_requests == 1
        assert effects == ["sibling"]
        for invalid in (
            None,
            RunBudget(max_requests=max_requests + 1),
            RunBudget(max_requests=max_requests, max_tokens=1000),
        ):
            with pytest.raises(CapabilityError):
                async with agent.session(state=saved, resume_budget=invalid):
                    pytest.fail("Invalid trusted ceiling opened a Session")
        assert len(responses.requests) == 1
        async with agent.session(state=saved, resume_budget=limits) as restored:
            request = await next_request(restored.interactions(), pending)
            if approval:
                await request.approve()
            else:
                await request.respond("live result")
            if max_requests == 1:
                with pytest.raises(NativeError) as stopped:
                    await restored.restored_turn.get_result()
                assert stopped.value.code == "run_budget_exceeded"
            else:
                assert (await restored.restored_turn.get_result()).final_output == "Done"
        assert effects == (["sibling", "approved"] if approval else ["sibling"])
        assert len(responses.requests) == max_requests


async def test_restore_detaches_caller_input_before_native_acquisition(responses, monkeypatch):
    from agents import RunDiagnosticError, _native

    lookup = lookup_tool()
    limits = RunBudget(max_requests=1, max_tokens=14)
    agent = Agent(instructions="Look up and answer.", provider=provider(responses), tools=[lookup])
    responses.respond(function_call("lookup", {"key": "A"}, "pending"))
    original_input = {"role": "user", "content": [{"type": "input_text", "text": "Look up A"}]}
    acquired, release = asyncio.Event(), asyncio.Event()
    create = _native.create_checkpoint_engine
    acquisitions = []

    async def held_acquisition(*args, **kwargs):
        engine = await create(*args, **kwargs)
        acquisitions.append(engine)
        acquired.set()
        await release.wait()
        return engine

    async with timeout(20):
        with pytest.raises(RunDiagnosticError) as pending:
            await agent.run(original_input, budget=limits, capture_error_details=True)
        assert isinstance(pending.value.__cause__, InteractionRequired)
        saved = pending.value.__cause__.state
        monkeypatch.setattr(_native, "create_checkpoint_engine", held_acquisition)
        for invalid in (
            None,
            RunBudget(max_requests=2, max_tokens=14),
            RunBudget(max_requests=1, max_tokens=14, missing_usage="continue"),
        ):
            with pytest.raises(CapabilityError):
                async with agent.session(state=saved, resume_budget=invalid):
                    pytest.fail("Invalid current limits acquired restore resources")
        assert acquisitions == [] and len(responses.requests) == 1

        scope = agent.session(state=saved, resume_budget=limits)
        assert acquisitions == []
        saved.original_input["content"][0]["text"] = "Captured on context entry"
        expected_input = {
            "role": "user",
            "content": [{"type": "input_text", "text": "Captured on context entry"}],
        }

        async def restore():
            async with scope as restored:
                request = await next_request(restored.interactions(), lookup)
                assert request.call_id == "pending"
                return await restored.suspend()

        opening = asyncio.create_task(restore())
        try:
            await acquired.wait()
            assert saved.original_input == expected_input
            saved.original_input["content"][0]["text"] = "Changed after acquisition"
        finally:
            release.set()
            detached = await opening
        assert detached.original_input == expected_input
        assert detached.run_budget == saved.run_budget
        assert detached.run_budget.admitted_requests == 1
        assert detached.run_budget.consumed_tokens == 14
        assert detached.run_budget.usage_missing is False
        assert detached.run_usage.total_tokens == 14
        async with agent.session(state=detached, resume_budget=limits) as resumed:
            request = await next_request(resumed.interactions(), lookup)
            with pytest.raises(NativeError) as stopped:
                await request.respond("A result")
                await resumed.restored_turn.get_result()
            assert stopped.value.code == "run_budget_exceeded"
        assert len(acquisitions) == 2 and len(responses.requests) == 1
    await agent.provider.aclose()


@pytest.mark.parametrize("version", [True, 1.0, 2])
def test_request_budget_checkpoint_requires_an_exact_version(version):
    from _agents_codex.state import _RequestRunBudgetCheckpoint

    with pytest.raises(ValueError):
        _RequestRunBudgetCheckpoint.model_validate(
            {"version": version, "max_requests": 2, "admitted_requests": 1}
        )


async def test_request_budget_one_shot_manual_resume(responses):
    lookup = lookup_tool()
    responses.respond(function_call("lookup", {"key": "A"}, "pending"))
    responses.respond(assistant_output("Done"))
    agent = Agent(instructions="Use lookup.", provider=provider(responses), tools=[lookup])
    async with timeout(20):
        with pytest.raises(InteractionRequired) as pending:
            await agent.run("begin", budget=RunBudget(max_requests=2))
        state = pending.value.state
        assert state is not None and state.run_budget is not None
        result = await agent.run(
            None,
            state=state,
            budget=RunBudget(max_requests=2),
            on_interaction=lambda event: lookup.match(event).reply("result"),
        )
        assert result.final_output == "Done"
    assert len(responses.requests) == 2


@pytest.mark.parametrize(
    "fields",
    [
        {"max_requests": True, "admitted_requests": 0},
        {"max_requests": 2, "admitted_requests": True},
        {"max_requests": 2, "admitted_requests": 1.0},
        {"max_requests": 2, "admitted_requests": 3},
        {"max_requests": -1, "admitted_requests": 0},
        {"max_requests": 2**53, "admitted_requests": 0},
        {"max_requests": 2, "admitted_requests": 0, "max_tokens": 10},
    ],
)
def test_request_budget_checkpoint_rejects_malformed_admissions(fields):
    from _agents_codex.state import _RequestRunBudgetCheckpoint

    with pytest.raises(ValueError):
        _RequestRunBudgetCheckpoint.model_validate({"version": 1, **fields})


@pytest.mark.parametrize("max_requests", [None, 2])
@pytest.mark.parametrize("max_tokens", [10, 15])
async def test_token_budget_survives_portable_approval(responses, max_requests, max_tokens):
    effects = []

    @function_tool(needs_approval=True)
    def record() -> str:
        effects.append("approved")
        return "recorded"

    responses.respond(function_call("record", {}, "pending"))
    responses.respond(assistant_output("Done"))
    limits = RunBudget(max_requests=max_requests, max_tokens=max_tokens)
    agent = Agent(instructions="Record and answer.", provider=provider(responses), tools=[record])
    async with timeout(20):
        async with agent.session() as source:
            await source.submit("begin", budget=limits)
            await next_request(source.interactions(), record)
            saved = await source.suspend()
        assert saved.run_budget.version == 2
        assert saved.run_budget.consumed_tokens == min(14, max_tokens)
        assert saved.run_usage.total_tokens == 14
        assert saved.run_budget.admitted_requests == (1 if max_requests is not None else None)
        for invalid in (
            None,
            RunBudget(max_requests=max_requests, max_tokens=max_tokens + 1),
            RunBudget(max_requests=max_requests, max_tokens=max_tokens, missing_usage="continue"),
            RunBudget(max_requests=3, max_tokens=max_tokens),
            RunBudget(max_tokens=max_tokens, max_estimated_cost_micros=100),
        ):
            with pytest.raises(CapabilityError):
                async with agent.session(state=saved, resume_budget=invalid):
                    pytest.fail("Invalid current limits acquired restore resources")
        assert len(responses.requests) == 1
        async with agent.session(state=saved, resume_budget=limits) as restored:
            # Resealing an untouched restored hold must not join its live reader.
            await next_request(restored.interactions(), record)
            saved_again = await restored.suspend()
        async with agent.session(state=saved_again, resume_budget=limits) as restored:
            request = await next_request(restored.interactions(), record)
            await request.approve()
            if max_tokens <= 14:
                with pytest.raises(NativeError) as stopped:
                    await restored.restored_turn.get_result()
                assert stopped.value.code == "run_budget_exceeded"
            else:
                result = await restored.restored_turn.get_result()
                assert result.final_output == "Done"
                assert result.run_usage.total_tokens == 28
    assert effects == ["approved"]
    assert len(responses.requests) == (1 if max_tokens <= 14 else 2)


@pytest.mark.parametrize("missing_usage", ["stop", "continue"])
async def test_token_budget_one_shot_preserves_missing_usage(responses, missing_usage):
    async def omit_usage(event):
        if event["type"] == "response.completed":
            event["response"].pop("usage", None)

    responses.before_event = omit_usage
    lookup = lookup_tool()
    responses.respond(function_call("lookup", {"key": "A"}, "pending"))
    responses.respond(assistant_output("Done"))
    agent = Agent(instructions="Look up and answer.", provider=provider(responses), tools=[lookup])
    limits = RunBudget(max_tokens=100, missing_usage=missing_usage)
    async with timeout(20):
        with pytest.raises(InteractionRequired) as pending:
            await agent.run("begin", budget=limits)
        saved = pending.value.state
        assert saved.run_budget.usage_missing is True
        assert saved.run_budget.consumed_tokens == 0
        restored = agent.run(
            None,
            state=saved,
            budget=limits,
            on_interaction=lambda event: lookup.match(event).reply("result"),
        )
        if missing_usage == "stop":
            with pytest.raises(NativeError) as stopped:
                await restored
            assert stopped.value.code == "run_budget_usage_unavailable"
        else:
            assert (await restored).final_output == "Done"
    assert len(responses.requests) == (1 if missing_usage == "stop" else 2)


@pytest.mark.parametrize(
    "fields",
    [
        {"version": True},
        {"version": 2.0},
        {"version": 3},
        {"max_tokens": True},
        {"max_tokens": 2**53},
        {"consumed_tokens": 11},
        {"consumed_tokens": -1},
        {"consumed_tokens": 1.0},
        {"usage_missing": 1},
        {"missing_usage": "ignore"},
        {"admitted_requests": 1},
        {"max_requests": 2},
        {"max_requests": 2, "admitted_requests": 3},
        {"extra": 1},
    ],
)
def test_token_budget_checkpoint_rejects_malformed_receipts(fields):
    from _agents_codex.state import _TokenRunBudgetCheckpoint

    with pytest.raises(ValueError):
        _TokenRunBudgetCheckpoint.model_validate(
            {
                "version": 2,
                "max_requests": None,
                "admitted_requests": None,
                "max_tokens": 10,
                "consumed_tokens": 1,
                "usage_missing": False,
                "missing_usage": "stop",
                **fields,
            }
        )


@pytest.mark.parametrize("reader_failure", [False, True])
async def test_manual_capture_drains_usage_before_retiring_reader(
    responses, monkeypatch, reader_failure
):
    from agents import _native

    usage_waiting, release_usage, sealed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_next = _native.NativeThread._next_session_event
    original_seal = _native.NativeThread._seal_manual

    manual_seen = False
    pending_usage = None

    async def delayed_next(native):
        nonlocal manual_seen, pending_usage
        if manual_seen and pending_usage is not None:
            raw, pending_usage = pending_usage, None
        else:
            raw = await original_next(native)
        event = json.loads(raw)
        message = event.get("msg", {})
        if message.get("type") == "dynamic_tool_call_request":
            manual_seen = True
        if message.get("type") == "raw_response_completed":
            if not manual_seen:
                # The independent tool channel can publish its request before usage.
                # Deliver that request before holding the root reader for capture.
                pending_usage = raw
                return await delayed_next(native)
            usage_waiting.set()
            await release_usage.wait()
            if reader_failure:
                raise RuntimeError("Synthetic reader failure")
        return raw

    async def seal(native, *args, **kwargs):
        result = await original_seal(native, *args, **kwargs)
        sealed.set()
        return result

    monkeypatch.setattr(_native.NativeThread, "_next_session_event", delayed_next)
    monkeypatch.setattr(_native.NativeThread, "_seal_manual", seal)
    lookup = lookup_tool()
    responses.respond(function_call("lookup", {"key": "A"}, "pending"))
    agent = Agent(instructions="Look up and answer.", provider=provider(responses), tools=[lookup])
    limits = RunBudget(max_tokens=14)
    async with timeout(20):
        async with agent.session() as source:
            capturing = None
            try:
                await source.submit("begin", budget=limits)
                await next_request(source.interactions(), lookup)
                await usage_waiting.wait()
                capturing = asyncio.create_task(source.suspend())
                await sealed.wait()
                assert not capturing.done()
                release_usage.set()
                if reader_failure:
                    with pytest.raises(NativeError, match="did not drain"):
                        await capturing
                    return
                saved = await capturing
            finally:
                release_usage.set()
                if capturing is not None:
                    await asyncio.gather(capturing, return_exceptions=True)
        assert saved.run_budget.consumed_tokens == 14
        async with agent.session(state=saved, resume_budget=limits) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("result")
            with pytest.raises(NativeError) as stopped:
                await restored.restored_turn.get_result()
            assert stopped.value.code == "run_budget_exceeded"
    assert len(responses.requests) == 1
