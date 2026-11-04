"""Native completed child work and planning state outlive their Python process."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    Environment,
    NativeAgents,
    NativeProvider,
    NativeRole,
    ToolContext,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer, assert_portable_executor_turns
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.native_planning_fixtures import legacy_checklist, legacy_goal
from tests.test_native_child_agents import ChildPeer, collaboration, handles, metadata, trust
from tests.test_native_executor_tools import declaration


class WorkflowPeer(ChildPeer):
    """Choose replies by child identity and provider credential, not request order."""

    def __init__(self, *, remote_effect: bool) -> None:
        super().__init__(("default",))
        self.remote_effect = remote_effect
        self.command = "printf 'child completed\\n' >> completed.txt"
        for event in self.release.values():
            event.set()

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        identity = metadata(body, self.request_headers[-1])["agent_name"]
        received = {
            item.get("call_id")
            for item in body.get("input", [])
            if item.get("type") in {"function_call_output", "custom_tool_call_output"}
        }
        credential = self.request_headers[-1].get("authorization")
        if credential == "Bearer current-credential":
            assert identity == "/root", "Reading restored history restarted a child"
            if "current-lookup" not in received:
                return [function_call("lookup", {}, "current-lookup")]
            return [assistant_output("Continue from the saved state.")]
        assert credential == "Bearer source-credential"
        if identity != "/root":
            if not self.remote_effect:
                if "shared-call-id" not in received:
                    return [function_call("lookup", {}, "shared-call-id")]
                return [assistant_output("alpha complete")]
            result = super().response()
            if "shared-call-id" not in received:
                return result
            receipts = [
                item["call_id"]
                for item in body.get("input", [])
                if item.get("type") == "function_call_output"
                and (
                    item.get("call_id") == "shared-call-id"
                    or item.get("call_id", "").startswith("workflow-stdin-")
                )
            ]
            output = tool_output(body, receipts[-1])
            running = re.search(r"Process running with session ID (\d+)", output)
            if running:
                return [
                    function_call(
                        "write_stdin",
                        {"session_id": int(running[1]), "chars": "", "yield_time_ms": 1000},
                        f"workflow-stdin-{len(receipts)}",
                    )
                ]
            assert "Process exited with code 0" in output, output
            return result
        if "source-lookup" not in received:
            return [function_call("lookup", {}, "source-lookup")]
        if "spawn-alpha" not in received:
            return [
                collaboration(
                    "spawn_agent",
                    {
                        "task_name": "alpha",
                        "message": "Plan and append the completed result once.",
                        "agent_type": "default",
                        "fork_turns": "none",
                    },
                    "spawn-alpha",
                )
            ]
        return [assistant_output("Root is idle; child may still be running.")]


@asynccontextmanager
async def workflow_peer(*, remote_effect: bool) -> AsyncIterator[WorkflowPeer]:
    peer = WorkflowPeer(remote_effect=remote_effect)
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with TestServer(app, shutdown_timeout=1) as server:
        peer.url = str(server.make_url("/v1"))
        yield peer


class RestoredModelPeer(ChildPeer):
    """Source sessions finish plainly; a restored session starts one native child."""

    def __init__(self) -> None:
        super().__init__(("default",))
        self.restored = False

    def response(self) -> list[dict[str, Any]]:
        if not self.restored:
            return [assistant_output("source complete")]
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        if meta["agent_name"] == "/root":
            return super().response()
        name = meta["agent_name"].rsplit("/", 1)[-1]
        self.child_requests.setdefault(name, []).append(body)
        self.child_metadata.setdefault(name, []).append(meta)
        return [assistant_output(name + " complete")]


@asynccontextmanager
async def restored_model_peer() -> AsyncIterator[RestoredModelPeer]:
    peer = RestoredModelPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with TestServer(app, shutdown_timeout=1) as server:
        peer.url = str(server.make_url("/v1"))
        yield peer


async def save_ordinary_model(peer: RestoredModelPeer, model: str) -> SessionState:
    source = Agent(
        instructions="Answer and retain the earlier work.",
        model=model,
        provider=peer.provider(websocket=False, key="source-model-credential"),
    )
    async with source.session() as session:
        assert (await (await session.submit("one")).get_result()).final_output == "source complete"
        saved = await session.save_state()
    assert peer.requests[-1]["model"] == model
    assert saved.format_version == 4 and saved.managed_tool_specs is not None
    return SessionState.from_json(saved.to_json())


def model_restore_agent(peer: RestoredModelPeer, executor: ExecutorPeer) -> Agent[None, str]:
    return Agent(
        instructions="Answer and retain the earlier work.",
        model="gpt-6.1-sol",
        tools=[
            NativeAgents(
                additional_roles=[
                    NativeRole(name="research", description="Read.", instructions="Answer.")
                ],
                max_children=1,
            )
        ],
        provider=peer.provider(websocket=False, key="current-model-credential"),
    )


@pytest.mark.parametrize(
    "saved_model,admitted",
    [
        pytest.param("unknown-review-model", False, id="genuine-fallback-saved-model"),
        pytest.param("gpt-5.5", True, id="known-saved-and-current-models-differ"),
    ],
)
async def test_saved_model_is_admitted_before_native_agents_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved_model: str, admitted: bool
) -> None:
    async with (
        timeout(30),
        restored_model_peer() as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        saved = await save_ordinary_model(peer, saved_model)
        before_model_requests = len(peer.requests)
        before_executor_requests = list(executor.requests)
        peer.restored = True
        current = model_restore_agent(peer, executor)
        if not admitted:
            with pytest.raises(
                RuntimeError, match="model metadata is incompatible with NativeAgents"
            ):
                async with current.session(state=saved, environment=declaration(executor)):
                    pytest.fail("Fallback metadata admitted an unsupported saved model")
            # Admission precedes executor connection and any current provider request.
            assert len(peer.requests) == before_model_requests
            assert executor.requests == before_executor_requests
            return
        async with current.session(state=saved, environment=declaration(executor)) as session:
            assert len(peer.requests) == before_model_requests, "Restoring history must not run it"
            root = await session.submit("Start one native child")
            assert (await (root).get_result()).final_output == "root complete"
            async with timeout(8):
                (child,) = await handles(session, 1)
                assert (await (child).get_result()).final_output == "alpha complete"
            assert peer.child_requests["alpha"], "Admission must enable the child's actual routing"
            current_requests = peer.requests[before_model_requests:]
            assert current_requests and all(
                request["model"] == saved_model for request in current_requests
            )
            assert all(
                headers.get("authorization") == "Bearer current-model-credential"
                for headers in peer.request_headers[before_model_requests:]
            )


async def test_native_restore_cannot_exchange_admitted_checkpoint_for_same_prefix_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pinned catalog accepts a longest-prefix match as non-fallback metadata.
    # Creation can freeze one model while a lower-level client supplies a
    # different, genuine checkpoint to restore_checkpoint. Test both real
    # native calls: only substitute the first call's checkpoint argument.
    async with (
        timeout(30),
        restored_model_peer() as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        initial = await save_ordinary_model(peer, "gpt-5.5")
        actual = await save_ordinary_model(peer, "gpt-5.5-review-alias")
        before_model_requests = len(peer.requests)
        native = importlib.import_module("agents._native")
        real_create = native.create_checkpoint_engine

        async def create_with_different_initial(*args: Any, **kwargs: Any) -> Any:
            # No validation or restore decision is mocked here.
            native_actual = json.loads(actual.to_json())
            native_actual["format_version"] = 2
            assert json.loads(kwargs["initial_checkpoint"]) == native_actual
            native_initial = json.loads(initial.to_json())
            native_initial["format_version"] = 2
            kwargs["initial_checkpoint"] = json.dumps(native_initial)
            return await real_create(*args, **kwargs)

        peer.restored = True
        current = model_restore_agent(peer, executor)
        with monkeypatch.context() as patch:
            patch.setattr(native, "create_checkpoint_engine", create_with_different_initial)
            with pytest.raises(
                RuntimeError, match="restored model differs from its initially admitted model"
            ):
                async with current.session(state=actual, environment=declaration(executor)):
                    pytest.fail("A different prefix-matched model changed the frozen catalog")
        assert len(peer.requests) == before_model_requests


async def run_worker(
    request: dict[str, Any], executor: ExecutorPeer | None, tmp_path: Path
) -> None:
    env = os.environ.copy()
    if executor is not None:
        env["SSL_CERT_FILE"] = str(executor.ca)
        env["SSL_CERT_DIR"] = str(tmp_path / "empty-certs")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import runpy, sys; sys.path.insert(0, sys.argv[1]); "
            "runpy.run_module('tests.test_native_workflow_persistence', run_name='__main__')"
        ),
        str(Path(__file__).resolve().parents[1]),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        stdout, stderr = await process.communicate(json.dumps(request).encode())
    except BaseException:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, (stdout.decode(), stderr.decode())


async def worker() -> None:
    request = json.load(sys.stdin)
    state_path = Path(request["state_path"])
    receipt_path = Path(request["receipt_path"])
    stage = request["stage"]
    calls: list[ToolContext[str]] = []

    @function_tool
    def lookup(context: ToolContext[str]) -> str:
        """Read the present application context."""
        calls.append(context)
        if context.call_id == "shared-call-id":
            # This represents an app-owned memory write by a child. The
            # destination outlives this process; restore must not replay it.
            with Path(request["memory"]).open("a") as destination:
                destination.write("child completed\n")
        return f"{stage}:{context.context}"

    connection = NativeProvider(
        base_url=request["provider_url"],
        api_key=f"{stage}-credential",
        supports_websockets=False,
    )
    agent = Agent(
        instructions="Work on the assigned tasks and retain a plan.",
        model="gpt-5.5",
        provider=connection,
        tools=[
            lookup,
            NativeAgents(
                additional_roles=[
                    NativeRole(
                        name="research",
                        description="Read a requested record.",
                        instructions="Complete the requested native task.",
                    )
                ]
            ),
        ],
    )
    if stage == "source":
        async with agent.session(
            context="original-context",
            environment=Environment(
                url=request["executor_url"],
                cwd=request["cwd"],
                headers={"Authorization": "Bearer " + request["executor_token"]},
                tools=["exec_command", "write_stdin", "apply_patch"],
            ),
        ) as session:
            root = await session.submit("Run child.")
            assert (await root.get_result()).final_output
            child = (await handles(session, 1))[0]
            completed = await (child).get_result()
            assert completed.final_output == "alpha complete"
            assert len(calls) == (1 if request["remote_effect"] else 2)
            assert calls[0].context == "original-context"
            # The current process supplies a real root/child history and effects.
            # Bind captured pre-removal receipts to those native identities.
            raw = json.loads((await session.save_state()).to_json())
            raw["root_goal"] = legacy_goal(
                session.id, objective="Continue the report after restart"
            )
            raw["root_checklist"] = legacy_checklist(session.id, root.turn_id)
            assert child.child_id is not None
            raw["descendants"][0]["checklist"] = legacy_checklist(child.child_id, child.turn_id)
            saved = SessionState.from_json(json.dumps(raw))
            assert saved.root_goal is not None
            assert saved.root_goal.objective == "Continue the report after restart"
            assert saved.root_goal.status == "paused"
            assert saved.root_checklist is not None
            assert len(saved.descendants) == 1
            assert saved.descendants[0].checklist is not None
            state_path.write_text(saved.to_json())
            receipt_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "session_id": session.id,
                        "child_id": child.child_id,
                        "child_turn_id": child.turn_id,
                        "root_items": [item async for item in session.items()],
                        "root_turns": [item async for item in session.turns()],
                        "child_items": [
                            item async for item in session.items(subagent_id=child.child_id)
                        ],
                        "child_turns": [
                            item async for item in session.turns(subagent_id=child.child_id)
                        ],
                    }
                )
            )
    else:
        previous = json.loads(receipt_path.read_text())
        assert previous["pid"] != os.getpid()
        saved = SessionState.from_json(state_path.read_text())
        async with agent.session(
            context="current-context",
            state=saved,
            environment=Environment(
                url=request["executor_url"],
                cwd=request["cwd"],
                headers={"Authorization": "Bearer " + request["executor_token"]},
                tools=["exec_command", "write_stdin", "apply_patch"],
            ),
        ) as session:
            assert session.id == previous["session_id"]
            restored_state = await session.save_state()
            assert restored_state.root_goal == saved.root_goal
            assert restored_state.root_checklist == saved.root_checklist
            assert [item async for item in session.items()] == previous["root_items"]
            assert_portable_executor_turns(
                [item async for item in session.turns()], previous["root_turns"]
            )
            child_id = previous["child_id"]
            children = [item async for item in session.subagents()]
            assert [child.id for child in children] == [child_id]
            # A durable terminal record can be inspected. It does not recreate
            # the source Session's live Turn.
            assert children[0].turn is None
            assert [item async for item in session.items(subagent_id=child_id)] == previous[
                "child_items"
            ]
            child_turns = [item async for item in session.turns(subagent_id=child_id)]
            assert_portable_executor_turns(child_turns, previous["child_turns"])
            assert any(
                turn["id"] == previous["child_turn_id"] and turn["status"] == "completed"
                for turn in child_turns
            )
            assert "alpha complete" in json.dumps(previous["child_items"])
            resaved = await session.save_state()
            assert resaved.descendants[0].checklist == saved.descendants[0].checklist
            assert calls == []
            completed = await (await session.submit("Continue with current bindings.")).get_result()
            assert completed.final_output == "Continue from the saved state."
            assert len(calls) == 1
            assert calls[0].context == "current-context"
            assert (await session.save_state()).root_goal == saved.root_goal
    await connection.aclose()


@pytest.mark.parametrize(
    "remote_effect",
    [False, pytest.param(True, marks=pytest.mark.requires_native_linux_sandbox)],
    ids=["python-memory", "remote-executor"],
)
async def test_root_and_terminal_child_state_survive_a_fresh_python_process(
    tmp_path: Path, remote_effect: bool
) -> None:
    async with (
        timeout(75),
        workflow_peer(remote_effect=remote_effect) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        state_path = tmp_path / "saved.json"
        receipt_path = tmp_path / "observable-history.json"
        common = {
            "state_path": str(state_path),
            "receipt_path": str(receipt_path),
            "provider_url": peer.url,
            "executor_url": executor.url,
            "executor_token": executor.token,
            "cwd": str(executor.root),
            "memory": str(tmp_path / "child-memory.txt"),
            "remote_effect": remote_effect,
        }
        await run_worker({**common, "stage": "source"}, executor, tmp_path)
        effect = executor.root / "completed.txt" if remote_effect else tmp_path / "child-memory.txt"
        assert effect.read_text() == "child completed\n"
        initial_requests = len(peer.requests)
        initial_starts = sum(
            request.get("method") == "process/start" for request in executor.requests
        )
        assert initial_starts == int(remote_effect)
        encoded = state_path.read_text()
        for credential in ("source-credential", executor.token):
            assert credential not in encoded
        await run_worker({**common, "stage": "current"}, executor, tmp_path)
        # Exactly two requests come from the explicit new turn; opening,
        # history reads, and re-saving never ask the provider to resume work.
        fresh = peer.requests[initial_requests:]
        assert len(fresh) == 2
        assert tool_output(fresh[-1], "current-lookup") == "current:current-context"
        assert "source:original-context" in json.dumps(fresh[0])
        assert all(
            headers["authorization"] == "Bearer current-credential"
            for headers in peer.request_headers[initial_requests:]
        )
        assert (
            sum(request.get("method") == "process/start" for request in executor.requests)
            == initial_starts
        )
        assert effect.read_text() == "child completed\n"


if __name__ == "__main__":
    asyncio.run(worker())
