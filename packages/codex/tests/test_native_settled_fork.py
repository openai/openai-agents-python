"""Settled native forks preserve conversation, not live application authority."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from agents import (
    Agent,
    FunctionTool,
    HttpMCP,
    LocalStorage,
    NativeError,
    NativeProvider,
    SessionBusyError,
    ToolContext,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.native_planning_fixtures import legacy_checklist, legacy_goal
from tests.test_native_child_agents import agent_for, child_output, child_peer, handles, trust
from tests.test_native_code_mode import declared_names
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request, provider
from tests.test_native_mcp_tools import Peer, mcp_call


async def test_fork_restores_native_history_and_rebinds_independent_destination(responses):
    effects: list[str] = []

    @function_tool
    def record(context: ToolContext[str], key: str) -> str:
        """Record an application effect."""
        result = context.context + ":" + key
        effects.append(result)
        return result

    responses.respond(function_call("record", {"key": "receipt"}, "call-original"))
    responses.respond(assistant_output("original-result"))
    source = Agent(
        instructions="Preserve this instruction.", tools=[record], provider=provider(responses)
    )
    async with timeout(35), source.session(context="source") as original:
        source_id = original.id
        turn = await original.submit("Do one job.")
        completed = await turn.get_result()
        assert completed.final_output == "original-result"
        assert completed.usage is not None
        original_usage = completed.usage.total_token_usage.total_tokens
        forked = await original.fork()
        assert forked.source_session_id != source_id
        assert effects == ["source:receipt"]
        assert len(responses.requests) == 2
        # Forking does not seal or retire the source or copy its later turns.
        responses.respond(assistant_output("private-original-result"))
        assert (
            await (await original.submit("source-only-future")).get_result()
        ).final_output == "private-original-result"
    # Close of the source Engine must not close the restored owner.
    new_provider = NativeProvider(base_url=responses.url, api_key="synthetic-fork-destination")
    destination = Agent(
        instructions="Preserve this instruction.", tools=[record], provider=new_provider
    )
    responses.respond(function_call("record", {"key": "new"}, "call-destination"))
    responses.respond(assistant_output("destination-result"))
    async with timeout(35), new_provider, destination.session(state=forked, context="new") as child:
        assert child.id != source_id
        inherited = [value async for value in child.turns()]
        [previous] = [value for value in inherited if value["id"] == turn.turn_id]
        assert previous["status"] == "completed" and previous["session_id"] == child.id
        result = await (await child.submit("Do another job.")).get_result()
        assert result.final_output == "destination-result"
        assert result.usage is not None
        assert result.usage.total_token_usage.total_tokens > original_usage
        items = [item async for item in child.items()]
    assert effects == ["source:receipt", "new:new"]
    assert len(responses.requests) == 5
    history = responses.requests[3]["input"]
    calls = [item for item in history if item.get("type") == "function_call"]
    receipts = [item for item in history if item.get("type") == "function_call_output"]
    assert [call["call_id"] for call in calls] == ["call-original"]
    assert [reply["call_id"] for reply in receipts] == ["call-original"]
    assert "source:receipt" in json.dumps(receipts)
    assert "original-result" in json.dumps(history)
    assert "source-only-future" not in json.dumps(history)
    assert "Preserve this instruction." in json.dumps(responses.requests[3])
    assert responses.request_headers[3]["authorization"] == "Bearer synthetic-fork-destination"
    assert {item["call_id"] for item in items if item["type"] == "function_call_output"} == {
        "call-original",
        "call-destination",
    }


async def test_fork_keeps_current_model_and_never_reconnects_source_mcp(responses):
    source_peer = Peer(names=("search",), label="source receipt")
    destination_peer = Peer(names=("search",), label="destination receipt")
    async with timeout(45), source_peer.serve() as source_url, destination_peer.serve() as dest_url:
        source = Agent(
            instructions="Read current records.",
            model="gpt-5.5",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-source"),
            tools=[HttpMCP(name="records", url=source_url, tools=["search"])],
        )
        async with source.session() as original:
            await original.update(
                model="gpt-5.1-codex",
                model_settings={"reasoning_effort": "low", "service_tier": "fast"},
            )
            responses.respond(mcp_call("search", "mcp__records", "source-call"))
            responses.respond(assistant_output("source result"))
            assert (
                await (await original.submit("read")).get_result()
            ).final_output == "source result"
            before = len(source_peer.requests)
            forked = await original.fork()
            assert len(source_peer.requests) == before
            assert len(source_peer.calls) == 1
            assert forked.source_session_id != original.id
            # The source is still live, on its accepted current model.
            responses.respond(assistant_output("source still live"))
            assert (await (await original.submit("continue")).get_result()).final_output == (
                "source still live"
            )
            assert responses.requests[-1]["model"] == "gpt-5.1-codex"
            before = len(source_peer.requests)
            destination = Agent(
                instructions="Read current records.",
                model="gpt-5.5",
                provider=NativeProvider(base_url=responses.url, api_key="synthetic-destination"),
                tools=[HttpMCP(name="records", url=dest_url, tools=["search"])],
            )
            responses.respond(mcp_call("search", "mcp__records", "dest-call"))
            responses.respond(assistant_output("destination result"))
            async with destination.session(state=forked) as resumed:
                result = await (await resumed.submit("use current records")).get_result()
                assert result.final_output == "destination result"
                assert resumed.id != original.id
            assert len(source_peer.requests) == before
            assert len(source_peer.calls) == len(destination_peer.calls) == 1
            assert any(r.get("method") == "tools/list" for r in destination_peer.requests)
            for request, headers in zip(
                responses.requests[-2:], responses.request_headers[-2:], strict=True
            ):
                assert request["model"] == "gpt-5.1-codex"
                assert request["reasoning"]["effort"] == "low"
                assert headers["authorization"] == "Bearer synthetic-destination"
            assert "source receipt" in json.dumps(responses.requests[-2]["input"])
            assert "source still live" not in json.dumps(responses.requests[-2]["input"])


async def test_fork_does_not_grant_saved_tools_to_destination(responses):
    effects: list[str] = []

    @function_tool
    def record(key: str) -> str:
        effects.append(key)
        return key

    original = Agent(instructions="Read.", tools=[record], provider=provider(responses))
    responses.respond(function_call("record", {"key": "known"}, "effect-once"))
    responses.respond(assistant_output("done"))
    async with timeout(35), original.session() as source:
        assert (await (await source.submit("Read.")).get_result()).final_output == "done"
        forked = await source.fork()
    # Restores still require the fixed native tool declaration. No previous
    # callback or grant fills in a missing current declaration at admission.
    responses.respond(function_call("record", {"key": "forbidden"}, "old-grant"))
    responses.respond(assistant_output("not-granted"))
    no_tools = Agent(instructions="Read.", tools=[], provider=provider(responses))
    with pytest.raises(RuntimeError, match="current managed specs must match"):
        async with timeout(35), no_tools.session(state=forked):
            pytest.fail("Restored source grant replaced missing current binding")
    assert effects == ["known"]
    assert len(responses.requests) == 2


@pytest.mark.parametrize("status", ["paused", "complete"])
async def test_fork_copies_retained_goal_and_checklist_without_granting_tools(responses, status):
    agent = Agent(instructions="Preserve recorded work.", provider=provider(responses))
    responses.respond(assistant_output("original result"))
    async with timeout(45), agent.session() as first:
        turn = await first.submit("Record the work.")
        assert (await turn.get_result()).final_output == "original result"
        raw = json.loads((await first.save_state()).to_json())
        raw["root_checklist"] = legacy_checklist(first.id, turn.turn_id)
        raw["root_goal"] = legacy_goal(
            first.id,
            status=status,
            token_budget=2100,
            tokens_used=850,
            time_used_seconds=72,
            created_at=1700000000,
            updated_at=1700000072,
        )
    saved = SessionState.from_json(json.dumps(raw))
    async with timeout(45), agent.session(state=saved) as source:
        before = await source.save_state()
        forked = await source.fork()
        assert forked.source_session_id != source.id
        assert forked.root_goal is not None and before.root_goal is not None
        assert forked.root_checklist is not None and before.root_checklist is not None
        assert forked.root_goal.thread_id == forked.source_session_id
        assert forked.root_goal.model_dump(exclude={"thread_id"}) == before.root_goal.model_dump(
            exclude={"thread_id"}
        )
        assert forked.root_checklist.thread_id == forked.source_session_id
        assert forked.root_checklist.model_dump(
            exclude={"thread_id"}
        ) == before.root_checklist.model_dump(exclude={"thread_id"})
        assert len(responses.requests) == 1
        after = await source.save_state()
        assert (after.root_goal, after.root_checklist) == (before.root_goal, before.root_checklist)
        async with agent.session(state=forked) as destination:
            current = await destination.save_state()
            assert (current.root_goal, current.root_checklist) == (
                forked.root_goal,
                forked.root_checklist,
            )
            responses.respond(assistant_output("independent continuation"))
            assert (
                await (await destination.submit("Continue from recorded work")).get_result()
            ).final_output == "independent continuation"
            assert declared_names(responses.requests[-1]).isdisjoint(
                {"update_plan", "create_goal", "get_goal", "update_goal"}
            )
            with pytest.raises(RuntimeError, match="goal storage|features.goals"):
                await destination.goal()


async def test_fork_refuses_active_manual_boundary_without_invalidating_receipt(responses):
    manual = FunctionTool(
        name="lookup", description="Read a key", input_type=LookupArgs, output_type=str
    )
    original = Agent(instructions="Read.", tools=[manual], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "held"}, "same-receipt"))
    responses.respond(assistant_output("source-completed"))
    async with timeout(35), original.session() as source:
        turn = await source.submit("Read.")
        request = await next_request(source.interactions(), manual)
        with pytest.raises(RuntimeError, match="idle"):
            await source.fork()
        assert (await source.pending_interactions())[0].call_id == "same-receipt"
        await request.respond("only-once")
        assert (await turn.get_result()).final_output == "source-completed"
        forked = await source.fork()
        assert forked.source_session_id != source.id


async def test_fork_does_not_export_native_local_profile(responses, tmp_path):
    original = Agent(instructions="Read.", provider=provider(responses))
    responses.respond(assistant_output("source-live"))
    async with timeout(35), original.session(storage=LocalStorage(tmp_path)) as source:
        with pytest.raises(ValueError, match="cannot export portable"):
            await source.fork()
        assert (
            await (await source.submit("Still live")).get_result()
        ).final_output == "source-live"


async def test_fork_destination_cancel_all_cannot_retire_active_source(responses):
    entered, release = asyncio.Event(), asyncio.Event()
    agent = Agent(instructions="Keep the owners separate.", provider=provider(responses))
    held = assistant_output("source completed after destination closed")
    held["item"]["id"] = "held-fork-original"

    async def hold(event):
        if event.get("item", {}).get("id") == "held-fork-original":
            entered.set()
            await release.wait()

    responses.before_event = hold
    async with timeout(40), agent.session() as source:
        responses.respond(assistant_output("shared settled history"))
        original = await source.submit("Record this once.")
        assert (await original.get_result()).final_output == "shared settled history"
        state = await source.fork()
        responses.respond(held)
        active = await source.submit("Source only active turn.")
        try:
            await entered.wait()
            async with agent.session(state=state) as destination:
                await destination.cancel_all(mode="force")
                assert source.active_turn is active
                assert destination.active_turn is None
                assert "Source only active turn." not in state.to_json()
            assert source.active_turn is active
        finally:
            release.set()
        assert (
            await active.get_result()
        ).final_output == "source completed after destination closed"
        assert (await original.get_result()).final_output == "shared settled history"
        assert len(responses.requests) == 2


async def test_cancelled_fork_joins_real_copy_without_closing_source_or_successor(
    responses, monkeypatch
):
    entered, release, retired = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = Agent(instructions="Keep native history.", provider=provider(responses))
    responses.respond(assistant_output("settled"))
    async with timeout(35), original.session() as source:
        assert (await (await source.submit("First")).get_result()).final_output == "settled"
        actual = source._engine

        class HeldEngine:
            def __getattr__(self, name: str) -> Any:
                return getattr(actual, name)

            async def fork_thread(self, *args: Any, **kwargs: Any) -> Any:
                copied = await actual.fork_thread(*args, **kwargs)

                class HeldCopy:
                    async def _export_checkpoint(self) -> str:
                        saved = await copied._export_checkpoint()
                        entered.set()
                        await release.wait()
                        return saved

                    async def close(self, **options: Any) -> None:
                        await copied.close(**options)
                        retired.set()

                return HeldCopy()

        monkeypatch.setattr(source, "_engine", HeldEngine())
        saving = asyncio.create_task(source.fork())
        await entered.wait()
        try:
            saving.cancel("stop the copy")
            await asyncio.sleep(0)
            saving.cancel("cancel again during native export")
            assert not saving.done() and not retired.is_set()
            with pytest.raises(SessionBusyError):
                await source.submit("must not race native fork")
            assert len(responses.requests) == 1
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await saving
        assert retired.is_set()
        responses.respond(assistant_output("next root remains live"))
        next_turn = await source.submit("After cancelled copy")
        assert (await next_turn.get_result()).final_output == "next root remains live"
        assert "must not race native fork" not in json.dumps(responses.requests[-1])
        assert (await source.save_state()).source_session_id == source.id


async def test_fork_rejects_active_child_without_sealing_root_or_child(tmp_path, monkeypatch):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, handles, metadata, trust
    from tests.test_native_executor_tools import declaration

    reached, release = asyncio.Event(), asyncio.Event()
    async with (
        timeout(45),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        ordinary = peer.response

        def response():
            default = ordinary()
            if metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"] == "/root":
                return default
            answer = assistant_output("child settled without replay")
            answer["item"]["id"] = "fork-held-child"
            return [answer]

        async def hold(event):
            if event.get("item", {}).get("id") == "fork-held-child":
                reached.set()
                await release.wait()

        monkeypatch.setattr(peer, "response", response)
        peer.before_event = hold
        try:
            async with agent_for(peer, executor).session(
                environment=declaration(executor)
            ) as session:
                root = await session.submit("Start one child")
                assert (await root.get_result()).final_output == "root complete"
                await reached.wait()
                await handles(session, 1)
                [child] = [value async for value in session.subagents()]
                turn = child.turn
                assert turn is not None
                before = len(peer.requests)
                with pytest.raises(RuntimeError, match="child work|callbacks|active"):
                    await session.fork()
                assert len(peer.requests) == before
                release.set()
                assert (await turn.get_result()).final_output == "child settled without replay"
                # A settled child's history is part of a portable save, but
                # the native fork cannot copy its graph. Never return a
                # silently incomplete copy or retire the real child on refusal.
                saved = await session.save_state()
                assert any(record.thread_id == child.id for record in saved.descendants)
                child_items = [item async for item in session.items(subagent_id=child.id)]
                assert child_items
                before = len(peer.requests)
                with pytest.raises(RuntimeError, match="fork.*descendant"):
                    await session.fork()
                assert len(peer.requests) == before
                after = await session.save_state()
                assert [
                    (c.thread_id, c.parent_thread_id, c.status, c.closed_at, c.checklist, c.goal)
                    for c in after.descendants
                ] == [
                    (c.thread_id, c.parent_thread_id, c.status, c.closed_at, c.checklist, c.goal)
                    for c in saved.descendants
                ]
                assert [item async for item in session.items(subagent_id=child.id)] == child_items
                continuation = await child.start("continue the same settled child")
                assert (
                    await continuation.get_result()
                ).final_output == "child settled without replay"
                assert (
                    await (await session.submit("continue parent only")).get_result()
                ).final_output == "root complete"
                assert len(peer.child_requests["alpha"]) == 2
        finally:
            release.set()


@pytest.mark.requires_native_linux_sandbox
async def test_fork_refuses_direct_root_terminal_until_source_retires_process(
    responses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        responses.respond(
            function_call(
                "exec_command",
                {
                    "cmd": "read -r first; printf 'received:%s\\n' \"$first\"; "
                    "read -r last; printf 'finished:%s\\n' \"$last\"",
                    "tty": True,
                    "yield_time_ms": 1000,
                },
                "start-background",
            )
        )
        responses.respond(assistant_output("root settled"))
        agent = Agent(instructions="Use selected tools.", provider=provider(responses))
        async with agent.session(environment=declaration(executor)) as source:
            assert (
                await (await source.submit("Start a process")).get_result()
            ).final_output == "root settled"
            output = tool_output(responses.requests[-1], "start-background")
            match = re.search(r"Process running with session ID (\d+)", output)
            assert match, output
            process_id = int(match[1])
            assert source.active_turn is None
            assert any(request.get("method") == "process/start" for request in executor.requests)
            assert not any(
                request.get("method") == "process/terminate" for request in executor.requests
            )
            with pytest.raises(RuntimeError, match="fork.*background terminal"):
                await source.fork()
            assert not any(
                request.get("method") == "process/terminate" for request in executor.requests
            )
            responses.respond(
                function_call(
                    "write_stdin",
                    {"session_id": process_id, "chars": "first\n", "yield_time_ms": 1000},
                    "still-owned",
                )
            )
            responses.respond(assistant_output("source controls process"))
            assert (
                await (await source.submit("Continue our process")).get_result()
            ).final_output == ("source controls process")
            alive = tool_output(responses.requests[-1], "still-owned")
            assert "received:first" in alive
            assert "Process running with session ID" in alive
            responses.respond(
                function_call(
                    "write_stdin",
                    {"session_id": process_id, "chars": "last\n", "yield_time_ms": 1000},
                    "retire-background",
                )
            )
            responses.respond(assistant_output("retired"))
            assert (
                await (await source.submit("Finish our process")).get_result()
            ).final_output == "retired"
            retired = tool_output(responses.requests[-1], "retire-background")
            assert "finished:last" in retired and "Process exited with code 0" in retired
            forked = await source.fork()
            assert forked.source_session_id != source.id


@pytest.mark.requires_native_linux_sandbox
async def test_fork_refuses_idle_child_terminal_until_current_environment_retires_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(40),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.command = (
            "while [ ! -f release-child ]; do sleep 0.05; done; "
            "printf 'child finished' > child-retired"
        )
        peer.release["alpha"].set()
        async with agent_for(peer, executor).session(environment=declaration(executor)) as source:
            assert (
                await (await source.submit("Start one child process")).get_result()
            ).final_output == ("root complete")
            child = (await handles(source, 1))[0]
            assert await child_output(source, child) == "alpha complete"
            assert "Process running with session ID" in tool_output(
                peer.child_requests["alpha"][-1], "shared-call-id"
            )
            assert source.active_turn is None
            before = len(executor.requests)
            model_requests = (len(peer.requests), len(peer.child_requests["alpha"]))
            with pytest.raises(
                RuntimeError, match="^fork cannot copy conversations with native descendants$"
            ):
                await source.fork()
            assert not any(
                request.get("method") in {"process/start", "process/terminate", "process/write"}
                for request in executor.requests[before:]
            )
            assert (len(peer.requests), len(peer.child_requests["alpha"])) == model_requests
            # The settled source still controls the same current environment.
            peer.action = function_call(
                "exec_command",
                {
                    "cmd": "printf release > release-child; "
                    "while [ ! -f child-retired ]; do sleep 0.05; done; cat child-retired",
                    "yield_time_ms": 1000,
                },
                "release-child",
            )
            assert (
                await (await source.submit("Retire the child's process")).get_result()
            ).final_output == ("root complete")
            output = tool_output(peer.requests[-1], "release-child")
            output_parts = [output]
            for attempt in range(10):
                running = re.search(r"Process running with session ID (\d+)", output)
                if running is None:
                    break
                # Poll the release command; do not start it again.
                call_id = f"poll-release-child-{attempt}"
                peer.action = function_call(
                    "write_stdin",
                    {"session_id": int(running[1]), "chars": "", "yield_time_ms": 1000},
                    call_id,
                )
                assert (
                    await (await source.submit("Wait for the release command")).get_result()
                ).final_output == "root complete"
                output = tool_output(peer.requests[-1], call_id)
                output_parts.append(output)
            assert "child finished" in "".join(output_parts)
            assert "Process exited with code 0" in output
            # Retiring the process does not make a descendant graph copyable.
            before = len(executor.requests)
            model_requests = (len(peer.requests), len(peer.child_requests["alpha"]))
            with pytest.raises(
                RuntimeError, match="^fork cannot copy conversations with native descendants$"
            ):
                await source.fork()
            assert not any(
                request.get("method") in {"process/start", "process/terminate", "process/write"}
                for request in executor.requests[before:]
            )
            assert (len(peer.requests), len(peer.child_requests["alpha"])) == model_requests
            saved = await source.save_state()
            assert saved.source_session_id == source.id
            [saved_child] = [row for row in saved.descendants if row.thread_id == child.child_id]
            assert saved_child.parent_thread_id == source.id


@pytest.mark.parametrize(
    "outcome", ["success", "export_failure", "cancel", "export_and_close_failure"]
)
async def test_fork_retires_only_its_temporary_store_record(responses, monkeypatch, outcome):
    reached, release, retired = (asyncio.Event() for _ in range(3))
    ids = []
    raw_results = []
    original_error = NativeError("synthetic export error")
    cleanup_error = NativeError("synthetic cleanup error after retiring copy")
    agent = Agent(
        instructions="Retain conversation with exact owner.", provider=provider(responses)
    )
    responses.respond(assistant_output("unique shared history before copy"))
    async with timeout(16), agent.session() as source:
        turn = await source.submit("synthetic historical prompt")
        assert (await turn.get_result()).final_output == "unique shared history before copy"
        actual = source._engine

        class HeldEngine:
            def __getattr__(self, name: str) -> Any:
                return getattr(actual, name)

            async def fork_thread(self, *args, **kwargs):
                copied = await actual.fork_thread(*args, **kwargs)
                ids.append(copied.id)

                class HeldCopy:
                    def __getattr__(self, name: str) -> Any:
                        return getattr(copied, name)

                    async def _export_checkpoint(self):
                        raw = await copied._export_checkpoint()
                        raw_results.append(raw)
                        reached.set()
                        if outcome == "cancel":
                            await release.wait()
                        if outcome in {"export_failure", "export_and_close_failure"}:
                            raise original_error
                        return raw

                    async def close(self, **options):
                        await copied.close(**options)
                        retired.set()
                        if outcome == "export_and_close_failure":
                            raise cleanup_error

                return HeldCopy()

        monkeypatch.setattr(source, "_engine", HeldEngine())
        # Establish the pinned store's actual missing-record response without
        # touching any existing thread. resume_thread reads history first.
        missing_id = str(uuid.uuid4())
        with pytest.raises(RuntimeError) as absent:
            await actual.resume_thread(missing_id)
        saving = asyncio.create_task(source.fork())
        if outcome == "cancel":
            try:
                await reached.wait()
                saving.cancel("stop this copy")
                await asyncio.sleep(0)
                assert not saving.done() and not retired.is_set()
                with pytest.raises(SessionBusyError, match="changing or saving"):
                    await source.submit("must not race")
            finally:
                release.set()
                await asyncio.gather(saving, return_exceptions=True)
            with pytest.raises(asyncio.CancelledError):
                await saving
        elif outcome in {"export_failure", "export_and_close_failure"}:
            with pytest.raises(NativeError) as failed:
                await saving
            assert failed.value is original_error
            if outcome == "export_and_close_failure":
                assert failed.value.__context__ is cleanup_error
        else:
            saved = await saving
            assert saved.source_session_id == ids[-1] and saved.source_session_id != source.id
            assert "unique shared history before copy" in saved.to_json()

        assert reached.is_set() and retired.is_set() and len(ids) == 1
        assert "unique shared history before copy" in raw_results[0]
        assert len(responses.requests) == 1
        responses.respond(assistant_output("original stays usable"))
        assert (
            await (await source.submit("next source input")).get_result()
        ).final_output == "original stays usable"
        assert (await source.save_state()).source_session_id == source.id
        assert len(responses.requests) == 2 and "must not race" not in json.dumps(
            responses.requests[-1]
        )
        # Read the pinned store after joining close, before the source engine
        # exits. An unchanged managed-record error proves history was loaded.
        with pytest.raises(RuntimeError) as after:
            await actual.resume_thread(ids[-1])
        # Both are genuinely missing IDs, so normalize the random identifier.
        want = str(absent.value)
        got = str(after.value)
        assert "managed thread" not in got, (
            f"retained fork history: {got}; missing-record control: {want}"
        )
        assert type(after.value) is type(absent.value)
        assert got.replace(ids[-1], "<id>") == want.replace(missing_id, "<id>"), (got, want)
