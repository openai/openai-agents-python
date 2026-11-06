"""Legacy planning checkpoint data without model-facing planning capabilities."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import ValidationError

from agents import Agent, NativeProvider, function_tool
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.native_planning_fixtures import legacy_checklist, legacy_goal
from tests.test_native_child_agents import ChildPeer, agent_for, handles, metadata, trust
from tests.test_native_code_mode import declared_names
from tests.test_native_executor_tools import declaration


def provider(responses: ResponsesFixture, key: str) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key=key, supports_websockets=False)


async def test_python_update_plan_remains_usable_after_tool_update(
    responses: ResponsesFixture,
) -> None:
    calls: list[str] = []

    @function_tool
    async def update_plan(text: str) -> str:
        calls.append(text)
        return "Application plan: " + text

    connection = provider(responses, "python-plan-key")
    agent = Agent(
        instructions="Use the application plan.", tools=[update_plan], provider=connection
    )
    responses.respond(function_call("update_plan", {"text": "Read the report"}, "python-plan"))
    responses.respond(assistant_output("Application answered."))
    async with timeout(30), agent.session() as session:
        turn = await session.submit("Make a plan.", tools=[update_plan])
        assert (await turn.get_result()).final_output == "Application answered."
        assert calls == ["Read the report"]
        assert "Application plan: Read the report" in json.dumps(responses.requests[1]["input"])
        assert (await session.save_state()).root_checklist is None
    await connection.aclose()


@pytest.mark.parametrize(
    "include_goal", [False, True], ids=["checklist-only", "checklist-and-goal"]
)
async def test_legacy_records_restore_without_exposing_planning_tools(
    responses: ResponsesFixture, include_goal: bool
) -> None:
    first_provider = provider(responses, "planning-initial-key")
    original = Agent(instructions="Review work.", provider=first_provider)
    responses.respond(assistant_output("Review complete."))
    async with timeout(45), original.session() as session:
        empty = await session.save_state()
        assert empty.root_checklist is None and empty.root_goal is None
        handle = await session.submit("Review it once.")
        assert (await handle.get_result()).final_output == "Review complete."
        raw = json.loads((await session.save_state()).to_json())
        raw["root_checklist"] = legacy_checklist(session.id, handle.turn_id)
        if include_goal:
            raw["root_goal"] = legacy_goal(session.id)
    state = SessionState.from_json(json.dumps(raw))
    assert "planning-initial-key" not in state.to_json()
    current_provider = provider(responses, "planning-restore-key")
    current = Agent(instructions=original.instructions, provider=current_provider)
    async with timeout(45), current.session(state=state) as session:
        again = await session.save_state()
        assert again.root_checklist == state.root_checklist
        assert again.root_goal == state.root_goal
        assert len(responses.requests) == 1, "Restore must not start model work"
        responses.respond(assistant_output("No planner was enabled."))
        turn = await session.submit("Continue.")
        assert (await turn.get_result()).final_output == "No planner was enabled."
        assert (await session.save_state()).root_checklist == state.root_checklist
    for request in responses.requests:
        assert declared_names(request).isdisjoint(
            {"update_plan", "create_goal", "get_goal", "update_goal"}
        )
    assert responses.request_headers[-1]["authorization"] == "Bearer planning-restore-key"
    await first_provider.aclose()
    await current_provider.aclose()


async def test_failed_goal_validation_allows_corrected_restore(
    responses: ResponsesFixture,
) -> None:
    connection = provider(responses, "planning-corrected-key")
    agent = Agent(instructions="Plan work.", provider=connection)
    async with timeout(45), agent.session() as session:
        raw = json.loads((await session.save_state()).to_json())
        raw["root_goal"] = legacy_goal(session.id, objective="Finish the appendix")
    saved = SessionState.from_json(json.dumps(raw))
    altered = json.loads(saved.to_json())
    altered["root_goal"]["thread_id"] = "10000000-0000-0000-0000-000000000001"
    with pytest.raises(ValidationError, match="goal thread identity"):
        SessionState.from_json(json.dumps(altered))
    altered = json.loads(saved.to_json())
    altered["root_goal"]["status"] = "active"
    with pytest.raises(ValidationError):
        SessionState.from_json(json.dumps(altered))
    async with timeout(45), agent.session(state=saved) as restored:
        fresh_state = await restored.save_state()
        assert fresh_state.root_goal == saved.root_goal
        assert fresh_state.root_checklist is None and responses.requests == []
        responses.respond(assistant_output("No work started during restore."))
        turn = await restored.submit("Check status.")
        assert (await turn.get_result()).final_output == "No work started during restore."
    await connection.aclose()


async def test_legacy_checkpoint_without_planning_fields_restores_with_empty_native_state(
    responses: ResponsesFixture,
) -> None:
    connection = provider(responses, "planning-legacy-key")
    original = Agent(instructions="Answer.", provider=connection)
    responses.respond(assistant_output("Before planning was enabled."))
    async with timeout(45), original.session() as session:
        await (await session.submit("Remember this.")).get_result()
        encoded = json.loads((await session.save_state()).to_json())
    assert "root_checklist" not in encoded and "root_goal" not in encoded
    async with (
        timeout(45),
        original.session(state=SessionState.from_json(json.dumps(encoded))) as session,
    ):
        fresh = await session.save_state()
        assert fresh.root_checklist is None and fresh.root_goal is None
        assert len(responses.requests) == 1
    await connection.aclose()


@pytest.mark.parametrize("status", ["paused", "complete"])
async def test_goal_snapshot_restores_totals_without_starting_or_refilling_requests(
    responses: ResponsesFixture, status: str
) -> None:
    connection = provider(responses, "saved-goal-key")
    agent = Agent(instructions="Track work.", provider=connection)
    async with timeout(40):
        async with agent.session() as session:
            raw = json.loads((await session.save_state()).to_json())
            raw["root_goal"] = legacy_goal(
                session.id,
                status=status,
                token_budget=2100,
                tokens_used=850,
                time_used_seconds=72,
                created_at=1700000000,
                updated_at=1700000072,
            )
        saved = SessionState.from_json(json.dumps(raw))
        async with agent.session(state=saved) as restored:
            again = await restored.save_state()
            assert again.root_goal == saved.root_goal
            assert again.root_goal is not None
            assert (again.root_goal.status, again.root_goal.token_budget) == (status, 2100)
            assert (again.root_goal.tokens_used, again.root_goal.time_used_seconds) == (850, 72)
            assert responses.requests == []
        for field, invalid in (
            ("token_budget", 0),
            ("tokens_used", -1),
            ("time_used_seconds", -1),
            ("created_at", -1),
            ("updated_at", 2**63),
            ("goal_id", "imported-identity"),
        ):
            bad = json.loads(saved.to_json())
            bad["root_goal"][field] = invalid
            with pytest.raises(ValidationError):
                SessionState.from_json(json.dumps(bad))
        for field in ("created_at", "updated_at"):
            bad = json.loads(saved.to_json())
            bad["root_goal"][field] = 2**63 - 1
            parsed = SessionState.from_json(json.dumps(bad))
            with pytest.raises(RuntimeError, match="timestamp range"):
                async with agent.session(state=parsed):
                    pytest.fail("An out-of-range native timestamp was imported")
        assert responses.requests == []
    await connection.aclose()


async def test_three_field_goal_uses_fresh_default_accounting(responses: ResponsesFixture) -> None:
    connection = provider(responses, "legacy-three-field-goal")
    agent = Agent(instructions="Track work.", provider=connection)
    async with timeout(30):
        async with agent.session() as session:
            raw = json.loads((await session.save_state()).to_json())
            raw["root_goal"] = {
                "thread_id": session.id,
                "objective": "Legacy paused goal",
                "status": "paused",
            }
        saved = SessionState.from_json(json.dumps(raw))
        assert saved.root_goal is not None
        assert saved.root_goal.token_budget is None
        assert saved.root_goal.tokens_used == saved.root_goal.time_used_seconds == 0
        assert saved.root_goal.created_at is saved.root_goal.updated_at is None
        async with agent.session(state=saved) as restored:
            current = (await restored.save_state()).root_goal
            assert current is not None
            assert (current.thread_id, current.objective, current.status) == (
                saved.root_goal.thread_id,
                saved.root_goal.objective,
                saved.root_goal.status,
            )
            assert (current.token_budget, current.tokens_used, current.time_used_seconds) == (
                None,
                0,
                0,
            )
            assert current.created_at is not None and current.updated_at is not None
            assert responses.requests == []
    await connection.aclose()


async def test_goal_budget_respects_current_native_cap_on_restore(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = provider(responses, "goal-cap-key")
    agent = Agent(instructions="Track work.", provider=connection)
    async with timeout(40):
        async with agent.session() as session:
            raw = json.loads((await session.save_state()).to_json())
            raw["root_goal"] = legacy_goal(session.id, token_budget=900)
        saved = SessionState.from_json(json.dumps(raw))
        native = importlib.import_module("agents._native")
        create_checkpoint_engine = native.create_checkpoint_engine

        def capped(config_json: str, *args: Any, **kwargs: Any) -> Any:
            config = json.loads(config_json)
            config["goals.max_goal_token_budget"] = 500
            return create_checkpoint_engine(json.dumps(config), *args, **kwargs)

        monkeypatch.setattr(native, "create_checkpoint_engine", capped)
        with pytest.raises(RuntimeError, match="exceeds the maximum"):
            async with agent.session(state=saved):
                pytest.fail("Saved budget bypassed the current native cap")
        assert responses.requests == []
    await connection.aclose()


class TerminalChildPeer(ChildPeer):
    """Complete each child without starting speculative model planning work."""

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        name = metadata(body, self.request_headers[-1])["agent_name"].rsplit("/", 1)[-1]
        if name == "root":
            return super().response()
        self.child_requests.setdefault(name, []).append(body)
        answer = assistant_output(name + " complete")
        answer["item"]["id"] = "done-" + name
        return [answer]


@pytest.mark.parametrize("include_goals", [False, True])
async def test_child_legacy_data_survives_repeated_checkpoints_without_resuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include_goals: bool
) -> None:
    peer = TerminalChildPeer(("default", "research"))
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(65),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = agent_for(peer, executor)
        try:
            async with agent.session(environment=declaration(executor)) as session:
                root = await session.submit("Start both reviews.")
                assert (await root.get_result()).final_output == "root complete"
                await asyncio.gather(*(event.wait() for event in peer.entered.values()))
                turns = await handles(session, 2)
                for event in peer.release.values():
                    event.set()
                results = await asyncio.gather(*(turn.get_result() for turn in turns))
                assert {result.final_output for result in results} == {
                    "alpha complete",
                    "beta complete",
                }
                raw = json.loads((await session.save_state()).to_json())
                turn_ids = {turn.child_id: turn.turn_id for turn in turns}
                for row in raw["descendants"]:
                    row["checklist"] = legacy_checklist(
                        row["thread_id"], turn_ids[row["thread_id"]]
                    )
                    if include_goals:
                        row["goal"] = legacy_goal(
                            row["thread_id"], status="complete", token_budget=1300
                        )
                saved = SessionState.from_json(json.dumps(raw))
                assert saved.root_goal is None and saved.root_checklist is None
                before_restore = len(peer.requests)
            async with agent.session(state=saved, environment=declaration(executor)) as restored:
                assert all([row.turn is None async for row in restored.subagents()])
                again = await restored.save_state()
                assert {row.thread_id: row.goal for row in again.descendants} == {
                    row.thread_id: row.goal for row in saved.descendants
                }
                assert {row.thread_id: row.checklist for row in again.descendants} == {
                    row.thread_id: row.checklist for row in saved.descendants
                }
                assert all(
                    current.history == previous.history
                    for current, previous in zip(again.descendants, saved.descendants, strict=True)
                )
                assert len(peer.requests) == before_restore
            async with agent.session(
                state=again, environment=declaration(executor)
            ) as restored_again:
                final = await restored_again.save_state()
                assert {row.thread_id: row.goal for row in final.descendants} == {
                    row.thread_id: row.goal for row in saved.descendants
                }
                assert all([row.turn is None async for row in restored_again.subagents()])
                assert len(peer.requests) == before_restore
            for body in peer.requests:
                assert declared_names(body).isdisjoint(
                    {"update_plan", "create_goal", "get_goal", "update_goal"}
                )
        finally:
            for event in peer.release.values():
                event.set()
            await agent.provider.aclose()
