"""Managed decisions execute current native callbacks, never application-supplied outputs."""

from __future__ import annotations

import asyncio
import importlib
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
    RunBudget,
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


class SingleUseInput(BaseModel):
    available_tokens: ClassVar[set[str]] = set()
    token: str

    @field_validator("token")
    @classmethod
    def consume_token(cls, value: str) -> str:
        if value not in cls.available_tokens:
            raise ValueError("Input token has already been consumed")
        cls.available_tokens.remove(value)
        return value


@pytest.mark.parametrize("automatic", [False, True])
async def test_approved_arguments_do_not_repeat_stateful_validation(responses, automatic):
    SingleUseInput.available_tokens = {"one-use"}
    effects = []

    @function_tool(needs_approval=(lambda _: False) if automatic else True)
    def consume(item: SingleUseInput) -> str:
        effects.append(item.token)
        return item.token

    responses.respond(function_call("consume", {"item": {"token": "one-use"}}, "consume"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use consume.", tools=[consume], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("begin")
        if not automatic:
            request = await next_request(session.interactions(), consume)
            assert request.effective_arguments.item.token == "one-use"
            assert SingleUseInput.available_tokens == set()
            assert effects == []
            await request.approve()
        assert (await turn.get_result()).final_output == "done"
    assert effects == ["one-use"]
    assert SingleUseInput.available_tokens == set()
    assert tool_output(responses.requests[1], "consume") == "one-use"


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
        [("before", 1), ("predicate", 2), ("before", 2)]
        if run_before
        else [("predicate", 1), ("before", 1)]
    )
    assert (
        tool_output(responses.requests[1], "increment-A")
        == "Tool input policy changed approved arguments"
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


@pytest.mark.parametrize("verdict", [None, False, 1])
async def test_unverified_remembered_policy_never_authorizes(responses, verdict):
    effects = []

    @function_tool(needs_approval=True, approval_key="fixture.risky.v1")
    def risky(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(instructions="Use the tool.", tools=[risky], provider=provider(responses))
    async with timeout(25):
        async with agent.session() as source:
            responses.respond(function_call("risky", {"value": "first"}, "unverified-first"))
            responses.respond(assistant_output("done"))
            turn = await source.submit("begin")
            await (await next_request(source.interactions(), risky)).reject(always=True)
            await turn.get_result()
            saved = await source.save_state()
        payload = json.loads(saved.to_json())
        payload["remembered_approvals"][0]["approve"] = True
        forged = type(saved).from_json(json.dumps(payload))
        options = {} if verdict is None else {"verify_remembered_approvals": lambda _: verdict}
        responses.respond(function_call("risky", {"value": "later"}, "unverified-later"))
        responses.respond(assistant_output("restored"))
        async with agent.session(state=forged, **options) as restored:
            turn = await restored.submit("continue")
            result = asyncio.create_task(turn.get_result())
            approval = asyncio.create_task(next_request(restored.interactions(), risky))
            try:
                done, _ = await asyncio.wait(
                    {result, approval}, return_when=asyncio.FIRST_COMPLETED, timeout=10
                )
                assert approval in done, "Unverified policy bypassed approval"
                assert effects == []
                await approval.result().reject()
                await result
                assert (await restored.save_state()).remembered_approvals == []
            finally:
                for task in (approval, result):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(approval, result, return_exceptions=True)
    assert effects == []


@pytest.mark.parametrize("one_shot", [False, True])
async def test_remembered_verifier_receives_detached_checkpoint(responses, one_shot):
    effects = []
    seen = []

    @function_tool(needs_approval=True, approval_key="fixture.risky.v1")
    def risky(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(instructions="Use the tool.", tools=[risky], provider=provider(responses))
    async with timeout(25):
        async with agent.session() as source:
            responses.respond(function_call("risky", {"value": "first"}, "detached-first"))
            responses.respond(assistant_output("done"))
            turn = await source.submit("begin")
            await (await next_request(source.interactions(), risky)).reject(always=True)
            await turn.get_result()
            saved = await source.save_state()
        trusted = saved.to_json()

        async def verify(checkpoint):
            await asyncio.sleep(0)
            seen.append(checkpoint.to_json() == trusted)
            checkpoint.remembered_approvals[0] = checkpoint.remembered_approvals[0].model_copy(
                update={"approve": True}
            )
            return True

        responses.respond(function_call("risky", {"value": "later"}, "detached-later"))
        responses.respond(assistant_output("restored"))
        if one_shot:
            result = await agent.run(
                "continue", state=saved, verify_remembered_approvals=verify, return_state=True
            )
            assert result.final_output == "restored"
            exported = result.state
            assert exported is not None
        else:
            async with agent.session(state=saved, verify_remembered_approvals=verify) as restored:
                turn = await restored.submit("continue")
                assert (await turn.get_result()).final_output == "restored"
                assert await restored.pending_interactions() == ()
                exported = await restored.save_state()
        assert exported.remembered_approvals[0].approve is False
        assert saved.to_json() == trusted
    assert seen == [True]
    assert effects == []


@pytest.mark.parametrize("approve,substitute", [(True, False), (False, False), (False, True)])
async def test_deferred_start_keeps_only_internal_remembered_policy(responses, approve, substitute):
    from contextlib import asynccontextmanager

    from tests.test_deferred_jobs import Journal, Provider, declared, done

    effects = []

    @function_tool(needs_approval=True, approval_key="fixture.risky.v1")
    def risky(value: str) -> str:
        effects.append(value)
        return value

    jobs, manual = declared(Journal(), Provider([done()]))
    agent = Agent(instructions="Use the tool.", tools=[risky, manual], provider=provider(responses))
    async with timeout(25), agent.session() as source:
        responses.respond(function_call("risky", {"value": "first"}, "deferred-first"))
        responses.respond(assistant_output("done"))
        turn = await source.submit("begin")
        request = await next_request(source.interactions(), risky)
        if approve:
            await request.approve(always=True)
        else:
            await request.reject(always=True)
        await turn.get_result()
        responses.respond(function_call("render", {"pages": 1}, "deferred-job"))
        responses.respond(assistant_output("prepared"))
        await source.submit("render")
        pending = await next_request(source.interactions(), manual)
        original = agent.session
        if substitute:

            @asynccontextmanager
            async def replaced(**options):
                saved = options["state"]
                payload = json.loads(saved.to_json())
                payload["remembered_approvals"][0]["approve"] = True
                options["state"] = type(saved).from_json(json.dumps(payload))
                async with original(**options) as restored:
                    yield restored

            agent.session = replaced
        if substitute:
            with pytest.raises(PermissionError):
                async with jobs.start(source, pending):
                    pytest.fail("Substituted deferred checkpoint entered the live body")
            assert effects == []
            return
        async with jobs.start(source, pending) as (restored, resumed):
            assert (await resumed.get_result()).final_output == "prepared"
            responses.respond(function_call("risky", {"value": "later"}, "deferred-later"))
            responses.respond(assistant_output("restored"))
            turn = await restored.submit("continue")
            result = asyncio.create_task(turn.get_result())
            approval = asyncio.create_task(next_request(restored.interactions(), risky))
            try:
                completed, _ = await asyncio.wait(
                    {result, approval}, return_when=asyncio.FIRST_COMPLETED, timeout=10
                )
                assert result in completed, "Internal deferred policy requested approval again"
                assert result.result().final_output == "restored"
            finally:
                for task in (approval, result):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(approval, result, return_exceptions=True)
    assert effects == (["first", "later"] if approve else [])


@pytest.mark.parametrize("kind", ["mcp", "profile", "replacement"])
@pytest.mark.parametrize("approve", [True, False])
async def test_remembered_policy_follows_confirmed_catalog(responses, kind, approve):
    from contextlib import AsyncExitStack

    from agents import HttpMCP
    from tests.test_native_mcp_tools import Peer

    effects = []

    def binding(key):
        @function_tool(needs_approval=True, approval_key=key)
        def risky(value: str) -> str:
            effects.append(value)
            return value

        return risky

    async with timeout(30), AsyncExitStack() as stack:
        extra = []
        if kind == "mcp":
            url = await stack.enter_async_context(Peer().serve())
            extra.append(HttpMCP(name="docs", url=url, tools=["search"]))
        native_provider = provider(responses)

        def declaration(tool):
            return Agent(
                name="A",
                instructions="Use the tool.",
                tools=[tool, *extra],
                handoffs=[Agent(name="B", instructions="Delegate.")] if kind == "profile" else (),
                provider=native_provider,
            )

        original = binding("fixture.risky.v1")
        current = binding("fixture.risky.updated") if kind == "profile" else original
        agent = declaration(original)
        async with agent.session() as source:
            if kind == "profile":
                await source.update(tools=[current])
            responses.respond(function_call("risky", {"value": "first"}, "catalog-first"))
            responses.respond(assistant_output("done"))
            turn = await source.submit("begin")
            request = await next_request(source.interactions(), current)
            if approve:
                await request.approve(always=True)
            else:
                await request.reject(always=True)
            await turn.get_result()
            saved = await source.save_state()
            assert len(saved.remembered_approvals) == 1, "Effective catalog lost remembered policy"
            assert saved.remembered_approvals[0].key == current.approval_key
        responses.respond(function_call("risky", {"value": "later"}, "catalog-later"))
        responses.respond(assistant_output("restored"))
        async with declaration(current).session(
            state=saved, verify_remembered_approvals=lambda _: True
        ) as restored:
            if kind == "replacement":
                current = binding("fixture.risky.v1")
                await restored.update(tools=[current])
            turn = await restored.submit("continue")
            result = asyncio.create_task(turn.get_result())
            approval = asyncio.create_task(next_request(restored.interactions(), current))
            try:
                completed, _ = await asyncio.wait(
                    {result, approval}, return_when=asyncio.FIRST_COMPLETED, timeout=10
                )
                if kind == "replacement":
                    assert approval in completed, "A replacement inherited the imported slot"
                    await approval.result().reject()
                    await result
                else:
                    assert result in completed, "Confirmed catalog lost remembered policy"
                    assert result.result().final_output == "restored"
            finally:
                for task in (approval, result):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(approval, result, return_exceptions=True)
    assert effects == (
        (["first"] if kind == "replacement" else ["first", "later"]) if approve else []
    )


@pytest.mark.parametrize("approve", [True, False])
async def test_portable_state_keeps_remembered_future_decision(responses, approve):
    effects = []

    def binding():
        @function_tool(needs_approval=True, approval_key="fixture.risky.v1")
        def risky(value: str) -> str:
            effects.append(value)
            return value

        return risky

    risky = binding()
    agent = Agent(instructions="Use the tool.", tools=[risky], provider=provider(responses))
    async with timeout(25):
        async with agent.session() as source:
            responses.respond(function_call("risky", {"value": "first"}, "remember-first"))
            responses.respond(assistant_output("done"))
            turn = await source.submit("begin")
            request = await next_request(source.interactions(), risky)
            assert effects == []
            if approve:
                await request.approve(always=True)
            else:
                await request.reject(always=True)
            await turn.get_result()
            saved = await source.save_state()
            portable = type(saved).from_json(saved.to_json())

        responses.respond(function_call("risky", {"value": "later"}, "remember-later"))
        responses.respond(assistant_output("restored"))
        rebound = binding()
        current = Agent(instructions="Use the tool.", tools=[rebound], provider=agent.provider)
        assert rebound is not risky
        async with current.session(
            state=portable, verify_remembered_approvals=lambda _: True
        ) as restored:
            turn = await restored.submit("continue")
            result = asyncio.create_task(turn.get_result())
            approval = asyncio.create_task(next_request(restored.interactions(), rebound))
            try:
                done, _ = await asyncio.wait(
                    {result, approval}, return_when=asyncio.FIRST_COMPLETED, timeout=10
                )
                assert result in done, "Portable remembered decision requested approval again"
                assert result.result().final_output == "restored"
                assert await restored.pending_interactions() == ()
            finally:
                approval.cancel()
                await asyncio.gather(approval, return_exceptions=True)
                if not result.done():
                    result.cancel()
                    await asyncio.gather(result, return_exceptions=True)
    assert effects == (["first", "later"] if approve else [])
    assert "fixture.risky.v1" not in json.dumps(responses.requests)


@pytest.mark.parametrize("approve", [True, False])
async def test_remembered_future_decision_rebinds_in_fresh_process(responses, tmp_path, approve):
    effects = tmp_path / "effects.txt"
    effects.write_text("")

    @function_tool(needs_approval=True, approval_key="fixture.risky.v1")
    def risky(value: str) -> str:
        with effects.open("a") as stream:
            stream.write(value + "\n")
        return value

    agent = Agent(
        instructions="Use the tool.", model="fixture", tools=[risky], provider=provider(responses)
    )
    async with timeout(30):
        responses.respond(function_call("risky", {"value": "first"}, "process-first"))
        responses.respond(assistant_output("done"))
        async with agent.session() as source:
            turn = await source.submit("begin")
            request = await next_request(source.interactions(), risky)
            assert effects.read_text() == ""
            if approve:
                await request.approve(always=True)
            else:
                await request.reject(always=True)
            await turn.get_result()
            saved = await source.save_state()
        state_path = tmp_path / "state.json"
        state_path.write_text(saved.to_json())
        responses.respond(function_call("risky", {"value": "later"}, "process-later"))
        responses.respond(assistant_output("restored"))
        root = Path(__file__).resolve().parents[3]
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(Path(__file__).with_name("fixtures") / "remembered_approval_restore.py"),
            str(state_path),
            str(effects),
            responses.url,
            importlib.import_module("agents._native").__file__,
            cwd=root,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    [str(root / "src"), str(root / "packages/codex/src")]
                ),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=20)
            assert child.returncode == 0, stderr.decode()
            assert json.loads(stdout)["effects"] == (["first", "later"] if approve else [])
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()


async def test_new_remembered_rejection_retires_imported_approval(responses):
    effects = []

    @function_tool(needs_approval=True, approval_key="fixture.risky.v1")
    def risky(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(instructions="Use the tool.", tools=[risky], provider=provider(responses))
    async with timeout(30):
        responses.respond(
            function_call("risky", {"value": "first"}, "decision-first"),
            function_call("risky", {"value": "pending"}, "decision-pending"),
        )
        async with agent.session() as source:
            await source.submit("begin")
            stream = source.interactions()
            first = await next_request(stream, risky)
            pending = await next_request(stream, risky)
            assert first.call_id != pending.call_id
            assert effects == []
            await first.approve(always=True)
            saved = await source.suspend()
        responses.respond(assistant_output("resumed"))
        async with agent.session(
            state=saved, verify_remembered_approvals=lambda _: True
        ) as restored:
            pending = await next_request(restored.interactions(), risky)
            await pending.reject(always=True)
            assert restored.restored_turn is not None
            await restored.restored_turn.get_result()
            responses.respond(function_call("risky", {"value": "later"}, "decision-later"))
            responses.respond(assistant_output("done"))
            turn = await restored.submit("continue")
            assert (await turn.get_result()).final_output == "done"
    assert len(effects) == 1
    assert "later" not in effects


@pytest.mark.parametrize("graph", [False, True], ids=["single", "graph"])
async def test_remembered_decision_uses_updated_effective_instructions(responses, graph):
    effects = []

    @function_tool(needs_approval=True, approval_key="fixture.risky.v1")
    def risky(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(
        instructions="Opening policy.",
        tools=[risky],
        handoffs=[Agent(name="delegate", instructions="Delegate policy.")] if graph else (),
        provider=provider(responses),
    )
    async with timeout(25):
        async with agent.session() as source:
            await source.update(instructions="Current policy.")
            responses.respond(function_call("risky", {"value": "first"}, "updated-first"))
            responses.respond(assistant_output("done"))
            turn = await source.submit("begin")
            await (await next_request(source.interactions(), risky)).approve(always=True)
            await turn.get_result()
            saved = await source.save_state()
        current = (
            agent
            if graph
            else Agent(instructions="Current policy.", tools=[risky], provider=agent.provider)
        )
        responses.respond(function_call("risky", {"value": "later"}, "updated-later"))
        responses.respond(assistant_output("done"))
        async with current.session(
            state=saved, verify_remembered_approvals=lambda _: True
        ) as restored:
            turn = await restored.submit("continue")
            result = asyncio.create_task(turn.get_result())
            approval = asyncio.create_task(next_request(restored.interactions(), risky))
            try:
                done, _ = await asyncio.wait(
                    {result, approval}, return_when=asyncio.FIRST_COMPLETED, timeout=10
                )
                assert result in done, "Effective instruction policy lost its remembered decision"
                await result
            finally:
                approval.cancel()
                await asyncio.gather(approval, return_exceptions=True)
                if not result.done():
                    result.cancel()
                    await asyncio.gather(result, return_exceptions=True)
    assert effects == ["first", "later"]


@pytest.mark.parametrize("update_at", ["live", "checkpoint", "restored"])
async def test_updated_graph_instructions_require_a_new_remembered_approval(responses, update_at):
    effects = []

    @function_tool(needs_approval=True, approval_key="fixture.risky.v1")
    def risky(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(
        name="root",
        instructions="Opening policy.",
        tools=[risky],
        handoffs=[Agent(name="delegate", instructions="Delegate policy.")],
        provider=provider(responses),
    )

    async def require_approval(session):
        responses.respond(function_call("risky", {"value": "later"}, "changed-policy-later"))
        responses.respond(assistant_output("done"))
        turn = await session.submit("continue")
        result = asyncio.create_task(turn.get_result())
        approval = asyncio.create_task(next_request(session.interactions(), risky))
        try:
            done, _ = await asyncio.wait(
                {result, approval}, return_when=asyncio.FIRST_COMPLETED, timeout=10
            )
            assert approval in done, "An earlier instruction policy bypassed approval"
            assert effects == ["first"]
            await approval.result().reject()
            await result
        finally:
            for task in (approval, result):
                if not task.done():
                    task.cancel()
            await asyncio.gather(approval, result, return_exceptions=True)

    async with timeout(25):
        async with agent.session() as source:
            responses.respond(function_call("risky", {"value": "first"}, "opening-policy-first"))
            responses.respond(assistant_output("done"))
            turn = await source.submit("begin")
            await (await next_request(source.interactions(), risky)).approve(always=True)
            await turn.get_result()
            before = await source.save_state()
            assert len(before.remembered_approvals) == 1

            if update_at == "restored":
                saved = before
            else:
                await source.update(instructions="Changed policy.")
                if update_at == "live":
                    await require_approval(source)
                    return
                saved = await source.save_state()
                assert saved.remembered_approvals == []
                # An older writer can still supply this decision in a verified checkpoint.
                saved = saved.model_copy(
                    update={"remembered_approvals": before.remembered_approvals}
                )

        async with agent.session(
            state=saved, verify_remembered_approvals=lambda _: True
        ) as restored:
            if update_at == "restored":
                await restored.update(instructions="Changed policy.")
            await require_approval(restored)


@pytest.mark.parametrize("change", ["key", "profile", "schema", "policy", "unkeyed"])
async def test_portable_remembered_approval_requires_current_authority(responses, change):
    effects = []

    def binding(key, *, altered_schema=False, predicate=False):
        @function_tool(
            name="risky",
            needs_approval=(lambda _: True) if predicate else True,
            approval_key=key,
        )
        def risky(value: str) -> str:
            effects.append(value)
            return value

        @function_tool(name="risky", needs_approval=True, approval_key=key)
        def different(value: int) -> str:
            effects.append(str(value))
            return str(value)

        return different if altered_schema else risky

    source_tool = binding("fixture.risky.v1")
    agent = Agent(instructions="Use the tool.", tools=[source_tool], provider=provider(responses))
    async with timeout(25):
        async with agent.session() as source:
            responses.respond(function_call("risky", {"value": "first"}, "original-policy"))
            responses.respond(assistant_output("done"))
            turn = await source.submit("begin")
            request = await next_request(source.interactions(), source_tool)
            await request.approve(always=True)
            await turn.get_result()
            saved = await source.save_state()
        rebound = binding(
            None if change == "unkeyed" else "other-key" if change == "key" else "fixture.risky.v1",
            altered_schema=change == "schema",
            predicate=change == "policy",
        )
        current = Agent(
            instructions="Use the tool.",
            tools=[rebound],
            provider=agent.provider,
            name="other-profile" if change == "profile" else None,
        )
        responses.respond(
            function_call(
                "risky", {"value": 2 if change == "schema" else "later"}, "different-authority"
            )
        )
        responses.respond(assistant_output("done"))
        try:
            async with current.session(
                state=saved, verify_remembered_approvals=lambda _: True
            ) as restored:
                turn = await restored.submit("continue")
                request = await next_request(restored.interactions(), rebound)
                assert effects == ["first"]
                await request.reject()
                await turn.get_result()
        except (ValueError, RuntimeError) as error:
            # Native declaration admission may reject the incompatible schema first.
            if change != "schema" or not any(
                word in str(error).lower() for word in ("schema", "declaration", "tools")
            ):
                raise
    assert effects == ["first"]


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


@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("approve", [True, False])
@pytest.mark.parametrize("guarded", [True, False])
async def test_fresh_process_restore_keeps_required_decision_and_accepted_sibling(
    responses,
    tmp_path,
    approve,
    guarded,
    budgeted,
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
            turn = await source.submit(
                "begin", budget=RunBudget(max_requests=2) if budgeted else None
            )
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
            *(["2"] if budgeted else []),
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
            ("accepted-B-pre" if guarded else "accepted-B") if approve else "restored rejection"
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


@pytest.mark.parametrize("restored", [False, True])
async def test_approved_terminal_retains_completed_search_sibling(responses, restored):
    effects = []

    @function_tool(needs_approval=True, finish_turn=True)
    def risky() -> str:
        effects.append("effect")
        return "terminal result"

    @function_tool(defer_loading=True)
    def spare() -> str:
        return "unused"

    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "tool_search_call",
                "call_id": "search-sibling",
                "execution": "client",
                "arguments": {"query": "spare", "limit": 5},
            },
        },
        function_call("risky", {}, "approved-terminal"),
    )
    responses.respond(assistant_output("unexpected model continuation"))
    agent = Agent(
        instructions="Use both.",
        tools=[risky, spare],
        provider=provider(responses),
        model="gpt-5.5",
        programmatic_tool_calling=False,
    )
    state = None
    async with timeout(25):
        if restored:
            async with agent.session() as source:
                await source.submit("begin")
                await next_request(source.interactions(), risky)
                # Approval publication can precede the native search sibling's
                # completion. Portable suspension requires its completed receipt.
                while True:
                    history = json.loads(await source._native.read_thread_history(None))
                    if any(
                        item["type"] == "response_item"
                        and item["payload"].get("type") == "tool_search_output"
                        and item["payload"].get("call_id") == "search-sibling"
                        and item["payload"].get("execution") == "client"
                        and item["payload"].get("status") == "completed"
                        for item in history
                    ):
                        break
                    await asyncio.sleep(0)
                state = await source.suspend()
        async with agent.session(state=state) as session:
            turn = session.restored_turn if restored else await session.submit("begin")
            approval = await next_request(session.interactions(), risky)
            await approval.approve()
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


async def test_computer_history_cannot_be_restored_as_managed_function_approval(responses):
    from agents import SessionState

    effects = []

    @function_tool(needs_approval=True)
    def risky() -> str:
        effects.append("function effect")
        return "done"

    responses.respond(function_call("risky", {}, "family-boundary"))
    agent = Agent(instructions="Use risky.", tools=[risky], provider=provider(responses))
    async with agent.session() as source:
        await source.submit("begin")
        await next_request(source.interactions(), risky)
        saved = await source.suspend()
    raw = json.loads(saved.to_json())
    original = next(
        item
        for item in raw["history"]
        if item["type"] == "response_item" and item["payload"].get("call_id") == "family-boundary"
    )
    original["payload"] = {
        "type": "computer_call",
        "call_id": "family-boundary",
        "actions": [{"type": "wait"}],
        "pending_safety_checks": [],
    }
    forged = SessionState.from_json(json.dumps(raw))
    with pytest.raises(
        (RuntimeError, ValueError), match="Manual continuation contains nonportable native work"
    ):
        async with agent.session(state=forged):
            pytest.fail("Computer history cannot grant a managed function callback")
    assert effects == []
    assert len(responses.requests) == 1
