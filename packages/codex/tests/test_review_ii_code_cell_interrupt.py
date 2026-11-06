"""Idle interruption stops selected real Code Mode cells and preserves Session reuse."""

from __future__ import annotations

import json
from collections import deque
from typing import Any, Literal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeAgents, NativeProvider, Session, Subagent
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_child_agents import ChildPeer, collaboration, handles, metadata, trust
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration

Program = deque[list[dict[str, Any]]]


def initial_program(label: str) -> Program:
    return deque(
        [
            [
                code_call(
                    f'store("committed", {json.dumps(label)});text("SEEDED:" + load("committed"));',
                    "seed-" + label,
                )
            ],
            [code_call("yield_control(); await new Promise(() => {});", "old-" + label)],
            [assistant_output(label + " idle")],
        ]
    )


def yielded_cell(request: dict[str, Any], label: str) -> str:
    result = code_output(request, "old-" + label)
    prefix = "Script running with cell ID "
    assert result.startswith(prefix), result
    cell_id = result.removeprefix(prefix).split("\n", 1)[0]
    assert cell_id
    return cell_id


async def inspect_and_reuse(
    target: Session[Any, Any] | Subagent,
    program: Program,
    requests: list[dict[str, Any]],
    label: str,
    cell_id: str,
    *,
    stopped: bool,
) -> None:
    start = len(requests)
    program.extend(
        [
            [function_call("wait", {"cell_id": cell_id, "yield_time_ms": 1}, "wait-" + label)],
            [
                code_call(
                    'text("REUSED:" + load("committed")); store("next", "accepted");',
                    "fresh-" + label,
                )
            ],
            [assistant_output(label + " inspected")],
        ]
    )
    prompt = "Inspect the original cell and execute fresh code: " + label
    current = (
        await target.submit(prompt) if isinstance(target, Session) else await target.start(prompt)
    )
    assert (await current.get_result()).final_output == label + " inspected"
    assert len(requests) == start + 3
    status = tool_output(requests[start + 1], "wait-" + label)
    if stopped:
        assert status.startswith("Script terminated") or (
            status.startswith("Script failed") and f"exec cell {cell_id} not found" in status
        ), status
    else:
        assert status.startswith(f"Script running with cell ID {cell_id}\n"), status
    fresh = code_output(requests[start + 2], "fresh-" + label)
    assert fresh.startswith("Script completed"), fresh
    assert "REUSED:" + label in fresh


@pytest.mark.parametrize("scope", ["root", "all"])
@pytest.mark.parametrize("mode", ["force", "graceful"])
async def test_idle_root_cells_stop_and_other_same_agent_session_keeps_running(
    responses: ResponsesFixture,
    scope: Literal["root", "all"],
    mode: Literal["force", "graceful"],
) -> None:
    # No function or result policy is installed. Bare JS is still owned work.
    # Each session uses the SDK's actual bundled process-owned code host. The
    # public API shares this Agent/provider but does not expose a host connection.
    agent = Agent(
        instructions="Use only exec and wait for this program.",
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-cells", supports_websockets=False
        ),
        programmatic_tool_calling=True,
    )
    async with timeout(45), agent.session() as selected, agent.session() as unrelated:
        responses.replies.extend(initial_program("selected"))
        first = await selected.submit("Start selected background code")
        assert (await first.get_result()).final_output == "selected idle"
        assert "SEEDED:selected" in code_output(responses.requests[-2], "seed-selected")
        selected_cell = yielded_cell(responses.requests[-1], "selected")

        responses.replies.extend(initial_program("unrelated"))
        other = await unrelated.submit("Start unrelated background code")
        assert (await other.get_result()).final_output == "unrelated idle"
        assert "SEEDED:unrelated" in code_output(responses.requests[-2], "seed-unrelated")
        unrelated_cell = yielded_cell(responses.requests[-1], "unrelated")

        await (first.cancel(mode=mode) if scope == "root" else selected.cancel_all(mode=mode))
        await inspect_and_reuse(
            selected,
            responses.replies,
            responses.requests,
            "selected",
            selected_cell,
            stopped=scope == "all",
        )
        await inspect_and_reuse(
            unrelated,
            responses.replies,
            responses.requests,
            "unrelated",
            unrelated_cell,
            stopped=False,
        )
        assert (await first.get_result()).final_output == "selected idle"
        assert (await other.get_result()).final_output == "unrelated idle"


class CellGraph(ChildPeer):
    def __init__(self) -> None:
        super().__init__(("default",))
        self.before_event = None
        self.programs = {"/root": initial_program("root"), "/root/alpha": initial_program("alpha")}
        self.programs["/root"].insert(
            2,
            [
                collaboration(
                    "spawn_agent",
                    {
                        "task_name": "alpha",
                        "message": "Start one background code cell.",
                        "agent_type": "default",
                        "fork_turns": "none",
                    },
                    "spawn-alpha",
                )
            ],
        )
        self.by_thread: dict[str, list[dict[str, Any]]] = {path: [] for path in self.programs}

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        path = metadata(body, self.request_headers[-1])["agent_name"]
        self.by_thread[path].append(body)
        assert self.programs[path], f"Unexpected request on {path}"
        return self.programs[path].popleft()


@pytest.mark.parametrize("scope", ["root", "all"])
@pytest.mark.parametrize("mode", ["force", "graceful"])
async def test_idle_descendant_cells_follow_selected_interrupt_scope(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    scope: Literal["root", "all"],
    mode: Literal["force", "graceful"],
) -> None:
    peer = CellGraph()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(55),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        # This pinned model supports native V2 children and Code Mode together.
        agent = Agent(
            instructions="Start the requested background program.",
            model="gpt-6.1-sol",
            provider=peer.provider(websocket=False),
            tools=[NativeAgents(max_children=2)],
            programmatic_tool_calling=True,
        )
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Start root code and an alpha child")
            assert (await root.get_result()).final_output == "root idle"
            [child_turn] = await handles(session, 1)
            assert (await child_turn.get_result()).final_output == "alpha idle"
            [child] = [item async for item in session.subagents()]
            assert child.is_live
            root_requests = peer.by_thread["/root"]
            child_requests = peer.by_thread["/root/alpha"]
            assert "SEEDED:root" in code_output(root_requests[1], "seed-root")
            assert "SEEDED:alpha" in code_output(child_requests[1], "seed-alpha")
            root_cell = yielded_cell(root_requests[2], "root")
            child_cell = yielded_cell(child_requests[2], "alpha")

            await (root.cancel(mode=mode) if scope == "root" else session.cancel_all(mode=mode))
            await inspect_and_reuse(
                session,
                peer.programs["/root"],
                root_requests,
                "root",
                root_cell,
                stopped=scope == "all",
            )
            await inspect_and_reuse(
                child,
                peer.programs["/root/alpha"],
                child_requests,
                "alpha",
                child_cell,
                stopped=scope == "all",
            )
            assert (await root.get_result()).final_output == "root idle"
            assert (await child_turn.get_result()).final_output == "alpha idle"
