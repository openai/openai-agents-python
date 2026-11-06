"""Native goal creation, accounting and current authority in a local profile."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from agents import (
    Agent,
    CapabilityError,
    Checklist,
    CustomStorage,
    LocalStorage,
    NativeGoals,
    NativeProvider,
    SessionBusyError,
    SQLiteSessionStore,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_environment_leases_checklist import Source, trust
from tests.test_native_local_storage import _collect


async def _worker():
    mode, url, directory, identity = sys.argv[1:]

    @function_tool
    async def external_effect(key: str) -> str:
        """One real application effect that crashes before its receipt."""
        with (Path(directory) / "effects").open("a") as file:
            file.write(key + "\n")
            file.flush()
            os.fsync(file.fileno())
        os._exit(67)

    provider = NativeProvider(base_url=url, api_key="synthetic-goal-" + mode)
    agent = Agent(
        instructions="Create and update a goal only if the user requests it.",
        tools=(
            []
            if mode == "denied"
            else [NativeGoals(), external_effect]
            if mode in ("unknown", "recover-unknown")
            else [NativeGoals()]
        ),
        provider=provider,
        model="gpt-5.1-codex",
    )
    async with (
        provider,
        agent.session(
            storage=LocalStorage(Path(directory) / "profile"), resume=identity or None
        ) as session,
    ):
        print(json.dumps({"opened": session.id, "pid": os.getpid()}), flush=True)
        if mode != "denied":
            print(json.dumps({"before": await session.goal()}), flush=True)
        if mode == "inspect":
            return
        turn = await session.submit(mode + " user goal request")
        async with timeout(30):
            result = await turn.get_result()
        if mode == "denied":
            with pytest.raises(RuntimeError, match="goal storage|features.goals"):
                await session.goal()
            print(json.dumps({"output": result.final_output}), flush=True)
        else:
            print(
                json.dumps({"output": result.final_output, "goal": await session.goal()}),
                flush=True,
            )


async def _start(responses, tmp_path, mode, identity=""):
    root = str(Path(__file__).resolve().parents[1])
    pythonpath = os.pathsep.join(filter(None, (root, os.environ.get("PYTHONPATH"))))
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "tests.test_native_local_goals",
        mode,
        responses.url,
        str(tmp_path),
        identity,
        env={**os.environ, "PYTHONPATH": pythonpath},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def test_set_paused_goal_persists_without_model_requests(responses, tmp_path):
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-paused-goal")
    agent = Agent(instructions="Inspect a record.", tools=[NativeGoals()], provider=provider)
    storage = LocalStorage(tmp_path / "profile")
    async with timeout(30), provider, agent.session(storage=storage) as session:
        identity = session.id
        assert await session.goal() is None
        assert await session.checklist() is None
        created = await session.set_paused_goal("Review the record")
        assert created["threadId"] == identity
        assert created["objective"] == "Review the record" and created["status"] == "paused"
        assert "tokenBudget" not in created
        assert created["tokensUsed"] == created["timeUsedSeconds"] == 0

        updated = await session.set_paused_goal("Review the revised record", token_budget=4000)
        assert updated["threadId"] == identity and updated["status"] == "paused"
        assert (
            updated["objective"] == "Review the revised record" and updated["tokenBudget"] == 4000
        )
        assert await session.goal() == updated
        assert await session.checklist() is None
        with pytest.raises(RuntimeError, match="goal objective"):
            await session.set_paused_goal("   ")
        with pytest.raises(RuntimeError, match="budgets must be positive"):
            await session.set_paused_goal("Invalid budget", token_budget=0)
        assert await session.goal() == updated

    with pytest.raises(RuntimeError, match="Session is closed"):
        await session.set_paused_goal("Closed owner")

    reopened = await _start(responses, tmp_path, "inspect", identity)
    code, rows, err = await _collect(reopened)
    assert code == 0, (rows, err)
    assert rows[0]["opened"] == identity and rows[0]["pid"] != os.getpid()
    assert rows[1]["before"] == updated
    assert not responses.requests


async def test_set_paused_goal_requires_current_local_authority(responses, tmp_path):
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-goal-authority")
    agent = Agent(instructions="Inspect a record.", tools=[NativeGoals()], provider=provider)
    storage = LocalStorage(tmp_path / "profile")
    async with timeout(30), provider:
        async with agent.session(storage=storage) as original:
            prior = await original.set_paused_goal("Preserve the authorized goal")
        async with agent.session(storage=storage, resume=original.id, tools=[]) as denied:
            with pytest.raises(RuntimeError, match="goal storage|features.goals"):
                await denied.set_paused_goal("Historical tools cannot authorize a write")
        async with agent.session(storage=storage, resume=original.id) as current:
            with pytest.raises(RuntimeError, match="Session is closed"):
                await original.set_paused_goal("Retired owner cannot replace the goal")
            assert await current.goal() == prior

        async with agent.session(tools=[]) as ephemeral:
            with pytest.raises(CapabilityError, match="LocalStorage"):
                await ephemeral.set_paused_goal("No local profile")
        if os.name == "posix":
            custom = CustomStorage(SQLiteSessionStore(tmp_path / "canonical.sqlite"))
            async with agent.session(storage=custom, tools=[]) as unsupported:
                with pytest.raises(CapabilityError, match="LocalStorage"):
                    await unsupported.set_paused_goal("Custom storage cannot own a goal")
    assert not responses.requests


async def test_set_paused_goal_rejects_active_turn_and_its_callback(responses, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        with pytest.raises(RuntimeError, match="tool callback cannot"):
            await session.set_paused_goal("Callback cannot replace the goal")
        entered.set()
        await release.wait()
        return "released"

    provider = NativeProvider(base_url=responses.url, api_key="synthetic-goal-busy")
    agent = Agent(
        instructions="Run the requested tool.",
        model="gpt-5.1-codex",
        tools=[NativeGoals(), hold],
        provider=provider,
    )
    responses.respond(function_call("hold", {}, "held"))
    responses.respond(assistant_output("done"))
    storage = LocalStorage(tmp_path / "profile")
    async with timeout(30), provider, agent.session(storage=storage) as session:
        prior = await session.set_paused_goal("Preserve the idle goal")
        turn = await session.submit("Run the held tool.")
        try:
            await entered.wait()
            with pytest.raises(RuntimeError, match="Session is busy"):
                await session.set_paused_goal("Active turn cannot replace the goal")
            with pytest.raises(RuntimeError, match="idle, open native thread"):
                await session._native.set_paused_goal("Native root is also busy")
        finally:
            release.set()
        assert (await turn.get_result()).final_output == "done"
        assert await session.goal() == prior
    assert len(responses.requests) == 2


async def test_set_paused_goal_joins_cancellation_and_context_exit(
    responses, tmp_path, monkeypatch
):
    entered, release, close_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    native_stop = asyncio.Event()
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-goal-join")
    agent = Agent(instructions="Inspect a record.", tools=[NativeGoals()], provider=provider)
    storage = LocalStorage(tmp_path / "profile")
    async with timeout(30), provider:
        scope = agent.session(storage=storage)
        session = await scope.__aenter__()
        native = session._native
        close_session = session._close_session

        class HeldGoalWrite:
            def __getattr__(self, name):
                return getattr(native, name)

            async def set_paused_goal(self, objective, *, token_budget):
                entered.set()
                await release.wait()
                return await native.set_paused_goal(objective, token_budget=token_budget)

            def _interrupt_graph(self, mode):
                native_stop.set()
                return native._interrupt_graph(mode)

        async def observe_close():
            close_entered.set()
            await close_session()

        monkeypatch.setattr(session, "_native", HeldGoalWrite())
        monkeypatch.setattr(session, "_close_session", observe_close)
        writer = asyncio.create_task(session.set_paused_goal("Joined write", token_budget=4000))
        exiting = None
        try:
            await entered.wait()
            writer.cancel()
            await asyncio.sleep(0)
            assert not writer.done()
            with pytest.raises(SessionBusyError):
                await session.submit("Cannot start during the write")
            with pytest.raises(RuntimeError, match="Session is busy"):
                await session.set_paused_goal("Cannot replace a pending write")

            exiting = asyncio.create_task(scope.__aexit__(None, None, None))
            await close_entered.wait()
            with pytest.raises(RuntimeError, match="Session is closed"):
                await session.set_paused_goal("Cannot start while closing")
            assert not exiting.done() and not writer.done()
            assert not native_stop.is_set()
        finally:
            release.set()
            if exiting is None:
                await scope.__aexit__(None, None, None)
            else:
                await exiting
        with pytest.raises(asyncio.CancelledError):
            await writer
        async with agent.session(storage=storage, resume=session.id) as reopened:
            goal = await reopened.goal()
            assert goal is not None and goal["objective"] == "Joined write"
            assert goal["status"] == "paused" and goal["tokenBudget"] == 4000
    assert not responses.requests


async def test_set_paused_goal_keeps_prior_goal_when_database_rejects_update(responses, tmp_path):
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-goal-storage")
    agent = Agent(instructions="Inspect a record.", tools=[NativeGoals()], provider=provider)
    storage = LocalStorage(tmp_path / "profile")
    async with timeout(30), provider:
        async with agent.session(storage=storage) as session:
            prior = await session.set_paused_goal("Preserve the prior goal", token_budget=4000)
            (path,) = storage.path.glob("goals_*.sqlite")
            with closing(sqlite3.connect(path)) as database:
                database.execute(
                    "CREATE TRIGGER reject_update BEFORE UPDATE ON thread_goals "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic goal update failure'); END"
                )
            with pytest.raises(RuntimeError, match="failed to update thread goal"):
                await session.set_paused_goal("Record intent but reject the database update")
            assert await session.goal() == prior
        async with agent.session(storage=storage, resume=session.id) as reopened:
            assert await reopened.goal() == prior
    assert not responses.requests


async def test_actual_core_goal_uses_native_profile_and_survives_new_process(responses, tmp_path):
    responses.respond(
        function_call(
            "create_goal",
            {"objective": "Complete the requested record", "token_budget": 4000},
            "created",
        )
    )
    responses.respond(function_call("get_goal", {}, "inspected"))
    responses.respond(function_call("update_goal", {"status": "complete"}, "updated"))
    responses.respond(assistant_output("native-goal-done"))
    first = await _start(responses, tmp_path, "source")
    code, rows, err = await _collect(first)
    assert code == 0, (rows, err)
    identity = rows[0]["opened"]
    assert rows[1]["before"] is None
    actual = rows[2]["goal"]
    assert rows[2]["output"] == "native-goal-done"
    assert actual is not None
    assert actual["threadId"] == identity
    assert actual["objective"] == "Complete the requested record"
    assert actual["status"] == "complete" and actual["tokenBudget"] == 4000
    assert actual["tokensUsed"] >= 0 and actual["timeUsedSeconds"] >= 0
    assert "update_plan" not in json.dumps(responses.requests[0]["tools"])
    assert all(
        name in json.dumps(responses.requests[0]["tools"])
        for name in ("create_goal", "get_goal", "update_goal")
    )
    assert "Complete the requested record" in json.dumps(responses.requests[2]["input"])
    assert "complete" in json.dumps(responses.requests[3]["input"])

    responses.respond(function_call("get_goal", {}, "later-get"))
    responses.respond(assistant_output("opened-same-native-goal"))
    second = await _start(responses, tmp_path, "destination", identity)
    code, newer, err = await _collect(second)
    assert code == 0, (newer, err)
    assert newer[0]["opened"] == identity and newer[0]["pid"] != rows[0]["pid"]
    assert newer[1]["before"] == actual
    assert newer[2]["goal"] == actual
    assert "Complete the requested record" in json.dumps(responses.requests[-1]["input"])

    responses.respond(function_call("create_goal", {"objective": "not authorized"}, "forbidden"))
    responses.respond(assistant_output("no-grant"))
    denied = await _start(responses, tmp_path, "denied", identity)
    code, rejected, err = await _collect(denied)
    assert code == 0, (rejected, err)
    assert rejected[1]["output"] == "no-grant"
    assert "create_goal" not in json.dumps(responses.requests[-2]["tools"])
    responses.respond(function_call("get_goal", {}, "still-complete"))
    responses.respond(assistant_output("done"))
    fourth = await _start(responses, tmp_path, "destination", identity)
    code, current, err = await _collect(fourth)
    assert code == 0, (current, err)
    assert current[1]["before"] == actual


async def test_goals_require_local_current_authority_before_model_or_directory(responses, tmp_path):
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-unavailable")
    agent = Agent(instructions="No memory goals.", tools=[NativeGoals()], provider=provider)
    with pytest.raises(ValueError, match="requires an explicit LocalStorage"):
        async with agent.session():
            pytest.fail("ephemeral goals admitted")
    assert not responses.requests


async def test_goals_cannot_be_invented_by_submit_and_reserve_native_names(responses, tmp_path):
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-current")

    @function_tool
    async def get_goal() -> str:
        return "not a native goal"

    ordinary = Agent(instructions="Ordinary tools.", tools=[get_goal], provider=provider)
    async with ordinary.session(storage=LocalStorage(tmp_path / "ordinary")) as session:
        with pytest.raises(CapabilityError, match="captured"):
            await session.submit("invalid", tools=[NativeGoals()])
    reserved = Agent(
        instructions="Do not disguise a native goal.",
        tools=[get_goal, NativeGoals()],
        provider=provider,
    )
    with pytest.raises(ValueError, match="reserves"):
        async with reserved.session(storage=LocalStorage(tmp_path / "reserved")):
            pytest.fail("colliding goal tool admitted")
    assert not (tmp_path / "reserved").exists() and not responses.requests


@pytest.mark.parametrize(
    ("opening", "requested", "granted"),
    [
        (None, NativeGoals, False),
        (Checklist, NativeGoals, False),
        (NativeGoals, Checklist, False),
        (NativeGoals, NativeGoals, True),
    ],
)
async def test_replacement_roster_requires_each_native_opening_capability(
    responses, tmp_path, opening, requested, granted
):
    effects = []

    @function_tool
    def lookup() -> str:
        effects.append("current")
        return "current lookup"

    provider = NativeProvider(base_url=responses.url, api_key="synthetic-current")
    current = [lookup, *([opening()] if opening else [])]
    agent = Agent(
        instructions="Use current grants only.",
        model="gpt-5.1-codex",
        tools=current,
        provider=provider,
    )
    async with timeout(30), agent.session(storage=LocalStorage(tmp_path / "profile")) as session:
        if not granted:
            with pytest.raises(CapabilityError, match="captured"):
                await session.submit("invalid", tools=[lookup.ref, requested()])
            assert not responses.requests and not effects
        responses.respond(function_call("lookup", {}, "current-lookup"))
        responses.respond(assistant_output("finished"))
        assert (
            await (await session.submit("Lookup", tools=current)).get_result()
        ).final_output == "finished"
        assert effects == ["current"]
        tools = json.dumps(responses.requests[0]["tools"])
        assert ("create_goal" in tools) == (opening is NativeGoals)
        assert ("update_plan" in tools) == (opening is Checklist)
        assert "current lookup" in json.dumps(responses.requests[-1]["input"])


async def test_active_goal_does_not_replay_unknown_effect_on_profile_reopen(responses, tmp_path):
    responses.respond(
        function_call("create_goal", {"objective": "Process this request"}, "native-goal")
    )
    responses.respond(function_call("external_effect", {"key": "observed-once"}, "missing-receipt"))
    crashed = await _start(responses, tmp_path, "unknown")
    code, rows, err = await _collect(crashed)
    assert code == 67, (rows, err)
    identity = rows[0]["opened"]
    assert (tmp_path / "effects").read_text() == "observed-once\n"
    assert len(responses.requests) == 2
    # Current goal and tool permission cannot resurrect an unknown external job.
    responses.respond(assistant_output("must-not-run"))
    reopened = await _start(responses, tmp_path, "recover-unknown", identity)
    code, rejected, err = await _collect(reopened)
    assert code != 0 and "unknown tool outcome" in err, (rejected, err)
    assert len(responses.requests) == 2
    assert (tmp_path / "effects").read_text() == "observed-once\n"
    # Opening failure releases the writer but never clears the unresolved outcome.
    again = await _start(responses, tmp_path, "recover-unknown", identity)
    code, rejected, err = await _collect(again)
    assert code != 0 and "unknown tool outcome" in err, (rejected, err)
    assert len(responses.requests) == 2
    assert (tmp_path / "effects").read_text() == "observed-once\n"


async def test_goal_native_accounting_and_optional_budget(responses, tmp_path):
    responses.respond(
        function_call("create_goal", {"objective": "Observe native accounting"}, "create")
    )
    responses.respond(function_call("get_goal", {}, "get-current"))
    responses.respond(function_call("update_goal", {"status": "paused"}, "pause"))
    responses.respond(assistant_output("paused"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-accounting")
    agent = Agent(
        instructions="Only create a goal on explicit request.",
        tools=[NativeGoals()],
        provider=provider,
        model="gpt-5.1-codex",
    )
    async with timeout(35), agent.session(storage=LocalStorage(tmp_path / "profile")) as session:
        assert await session.goal() is None
        result = await (await session.submit("Create a goal and then pause it.")).get_result()
        assert result.final_output == "paused"
        value = await session.goal()
        assert value is not None
        assert value["threadId"] == session.id and value["status"] == "paused"
        assert "tokenBudget" not in value
        assert value["tokensUsed"] > 0 and value["timeUsedSeconds"] >= 0
        assert 0 < value["createdAt"] <= value["updatedAt"]
        usage = result.usage
        assert usage is not None
        # Core is the source of both accounting records, not a Python counter.
        assert value["tokensUsed"] <= usage.total_token_usage.total_tokens
    assert all(
        name in json.dumps(responses.requests[0]["tools"])
        for name in ("create_goal", "get_goal", "update_goal")
    )


async def test_native_goal_budget_status_is_reported_from_core(responses, tmp_path):
    responses.respond(
        function_call(
            "create_goal",
            {"objective": "Use a real small budget", "token_budget": 1},
            "budget-goal",
        )
    )
    responses.respond(assistant_output("Budget used."))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-budget")
    agent = Agent(
        instructions="Only manage the requested goal.",
        tools=[NativeGoals()],
        provider=provider,
        model="gpt-5.1-codex",
    )
    storage = LocalStorage(tmp_path / "budget")
    async with timeout(25), provider, agent.session(storage=storage) as session:
        value = await (await session.submit("Create a goal with token budget 1.")).get_result()
        assert value.final_output == "Budget used."
        goal = await session.goal()
        assert goal is not None and goal["tokenBudget"] == 1
        assert goal["tokensUsed"] >= goal["tokenBudget"]
        assert goal["status"] == "budgetLimited"
        (path,) = storage.path.glob("goals_*.sqlite")
        with closing(sqlite3.connect(path)) as database:
            # The public ThreadGoal exposes the thread ID. The database also
            # records a separate native goal ID for updates and accounting.
            query = "SELECT goal_id FROM thread_goals WHERE thread_id = ?"
            original_id = database.execute(query, (session.id,)).fetchone()
            assert original_id is not None
            updated = await session.set_paused_goal("Revised objective at the same budget")
            assert database.execute(query, (session.id,)).fetchone() == original_id
        assert updated["objective"] == "Revised objective at the same budget"
        assert updated["status"] == "budgetLimited"
        for key in ("threadId", "tokenBudget", "tokensUsed", "timeUsedSeconds", "createdAt"):
            assert updated[key] == goal[key]
        assert await session.goal() == updated
    assert len(responses.requests) == 2


async def test_cancelled_sample_cannot_update_successor_goal(responses, tmp_path):
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def before_event(event):
        if event.get("item", {}).get("call_id") == "retired-complete":
            blocked.set()
            await release.wait()

    responses.before_event = before_event
    responses.respond(
        function_call("create_goal", {"objective": "Maintain the current goal"}, "create")
    )
    responses.respond(function_call("update_goal", {"status": "complete"}, "retired-complete"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-cancel")
    agent = Agent(
        instructions="Only manage requested goals.",
        tools=[NativeGoals()],
        provider=provider,
        model="gpt-5.1-codex",
    )
    try:
        async with (
            timeout(35),
            agent.session(storage=LocalStorage(tmp_path / "profile")) as session,
        ):
            prior = await session.submit("Create a goal.")
            await blocked.wait()
            assert "Maintain the current goal" in json.dumps(responses.requests[-1]["input"])
            cancelled = asyncio.create_task(prior.cancel())
            while session._active is not None and not session._active.stopping:
                await asyncio.sleep(0)
            release.set()
            await cancelled
            cancelled_goal = await session.goal()
            assert cancelled_goal is not None and cancelled_goal["status"] == "active"
            responses.respond(function_call("update_goal", {"status": "paused"}, "successor-pause"))
            responses.respond(assistant_output("successor-done"))
            newer = await session.submit("Please pause the goal.")
            result = await newer.get_result()
            assert result.final_output == "successor-done"
            release.set()
            # Calling an already retired exact handle cannot cancel or mutate
            # the new turn even if its old provider frame arrives late.
            await prior.cancel()
            current = await session.goal()
            assert current is not None and current["status"] == "paused"
            assert current["objective"] == "Maintain the current goal"
            items = [item async for item in session.items()]
            stale = [
                item
                for item in items
                if item.get("call_id") == "retired-complete"
                and item["type"] == "function_call_output"
            ]
            # Core may retain the aborted pair if it sees a frame while the
            # interrupt is being joined. It must never retain a successful
            # stale effect or misreport it as a result of the successor.
            assert not stale or all(
                str(item.get("output", "")).startswith("aborted by user") for item in stale
            )
    finally:
        release.set()


async def test_idle_active_goal_reopens_without_starting_a_hidden_native_turn(responses, tmp_path):
    responses.respond(function_call("create_goal", {"objective": "Keep until requested"}, "create"))
    responses.respond(assistant_output("active goal remains"))
    first = await _start(responses, tmp_path, "source")
    code, source, err = await _collect(first)
    assert code == 0, (source, err)
    goal = source[2]["goal"]
    assert goal is not None and goal["status"] == "active"
    assert len(responses.requests) == 2
    identity = source[0]["opened"]
    # Reopen in another process without queuing any provider response. Native
    # goal restoration may load state, but only an SDK caller starts a root.
    inspecting = await _start(responses, tmp_path, "inspect", identity)
    code, inspected, err = await _collect(inspecting)
    assert code == 0, (inspected, err)
    assert inspected[0]["opened"] == identity
    assert inspected[0]["pid"] != source[0]["pid"]
    assert inspected[1]["before"]["objective"] == goal["objective"]
    assert inspected[1]["before"]["status"] == "active"
    assert len(responses.requests) == 2
    responses.respond(function_call("update_goal", {"status": "complete"}, "finish"))
    responses.respond(assistant_output("explicit finish"))
    last = await _start(responses, tmp_path, "destination", identity)
    code, restored, err = await _collect(last)
    assert code == 0, (restored, err)
    assert restored[1]["before"]["status"] == "active"
    assert restored[2]["goal"]["status"] == "complete"
    assert restored[2]["goal"]["objective"] == goal["objective"]
    assert len(responses.requests) == 4


async def test_local_goal_with_current_environment_lease_cancel_and_reopen(
    responses, tmp_path, monkeypatch
):
    blocked, release = asyncio.Event(), asyncio.Event()

    async def hold_late_goal_update(event):
        if event.get("item", {}).get("call_id") == "retired-remote-complete":
            blocked.set()
            await release.wait()

    responses.before_event = hold_late_goal_update
    shared = NativeProvider(base_url=responses.url, api_key="synthetic-leased-goal")
    profile = LocalStorage(tmp_path / "local-profile")
    agent = Agent(
        instructions="Manage only the requested native goal.",
        tools=[NativeGoals()],
        provider=shared,
        model="gpt-5.1-codex",
    )
    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = Source(peer)
        try:
            responses.respond(
                function_call("create_goal", {"objective": "Remote work, local goal"}, "create")
            )
            responses.respond(
                function_call("update_goal", {"status": "complete"}, "retired-remote-complete")
            )
            async with agent.session(storage=profile, environment=source.lease()) as session:
                identity = session.id
                first = await session.submit("Create this goal.")
                await blocked.wait()
                assert (source.opened, source.released) == (1, 0)
                assert peer.transports
                with pytest.raises(RuntimeError, match="open or opening"):
                    await shared.aclose()
                cancelling = asyncio.create_task(first.cancel())
                while session._active is not None and not session._active.stopping:
                    await asyncio.sleep(0)
                release.set()
                await cancelling
                value = await session.goal()
                assert value is not None and value["status"] == "active"
                assert value["objective"] == "Remote work, local goal"
                assert "create_goal" in json.dumps(responses.requests[0]["tools"])
                # Cancelling a root does not release the native Session or its lease.
                assert (source.opened, source.released) == (1, 0)
            assert (source.opened, source.released) == (1, 1)
            await peer.wait_disconnected()

            # Reopening the same local native ID acquires a fresh lease and the
            # current Agent's grants. History must not supply permission.
            responses.respond(function_call("update_goal", {"status": "complete"}, "no-permission"))
            responses.respond(assistant_output("denied in destination"))
            denied = replace(agent, tools=[])
            begin = len(responses.requests)
            async with denied.session(
                storage=profile, resume=identity, environment=source.lease()
            ) as resumed:
                assert resumed.id == identity and (source.opened, source.released) == (2, 1)
                result = await (
                    await resumed.submit("Attempt an unauthorized goal update")
                ).get_result()
                assert result.final_output == "denied in destination"
                assert "update_goal" not in json.dumps(responses.requests[begin]["tools"])
                with pytest.raises(RuntimeError, match="goal storage|features.goals"):
                    await resumed.goal()
            assert (source.opened, source.released) == (2, 2)

            async with agent.session(
                storage=profile, resume=identity, environment=source.lease()
            ) as current:
                value = await current.goal()
                assert value is not None and value["status"] == "active"
                assert value["objective"] == "Remote work, local goal"
                responses.respond(function_call("update_goal", {"status": "paused"}, "current"))
                responses.respond(assistant_output("paused by current owner"))
                assert (
                    await (await current.submit("Pause this goal.")).get_result()
                ).final_output == "paused by current owner"
                assert (await current.goal())["status"] == "paused"
            assert (source.opened, source.released) == (3, 3)
            await peer.wait_disconnected()
            assert not responses.replies
            await shared.aclose()
        finally:
            release.set()


if __name__ == "__main__":
    asyncio.run(_worker())
