"""Accepted native root and child plans keep their own read and update authority."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agents import function_tool
from agents.tools import Checklist
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer, assert_portable_executor_turns
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_environment_leases_checklist import declarations
from tests.test_native_child_agents import agent_for, child_peer, handles, metadata, trust
from tests.test_native_executor_tools import declaration


@pytest.mark.parametrize(
    "remote_effect",
    [False, pytest.param(True, marks=pytest.mark.requires_native_linux_sandbox)],
    ids=["python-effect", "confined-executor"],
)
async def test_native_child_checklist_history_and_readonly_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_effect: bool
) -> None:
    async with (
        timeout(45),
        child_peer(("default", "research")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        original_reply = peer.response
        readonly = False
        requests: dict[str, list[dict[str, Any]]] = {}
        names: dict[str, str] = {}
        effects: list[str] = []

        @function_tool
        def record_child(name: str) -> str:
            effects.append(name)
            return "recorded-" + name

        def scripted_reply() -> list[dict[str, Any]]:
            body = peer.requests[-1]
            meta = metadata(body, peer.request_headers[-1])
            path = meta["agent_name"]
            names[meta["thread_id"]] = path.rsplit("/", 1)[-1]
            requests.setdefault(path, []).append(body)
            received = {
                item.get("call_id")
                for item in body.get("input", [])
                if item.get("type") in {"function_call_output", "custom_tool_call_output"}
            }
            if readonly:
                assert path == "/root", "A Checklist read restarted saved child work"
                if "denied-plan" not in received:
                    return [
                        function_call(
                            "update_plan",
                            {"plan": [{"step": "forbidden", "status": "completed"}]},
                            "denied-plan",
                        )
                    ]
                return [assistant_output("read only")]
            name = path.rsplit("/", 1)[-1]
            call_id = "plan-" + name
            if call_id not in received:
                return [
                    function_call(
                        "update_plan",
                        {
                            "explanation": "plan for " + name,
                            "plan": [{"step": name + "-only-step", "status": "in_progress"}],
                        },
                        call_id,
                    )
                ]
            if name != "root" and not remote_effect:
                if "child-effect" not in received:
                    return [function_call("record_child", {"name": name}, "child-effect")]
                result = assistant_output(name + " complete")
                result["item"]["id"] = "done-" + name
                return [result]
            return original_reply()

        monkeypatch.setattr(peer, "response", scripted_reply)
        base = agent_for(peer, executor)
        source = replace(base, tools=[*base.tools, Checklist(), record_child])
        async with source.session(environment=declaration(executor)) as session:
            assert await session.checklist() is None
            assert await session.checklist(subagent_id=None) is None
            root = await session.submit("Delegate exactly once to alpha and beta.")
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            assert (await root.get_result()).final_output == "root complete"
            children = await handles(session, 2)
            for event in peer.release.values():
                event.set()
            results = await asyncio.gather(*(child.get_result() for child in children))
            assert {result.final_output for result in results} == {
                "alpha complete",
                "beta complete",
            }

            rows = [row async for row in session.subagents()]
            assert len(rows) == 2
            before_count = len(peer.requests)
            root_plan = await session.checklist()
            assert root_plan is not None and root_plan.thread_id == session.id
            assert root_plan.turn_id == root.turn_id
            assert root_plan.plan[0].step == "root-only-step"
            root_items = [item async for item in session.items()]
            root_turns = [turn async for turn in session.turns()]
            snapshots: dict[str, Any] = {}
            for row in rows:
                name = names[row.id]
                assert name in {"alpha", "beta"}
                plan = await session.checklist(subagent_id=row.id)
                assert plan is not None and plan.thread_id == row.id
                assert plan.plan[0].step == name + "-only-step"
                assert "Plan updated" in tool_output(requests["/root/" + name][-2], "plan-" + name)
                child_items = [item async for item in session.items(subagent_id=row.id)]
                child_turns = [turn async for turn in session.turns(subagent_id=row.id)]
                assert all(turn["subagent_id"] == row.id for turn in child_turns)
                assert any(turn["id"] == plan.turn_id for turn in child_turns)
                recorded = json.dumps(child_items)
                assert name + " complete" in recorded
                assert name + "-only-step" in recorded
                assert name + "-only-step" not in json.dumps(root_items)
                other = "beta" if name == "alpha" else "alpha"
                assert other + "-only-step" not in recorded
                if remote_effect:
                    assert (executor.root / (name + ".txt")).read_text() == name
                else:
                    assert tool_output(requests["/root/" + name][-1], "child-effect") == (
                        "recorded-" + name
                    )
                snapshots[row.id] = (plan, child_items, child_turns)
            assert remote_effect or sorted(effects) == ["alpha", "beta"]
            assert len(peer.requests) == before_count
            with pytest.raises(RuntimeError, match="not part of this session"):
                await session.checklist(subagent_id=str(uuid4()))
            with pytest.raises((RuntimeError, ValueError)):
                await session.checklist(subagent_id="not-a-thread-id")
            async with source.session(environment=declaration(executor)) as sibling:
                with pytest.raises(RuntimeError, match="not part of this session"):
                    await session.checklist(subagent_id=sibling.id)
                assert await sibling.checklist() is None
            saved = await session.save_state()
            assert saved.root_checklist == root_plan
            assert {item.thread_id: item.checklist for item in saved.descendants} == {
                child_id: snapshot[0] for child_id, snapshot in snapshots.items()
            }

        readonly = True
        destination = replace(
            source, tools=[tool for tool in source.tools if not isinstance(tool, Checklist)]
        )
        async with destination.session(state=saved, environment=declaration(executor)) as restored:
            before_count = len(peer.requests)
            assert await restored.checklist() == root_plan
            assert [item async for item in restored.items()] == root_items
            assert_portable_executor_turns([turn async for turn in restored.turns()], root_turns)
            rows = [row async for row in restored.subagents()]
            assert all(row.turn is None for row in rows)
            for row in rows:
                assert await restored.checklist(subagent_id=row.id) == snapshots[row.id][0]
                assert [item async for item in restored.items(subagent_id=row.id)] == snapshots[
                    row.id
                ][1]
                assert_portable_executor_turns(
                    [turn async for turn in restored.turns(subagent_id=row.id)],
                    snapshots[row.id][2],
                )
            assert len(peer.requests) == before_count
            result = await (await restored.submit("Read, do not update.")).get_result()
            assert result.final_output == "read only"
            assert len(peer.requests) == before_count + 2
            denied = tool_output(peer.requests[-1], "denied-plan")
            assert "unsupported call:" in denied and "update_plan" in denied
            assert all(
                ("functions", "update_plan") not in declarations(request)
                for request in peer.requests[before_count:]
            )
            assert await restored.checklist() == root_plan
            for child_id, (plan, _, _) in snapshots.items():
                assert await restored.checklist(subagent_id=child_id) == plan
            for name in ("alpha", "beta"):
                if remote_effect:
                    assert (executor.root / (name + ".txt")).read_text() == name
            assert remote_effect or sorted(effects) == ["alpha", "beta"]
