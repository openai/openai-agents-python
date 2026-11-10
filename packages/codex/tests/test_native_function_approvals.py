"""Managed decisions execute current native callbacks, never application-supplied outputs."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, field_validator, model_validator

from agents import (
    Agent,
    Allow,
    ApprovalRequested,
    Deny,
    FunctionTool,
    InteractionExpiredError,
    ReplaceArguments,
    ToolContext,
    ToolOutput,
    TurnStoppedError,
    function_tool,
)
from agents.observation import ToolFinished, ToolStarted
from agents.voice import NativeSessionVoiceWorkflow
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import next_request, provider


class TransformedInput(BaseModel):
    value: str

    @field_validator("value")
    @classmethod
    def suffix(cls, value: str) -> str:
        return value + "!"


@pytest.mark.parametrize("replace_input", [False, True])
async def test_approval_preserves_raw_input_for_schema_transforms(responses, replace_input):
    effects = []

    def before(_context):
        return ReplaceArguments({"item": {"value": "replacement"}})

    @function_tool(
        needs_approval=True,
        before=before if replace_input else None,
        run_before_approval=replace_input,
    )
    def risky(item: TransformedInput) -> str:
        effects.append(item.value)
        return item.value

    responses.respond(function_call("risky", {"item": {"value": "original"}}, "transform"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use risky.", tools=[risky], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), risky)
        expected = "replacement!" if replace_input else "original!"
        assert request.effective_arguments.item.value == expected
        await request.approve()
        assert (await turn.get_result()).final_output == "done"
        assert effects == [expected]
        assert tool_output(responses.requests[1], "transform") == expected


@pytest.mark.parametrize("approve", [True, False])
async def test_managed_decision_gates_callback_and_consumes_exact_grant(responses, approve):
    effects = []

    @function_tool(needs_approval=True)
    def risky(value: str) -> str:
        effects.append(value)
        return "accepted-" + value

    responses.respond(function_call("risky", {"value": "A"}, "approval-A"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use the tool.", tools=[risky], provider=provider(responses))
    async with timeout(20), agent.session() as session, agent.session() as foreign:
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), risky)
        alias = await next_request(session.interactions(), risky)
        assert isinstance(request, ApprovalRequested)
        assert request.call_id == "approval-A" and request.turn_id == turn.turn_id
        assert effects == [] and len(responses.requests) == 1
        assert not await foreign._native._approve_manual_tool(request._grant)
        if approve:
            await request.approve()
        else:
            await request.reject("explicit denial")
        with pytest.raises(InteractionExpiredError):
            await alias.approve()
        assert (await turn.get_result()).final_output == "done"
        assert effects == (["A"] if approve else [])
        assert tool_output(responses.requests[1], "approval-A") == (
            "accepted-A" if approve else "explicit denial"
        )


async def test_false_predicate_runs_automatically_without_interaction_handler(responses):
    calls = []

    async def policy(context):
        calls.append(("policy", context.effective_arguments["value"]))
        return False

    @function_tool(needs_approval=policy)
    def safe(value: str) -> str:
        calls.append(("body", value))
        return value

    responses.respond(function_call("safe", {"value": "A"}, "safe-A"))
    responses.respond(assistant_output("done"))
    async with timeout(20):
        result = await Agent(
            instructions="Use the tool.", tools=[safe], provider=provider(responses)
        ).run("begin")
    assert result.final_output == "done"
    assert calls == [("policy", "A"), ("body", "A")]


@pytest.mark.parametrize("run_before", [False, True])
async def test_input_policy_rechecks_and_exposes_detached_effective_arguments(
    responses, run_before
):
    calls = []

    def before(context):
        value = context.effective_arguments["value"]
        calls.append(("before", value))
        return ReplaceArguments({"value": value + 1})

    def predicate(context):
        calls.append(("predicate", context.effective_arguments["value"]))
        return True

    @function_tool(needs_approval=predicate, run_before_approval=run_before, before=before)
    def increment(context: ToolContext, value: int) -> str:
        calls.append(("body", value))
        assert context.original_arguments["value"] == 1
        return str(value)

    responses.respond(function_call("increment", {"value": 1}, "increment-A"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use the tool.", tools=[increment], provider=provider(responses))
    async with timeout(20), agent.session() as session:
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), increment)
        assert isinstance(request, ApprovalRequested)
        assert request.arguments.value == 1
        assert request.effective_arguments.value == (2 if run_before else 1)
        detached = request.effective_arguments
        detached.value = 99
        assert request.effective_arguments.value == (2 if run_before else 1)
        assert not any(kind == "body" for kind, _ in calls)
        await request.approve()
        assert (await turn.get_result()).final_output == "done"
    assert calls == (
        [("before", 1), ("predicate", 2), ("before", 2), ("body", 3)]
        if run_before
        else [("predicate", 1), ("before", 1), ("body", 2)]
    )


@pytest.mark.parametrize("approve", [True, False])
async def test_remembered_decision_is_session_and_binding_scoped(responses, approve):
    effects = []

    @function_tool(needs_approval=True)
    def risky(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(instructions="Use the tool.", tools=[risky], provider=provider(responses))
    async with timeout(25):
        async with agent.session() as session:
            for index in range(2):
                responses.respond(function_call("risky", {"value": str(index)}, f"cached-{index}"))
                responses.respond(assistant_output("done"))
                turn = await session.submit("begin")
                if index == 0:
                    request = await next_request(session.interactions(), risky)
                    if approve:
                        await request.approve(always=True)
                    else:
                        await request.reject(always=True)
                assert (await turn.get_result()).final_output == "done"
                assert await session.pending_interactions() == ()
        responses.respond(function_call("risky", {"value": "new"}, "new-session"))
        responses.respond(assistant_output("done"))
        async with agent.session() as fresh:
            turn = await fresh.submit("begin")
            request = await next_request(fresh.interactions(), risky)
            assert isinstance(request, ApprovalRequested)
            await request.reject()
            await turn.get_result()
    assert effects == (["0", "1"] if approve else [])


async def test_approved_callback_cancellation_joins_physical_cleanup(responses):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool(needs_approval=True)
    async def hold() -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
        return "unreachable"

    responses.respond(function_call("hold", {}, "held-approval"))
    agent = Agent(instructions="Use the tool.", tools=[hold], provider=provider(responses))
    async with timeout(20), agent.session() as session:
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), hold)
        await request.approve()
        await entered.wait()
        cancellation = asyncio.create_task(turn.cancel())
        try:
            await cleaning.wait()
            assert not cancellation.done()
        finally:
            release.set()
        await cancellation
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        with pytest.raises(InteractionExpiredError):
            await request.approve()


async def test_preapproval_denial_skips_predicate_body_and_after(responses):
    calls = []

    def before(context):
        calls.append("before")
        return Deny("input denied")

    def predicate(context):
        calls.append("predicate")
        return True

    def after(context):
        calls.append("after")
        return Allow()

    @function_tool(needs_approval=predicate, run_before_approval=True, before=before, after=after)
    def risky(value: str) -> str:
        calls.append("body")
        return value

    responses.respond(function_call("risky", {"value": "A"}, "denied-input"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use the tool.", tools=[risky], provider=provider(responses)
        ).session() as session,
    ):
        assert (await (await session.submit("begin")).get_result()).final_output == "done"
        assert await session.pending_interactions() == ()
    assert calls == ["before"]


@pytest.mark.parametrize("approve", [True, False])
@pytest.mark.parametrize("guarded", [True, False])
async def test_fresh_process_restore_keeps_required_decision_and_accepted_sibling(
    responses,
    tmp_path,
    approve,
    guarded,
):
    effects_path = tmp_path / "effects.txt"
    effects_path.write_text("")

    def before(context):
        return ReplaceArguments({"value": context.effective_arguments["value"] + "-pre"})

    @function_tool(
        name="risky",
        description="Synthetic effect",
        needs_approval=True,
        before=before if guarded else None,
        run_before_approval=guarded,
    )
    def risky(value: str) -> str:
        with effects_path.open("a") as out:
            out.write("risky\n")
        return "accepted-" + value

    @function_tool(name="accepted", description="Accepted sibling")
    def accepted(value: str) -> str:
        with effects_path.open("a") as out:
            out.write("accepted\n")
        return "sibling-" + value

    responses.respond(
        function_call("accepted", {"value": "A"}, "accepted-sibling"),
        function_call("risky", {"value": "B"}, "pending-approval"),
    )
    agent = Agent(
        instructions="Use both tools.", tools=[accepted, risky], provider=provider(responses)
    )
    async with timeout(40):
        async with agent.session() as source:
            turn = await source.submit("begin")
            old = await next_request(source.interactions(), risky)
            saved = await source.suspend()
            assert effects_path.read_text().splitlines() == ["accepted"]
            with pytest.raises(InteractionExpiredError):
                await old.approve()
            state_path = tmp_path / "state.json"
            state_path.write_text(saved.to_json())
            turn_id = turn.turn_id
        responses.respond(assistant_output("resumed"))
        script = Path(__file__).with_name("fixtures") / "managed_approval_restore.py"
        root = Path(__file__).resolve().parents[3]
        env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join([str(root / "src"), str(root / "packages/codex/src")]),
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(script),
            str(state_path),
            str(effects_path),
            responses.url,
            "approve" if approve else "reject",
            turn_id,
            "guarded" if guarded else "plain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=root,
            env=env,
        )
        stdout, stderr = await child.communicate()
        assert child.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        assert result["effects"] == (["accepted", "risky"] if approve else ["accepted"])
        assert tool_output(responses.requests[1], "accepted-sibling") == "sibling-A"
        assert tool_output(responses.requests[1], "pending-approval") == (
            ("accepted-B-pre-post" if guarded else "accepted-B")
            if approve
            else "restored rejection"
        )
        assert len(responses.requests) == 2


async def test_one_shot_exports_required_approval_before_callback_effects(responses):
    from agents import InteractionRequired

    effects = []

    @function_tool(needs_approval=True)
    def risky(value: str) -> str:
        effects.append(value)
        return value

    responses.respond(function_call("risky", {"value": "A"}, "one-shot-approval"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use the tool.", tools=[risky], provider=provider(responses))
    async with timeout(25):
        with pytest.raises(InteractionRequired) as interrupted:
            await agent.run("begin")
        assert interrupted.value.state is not None and effects == []
        async with agent.session(state=interrupted.value.state) as restored:
            request = await next_request(restored.interactions(), risky)
            assert isinstance(request, ApprovalRequested)
            await request.approve()
            turn = restored.restored_turn
            assert turn is not None
            assert (await turn.get_result()).final_output == "done"
    assert effects == ["A"]


async def test_remembered_approval_does_not_follow_another_agents_callback(responses):
    effects = []

    def declaration(label):
        @function_tool(name="risky", description="Same declaration schema", needs_approval=True)
        def risky(value: str) -> str:
            effects.append(label)
            return value

        return risky

    first, second = declaration("old"), declaration("new")
    options = {"instructions": "Use the tool.", "provider": provider(responses)}
    other = Agent(name="other", tools=[second], **options)
    responses.respond(function_call("risky", {"value": "A"}, "old-binding"))
    responses.respond(assistant_output("done"))
    responses.respond(function_call("risky", {"value": "B"}, "new-binding"))
    responses.respond(assistant_output("done"))
    async with timeout(25), Agent(tools=[first], handoffs=[other], **options).session() as session:
        turn = await session.submit("first")
        request = await next_request(session.interactions(), first)
        await request.approve(always=True)
        await turn.get_result()
        await session.update(agent=other)
        turn = await session.submit("second")
        request = await next_request(session.interactions(), second)
        assert isinstance(request, ApprovalRequested) and effects == ["old"]
        await request.approve()
        await turn.get_result()
    assert effects == ["old", "new"]


async def test_approval_preserves_terminal_callback_without_an_extra_model_step(responses):
    @function_tool(needs_approval=True, finish_turn=True)
    def finish(value: str) -> str:
        return "finished-" + value

    responses.respond(function_call("finish", {"value": "A"}, "terminal-approval"))
    async with (
        timeout(20),
        Agent(
            instructions="Use the tool.", tools=[finish], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), finish)
        await request.approve()
        assert (await turn.get_result()).final_output == "finished-A"
    assert len(responses.requests) == 1


@pytest.mark.parametrize("mode", ["approved", "automatic", "rejected"])
async def test_managed_execution_observations_are_balanced(responses, mode):
    effects = []

    @function_tool(needs_approval=(lambda _context: False) if mode == "automatic" else True)
    def risky(value: str) -> str:
        effects.append(value)
        return value

    responses.respond(function_call("risky", {"value": "A"}, "observed-approval"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use risky.", tools=[risky], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("begin")
        if mode != "automatic":
            request = await next_request(session.interactions(), risky)
            assert effects == []
            if mode == "approved":
                await request.approve()
            else:
                await request.reject("denied")
        assert (await turn.get_result()).final_output == "done"
        facts = [
            type(event)
            async for event in turn.events()
            if isinstance(event, (ToolStarted, ToolFinished))
            and event.call_id == "observed-approval"
        ]
        # Python observes callback execution, so rejection has no execution facts.
        assert facts == ([] if mode == "rejected" else [ToolStarted, ToolFinished])
        assert effects == ([] if mode == "rejected" else ["A"])


@pytest.mark.parametrize("explicit_tools", [False, True])
async def test_voice_workflow_managed_approval_tool_ceiling(responses, explicit_tools):
    effects = []

    @function_tool(needs_approval=True)
    def risky(value: str) -> str:
        effects.append(value)
        return value

    responses.respond(function_call("risky", {"value": "A"}, "voice-workflow"))
    responses.respond(assistant_output("spoken output"))
    async with (
        timeout(20),
        Agent(
            instructions="Use risky.", tools=[risky], provider=provider(responses)
        ).session() as session,
    ):
        workflow = NativeSessionVoiceWorkflow(session, tools=(risky,) if explicit_tools else None)

        async def speak():
            return [text async for text in workflow.run("begin")]

        output = asyncio.create_task(speak())
        try:
            request = await next_request(session.interactions(), risky)
            assert effects == []
            await request.approve()
            assert await output == ["spoken output"]
            assert effects == ["A"]
        finally:
            output.cancel()
            await asyncio.gather(output, return_exceptions=True)


@pytest.mark.parametrize("phase", ["predicate", "before"])
async def test_managed_preparation_rejects_callback_self_close(responses, phase):
    prevented = []

    async def check_close(_context):
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="close its own Session"):
            await session.aclose()
        prevented.append(phase)

    async def predicate(context):
        await check_close(context)
        return False

    async def before(context):
        await check_close(context)
        return Allow()

    @function_tool(
        needs_approval=predicate if phase == "predicate" else (lambda _: False),
        before=before if phase == "before" else None,
        run_before_approval=phase == "before",
    )
    def risky(value: str) -> str:
        return value

    responses.respond(function_call("risky", {"value": "A"}, "self-close"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use risky.", tools=[risky], provider=provider(responses)
        ).session() as session,
    ):
        assert (await (await session.submit("begin")).get_result()).final_output == "done"
        assert prevented == ([phase, phase] if phase == "before" else [phase])


class AdmissionInput(BaseModel):
    value: int
    validated: ClassVar[list[int]] = []

    @field_validator("value")
    @classmethod
    def record(cls, value):
        cls.validated.append(value)
        return value


@pytest.mark.parametrize("limit", [1, 2])
async def test_managed_preparation_shares_function_admission_and_releases_before_approval(
    responses, limit
):
    from tests.test_native_function_concurrency import queued

    AdmissionInput.validated = []
    before, policies, effects = [], [], []
    entered, release, all_prepared = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def guard(context):
        before.append(context.original_arguments["payload"]["value"])
        if len(before) == limit:
            entered.set()
        await release.wait()
        return Allow()

    def predicate(context):
        policies.append(context.effective_arguments["payload"].value)
        if len(policies) == 3:
            all_prepared.set()
        return True

    @function_tool(needs_approval=predicate, before=guard, run_before_approval=True)
    def risky(payload: AdmissionInput) -> str:
        effects.append(payload.value)
        return str(payload.value)

    responses.respond(
        *[function_call("risky", {"payload": {"value": i}}, f"admission-{i}") for i in range(3)]
    )
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(instructions="Use risky.", tools=[risky], provider=provider(responses)).session(
            max_function_concurrency=limit
        ) as session,
    ):
        turn = await session.submit("begin")
        await entered.wait()
        await queued(session, 3)
        assert len(before) == limit
        assert AdmissionInput.validated == before
        assert policies == [] and effects == []
        release.set()
        await all_prepared.wait()
        assert sorted(policies) == [0, 1, 2] and effects == []
        interactions = session.interactions()
        for _ in range(3):
            request = await next_request(interactions, risky)
            await request.approve()
        assert (await turn.get_result()).final_output == "done"
        assert sorted(effects) == [0, 1, 2]


class MutatingApprovalInput(BaseModel):
    value: int

    @model_validator(mode="before")
    @classmethod
    def increment(cls, data):
        data["value"] += 1
        return data


@pytest.mark.parametrize("automatic", [False, True])
async def test_raw_preparation_input_survives_mutating_validators(responses, automatic):
    originals, effects = [], []

    def policy(context):
        originals.append(context.original_arguments["payload"]["value"])
        return not automatic

    @function_tool(needs_approval=policy)
    def risky(context: ToolContext, payload: MutatingApprovalInput) -> str:
        originals.append(context.original_arguments["payload"]["value"])
        effects.append(payload.value)
        return str(payload.value)

    responses.respond(function_call("risky", {"payload": {"value": 1}}, "mutating-input"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use risky.", tools=[risky], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("begin")
        if not automatic:
            request = await next_request(session.interactions(), risky)
            assert request.effective_arguments.payload.value == 2
            await request.approve()
        assert (await turn.get_result()).final_output == "done"
        assert effects == [2]
        assert originals == [1, 1]
        assert tool_output(responses.requests[1], "mutating-input") == "2"


@pytest.mark.parametrize("delivery", ["respond", "handler"])
@pytest.mark.parametrize("structured", [False, True])
async def test_supported_decision_replies_reach_managed_callback_owner(
    responses, delivery, structured
):
    from agents import ApprovalDecision

    effects = []

    @function_tool(needs_approval=True)
    def risky(value: str) -> str:
        effects.append(value)
        return value

    responses.respond(function_call("risky", {"value": "A"}, "decision-reply"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use risky.", tools=[risky], provider=provider(responses))
    decision = ApprovalDecision(False, reason="explicit denial") if structured else True
    async with timeout(20):
        if delivery == "handler":

            def handler(event):
                request = risky.match(event)
                assert isinstance(request, ApprovalRequested)
                return request.reply(decision)

            assert (await agent.run("begin", on_interaction=handler)).final_output == "done"
        else:
            async with agent.session() as session:
                turn = await session.submit("begin")
                request = await next_request(session.interactions(), risky)
                await session.respond(request.reply(decision))
                assert (await turn.get_result()).final_output == "done"
    assert effects == ([] if structured else ["A"])


async def test_destination_only_approval_exports_portable_one_shot_state(responses):
    from agents import InteractionRequired

    effects = []

    @function_tool(needs_approval=True)
    def risky(value: str) -> str:
        effects.append(value)
        return value

    target = Agent(name="B", instructions="Use risky.", tools=[risky])
    agent = Agent(
        name="A", instructions="Transfer.", handoffs=[target], provider=provider(responses)
    )
    responses.respond(function_call("transfer_to_b", {}, "transfer"))
    responses.respond(function_call("risky", {"value": "A"}, "destination-approval"))
    responses.respond(assistant_output("done"))
    async with timeout(25):
        with pytest.raises(InteractionRequired) as interrupted:
            await agent.run("begin")
        assert interrupted.value.state is not None and effects == []
        async with agent.session(state=interrupted.value.state) as restored:
            request = await next_request(restored.interactions(), risky)
            await request.approve()
            assert (await restored.restored_turn.get_result()).final_output == "done"
        assert effects == ["A"]


@pytest.mark.parametrize("automatic", [False, True])
async def test_datetime_approval_inputs_keep_parsed_callback_semantics(responses, automatic):
    effects = []

    @function_tool(needs_approval=(lambda _: False) if automatic else True)
    def risky(when: datetime) -> str:
        effects.append(when)
        return when.isoformat()

    raw = "2026-11-01T12:00:00+00:00"
    responses.respond(function_call("risky", {"when": raw}, "datetime-input"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use risky.", tools=[risky], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("begin")
        if not automatic:
            request = await next_request(session.interactions(), risky)
            assert request.effective_arguments.when == datetime.fromisoformat(raw)
            await request.approve()
        assert (await turn.get_result()).final_output == "done"
        assert effects == [datetime.fromisoformat(raw)]


@pytest.mark.parametrize("restored", [False, True])
async def test_approved_terminal_function_finishes_without_another_sample(responses, restored):
    effects = []

    @function_tool(needs_approval=True, finish_turn=True)
    def risky(value: str) -> str:
        effects.append(value)
        return "terminal result"

    responses.respond(function_call("risky", {"value": "A"}, "terminal-approval"))
    responses.respond(assistant_output("unexpected model continuation"))
    agent = Agent(instructions="Use risky.", tools=[risky], provider=provider(responses))
    async with timeout(25):
        if restored:
            async with agent.session() as source:
                await source.submit("begin")
                await next_request(source.interactions(), risky)
                saved = await source.suspend()
            async with agent.session(state=saved) as session:
                request = await next_request(session.interactions(), risky)
                await request.approve()
                result = await session.restored_turn.get_result()
        else:
            async with agent.session() as session:
                turn = await session.submit("begin")
                request = await next_request(session.interactions(), risky)
                await request.approve()
                result = await turn.get_result()
        assert result.final_output == "terminal result"
        assert result.tool_call.call_id == "terminal-approval"
        assert effects == ["A"]
        assert len(responses.requests) == 1


@pytest.mark.parametrize("restored", [False, True])
async def test_approved_terminal_waits_for_pending_manual_sibling(responses, restored):
    effects = []

    @function_tool(needs_approval=True, finish_turn=True)
    def risky() -> str:
        effects.append("effect")
        return "terminal result"

    class ReviewInput(BaseModel):
        pass

    review = FunctionTool(
        name="review", description="Review.", input_type=ReviewInput, output_type=str
    )
    responses.respond(
        function_call("risky", {}, "approved-terminal"),
        function_call("review", {}, "manual-sibling"),
    )
    responses.respond(assistant_output("unexpected model continuation"))
    agent = Agent(instructions="Use both.", tools=[risky, review], provider=provider(responses))
    state = None
    async with timeout(25):
        if restored:
            async with agent.session() as source:
                await source.submit("begin")
                await next_request(source.interactions(), risky)
                await next_request(source.interactions(), review)
                state = await source.suspend()
        async with agent.session(state=state) as session:
            turn = session.restored_turn if restored else await session.submit("begin")
            approval = await next_request(session.interactions(), risky)
            manual = await next_request(session.interactions(), review)
            await approval.approve()
            await manual.respond("accepted manual sibling")
            result = await turn.get_result()
            assert result.final_output == "terminal result"
            assert result.tool_call.call_id == "approved-terminal"
            assert effects == ["effect"]
            assert len(responses.requests) == 1


@pytest.mark.parametrize("automatic", [False, True])
async def test_approval_preserves_rich_callback_result(responses, automatic):
    effects = []

    @function_tool(needs_approval=(lambda context: False) if automatic else True)
    def risky() -> ToolOutput | str:
        effects.append("effect")
        return ToolOutput.text("validated result")

    responses.respond(function_call("risky", {}, "rich-result"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(25),
        Agent(
            instructions="Use risky.", tools=[risky], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("begin")
        if not automatic:
            await (await next_request(session.interactions(), risky)).approve()
        assert (await turn.get_result()).final_output == "done"
        assert effects == ["effect"]
        assert "validated result" in json.dumps(responses.requests[1])
