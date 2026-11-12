"""Settled native manual state preserves receipts and rebinds live authority."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, model_validator

from agents import (
    Agent,
    Environment,
    FunctionTool,
    HttpMCP,
    InteractionExpiredError,
    InteractionRequired,
    NativeError,
    NativeProvider,
    Session,
    Tool,
    function_tool,
)
from agents.observation import ToolFinished
from agents.state import SessionState
from agents.tools import ToolOutput
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.native_planning_fixtures import legacy_checklist, legacy_goal
from tests.test_native_child_agents import agent_for
from tests.test_native_manual_profiles import save_at_manual_boundary
from tests.test_native_manual_tools import LookupArgs, next_request, provider
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_provider_websockets import peer


async def test_duplicate_manual_call_id_refuses_suspension_without_spending_live_grants(
    responses: ResponsesFixture,
) -> None:
    left = FunctionTool(
        name="left_lookup", description="Read left", input_type=LookupArgs, output_type=str
    )
    right = FunctionTool(
        name="right_lookup", description="Read right", input_type=LookupArgs, output_type=str
    )
    call_id = "manual-A"
    responses.respond(
        function_call(left.name, {"key": "A"}, call_id),
        function_call(right.name, {"key": "B"}, call_id),
    )
    responses.respond(assistant_output("both live replies accepted"))
    agent = Agent(
        instructions="Read both records.", tools=[left, right], provider=provider(responses)
    )
    async with timeout(15), agent.session() as session:
        original = await session.submit("Read both")
        # Independent cursors cannot consume the other declaration's request
        # first if Core schedules the calls in the opposite order.
        left_request = await next_request(session.interactions(), left)
        right_request = await next_request(session.interactions(), right)
        assert (left_request.arguments.key, right_request.arguments.key) == ("A", "B")
        assert (left_request.turn_id, right_request.turn_id) == (original.turn_id, original.turn_id)
        assert (left_request.call_id, right_request.call_id) == (call_id, call_id)
        assert left.match(right_request) is None
        assert right.match(left_request) is None
        with pytest.raises(ValueError, match="Only user messages"):
            await session.submit(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps("ambiguous wire answer"),
                }
            )
        with pytest.raises(RuntimeError):
            async with timeout(2):
                await session.suspend()
        with pytest.raises(ValueError, match="Only user messages"):
            await session.submit(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps("still ambiguous"),
                }
            )
        assert await left_request.respond("left answer") is None
        assert await right_request.respond("right answer") is None
        assert (await original.get_result()).final_output == "both live replies accepted"
        for spent in (left_request, right_request):
            with pytest.raises(InteractionExpiredError, match="expired|used|delivered|stale"):
                await spent.respond("already answered")
        assert len(responses.requests) == 2


@pytest.mark.parametrize(
    "provider_call_id",
    ["not-a-pending-call", "manual-A"],
    ids=["unknown_output_id", "pending_output_id"],
)
async def test_provider_receipt_fails_its_generation_and_fresh_manual_turn_remains_usable(
    responses: ResponsesFixture, provider_call_id: str
) -> None:
    left = FunctionTool(
        name="left_lookup", description="Read left", input_type=LookupArgs, output_type=str
    )
    right = FunctionTool(
        name="right_lookup", description="Read right", input_type=LookupArgs, output_type=str
    )
    receipt_text = "synthetic provider receipt result only"
    responses.respond(
        function_call(left.name, {"key": "bad-A"}, "manual-A"),
        function_call(right.name, {"key": "bad-B"}, "manual-B"),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call_output",
                "call_id": provider_call_id,
                "output": receipt_text,
            },
        },
    )
    responses.respond(
        function_call(left.name, {"key": "fresh-A"}, "fresh-A"),
        function_call(right.name, {"key": "fresh-B"}, "fresh-B"),
    )
    responses.respond(assistant_output("fresh live replies accepted"))
    agent = Agent(
        instructions="Read both records.", tools=[left, right], provider=provider(responses)
    )
    async with timeout(15), agent.session() as session:
        malformed = await session.submit("Read both")
        # Provider-generated receipts cannot complete an SDK tool. Stop before
        # Core records them; its next request cannot normalize an orphan receipt.
        with pytest.raises(
            NativeError, match="Native model response ended without complete supported output"
        ) as failed:
            async with timeout(3):
                await malformed.get_result()
        assert receipt_text not in str(failed.value)
        assert provider_call_id not in str(failed.value)
        assert len(responses.requests) == 1

        fresh = await session.submit("Start a fresh manual turn")
        assert fresh.turn_id != malformed.turn_id
        first = await next_request(session.interactions(), left)
        second = await next_request(session.interactions(), right)
        assert (first.arguments.key, second.arguments.key) == ("fresh-A", "fresh-B")
        assert (first.turn_id, second.turn_id) == (fresh.turn_id, fresh.turn_id)
        assert (first.call_id, second.call_id) == ("fresh-A", "fresh-B")
        assert left.match(second) is None
        assert right.match(first) is None
        assert await first.respond("first fresh answer") is None
        assert await second.respond("second fresh answer") is None
        assert (await fresh.get_result()).final_output == "fresh live replies accepted"
        assert len(responses.requests) == 3
        assert tool_output(responses.requests[2], "fresh-A") == "first fresh answer"
        assert tool_output(responses.requests[2], "fresh-B") == "second fresh answer"
        assert receipt_text not in json.dumps(responses.requests[1]["input"])
        assert receipt_text not in json.dumps(responses.requests[2]["input"])
        assert receipt_text not in (await session.save_state()).to_json()


@pytest.mark.parametrize(
    "provider_call_id",
    ["not-a-pending-call", "manual-A"],
    ids=["unknown_output_id", "pending_output_id"],
)
async def test_run_rejects_provider_receipt_and_joins_unanswered_handler_and_automatic_sibling(
    responses: ResponsesFixture, provider_call_id: str
) -> None:
    left = FunctionTool(
        name="left_lookup", description="Read left", input_type=LookupArgs, output_type=str
    )
    right = FunctionTool(
        name="right_lookup", description="Read right", input_type=LookupArgs, output_type=str
    )
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool(description="Wait for the original turn's declared SDK work.")
    async def slow_record() -> str:
        entered.set()
        try:
            await release.wait()
            return "automatic work was released"
        finally:
            cleaned.set()

    receipt_text = "synthetic receipt from malformed one-shot generation"
    responses.respond(
        function_call(left.name, {"key": "bad-A"}, "manual-A"),
        function_call(right.name, {"key": "bad-B"}, "manual-B"),
        function_call("slow_record", {}, "automatic-original"),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call_output",
                "call_id": provider_call_id,
                "output": receipt_text,
            },
        },
    )
    responses.respond(
        function_call(left.name, {"key": "fresh-A"}, "fresh-A"),
        function_call(right.name, {"key": "fresh-B"}, "fresh-B"),
    )
    responses.respond(assistant_output("fresh manual turn complete"))

    async def wait_for_entered_sibling(event: dict[str, Any]) -> None:
        if event.get("item", {}).get("output") == receipt_text:
            await entered.wait()

    responses.before_event = wait_for_entered_sibling
    agent = Agent(
        instructions="Read both records.",
        tools=[left, right, slow_record],
        provider=provider(responses),
    )
    try:
        async with timeout(15):
            # Error delivery must join unanswered handlers and blocked work,
            # without accepting provider text as either manual reply.
            async def wait_for_application(request):
                await asyncio.Event().wait()

            with pytest.raises(
                NativeError, match="Native model response ended without complete supported output"
            ) as failed:
                async with timeout(4):
                    await agent.run("Read both", on_interaction=wait_for_application)
            assert entered.is_set()
            assert cleaned.is_set()
            assert not release.is_set()
            assert receipt_text not in str(failed.value)
            assert provider_call_id not in str(failed.value)
            assert len(responses.requests) == 1

            # The owned run retires its Session. Its Agent/provider can still
            # start a real manual Turn; same-Session recovery is covered above.
            async with agent.session() as session:
                fresh = await session.submit("Start another manual turn")
                first = await next_request(session.interactions(), left)
                second = await next_request(session.interactions(), right)
                assert (first.arguments.key, second.arguments.key) == ("fresh-A", "fresh-B")
                assert (first.turn_id, second.turn_id) == (fresh.turn_id, fresh.turn_id)
                assert await first.respond("first fresh answer") is None
                assert await second.respond("second fresh answer") is None
                assert (await fresh.get_result()).final_output == "fresh manual turn complete"
                assert len(responses.requests) == 3
                assert tool_output(responses.requests[2], "fresh-A") == "first fresh answer"
                assert tool_output(responses.requests[2], "fresh-B") == "second fresh answer"
                assert receipt_text not in json.dumps(responses.requests[1]["input"])
                assert receipt_text not in (await session.save_state()).to_json()
    finally:
        release.set()


async def test_sealed_checkpoint_remains_detached_during_and_after_close(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
    native_provider = provider(responses)
    agent = Agent(instructions="Look up A.", tools=[lookup], provider=native_provider)
    closing = asyncio.Event()
    release_close = asyncio.Event()
    async with timeout(8), agent.session() as session:
        await session.submit("Read A")
        await next_request(session.interactions(), lookup)
        first = await session.suspend()
        raw = first.to_json()
        repeated = await session.suspend()
        assert repeated is not first
        assert repeated.to_json() == raw
        first.history.clear()
        assert repeated.to_json() == raw

        close_session = session._close_session

        async def held_close() -> None:
            closing.set()
            await release_close.wait()
            await close_session()

        monkeypatch.setattr(session, "_close_session", held_close)
        close_task = asyncio.create_task(session.aclose())
        try:
            await closing.wait()
            during_close = await session.suspend()
            assert during_close is not repeated
            assert during_close.to_json() == raw
        finally:
            release_close.set()
            await close_task
        after_close = await session.suspend()
        assert after_close is not during_close
        assert after_close.to_json() == raw
        repeated.history.clear()
        assert (await session.suspend()).to_json() == raw
        with pytest.raises(RuntimeError, match="event loop"):
            await asyncio.to_thread(lambda: asyncio.run(session.suspend()))
    assert (await session.suspend()).to_json() == raw
    await native_provider.aclose()


@pytest.mark.parametrize("namespace", [None, "", "functions"])
async def test_manual_checkpoint_restores_core_default_namespace(
    responses: ResponsesFixture, namespace: str | None
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    call = function_call("lookup", {"key": "A"}, "call-A")
    if namespace is not None:
        call["item"]["namespace"] = namespace
    responses.respond(call)
    native_provider = provider(responses)
    agent = Agent(instructions="Look up A.", tools=[lookup], provider=native_provider)
    async with timeout(8):
        async with agent.session() as source:
            await source.submit("Read A")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        state = saved_state
        assert state is not None
        original_calls = [
            row["payload"]
            for row in state.history
            if row["type"] == "response_item" and row["payload"]["type"] == "function_call"
        ]
        assert len(original_calls) == 1
        assert original_calls[0].get("namespace") == namespace
        original_turn = json.loads(state.to_json())["native_continuation"]["turn_id"]
        async with agent.session(state=state) as restored:
            request = await next_request(restored.interactions(), lookup)
            assert request.call_id == "call-A"
            assert request.turn_id == original_turn
            assert len(responses.requests) == 1
            responses.respond(assistant_output("restored"))
            await request.respond("answer-A")
            assert (await restored.restored_turn.get_result()).final_output == "restored"
            with pytest.raises(InteractionExpiredError, match="expired|used|stale"):
                await request.respond("answer-A")
            completed = await restored.save_state()
        await native_provider.aclose()
    assert len(responses.requests) == 2
    assert tool_output(responses.requests[1], "call-A") == "answer-A"
    restored_calls = [
        row["payload"]
        for row in completed.history
        if row["type"] == "response_item" and row["payload"]["type"] == "function_call"
    ]
    assert restored_calls == original_calls
    outputs = [
        row["payload"]
        for row in completed.history
        if row["type"] == "response_item"
        and row["payload"]["type"] == "function_call_output"
        and row["payload"]["call_id"] == "call-A"
    ]
    assert len(outputs) == 1


async def test_cancelled_manual_reply_after_native_acceptance_resumes_restored_turn(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "cancelled-reply"))
    native_provider = provider(responses)
    agent = Agent(instructions="Look up A.", tools=[lookup], provider=native_provider)
    accepted, release = asyncio.Event(), asyncio.Event()
    async with timeout(15):
        async with agent.session() as source:
            await source.submit("Read A")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        assert saved is not None
        async with agent.session(state=saved) as restored:
            request = await next_request(restored.interactions(), lookup)
            actual = restored._native

            class HeldReply:
                def __getattr__(self, name: str) -> Any:
                    return getattr(actual, name)

                async def _respond_manual_tool(self, *args: Any) -> bool:
                    result = await actual._respond_manual_tool(*args)
                    assert result is True
                    accepted.set()
                    await release.wait()
                    return result

            restored._native = HeldReply()
            responses.respond(assistant_output("restored after cancellation"))
            replying = asyncio.create_task(request.respond("accepted answer"))
            try:
                await accepted.wait()
                replying.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await replying
                assert restored.restored_turn is not None
                async with timeout(3):
                    result = await restored.restored_turn.get_result()
                assert result.final_output == "restored after cancellation"
                with pytest.raises(InteractionExpiredError, match="expired|used|stale|delivered"):
                    await request.respond("already accepted")
                assert len(responses.requests) == 2
                outputs = [
                    item
                    for item in responses.requests[1]["input"]
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == "cancelled-reply"
                ]
                assert len(outputs) == 1
                assert tool_output(responses.requests[1], "cancelled-reply") == "accepted answer"
            finally:
                release.set()
                await asyncio.gather(replying, return_exceptions=True)
        await native_provider.aclose()


@pytest.mark.parametrize("fail_cleanup", [False, True])
async def test_failed_manual_restore_joins_unpublished_submission(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, fail_cleanup: bool
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
    native_provider = provider(responses)
    agent = Agent(instructions="Look up A.", tools=[lookup], provider=native_provider)
    async with agent.session() as source:
        await source.submit("Read A")
        await next_request(source.interactions(), lookup)
        saved_state = await source.suspend()
    assert saved_state is not None
    submissions = []
    cleanup_error = RuntimeError("controlled native close failure")

    class FailedClose:
        def __init__(self, native: Any) -> None:
            self.native = native

        def __getattr__(self, name: str) -> Any:
            return getattr(self.native, name)

        async def close(self) -> None:
            raise cleanup_error

    def fail_before_readers(session: Session[Any, Any]) -> None:
        assert session._active is not None
        assert session._active.task is not None
        submissions.append(session._active.task)
        if fail_cleanup:
            session._native = FailedClose(session._native)
        raise RuntimeError("controlled restored reader failure")

    monkeypatch.setattr(Session, "_start_readers", fail_before_readers)
    try:
        async with timeout(5):
            with pytest.raises(RuntimeError, match="controlled restored reader failure") as opened:
                async with agent.session(state=saved_state):
                    pytest.fail("partially armed restored Session entered")
            await native_provider.aclose()
        assert len(submissions) == 1
        assert submissions[0].done()
        assert len(responses.requests) == 1
        if fail_cleanup:
            errors = []
            current: BaseException | None = opened.value
            while current is not None and current not in errors:
                errors.append(current)
                current = current.__context__
            assert cleanup_error in errors
    finally:
        for task in submissions:
            task.cancel()
        await asyncio.gather(*submissions, return_exceptions=True)


async def test_mixed_native_manual_run_fails_before_inference_or_effects(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    peer = Peer(label="known-native-B")
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        mcp_call("search", "mcp__docs", "call-B"),
    )
    async with timeout(8), peer.serve() as url:
        agent = Agent(
            instructions="Use both tools.",
            tools=[lookup, HttpMCP(name="docs", url=url, tools=["search"])],
            provider=provider(responses),
            model="gpt-5.5",
        )
        with pytest.raises(InteractionRequired) as paused:
            await agent.run("Read both")
        assert paused.value.state is None
        assert peer.requests == []
        assert responses.requests == []


async def test_manual_checkpoint_preserves_completed_mcp_without_result_transform(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    peer = Peer(label="known-native-B")
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        mcp_call("search", "mcp__docs", "call-B"),
    )
    async with timeout(8), peer.serve() as url:
        agent = Agent(
            instructions="Use both tools.",
            tools=[lookup, HttpMCP(name="docs", url=url, tools=["search"])],
            provider=provider(responses),
            model="gpt-5.5",
        )
        async with agent.session() as session:
            handle = await session.submit("Read both")
            request = await next_request(session.interactions(), lookup)
            async for event in handle.events():
                if isinstance(event, ToolFinished) and event.call_id == "call-B":
                    break
            assert len(peer.calls) == 1
            while True:
                try:
                    saved = await session.suspend()
                    break
                except RuntimeError as error:
                    if str(error) != "The active submission is not a settled manual boundary":
                        raise
        async with agent.session(state=saved) as restored:
            current = await next_request(restored.interactions(), lookup)
            assert current.call_id == request.call_id
            responses.respond(assistant_output("continued"))
            await current.respond("answer-A")
            assert (await restored.restored_turn.get_result()).final_output == "continued"
        assert "known-native-B" in json.dumps(responses.requests[1]["input"])
        assert len(peer.calls) == 1


@pytest.mark.parametrize("profile", ["mcp", "executor", "native_agents"])
async def test_unused_native_declarations_keep_manual_checkpoint_and_current_reply(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    async with timeout(30), AsyncExitStack() as stack:
        model = await stack.enter_async_context(peer())
        tools: list[Tool[None]] = [lookup]
        mcp = Peer()
        executor = None
        environment = None
        if profile == "mcp":
            url = await stack.enter_async_context(mcp.serve())
            tools.append(HttpMCP(name="docs", url=url, tools=["search"]))
        if profile in ("executor", "native_agents"):
            executor = await stack.enter_async_context(ExecutorPeer(tmp_path / "remote").serve())
            monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
            monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-ca"))
            environment = Environment(
                url=executor.url,
                **({"write_policy": "legacy"} if sys.platform == "win32" else {}),
                cwd=str(executor.root),
                headers={"Authorization": "Bearer " + executor.token},
                tools=["exec_command", "write_stdin", "apply_patch"],
            )
        if profile == "native_agents":
            assert executor is not None
            agent = replace(agent_for(model, executor), instructions="Use lookup.", tools=tools)
        else:
            agent = Agent(
                instructions="Use lookup.", tools=tools, provider=model.provider(websocket=False)
            )
        model.respond(function_call("lookup", {"key": "A"}, "call-A"))
        async with agent.session(environment=environment) as session:
            await session.submit("Look up A")
            original = await next_request(session.interactions(), lookup)
            while True:
                try:
                    saved = await session.suspend()
                    break
                except RuntimeError as error:
                    if str(error) != "The active submission is not a settled manual boundary":
                        raise
            assert saved.format_version == 5
            assert saved.descendants == []
            assert len(model.requests) == 1
        # Abandoning restored replies must still close the executor cleanly.
        async with agent.session(
            state=SessionState.from_json(saved.to_json()), environment=environment
        ):
            pass
        async with agent.session(
            state=SessionState.from_json(saved.to_json()), environment=environment
        ) as restored:
            current = await next_request(restored.interactions(), lookup)
            assert current.call_id == original.call_id
            assert current.turn_id == original.turn_id
            with pytest.raises((ValueError, RuntimeError), match="foreign|expired|closed"):
                await original.respond("stale")
            assert len(model.requests) == 1
            model.respond(assistant_output("continued"))
            await current.respond("answer-A")
            assert (await restored.restored_turn.get_result()).final_output == "continued"
            assert (await restored.save_state()).format_version == 4
        assert len(model.requests) == 2
        assert tool_output(model.requests[1], "call-A") == "answer-A"
        assert mcp.calls == []
        if executor is not None:
            assert not any(
                request.get("method") == "process/start" for request in executor.requests
            )


@pytest.mark.parametrize(
    "extra",
    [
        {
            "type": "function_call",
            "call_id": "native-extra",
            "name": "lookup",
            "namespace": "mcp__docs",
            "arguments": '{"key":"A"}',
        },
        {
            "type": "custom_tool_call",
            "call_id": "native-extra",
            "name": "apply_patch",
            "input": "*** Begin Patch\n*** End Patch",
        },
        {
            "type": "custom_tool_call",
            "call_id": "call-A",
            "name": "apply_patch",
            "input": "*** Begin Patch\n*** End Patch",
        },
        {
            "type": "custom_tool_call_output",
            "call_id": "call-A",
            "output": "wrong result kind",
        },
        {"type": "function_call_output", "output": "missing call identity"},
        {
            "type": "custom_tool_call",
            "call_id": "native-extra",
            "name": "unsupported_native_action",
            "input": "untracked work",
        },
    ],
)
async def test_manual_restore_rejects_native_actions_missing_from_saved_receipt_frame(
    responses: ResponsesFixture, extra: dict[str, Any]
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    agent = Agent(instructions="Read.", tools=[lookup], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
    async with timeout(15):
        async with agent.session() as source:
            await source.submit("Read A")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        assert saved_state is not None
        raw = json.loads(saved_state.to_json())
        raw["history"].append({"type": "response_item", "payload": extra})
        changed = SessionState.from_json(json.dumps(raw))
        with pytest.raises(RuntimeError):
            async with agent.session(state=changed):
                pytest.fail("Manual restore accepted an unowned native action")
        assert len(responses.requests) == 1


async def test_manual_planning_reseal_preserves_completed_work_and_goal(
    responses: ResponsesFixture,
) -> None:
    effects: list[str] = []

    @function_tool
    async def record(key: str) -> str:
        effects.append(key)
        return "known-" + key

    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Plan, record B, then look up A and C.",
        tools=[lookup, record],
        provider=provider(responses),
    )
    responses.respond(function_call("record", {"key": "B"}, "call-B"))
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("lookup", {"key": "C"}, "call-C"),
    )
    async with timeout(25):
        async with agent.session() as source:
            await source.submit("read")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        initial = saved_state
        assert initial is not None
        assert initial.native_continuation is not None
        raw = json.loads(initial.to_json())
        raw["root_goal"] = legacy_goal(initial.source_session_id, objective="Read and summarize")
        raw["root_checklist"] = legacy_checklist(
            initial.source_session_id, initial.native_continuation.turn_id
        )
        initial = SessionState.from_json(json.dumps(raw))
        assert initial.root_checklist is not None
        assert effects == ["B"]
        assert len(responses.requests) == 2

        async with agent.session(state=initial) as restored:
            stream = restored.interactions()
            first = await next_request(stream, lookup)
            second = await next_request(stream, lookup)
            assert {first.call_id, second.call_id} == {"call-A", "call-C"}
            # Re-saving a manual boundary retains stored data and actual
            # accepted replies without resuming the removed goal capability.
            await first.respond("answer-" + first.arguments.key)
            resealed = await restored.suspend()
        goal = resealed.root_goal
        assert goal is not None
        assert (goal.objective, goal.status) == ("Read and summarize", "paused")
        assert resealed.root_checklist == initial.root_checklist
        snapshot = json.loads(resealed.to_json())
        assert len(snapshot["native_continuation"]["pending_manual"]) == 1
        assert len(responses.requests) == 2

        # Continue the remaining request without repeating accepted tools.
        async with agent.session(state=resealed) as restored:
            request = await next_request(restored.interactions(), lookup)
            assert request.call_id == second.call_id
            responses.respond(assistant_output("manual work complete"))
            await request.respond("answer-" + request.arguments.key)
            assert (
                await restored.restored_turn.get_result()
            ).final_output == "manual work complete"
            assert len(responses.requests) == 3
            responses.respond(assistant_output("fresh turn"))
            fresh = await restored.submit("Continue as new work")
            assert (await (fresh).get_result()).final_output == "fresh turn"
            completed = await restored.save_state()
            assert completed.root_goal == goal
        assert len(responses.requests) == 4
        assert effects == ["B"]
        history_results = [
            row["payload"]["call_id"]
            for row in completed.history
            if row["type"] == "response_item" and row["payload"]["type"] == "function_call_output"
        ]
        assert history_results == ["call-B", "call-A", "call-C"]


async def test_failed_restored_manual_snapshot_read_keeps_live_reply(
    responses: ResponsesFixture,
) -> None:
    class FailedSnapshot:
        def __init__(self, native: Any) -> None:
            self.native = native

        def __getattr__(self, name: str) -> Any:
            return getattr(self.native, name)

        async def _seal_manual(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("controlled snapshot read failure")

    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Read.",
        tools=[lookup],
        provider=provider(responses),
    )
    responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
    async with timeout(15):
        async with agent.session() as source:
            await source.submit("read")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        assert saved_state is not None
        async with agent.session(state=saved_state) as session:
            request = await next_request(session.interactions(), lookup)
            original = session._native
            session._native = FailedSnapshot(original)
            with pytest.raises(RuntimeError, match="controlled snapshot read failure"):
                await session.suspend()
            session._native = original
            assert len(responses.requests) == 1
            responses.respond(assistant_output("restored after failed save"))
            await request.respond("A")
            assert (
                await session.restored_turn.get_result()
            ).final_output == "restored after failed save"
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "call-A") == "A"


async def test_websocket_provider_preserves_explicit_manual_state() -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    async with timeout(20), peer() as model:
        connection = NativeProvider(
            base_url=model.url, api_key="synthetic-websocket", supports_websockets=True
        )
        model.providers.append(connection)
        agent = Agent(instructions="Use lookup.", tools=[lookup], provider=connection)
        model.respond(function_call("lookup", {"key": "A"}, "call-A"))
        async with agent.session() as source:
            await source.submit("Read A")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        assert saved_state is not None
        model.respond(assistant_output("continued"))
        async with agent.session(state=saved_state) as session:
            request = await next_request(session.interactions(), lookup)
            await request.respond("answer-A")
            assert (await session.restored_turn.get_result()).final_output == "continued"
        assert len(model.requests) == 2
        assert model.connections >= 2
        assert model.http_requests == []


async def test_manual_checkpoint_usage_belongs_to_suspended_turn(
    responses: ResponsesFixture,
) -> None:
    async def distinct_usage(event: dict[str, Any]) -> None:
        if event["type"] == "response.completed":
            count = len(responses.requests)
            event["response"]["usage"] = {
                "input_tokens": count * 10,
                "output_tokens": count,
                "total_tokens": count * 11,
            }

    responses.before_event = distinct_usage
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Use lookup when needed.", tools=[lookup], provider=provider(responses)
    )
    async with timeout(15), agent.session() as session:
        responses.respond(assistant_output("first turn"))
        first = await session.submit("Answer without a tool")
        assert (await (first).get_result()).final_output == "first turn"
        responses.respond(function_call("lookup", {"key": "A"}, "call-second-turn"))
        second = await session.submit("Look up A")
        await next_request(session.interactions(), lookup)
        while not await session._native._manual_ready():
            await asyncio.sleep(0)
        state = await session.suspend()
        frame = json.loads(state.to_json())["native_continuation"]
        assert frame["turn_id"] == second.turn_id
        assert frame["turn_usage"] == {
            "input_tokens": 20,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 2,
            "reasoning_output_tokens": 0,
            "total_tokens": 22,
        }
    async with agent.session(state=state) as restored:
        resealed = await restored.suspend()
        assert (
            json.loads(resealed.to_json())["native_continuation"]["turn_usage"]
            == frame["turn_usage"]
        )
    assert len(responses.requests) == 2


async def test_manual_cursor_can_seal_before_reader_delivers_native_notice(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents.session import Session

    release = asyncio.Event()
    read_events = Session._read_events

    async def hold_reader(session: Session[Any, Any]) -> None:
        await release.wait()
        await read_events(session)

    monkeypatch.setattr(Session, "_read_events", hold_reader)
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "call-before-reader"))
    async with timeout(15):
        async with agent.session() as session:
            try:
                handle = await session.submit("Read A")
                # Wait for native sampling, while the Python observer has not
                # received any notice. A live cursor reads the native grant.
                while not await session._native._manual_ready():
                    await asyncio.sleep(0)
                request = await next_request(session.interactions(), lookup)
                assert request.call_id == "call-before-reader"
                assert request.turn_id == handle.turn_id
                assert request.arguments.key == "A"
                assert len(responses.requests) == 1
                assert not release.is_set()
                state = await session.suspend()
                assert (await session.suspend()).to_json() == state.to_json()
                release.set()
                with pytest.raises(InteractionRequired) as paused:
                    await (handle).get_result()
                assert paused.value.state is not None
                assert paused.value.state.to_json() == state.to_json()
            finally:
                release.set()
        frame = json.loads(state.to_json())["native_continuation"]
        assert frame["turn_id"] == handle.turn_id
        assert frame["reply_order"] == ["call-before-reader"]
        assert frame["accepted_replies"] == {}
        assert [item["call_id"] for item in frame["pending_manual"]] == ["call-before-reader"]
        assert json.loads(frame["pending_manual"][0]["arguments"]) == {"key": "A"}
        assert len(responses.requests) == 1

        responses.respond(assistant_output("continued"))
        async with agent.session(state=state) as restored:
            current = await next_request(restored.interactions(), lookup)
            assert current.turn_id == handle.turn_id
            assert current.call_id == request.call_id
            with pytest.raises((ValueError, RuntimeError), match="foreign|expired|closed"):
                await request.respond("stale")
            assert len(responses.requests) == 1
            await current.respond("answer-A")
            assert (await restored.restored_turn.get_result()).final_output == "continued"
            outputs = [
                item
                for item in [item async for item in restored.items()]
                if item["type"] == "function_call_output"
            ]
            assert [item["call_id"] for item in outputs] == ["call-before-reader"]
            assert outputs[0]["turn_id"] == handle.turn_id
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "call-before-reader") == "answer-A"
        assert all(
            item.get("id") is None or item["id"].startswith("msg_")
            for request in responses.requests
            for item in request["input"]
            if item.get("type") == "message"
        )


@pytest.mark.parametrize("namespace", [None, "billing"])
async def test_settled_manual_state_preserves_blocked_accepted_receipt(
    responses: ResponsesFixture,
    namespace: str | None,
) -> None:
    effects: list[str] = []
    replayed: list[str] = []
    outputs_validated: list[Any] = []

    class Answer(BaseModel):
        value: str

        @model_validator(mode="after")
        def validate_current(self) -> Answer:
            outputs_validated.append(self.value)
            return self

    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=Answer
    )

    @function_tool(name="record", namespace=namespace, description="Record a key")
    async def original(key: str) -> str:
        effects.append(key)
        return "known-B"

    @function_tool(name="record", namespace=namespace, description="Record a key")
    async def current(key: str) -> str:
        replayed.append(key)
        return "new-B"

    original_arguments = ' { "key" : "A" } '
    manual_call = function_call("lookup", {"key": "A"}, "call-A")
    manual_call["item"]["arguments"] = original_arguments
    automatic_call = function_call("record", {"key": "B"}, "call-B")
    if namespace is not None:
        automatic_call["item"]["namespace"] = namespace
    responses.respond(manual_call, automatic_call)
    agent = Agent(
        instructions="Use both tools.", tools=[lookup, original], provider=provider(responses)
    )
    old_request = None
    async with timeout(15):
        with pytest.raises(InteractionRequired) as paused:
            async with agent.session() as session:
                handle = await session.submit("Read A and record B")
                old_request = await next_request(session.interactions(), lookup)
                # Replay B on a cursor independent of live request discovery.
                async for event in session.events():
                    if isinstance(event, ToolFinished) and event.call_id == "call-B":
                        break
                entered = asyncio.Event()

                async def wait_without_consuming() -> Any:
                    entered.set()
                    return await (handle).get_result()

                waiter = asyncio.create_task(wait_without_consuming())
                await entered.wait()
                assert not waiter.done()
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                # ToolFinished can precede persistence of the full Core sample.
                state = await save_at_manual_boundary(session)
                await (handle).get_result()
        assert paused.value.state is not None
        assert paused.value.state.to_json() == state.to_json()
        assert effects == ["B"]
        assert len(responses.requests) == 1
        saved = json.loads(state.to_json())
        continuation = saved["native_continuation"]
        assert state.format_version == 5
        assert continuation["reply_order"] == ["call-A", "call-B"]
        assert set(continuation["accepted_replies"]) == {"call-B"}
        assert [item["call_id"] for item in continuation["pending_manual"]] == ["call-A"]
        assert continuation["pending_manual"][0]["arguments"] == original_arguments
        assert not any("grant" in key or "nonce" in key for key in continuation)

        current_lookup = FunctionTool(
            name="lookup", description="Look up a key", input_type=LookupArgs, output_type=Answer
        )
        restored_agent = Agent(
            instructions=agent.instructions,
            tools=[current_lookup, current],
            provider=provider(responses),
        )
        responses.respond(assistant_output("continued"))
        async with restored_agent.session(
            state=SessionState.from_json(state.to_json())
        ) as restored:
            # Restore continues the same native conversation, as for an idle
            # Core checkpoint. Request and handle grants are newly bound.
            assert restored.id == state.source_session_id
            request = await next_request(restored.interactions(), current_lookup)
            assert request.turn_id == continuation["turn_id"]
            assert request.call_id == "call-A"
            assert request.arguments.key == "A"
            assert len(responses.requests) == 1
            if old_request is not None:
                assert request.submission_id != old_request.submission_id
                with pytest.raises(
                    (RuntimeError, ValueError), match="foreign|expired|closed|sealed"
                ):
                    await old_request.respond(Answer.model_construct(value="stale"))
                assert outputs_validated == []
            await request.respond(Answer.model_construct(value="answer-A"))
            result = await restored.restored_turn.get_result()
            assert result.turn.turn_id == continuation["turn_id"]
            assert result.final_output == "continued"
            page = [item async for item in restored.items()]
            outputs = [item for item in page if item["type"] == "function_call_output"]
            assert [item["call_id"] for item in outputs] == ["call-A", "call-B"]
            assert all(item["turn_id"] == continuation["turn_id"] for item in outputs)
        assert replayed == []
        assert len(responses.requests) == 2
        assert json.loads(tool_output(responses.requests[1], "call-A")) == {"value": "answer-A"}
        assert tool_output(responses.requests[1], "call-B") == "known-B"
        assert (
            len([item for item in responses.requests[1]["input"] if item.get("role") == "user"])
            == 1
        )


@pytest.mark.parametrize("namespace", [None, "billing"])
async def test_save_state_refuses_a_running_automatic_effect(
    responses: ResponsesFixture, namespace: str | None
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )

    @function_tool(namespace=namespace)
    async def record(key: str) -> str:
        entered.set()
        await release.wait()
        effects.append(key)
        return "done"

    automatic_call = function_call("record", {"key": "B"}, "call-B")
    if namespace is not None:
        automatic_call["item"]["namespace"] = namespace
    responses.respond(function_call("lookup", {"key": "A"}, "call-A"), automatic_call)
    agent = Agent(
        instructions="Use both tools.", tools=[lookup, record], provider=provider(responses)
    )
    async with timeout(15), agent.session() as session:
        await session.submit("both")
        await next_request(session.interactions(), lookup)
        await entered.wait()
        try:
            pending_save = asyncio.create_task(session.suspend())
            done, _ = await asyncio.wait((pending_save,), timeout=0.1)
            assert not done, "suspend must wait for the entered Python callback"
            pending_save.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending_save
            assert effects == []
            assert len(responses.requests) == 1
        finally:
            release.set()
        async for event in session.events():
            if isinstance(event, ToolFinished) and event.call_id == "call-B":
                break
        state = await save_at_manual_boundary(session)
        assert state.format_version == 5
        assert effects == ["B"]


@pytest.mark.parametrize("late_callback", [False, True])
async def test_native_preseal_refusal_keeps_original_manual_reply(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, late_callback: bool
) -> None:
    queued, capturing, entered, release = (asyncio.Event() for _ in range(4))
    effects: list[str] = []
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )

    @function_tool
    async def record(key: str) -> str:
        entered.set()
        await release.wait()
        effects.append(key)
        return "done"

    class DelayedNative:
        def __init__(self, native: Any) -> None:
            self.native = native
            self.manual_seen = False
            self.pending_callback: str | None = None

        def __getattr__(self, name: str) -> Any:
            return getattr(self.native, name)

        async def _next_session_event(self) -> str:
            if self.manual_seen and self.pending_callback is not None:
                raw, self.pending_callback = self.pending_callback, None
            else:
                raw = await self.native._next_session_event()
            event = json.loads(raw)
            message = event.get("msg", {}) if event else {}
            if (
                message.get("type") == "dynamic_tool_call_request"
                and message.get("callId") == "call-A"
            ):
                self.manual_seen = True
            if (
                message.get("type") == "dynamic_tool_call_request"
                and message.get("callId") == "call-B"
            ):
                # Native tools dispatch concurrently. Deliver the manual request
                # before holding the reader for a checkpoint started by that request.
                if not self.manual_seen:
                    self.pending_callback = raw
                    return await self._next_session_event()
                queued.set()
                if late_callback:
                    await capturing.wait()
            return raw

        async def _seal_manual(self, *args: Any, **kwargs: Any) -> str:
            capturing.set()
            await asyncio.wait_for(entered.wait(), 1)
            return await self.native._seal_manual(*args, **kwargs)

    start_readers = Session._start_readers

    def start_delayed_readers(session: Session[Any, Any]) -> None:
        session._native = DelayedNative(session._native)
        start_readers(session)

    async def stale_ready(session: Session[Any, Any], state: Any) -> bool:
        return True

    monkeypatch.setattr(Session, "_start_readers", start_delayed_readers)
    monkeypatch.setattr(Session, "_manual_ready", stale_ready)
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("record", {"key": "B"}, "call-B"),
    )
    responses.respond(assistant_output("finished"))
    agent = Agent(
        instructions="Use both tools.", tools=[lookup, record], provider=provider(responses)
    )
    async with timeout(8), agent.session() as session:
        try:
            handle = await session.submit("both")
            request = await next_request(session.interactions(), lookup)
            await queued.wait()
            if not late_callback:
                await entered.wait()
            with pytest.raises(RuntimeError, match="not a settled manual boundary"):
                await session.suspend()
            assert await request.respond("answer-A") is None
            release.set()
            assert (await (handle).get_result()).final_output == "finished"
        finally:
            capturing.set()
            release.set()
    assert effects == ["B"]
    assert len(responses.requests) == 2
    assert tool_output(responses.requests[1], "call-A") == "answer-A"
    assert tool_output(responses.requests[1], "call-B") == "done"


async def test_preseal_refusal_does_not_revive_an_ended_reader(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
    agent = Agent(instructions="Look up A.", tools=[lookup], provider=provider(responses))
    async with timeout(8), agent.session() as session:
        handle = await session.submit("A")
        request = await next_request(session.interactions(), lookup)
        # Enter the intended native pre-seal step, not an earlier refusal
        # because the model's response sample is still being recorded.
        while not await session._native._manual_ready():
            await asyncio.sleep(0)
        native = session._native

        class EndedReader:
            def __getattr__(self, name: str) -> Any:
                return getattr(native, name)

            async def _seal_manual(self, *args: Any, **kwargs: Any) -> str:
                session._reader.cancel()
                await asyncio.gather(session._reader, return_exceptions=True)
                return "null"

        session._native = EndedReader()
        with pytest.raises(NativeError, match="event stream ended"):
            await session.suspend()
        with pytest.raises(RuntimeError):
            await request.respond("answer-A")
        with pytest.raises(NativeError, match="event stream ended"):
            await (handle).get_result()
    assert len(responses.requests) == 1


@pytest.mark.parametrize("mode", ["one_shot", "live"])
async def test_invalid_manual_input_fails_without_an_observer(
    responses: ResponsesFixture, mode: str
) -> None:
    validated: list[Any] = []

    class Input(BaseModel):
        key: str

        @model_validator(mode="after")
        def refuse(self) -> Input:
            validated.append(self.key)
            raise ValueError("custom input refusal")

    lookup = FunctionTool(name="lookup", description="Read", input_type=Input, output_type=str)
    responses.respond(function_call("lookup", {"key": "bad"}, "invalid"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(15):
        with pytest.raises(
            NativeError, match="Manual tool arguments could not be delivered"
        ) as error:
            if mode == "one_shot":
                await agent.run("read", on_interaction=lambda request: request.reply("unused"))
            else:
                async with agent.session() as session:
                    await (await session.submit("read")).get_result()
        assert not isinstance(error.value, InteractionRequired)
        assert validated
        assert len(responses.requests) == 1


async def test_restored_manual_requires_current_matching_declaration_before_io(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(15):
        async with agent.session() as source:
            await source.submit("read")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        assert saved_state is not None
        changed = Agent(instructions=agent.instructions, tools=[], provider=provider(responses))
        with pytest.raises((ValueError, RuntimeError)):
            async with changed.session(state=saved_state):
                pytest.fail("Restore accepted a missing current manual declaration")
        assert len(responses.requests) == 1


async def test_restored_manual_accepts_concurrent_replies_once(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("lookup", {"key": "C"}, "call-C"),
    )
    agent = Agent(instructions="Use lookup twice.", tools=[lookup], provider=provider(responses))
    async with timeout(15):
        async with agent.session() as source:
            await source.submit("read A and C")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        assert saved_state is not None
        assert len(responses.requests) == 1
        responses.respond(assistant_output("both answered"))
        async with agent.session(state=saved_state) as restored:
            stream = restored.interactions()
            first = await next_request(stream, lookup)
            second = await next_request(stream, lookup)
            assert {first.call_id, second.call_id} == {"call-A", "call-C"}
            assert await asyncio.gather(
                first.respond("answer-" + first.arguments.key),
                second.respond("answer-" + second.arguments.key),
            ) == [None, None]
            result = await restored.restored_turn.get_result()
            assert result.turn.turn_id == first.turn_id == second.turn_id
            assert result.final_output == "both answered"
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "call-A") == "answer-A"
        assert tool_output(responses.requests[1], "call-C") == "answer-C"


async def test_manual_session_compacts_before_a_later_manual_checkpoint(
    responses: ResponsesFixture,
) -> None:
    effects: list[str] = []

    async def force_compaction(event: dict[str, Any]) -> None:
        if len(responses.requests) == 1 and event["type"] == "response.completed":
            event["response"]["usage"] = {
                "input_tokens": 249_999,
                "output_tokens": 1,
                "total_tokens": 250_000,
            }

    @function_tool
    async def record(key: str) -> str:
        effects.append(key)
        return "known"

    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.before_event = force_compaction
    responses.respond(function_call("record", {"key": "B"}, "call-B"))
    responses.respond(assistant_output("Compacted summary: B was recorded."))
    responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
    agent = Agent(
        instructions="Use both tools.",
        model="gpt-5.2",
        tools=[lookup, record],
        provider=provider(responses),
    )
    async with timeout(15):
        async with agent.session() as source:
            await source.submit("record B, then read A")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        state = saved_state
        assert state is not None
        frame = json.loads(state.to_json())["native_continuation"]
        assert [call["call_id"] for call in frame["pending_manual"]] == ["call-A"]
        assert effects == ["B"]
        assert len(responses.requests) == 3
        assert responses.requests[1].get("tools", []) == []


@pytest.mark.parametrize("corruption", ["early_receipt", "pending_order"])
async def test_manual_continuation_rejects_reordered_native_receipts_before_io(
    responses: ResponsesFixture, corruption: str
) -> None:
    effects: list[str] = []
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )

    @function_tool
    async def record(key: str) -> str:
        effects.append(key)
        return "known-B"

    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("record", {"key": "B"}, "call-B"),
        function_call("lookup", {"key": "C"}, "call-C"),
    )
    agent = Agent(
        instructions="Use the tools.", tools=[lookup, record], provider=provider(responses)
    )
    async with timeout(15):
        async with agent.session() as source:
            await source.submit("A, B, C")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        assert saved_state is not None
        raw = json.loads(saved_state.to_json())
        frame = raw["native_continuation"]
        assert frame["reply_order"] == ["call-A", "call-B", "call-C"]
        assert [item["call_id"] for item in frame["pending_manual"]] == ["call-A", "call-C"]
        if corruption == "early_receipt":
            receipt = frame["accepted_replies"].pop("call-B")
            frame["reply_order"].remove("call-B")
            raw["history"].append(
                receipt
                if receipt.get("type") == "response_item"
                else {"type": "response_item", "payload": receipt}
            )
        else:
            frame["pending_manual"].reverse()
        changed = SessionState.from_json(json.dumps(raw))
        with pytest.raises((ValueError, RuntimeError)):
            async with agent.session(state=changed):
                pytest.fail("Restore accepted a noncanonical manual receipt order")
        assert effects == ["B"]
        assert len(responses.requests) == 1


async def test_manual_restore_preserves_headers_across_idle_restore_and_second_seal(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "first-call"))

    def check_headers(state: SessionState, expected: str) -> None:
        raw = json.loads(state.to_json())
        assert raw["source_session_id"] == expected
        assert raw["root_metadata"]["thread_id"] == expected
        headers = [item["payload"] for item in raw["history"] if item["type"] == "session_meta"]
        assert len(headers) == 1
        assert headers[0]["id"] == expected

    async with timeout(20):
        async with agent.session() as source:
            await source.submit("first")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        first_state = saved_state
        assert first_state is not None
        first_turn = json.loads(first_state.to_json())["native_continuation"]["turn_id"]
        responses.respond(assistant_output("first complete"))
        async with agent.session(state=first_state) as restored:
            request = await next_request(restored.interactions(), lookup)
            assert request.turn_id == first_turn
            await request.respond("A accepted")
            assert (await restored.restored_turn.get_result()).final_output == "first complete"
            idle = await restored.save_state()
            assert idle.format_version == 4
            check_headers(idle, restored.id)
            restored_id = restored.id
            responses.respond(function_call("lookup", {"key": "B"}, "second-call"))
            second = await restored.submit("second")
            second_request = await next_request(restored.interactions(), lookup)
            assert second_request.turn_id == second.turn_id != first_turn
            # The request can arrive before Core records the completed response.
            while not await restored._native._manual_ready():
                await asyncio.sleep(0)
            second_state = await restored.suspend()
            check_headers(second_state, restored_id)
            assert second_state.format_version == 5
            assert (
                json.loads(second_state.to_json())["native_continuation"]["turn_id"]
                == second.turn_id
            )
        # Old idle format2 keeps its existing importer; no manual work is resumed here.
        async with agent.session(state=idle) as idle_restored:
            assert idle_restored.id == restored_id
            check_headers(await idle_restored.save_state(), idle_restored.id)
            assert [item async for item in idle_restored.turns()][0]["id"] == first_turn
        assert len(responses.requests) == 3
        responses.respond(assistant_output("second complete"))
        async with agent.session(state=second_state) as resumed:
            current = await next_request(resumed.interactions(), lookup)
            assert current.turn_id == second.turn_id
            await current.respond("B accepted")
            recovered = resumed.restored_turn
            assert recovered is not None
            assert (await recovered.get_result()).final_output == "second complete"
            check_headers(await resumed.save_state(), resumed.id)
        assert len(responses.requests) == 4


async def test_nontext_accepted_effect_survives_explicit_manual_checkpoint(
    responses: ResponsesFixture,
) -> None:
    from tests.test_native_session_input import IMAGE

    effects: list[str] = []
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )

    @function_tool
    async def picture(key: str) -> ToolOutput:
        effects.append(key)
        return ToolOutput.image(IMAGE)

    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("picture", {"key": "B"}, "call-B"),
    )
    agent = Agent(
        instructions="Use both tools.", tools=[lookup, picture], provider=provider(responses)
    )
    async with timeout(15):
        async with agent.session() as source:
            await source.submit("both")
            await next_request(source.interactions(), lookup)
            saved_state = await source.suspend()
        assert saved_state is not None
        assert effects == ["B"]
        assert len(responses.requests) == 1
        responses.respond(assistant_output("continued"))
        async with agent.session(state=saved_state) as session:
            request = await next_request(session.interactions(), lookup)
            await request.respond("answer-A")
            assert (await session.restored_turn.get_result()).final_output == "continued"
        assert effects == ["B"]
        assert len(responses.requests) == 2


@pytest.mark.parametrize("restore", [False, True])
async def test_manual_receipts_survive_core_compaction_without_replaying_effects(
    responses: ResponsesFixture, restore: bool
) -> None:
    from agents.observation import ContextCompacted

    effects: list[str] = []
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )

    @function_tool
    async def record(key: str) -> str:
        effects.append(key)
        return "known-B"

    async def report_large_usage(event: dict[str, Any]) -> None:
        if len(responses.requests) == 1 and event["type"] == "response.completed":
            event["response"]["usage"] = {
                "input_tokens": 249_999,
                "output_tokens": 1,
                "total_tokens": 250_000,
            }

    responses.before_event = report_large_usage
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("record", {"key": "B"}, "call-B"),
    )
    agent = Agent(
        instructions="Read A and record B, then answer.",
        model="gpt-5.2",
        tools=[lookup, record],
        provider=provider(responses),
    )
    saved = None
    async with timeout(30):
        if restore:
            async with agent.session() as source:
                await source.submit("both")
                await next_request(source.interactions(), lookup)
                saved_state = await source.suspend()
            saved = saved_state
            assert saved is not None
            assert effects == ["B"]
        async with agent.session(state=saved) as session:
            handle = session.restored_turn if restore else await session.submit("both")
            assert handle is not None
            request = await next_request(session.interactions(), lookup)
            responses.respond(assistant_output("Compacted summary: A answer-A and B known-B."))
            responses.respond(assistant_output("continued after compaction"))
            await request.respond("answer-A")
            completed = await handle.get_result()
            assert completed.turn is handle
            assert completed.turn.turn_id == request.turn_id
            assert completed.final_output == "continued after compaction"
            settled_events = [event async for event in completed.turn.events()]
            assert any(isinstance(event, ContextCompacted) for event in settled_events)
            history = (await session.save_state()).history
            results = [
                row["payload"]
                for row in history
                if row["type"] == "response_item"
                and row["payload"].get("type") == "function_call_output"
            ]
            assert [item["call_id"] for item in results] == ["call-A", "call-B"]
            assert results[0]["output"] == "answer-A"
            assert results[1]["output"] == "known-B"
            assert [item async for item in session.turns()][0]["id"] == request.turn_id
        assert effects == ["B"]
        assert len(responses.requests) == 3
        assert responses.requests[1].get("tools", []) == []


@pytest.mark.parametrize("seal", [False, True])
async def test_manual_input_validation_is_once_per_native_invocation_and_cursor_values_are_private(
    responses: ResponsesFixture, seal: bool
) -> None:
    inputs_validated: list[str] = []
    outputs_validated: list[str] = []
    validated = asyncio.Event()

    class Input(BaseModel):
        key: str
        values: list[str]

        @model_validator(mode="after")
        def validate_once(self) -> Input:
            inputs_validated.append(self.key)
            validated.set()
            if len(inputs_validated) > 1:
                raise ValueError("A cursor revalidated an admitted input")
            return self

    class Output(BaseModel):
        value: str

        @model_validator(mode="after")
        def record_output(self) -> Output:
            outputs_validated.append(self.value)
            return self

    lookup = FunctionTool(name="lookup", description="Read", input_type=Input, output_type=Output)
    responses.respond(function_call("lookup", {"key": "A", "values": ["original"]}, "call-A"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    saved = None
    async with timeout(15), agent.session() as session:
        original = await session.submit("read")
        await validated.wait()
        first = await next_request(session.interactions(), lookup)
        first.arguments.values.append("first cursor")
        second = await next_request(session.interactions(), lookup)
        assert second.arguments.values == ["original"]
        second.arguments.values.append("second cursor")
        assert first.arguments.values == ["original", "first cursor"]
        assert inputs_validated == ["A"]
        if seal:
            saved = await save_at_manual_boundary(session)
        else:
            await original.cancel()
        with pytest.raises(InteractionExpiredError, match="foreign|expired"):
            await first.respond(Output.model_construct(value="stale"))
        assert outputs_validated == []
        assert inputs_validated == ["A"]
        assert len(responses.requests) == 1
    if saved is not None:
        inputs_validated.clear()
        validated.clear()
        async with timeout(15), agent.session(state=saved) as restored:
            await validated.wait()
            request = await next_request(restored.interactions(), lookup)
            assert request.arguments.values == ["original"]
            assert inputs_validated == ["A"]
            assert len(responses.requests) == 1


@pytest.mark.parametrize("explicit_settings", [False, True])
async def test_manual_restore_keeps_saved_model_settings_and_uses_current_credentials(
    responses: ResponsesFixture, explicit_settings: bool
) -> None:
    from agents import NativeProvider

    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    original = Agent(
        instructions="Use lookup.",
        model="gpt-5.2-codex",
        model_settings={
            "reasoning_effort": "low",
            "reasoning_summary": "concise",
            "service_tier": "flex",
            "tool_choice": "required",
        }
        if explicit_settings
        else None,
        tools=[lookup],
        provider=provider(responses),
    )
    responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
    async with timeout(20):
        async with original.session() as source:
            await source.submit("read")
            original_request = await next_request(source.interactions(), lookup)
            assert original_request.call.model == responses.requests[0]["model"]
            assert original_request.call.model == "gpt-5.2-codex"
            saved_state = await source.suspend()
        assert saved_state is not None
        current = Agent(
            instructions=original.instructions,
            model="gpt-5.2",
            model_settings={
                "reasoning_effort": "high",
                "reasoning_summary": "detailed",
                "service_tier": "flex",
                "tool_choice": "none",
            },
            tools=[lookup],
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-current-restore"),
        )
        responses.respond(assistant_output("restored"))
        async with current.session(state=saved_state) as restored:
            request = await next_request(restored.interactions(), lookup)
            assert request.call.model == original_request.call.model
            assert request.call.model != current.model
            assert request.call.call_id == original_request.call.call_id == "call-A"
            assert request.call.tool == original_request.call.tool == lookup.ref
            await request.respond("A")
            assert (await restored.restored_turn.get_result()).final_output == "restored"
        assert len(responses.requests) == 2
        before, after = responses.requests
        assert before["model"] == after["model"] == "gpt-5.2-codex"
        assert before.get("reasoning") == after.get("reasoning")
        assert before.get("service_tier") == after.get("service_tier")
        assert (
            before["tool_choice"]
            == after["tool_choice"]
            == ("required" if explicit_settings else "auto")
        )
        assert responses.request_headers[0]["authorization"] == "Bearer synthetic-manual-test"
        assert responses.request_headers[1]["authorization"] == "Bearer synthetic-current-restore"
        assert current.model == "gpt-5.2"


async def test_manual_checkpoint_rebinds_qualified_callbacks_with_equal_short_names(
    responses: ResponsesFixture,
) -> None:
    from tests.test_native_function_namespaces import lookup_for, namespaced_call
    from tests.test_native_mcp_tools import declarations

    manual = FunctionTool(
        name="lookup",
        description="Read a record",
        input_type=LookupArgs,
        output_type=str,
    )
    effects: list[str] = []
    current_effects: list[str] = []
    billing, audit = (lookup_for(name, effects) for name in ("billing", "audit"))
    agent = Agent(
        instructions="Use the requested lookup.",
        tools=[manual, billing, audit],
        provider=provider(responses),
    )
    responses.respond(
        function_call("lookup", {"key": "manual"}, "manual"),
        namespaced_call("billing", "lookup", {"key": "saved"}, "saved-billing"),
    )
    async with timeout(15):
        async with agent.session(context=effects) as source:
            await source.submit("Read both")
            original = await next_request(source.interactions(), manual)
            async for event in source.events():
                if isinstance(event, ToolFinished) and event.call_id == "saved-billing":
                    break
            saved = await source.suspend()
        assert effects == ["billing:saved"]
        assert len(responses.requests) == 1
        billing, audit = (lookup_for(name, current_effects) for name in ("billing", "audit"))
        # Ordinary manual restore preserves declaration order and rebinds current callbacks.
        current = replace(agent, tools=[manual, billing, audit])
        responses.respond(
            namespaced_call("billing", "lookup", {"key": "new-billing"}, "new-billing"),
            namespaced_call("audit", "lookup", {"key": "new-audit"}, "new-audit"),
        )
        responses.respond(assistant_output("continued"))
        async with current.session(
            context=current_effects, state=SessionState.from_json(saved.to_json())
        ) as restored:
            request = await next_request(restored.interactions(), manual)
            assert (request.call_id, request.turn_id) == (
                original.call_id,
                original.turn_id,
            )
            assert len(responses.requests) == 1
            with pytest.raises((RuntimeError, ValueError), match="foreign|expired|closed|sealed"):
                await original.respond("stale")
            await request.respond("manual-answer")
            assert (await restored.restored_turn.get_result()).final_output == "continued"
        assert effects == ["billing:saved"]
        assert sorted(current_effects) == ["audit:new-audit", "billing:new-billing"]
        assert len(responses.requests) == 3
        assert tool_output(responses.requests[1], "manual") == "manual-answer"
        assert tool_output(responses.requests[1], "saved-billing") == "billing:saved"
        assert tool_output(responses.requests[2], "new-billing") == "billing:new-billing"
        assert tool_output(responses.requests[2], "new-audit") == "audit:new-audit"
        assert set(declarations(responses.requests[1])) == {
            ("functions", "lookup"),
            ("billing", "lookup"),
            ("audit", "lookup"),
        }


@pytest.mark.parametrize("selection", ["default", "manual-only", "idle-restore"])
async def test_manual_one_shot_captures_ordinary_namespaced_callback_catalog(
    responses: ResponsesFixture, selection: str
) -> None:
    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    effects: list[str] = []

    @function_tool(name="record", namespace="billing", description="Record a key")
    async def record(key: str) -> str:
        effects.append(key)
        return "recorded-" + key

    agent = Agent(
        instructions="Use the requested tools.",
        tools=[manual, record],
        provider=provider(responses),
    )
    idle = None
    async with timeout(15):
        if selection == "idle-restore":
            async with agent.session() as source:
                idle = await source.save_state()
        calls = [function_call("lookup", {"key": "A"}, "manual")]
        if selection != "manual-only":
            automatic = function_call("record", {"key": "B"}, "automatic")
            automatic["item"]["namespace"] = "billing"
            calls.append(automatic)
        responses.respond(*calls)
        with pytest.raises(InteractionRequired) as paused:
            await agent.run(
                "Read A and record B",
                state=idle,
                tools=[manual.ref] if selection == "manual-only" else None,
            )
        checkpoint = paused.value.state
        assert checkpoint is not None
        assert effects == ([] if selection == "manual-only" else ["B"])
        assert len(responses.requests) == 1

        async def answer(event):
            request = manual.match(event)
            assert request is not None and request.arguments.key == "A"
            return request.reply("manual-answer")

        responses.respond(assistant_output("continued"))
        result = await agent.run(
            None, state=SessionState.from_json(checkpoint.to_json()), on_interaction=answer
        )
        assert result.final_output == "continued"
        assert effects == ([] if selection == "manual-only" else ["B"])
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "manual") == "manual-answer"
        if selection != "manual-only":
            assert tool_output(responses.requests[1], "automatic") == "recorded-B"
