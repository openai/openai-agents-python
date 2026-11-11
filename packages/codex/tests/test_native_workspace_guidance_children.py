"""Current workspace descriptions follow the public native descendant owner."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Environment, NativeAgents
from agents.sandbox import Manifest, sandbox_environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import (
    ChildPeer,
    agent_for,
    collaboration,
    handles,
    metadata,
    trust,
)
from tests.test_native_workspace_guidance import _latest_user_instructions
from tests.test_sandbox_environment import Client, Options, declaration


class GuidanceFamilyPeer(ChildPeer):
    def __init__(self) -> None:
        super().__init__(("research",))
        self.phase = "initial"
        self.captures: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        path = metadata(body, self.request_headers[-1])["agent_name"]
        captured = self.captures.setdefault(self.phase, {}).setdefault(path, [])
        captured.append(body)
        if path == "/root":
            return super().response()
        if path == "/root/alpha" and len(captured) == 1:
            return [
                collaboration(
                    "spawn_agent",
                    {
                        "task_name": "grandchild",
                        "message": "Read the current description without executing tools.",
                        "agent_type": "research",
                        "fork_turns": "none",
                    },
                    "spawn-grandchild",
                )
            ]
        return [assistant_output("descendant complete")]


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
async def test_current_guidance_reaches_cold_grandchild_without_loading_parent_or_foreign_resource(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("SYNTHETIC_ORIGINAL_GLOBAL")
    monkeypatch.setenv("CODEX_HOME", str(home))
    peer = GuidanceFamilyPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(45),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "workspace").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)

        def environment(cwd=None):
            return Environment(
                url=executor.url,
                headers={"Authorization": "Bearer " + executor.token},
                cwd=str(cwd or executor.root),
                tools=("exec_command", "write_stdin", "apply_patch"),
            )

        @asynccontextmanager
        async def connect(selected):
            yield environment()

        def source(marker):
            return sandbox_environment(
                Client([]),
                manifest=Manifest(
                    root=str(executor.root), entries={"data": declaration(description=marker)}
                ),
                options=Options(),
                connect=connect,
            )

        base = agent_for(peer, executor)
        native = next(tool for tool in base.tools if isinstance(tool, NativeAgents))
        agent = replace(base, model="gpt-6.1-sol", tools=[replace(native, max_depth=2)])
        guidance = {"project_guidance": True, "discover_agents_md": True}
        async with agent.session(environment=source("SYNTHETIC_GUIDE_A"), **guidance) as first:
            await (await first.submit("Create alpha and grandchild.")).get_result()
            for turn in await handles(first, 2):
                assert (await turn.get_result()).final_output == "descendant complete"
            saved = await first.save_state()
            for path in ("/root/alpha", "/root/alpha/grandchild"):
                text = _latest_user_instructions(peer.captures["initial"][path][0])
                assert "SYNTHETIC_GUIDE_A" in text
                assert "SYNTHETIC_ORIGINAL_GLOBAL" in text
                assert text.count("# Current SDK workspace snapshot") == 1

            # The first native family remains alive while a distinct current
            # Environment/cwd starts another root and its own descendants.
            foreign = executor.root / "foreign"
            foreign.mkdir()
            peer.phase = "foreign"
            peer.root_started = False
            async with agent.session(environment=environment(foreign), **guidance) as separate:
                await (await separate.submit("Create a separate resource family.")).get_result()
                for turn in await handles(separate, 2):
                    await turn.get_result()
                for path in ("/root", "/root/alpha", "/root/alpha/grandchild"):
                    text = _latest_user_instructions(peer.captures["foreign"][path][0])
                    assert "SYNTHETIC_GUIDE_A" not in text
                    assert "# Current SDK workspace snapshot" not in text

        for phase, opening, expected in (
            ("replacement", source("SYNTHETIC_GUIDE_B"), "SYNTHETIC_GUIDE_B"),
            ("clear", environment(), None),
        ):
            peer.phase = phase
            peer.action = collaboration(
                "followup_task",
                {"target": "/root/alpha/grandchild", "message": "Read the current description."},
                "wake-" + phase,
            )
            async with agent.session(state=saved, environment=opening, **guidance) as restored:
                before = [child async for child in restored.subagents()]
                assert len(before) == 2 and all(not child.is_live for child in before)
                child_ids = {child.id for child in before}
                grandchild_id = next(
                    child.id for child in before if child.parent_agent_id in child_ids
                )
                await (await restored.submit("Resume the grandchild directly.")).get_result()
                while True:
                    rows = [child async for child in restored.subagents()]
                    child = next(row for row in rows if row.id == grandchild_id)
                    if child.turn is not None:
                        break
                    await asyncio.sleep(0)
                assert (await child.turn.get_result()).final_output == "descendant complete"
                assert all(
                    not row.is_live and row.turn is None for row in rows if row.id != child.id
                )
                assert "/root/alpha" not in peer.captures[phase]
                text = _latest_user_instructions(peer.captures[phase]["/root/alpha/grandchild"][0])
                assert "SYNTHETIC_GUIDE_A" not in text
                assert "SYNTHETIC_ORIGINAL_GLOBAL" in text
                if expected is None:
                    assert "SYNTHETIC_GUIDE_B" not in text
                    assert "# Current SDK workspace snapshot" not in text
                else:
                    assert expected in text
                    assert text.count("# Current SDK workspace snapshot") == 1
                saved = await restored.save_state()
