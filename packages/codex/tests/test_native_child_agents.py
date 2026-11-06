"""Real Core children share one declared executor and one native event owner."""

from __future__ import annotations

import asyncio
import json
import pickle
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    ExecutorContext,
    NativeAgents,
    NativeError,
    NativeRole,
    Session,
    Turn,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_executor_tools import declaration
from tests.test_native_provider_websockets import WebSocketPeer


def metadata(body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    raw = body.get("client_metadata", {}).get("x-codex-turn-metadata")
    return json.loads(raw if raw is not None else headers["x-codex-turn-metadata"])


def collaboration(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    result = function_call(name, arguments, call_id)
    result["item"]["namespace"] = "collaboration"
    return result


class Program(deque[list[dict[str, Any]]]):
    def __init__(self, peer: ChildPeer) -> None:
        super().__init__()
        self.peer = peer

    def __bool__(self) -> bool:
        return True

    def popleft(self) -> list[dict[str, Any]]:
        return self.peer.response()


class ChildPeer(WebSocketPeer):
    def __init__(self, roles: tuple[str, ...], *, bad: str | None = None) -> None:
        super().__init__()
        self.roles = roles
        self.bad = bad
        self.replies = Program(self)
        self.before_event = self.hold
        self.entered = {name: asyncio.Event() for name in ("alpha", "beta")}
        self.release = {name: asyncio.Event() for name in ("alpha", "beta")}
        self.child_requests: dict[str, list[dict[str, Any]]] = {}
        self.child_metadata: dict[str, list[dict[str, Any]]] = {}
        self.action: dict[str, Any] | None = None
        self.root_started = False
        self.command: str | None = None
        self.extra_arguments: dict[str, Any] = {}
        self.patch: str | None = None

    async def hold(self, event: dict[str, Any]) -> None:
        item = event.get("item", {})
        if item.get("id", "").startswith("done-"):
            name = item["id"].split("-")[1]
            self.entered[name].set()
            await self.release[name].wait()

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        path = meta["agent_name"]
        if path != "/root":
            name = path.rsplit("/", 1)[-1]
            self.child_requests.setdefault(name, []).append(body)
            self.child_metadata.setdefault(name, []).append(meta)
            calls = [
                item
                for item in body.get("input", [])
                if item.get("type") in {"function_call_output", "custom_tool_call_output"}
                and item.get("call_id") == "shared-call-id"
            ]
            if not calls:
                if self.patch is not None:
                    return [
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "custom_tool_call",
                                "call_id": "shared-call-id",
                                "name": "apply_patch",
                                "input": self.patch,
                            },
                        }
                    ]
                args: dict[str, Any] = {
                    "cmd": self.command or f"printf {name} > {name}.txt",
                    "max_output_tokens": 100,
                }
                if self.command is not None:
                    args["yield_time_ms"] = 1000
                args.update(self.extra_arguments)
                tool = "exec_command"
                if self.bad == "extra-key":
                    args["private-unselected-key"] = True
                elif self.bad == "null-workdir":
                    args["workdir"] = None
                elif self.bad == "unselected":
                    tool = "view_image"
                    args = {"path": "missing.png"}
                return [function_call(tool, args, "shared-call-id")]
            result = assistant_output(name + " complete")
            result["item"]["id"] = "done-" + name
            return [result]
        if self.action is not None:
            action, self.action = self.action, None
            return [action]
        if not self.root_started:
            self.root_started = True
            return [
                collaboration(
                    "spawn_agent",
                    {
                        "task_name": name,
                        "message": "Execute the requested native work.",
                        "agent_type": role,
                        "fork_turns": "none",
                        **({"model": "unselected-model"} if self.bad == "override" else {}),
                    },
                    "spawn-" + name,
                )
                for name, role in zip(("alpha", "beta"), self.roles, strict=False)
            ]
        return [assistant_output("root complete")]


@asynccontextmanager
async def child_peer(roles: tuple[str, ...], *, bad: str | None = None) -> AsyncIterator[ChildPeer]:
    peer = ChildPeer(roles, bad=bad)
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    try:
        async with TestServer(app, shutdown_timeout=1) as server:
            peer.url = str(server.make_url("/v1"))
            yield peer
    finally:
        for release in peer.release.values():
            release.set()


def agent_for(
    peer: WebSocketPeer,
    executor: ExecutorPeer,
    websocket: bool = False,
) -> Agent[None, str]:
    return Agent(
        instructions="Use native children for the requested work.",
        model="gpt-5.5",
        provider=peer.provider(websocket=websocket),
        tools=[
            NativeAgents(
                additional_roles=[
                    NativeRole(
                        name="research",
                        description="Read the requested record.",
                        instructions="CUSTOM_ROLE_INSTRUCTIONS: complete the native task. 🦉",
                    )
                ],
                max_children=2,
            )
        ],
    )


def trust(executor: ExecutorPeer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certificates"))


async def handles(session: Session[None, str], count: int) -> list[Turn]:
    while True:
        children = [item async for item in session.subagents()]
        result = [child.turn for child in children]
        if len(result) == count and all(handle is not None for handle in result):
            return [handle for handle in result if handle is not None]
        await asyncio.sleep(0)


async def captured_turn(session: Session[None, str], child_id: str) -> Turn | None:
    for child in [item async for item in session.subagents()]:
        if child.id == child_id:
            return child.turn
    return None


async def child_output(session: Session[None, str], handle: Turn) -> str:
    result = await (handle).get_result()
    assert result.turn is handle
    return result.final_output


def child_usage(peer: ChildPeer, counts: dict[str, tuple[int, int, int, int] | None]) -> None:
    async def report(event: dict[str, Any]) -> None:
        await peer.hold(event)
        if event["type"] != "response.completed":
            return
        response = event["response"]
        index = int(response["id"].removeprefix("resp_")) - 1
        name = metadata(peer.requests[index], peer.request_headers[index])["agent_name"]
        usage = counts[name]
        if usage is None:
            response.pop("usage")
            return
        inputs, outputs, cached, reasoning = usage
        response["usage"] = {
            "input_tokens": inputs,
            "output_tokens": outputs,
            "total_tokens": inputs + outputs,
            "input_tokens_details": {"cached_tokens": cached},
            "output_tokens_details": {"reasoning_tokens": reasoning},
        }

    peer.before_event = report


@pytest.mark.requires_native_linux_sandbox
async def test_child_usage_is_retained_per_exact_turn_and_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        child_peer(("default", "research")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        counts: dict[str, tuple[int, int, int, int] | None] = {
            "/root": (101, 7, 9, 2),
            "/root/alpha": (7, 3, 2, 1),
            "/root/beta": (17, 5, 4, 2),
        }
        child_usage(peer, counts)
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            root = await session.submit("Use two children")
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            root_result = await (root).get_result()
            assert root_result.usage is not None
            assert root_result.usage.total_token_usage.total_tokens == 216
            by_id = {handle.child_id: handle for handle in await handles(session, 2)}
            alpha = by_id[peer.child_metadata["alpha"][0]["thread_id"]]
            beta = by_id[peer.child_metadata["beta"][0]["thread_id"]]
            for release in peer.release.values():
                release.set()
            first, other = await asyncio.gather((alpha).get_result(), (beta).get_result())
            assert first.turn is alpha and other.turn is beta
            assert first.final_output == "alpha complete" and other.final_output == "beta complete"
            assert first.usage is not None and other.usage is not None
            assert first.usage.total_token_usage.total_tokens == 20
            assert first.usage.last_token_usage.total_tokens == 10
            assert first.usage.total_token_usage.cached_input_tokens == 4
            assert first.usage.total_token_usage.reasoning_output_tokens == 2
            assert other.usage.total_token_usage.total_tokens == 44
            assert other.usage.last_token_usage.total_tokens == 22
            assert (await (root).get_result()).usage == root_result.usage

            counts["/root/alpha"] = (23, 7, 3, 2)
            peer.entered["alpha"].clear()
            peer.release["alpha"].clear()
            peer.action = collaboration(
                "followup_task", {"target": "/root/alpha", "message": "Continue."}, "followup"
            )
            await (await session.submit("Continue alpha")).get_result()
            await peer.entered["alpha"].wait()
            current = await captured_turn(session, alpha.child_id)
            while current is None or current.turn_id == alpha.turn_id:
                await asyncio.sleep(0)
                current = await captured_turn(session, alpha.child_id)
            assert current.instance_id == alpha.instance_id
            assert await session._native._cancel_child_turn((alpha)._native) is False
            assert await (alpha).get_result() == first
            assert await (beta).get_result() == other
            peer.release["alpha"].set()
            later = await (current).get_result()
            assert len(peer.child_requests["alpha"]) == 3
            assert later.usage is not None
            assert later.usage.total_token_usage.total_tokens == 50
            assert later.usage.last_token_usage.total_tokens == 30
            assert later.usage.total_token_usage.cached_input_tokens == 7
            assert later.usage.total_token_usage.reasoning_output_tokens == 4
            assert await (alpha).get_result() == first
        assert await (alpha).get_result() == first
        assert await (beta).get_result() == other
        assert await (current).get_result() == later


@pytest.mark.requires_native_linux_sandbox
async def test_child_missing_usage_remains_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        child_usage(peer, {"/root": (101, 7, 9, 2), "/root/alpha": None})
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            root = await (await session.submit("Use a child without usage")).get_result()
            await peer.entered["alpha"].wait()
            child = (await handles(session, 1))[0]
            peer.release["alpha"].set()
            result = await (child).get_result()
            assert result.turn is child
            assert result.final_output == "alpha complete"
            assert result.usage is None
            assert root.usage is not None
        assert (await (child).get_result()).usage is None


def assert_catalog(request: dict[str, Any]) -> None:
    assert {tool.get("name") for tool in request["tools"]} == {
        "exec_command",
        "write_stdin",
        "apply_patch",
    }
    assert all(tool["type"] in {"function", "custom"} for tool in request["tools"])
    assert request.get("text", {}).get("format", {}).get("type") != "json_schema"


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("websocket", [False, True])
@pytest.mark.parametrize("roles", [("default", "research"), ("explorer", "worker")])
async def test_two_genuine_children_keep_exact_turn_results(
    websocket: bool, roles: tuple[str, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        child_peer(roles) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor, websocket).session(
            environment=declaration(executor)
        ) as session:
            root = await session.submit("Use two children")
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            assert (await (root).get_result()).final_output == "root complete"
            child_handles = await handles(session, 2)
            assert len({handle.child_id for handle in child_handles}) == 2
            assert all(
                handle.session_id == session.id and handle.instance_id > 0
                for handle in child_handles
            )
            assert all(handle.turn_id != root.turn_id for handle in child_handles)
            for handle in child_handles:
                with pytest.raises(TypeError, match="cannot be serialized"):
                    pickle.dumps(handle)
                with pytest.raises(TypeError):
                    type(handle._native)()
            waiting = asyncio.create_task(child_output(session, child_handles[0]))
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert not peer.release["alpha"].is_set() and not peer.release["beta"].is_set()
            for release in peer.release.values():
                release.set()
            assert set(
                await asyncio.gather(*(child_output(session, handle) for handle in child_handles))
            ) == {"alpha complete", "beta complete"}
            assert await asyncio.gather(
                *(session._native._cancel_child_turn((handle)._native) for handle in child_handles)
            ) == [
                False,
                False,
            ]
            for name, requests in peer.child_requests.items():
                assert_catalog(requests[0])
                assert requests[0]["model"] == peer.requests[0]["model"] == "gpt-5.5"
                for tool in requests[0]["tools"]:
                    root_tool = next(
                        item
                        for item in peer.requests[0]["tools"]
                        if item.get("name") == tool["name"]
                    )
                    assert tool == root_tool
                output = json.dumps(requests[-1]).lower()
                assert "process exited with code 0" in output
                assert (executor.root / (name + ".txt")).read_text() == name
                assert peer.child_metadata[name][0]["root_turn_id"] == root.turn_id
                instructions = json.dumps(
                    [item for item in requests[0]["input"] if item.get("role") == "developer"]
                )
                role = roles[0 if name == "alpha" else 1]
                if role == "research":
                    assert "CUSTOM_ROLE_INSTRUCTIONS" in instructions
                    assert "🦉" in json.dumps(requests[0]["input"], ensure_ascii=False)
                else:
                    assert "Use native children for the requested work." in instructions
                    assert "CUSTOM_ROLE_INSTRUCTIONS" not in instructions
            assert "CUSTOM_ROLE_INSTRUCTIONS" not in json.dumps(peer.requests[0]["input"])
            root_tools = next(
                tool for tool in peer.requests[0]["tools"] if tool.get("name") == "collaboration"
            )
            spawn = next(tool for tool in root_tools["tools"] if tool["name"] == "spawn_agent")
            assert "agent_type" in spawn["parameters"]["properties"]
            assert "model" not in spawn["parameters"]["properties"]
            assert "reasoning_effort" not in spawn["parameters"]["properties"]
            assert all(
                role in spawn["parameters"]["properties"]["agent_type"]["description"]
                for role in ("default", "explorer", "worker", "research")
            )
            saved = await session.save_state()
            assert {child.thread_id for child in saved.descendants} == {
                handle.child_id for handle in child_handles
            }
        assert set(
            await asyncio.gather(*(child_output(session, handle) for handle in child_handles))
        ) == {"alpha complete", "beta complete"}
        assert bool(peer.frames) is websocket


@pytest.mark.parametrize("bad", ["extra-key", "null-workdir", "unselected"])
async def test_child_first_call_admission_precedes_remote_effects(
    bad: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        child_peer(("research",), bad=bad) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            await (await session.submit("Use one child")).get_result()
            handle = (await handles(session, 1))[0]
            if bad == "unselected":
                peer.release["alpha"].set()
                assert await child_output(session, handle) == "alpha complete"
                assert peer.entered["alpha"].is_set()
                assert len(peer.child_requests["alpha"]) == 2
                rejected = [
                    item
                    for item in peer.child_requests["alpha"][-1]["input"]
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == "shared-call-id"
                ]
                assert len(rejected) == 1
                assert rejected[0]["output"] == "unsupported call: view_image"
            else:
                with pytest.raises(NativeError):
                    await child_output(session, handle)
            for request in peer.child_requests["alpha"]:
                assert_catalog(request)
            assert not (executor.root / "alpha.txt").exists()
            assert not any(
                request.get("method") in {"process/start", "fs/writeFile"}
                for request in executor.requests
            )


@pytest.mark.parametrize("bad,role", [("override", "research"), (None, "unknown_role")])
async def test_unselected_spawn_is_rejected_before_native_child_creation(
    bad: str | None, role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        child_peer((role,), bad=bad) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            with pytest.raises(NativeError):
                await (await session.submit("Use one child")).get_result()
            assert [item async for item in session.subagents()] == []
            assert not peer.child_requests
            assert not any(
                request.get("method") in {"process/start", "fs/writeFile"}
                for request in executor.requests
            )


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("websocket", [False, True])
async def test_cancel_is_exact_and_old_handle_cannot_cancel_later_turn(
    websocket: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        child_peer(("research", "default")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor, websocket).session(
            environment=declaration(executor)
        ) as session:
            await (await session.submit("Start both children")).get_result()
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            captured = await handles(session, 2)
            by_id = {handle.child_id: handle for handle in captured}
            alpha = by_id[peer.child_metadata["alpha"][0]["thread_id"]]
            beta = by_id[peer.child_metadata["beta"][0]["thread_id"]]
            assert await session._native._cancel_child_turn((alpha)._native) is True
            with pytest.raises(NativeError, match="interrupted"):
                await child_output(session, alpha)
            pending_beta = asyncio.create_task(child_output(session, beta))
            await asyncio.sleep(0)
            assert not pending_beta.done()
            peer.release["beta"].set()
            assert await pending_beta == "beta complete"
            # A native follow-up creates a new native turn in the same child.
            peer.action = collaboration(
                "followup_task",
                {"target": "/root/alpha", "message": "Continue the work."},
                "followup",
            )
            previous_requests = len(peer.child_metadata["alpha"])
            await (await session.submit("Continue alpha")).get_result()
            while len(peer.child_metadata["alpha"]) == previous_requests:
                await asyncio.sleep(0)
            current = await captured_turn(session, alpha.child_id)
            while current is None or current.turn_id == alpha.turn_id:
                await asyncio.sleep(0)
                current = await captured_turn(session, alpha.child_id)
            assert current.instance_id == alpha.instance_id
            assert await session._native._cancel_child_turn((alpha)._native) is False
            peer.release["alpha"].set()
            assert await child_output(session, current) == "alpha complete"
            with pytest.raises(NativeError, match="interrupted"):
                await child_output(session, alpha)


@pytest.mark.requires_native_linux_sandbox
async def test_close_retires_waiting_child_and_foreign_session_cannot_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            await (await session.submit("Start child")).get_result()
            await peer.entered["alpha"].wait()
            child = (await handles(session, 1))[0]
            with pytest.raises(RuntimeError, match="not a child"):
                await session._native._child_turn(session.id)
            foreign = Agent(
                instructions="Separate native owner.", provider=peer.provider(websocket=False)
            )
            async with foreign.session() as other:
                # Native child cancellation still rejects foreign graph owners.
                with pytest.raises(RuntimeError, match="another Session"):
                    await other._native._cancel_child_turn(child._native)
                with pytest.raises(RuntimeError, match="not a child"):
                    await other._native._child_turn(child.child_id)
            waiting = asyncio.create_task(child_output(session, child))
            await asyncio.sleep(0)
            await session._aclose()
            # Close fences the graph first. A real abort can settle the exact
            # turn before retirement supplies its fallback closed error.
            with pytest.raises(
                NativeError,
                match=r"^Native child (?:turn was interrupted|runtime closed or was replaced)$",
            ) as stopped:
                await waiting
            with pytest.raises(RuntimeError, match="^Session is closed$"):
                await session.submit("Cannot submit after close")
            with pytest.raises(NativeError) as repeated:
                await child_output(session, child)
            assert str(repeated.value) == str(stopped.value)


@pytest.mark.requires_native_linux_sandbox
async def test_pending_native_message_makes_conditional_cancel_return_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            await (await session.submit("Start child")).get_result()
            await peer.entered["alpha"].wait()
            child = (await handles(session, 1))[0]
            peer.action = collaboration(
                "send_message",
                {"target": "/root/alpha", "message": "PENDING_CHILD_MESSAGE: keep working."},
                "pending-message",
            )
            await (await session.submit("Send more input")).get_result()
            assert await session._native._cancel_child_turn((child)._native) is False
            assert not peer.release["alpha"].is_set()
            peer.release["alpha"].set()
            assert await child_output(session, child) == "alpha complete"
            # QueueOnly need not inject into an already in-flight request.
            # A later native turn must receive the retained message.
            peer.action = collaboration(
                "followup_task",
                {"target": "/root/alpha", "message": "Observe the queued message."},
                "wake-queued-message",
            )
            await (await session.submit("Continue the child")).get_result()
            current = await captured_turn(session, child.child_id)
            while current is None or current.turn_id == child.turn_id:
                await asyncio.sleep(0)
                current = await captured_turn(session, child.child_id)
            assert await child_output(session, current) == "alpha complete"
            assert "PENDING_CHILD_MESSAGE" in json.dumps(peer.child_requests["alpha"][-1])


@pytest.mark.requires_native_linux_sandbox
async def test_native_child_limit_rejects_excess_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        child_peer(("default", "research")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        for release in peer.release.values():
            release.set()
        agent = agent_for(peer, executor)
        agent = Agent(
            instructions=agent.instructions,
            model=agent.model,
            provider=agent.provider,
            tools=[
                NativeAgents(
                    additional_roles=[
                        NativeRole(
                            name="research", instructions="Research.", description="Research"
                        )
                    ],
                    max_children=1,
                )
            ],
        )
        async with agent.session(environment=declaration(executor)) as session:
            await (await session.submit("Try two children")).get_result()
            child = (await handles(session, 1))[0]
            assert await child_output(session, child) in {"alpha complete", "beta complete"}
            assert len([item async for item in session.subagents()]) == 1
            assert len(peer.child_requests) == 1
            root_requests = [
                request
                for request, headers in zip(peer.requests, peer.request_headers, strict=True)
                if metadata(request, headers)["agent_name"] == "/root"
            ]
            assert "limit" in json.dumps(root_requests[-1]).lower()


@pytest.mark.requires_native_linux_sandbox
async def test_reader_loss_settles_native_child_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(20),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            await (await session.submit("Start child")).get_result()
            await peer.entered["alpha"].wait()
            child = (await handles(session, 1))[0]
            session._reader.cancel()
            await session._reader
            with pytest.raises(NativeError, match="closed|unavailable|child event stream ended"):
                await asyncio.wait_for(child_output(session, child), 1)


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("termination", ["acknowledge", "reject", "disconnect"])
async def test_child_background_process_close_checks_native_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, termination: str
) -> None:
    async with (
        timeout(25),
        child_peer(("research",)) as peer,
        ExecutorPeer(
            tmp_path / "remote",
            deny_terminate=termination == "reject",
            drop_terminate_reply=termination == "disconnect",
        ).serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.command = "sleep 10"
        peer.release["alpha"].set()

        async def run() -> None:
            async with agent_for(peer, executor).session(
                environment=declaration(executor)
            ) as session:
                await (await session.submit("Start child background process")).get_result()
                child = (await handles(session, 1))[0]
                assert await child_output(session, child) == "alpha complete"
                assert "Process running with session ID" in json.dumps(
                    peer.child_requests["alpha"][-1]
                )

        if termination == "acknowledge":
            await run()
        else:
            with pytest.raises(RuntimeError, match="termination attempt") as error:
                await run()
            assert "private-termination-error" not in str(error.value)
            assert executor.token not in str(error.value)
        assert any(request.get("method") == "process/terminate" for request in executor.requests)


@pytest.mark.requires_native_linux_sandbox
async def test_child_uses_current_executor_workspace_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(20),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        (executor.root / "allowed.txt").write_text("workspace-child-proof")
        protected = executor.root.parent / "protected.txt"
        protected.write_text("original\n")
        peer.command = "cat allowed.txt; printf denied > ../protected.txt"
        peer.release["alpha"].set()
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            await (await session.submit("Use the child workspace")).get_result()
            child = (await handles(session, 1))[0]
            assert await child_output(session, child) == "alpha complete"
            output = json.dumps(peer.child_requests["alpha"][-1]).lower()
            assert "workspace-child-proof" in output
            assert "read-only file system" in output or "permission denied" in output
            assert protected.read_text() == "original\n"
            assert any(request.get("method") == "process/start" for request in executor.requests)


async def test_child_current_approval_denies_entire_remote_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts: list[ExecutorContext[None]] = []
    async with (
        timeout(20),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        (executor.root / "allowed.txt").write_text("remote-command-should-not-read")
        peer.command = "cat allowed.txt; printf denied > forbidden.txt"
        peer.release["alpha"].set()
        agent = agent_for(peer, executor)

        def approval(ctx: ExecutorContext[None], args: Any) -> bool:
            contexts.append(ctx)
            # Deny the actual executing role/model, even though it shares the
            # owning Python Agent with the root.
            return not (
                ctx.agent_role == "research"
                and ctx.model == peer.child_requests["alpha"][0]["model"]
            )

        async with agent.session(environment=declaration(executor, approval=approval)) as session:
            await (await session.submit("Deny the child command")).get_result()
            child = (await handles(session, 1))[0]
            with pytest.raises(NativeError, match="Native child turn failed"):
                await asyncio.wait_for(child_output(session, child), 3)
            assert len(contexts) == 1
            assert contexts[0].agent is agent
            assert contexts[0].session is session
            assert contexts[0].thread_id == child.child_id
            assert contexts[0].turn_id == child.turn_id
            output = json.dumps(peer.child_requests["alpha"][-1]).lower()
            assert "remote-command-should-not-read" not in output
            assert not (executor.root / "forbidden.txt").exists()
            assert not any(
                request.get("method") == "process/start" for request in executor.requests
            )


@pytest.mark.requires_native_linux_sandbox
async def test_child_executor_approval_uses_executing_role_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts: list[ExecutorContext[None]] = []
    async with (
        timeout(25),
        child_peer(("default", "research")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.release["alpha"].set()
        peer.release["beta"].set()
        agent = agent_for(peer, executor)

        def approve(ctx: ExecutorContext[None], args: Any) -> bool:
            contexts.append(ctx)
            return (
                ctx.agent_role == "research"
                and ctx.model == peer.child_requests["beta"][0]["model"]
            )

        async with agent.session(environment=declaration(executor, approval=approve)) as session:
            await (await session.submit("Approve only the selected research child.")).get_result()
            child_turns = await handles(session, 2)
            results = await asyncio.gather(
                *(child.get_result() for child in child_turns), return_exceptions=True
            )
            assert len(contexts) == 2
            for child, result in zip(child_turns, results, strict=True):
                context = next(ctx for ctx in contexts if ctx.thread_id == child.child_id)
                assert context.session is session
                assert context.agent is agent
                assert context.turn_id == child.turn_id
                if context.agent_role == "research":
                    assert not isinstance(result, BaseException)
                    assert result.final_output == "beta complete"
                else:
                    assert isinstance(result, NativeError)

            assert {ctx.agent_role for ctx in contexts} == {"default", "research"}
            for name, role in (("alpha", "default"), ("beta", "research")):
                context = next(ctx for ctx in contexts if ctx.agent_role == role)
                assert context.model == peer.child_requests[name][0]["model"]
            assert not (executor.root / "alpha.txt").exists()
            assert (executor.root / "beta.txt").read_text() == "beta"


async def test_child_escalation_cannot_wait_for_an_unhandled_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(15),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.command = "printf denied > forbidden.txt"
        peer.extra_arguments = {
            "sandbox_permissions": "require_escalated",
            "justification": "Synthetic child permission test.",
        }
        peer.release["alpha"].set()
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            await (await session.submit("Deny child escalation")).get_result()
            child = (await handles(session, 1))[0]
            assert await asyncio.wait_for(child_output(session, child), 3) == "alpha complete"
            denial = json.dumps(peer.child_requests["alpha"][-1]).lower()
            assert "approval" in denial and "never" in denial
            assert not (executor.root / "forbidden.txt").exists()
            assert not any(
                request.get("method") == "process/start" for request in executor.requests
            )


async def test_child_patch_denial_cannot_wait_for_an_unhandled_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(15),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        target = executor.root.parent / "protected.txt"
        target.write_text("original\n")
        peer.patch = (
            "*** Begin Patch\n*** Update File: "
            + str(target)
            + "\n@@\n-original\n+forbidden\n*** End Patch\n"
        )
        peer.release["alpha"].set()
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            await (await session.submit("Deny child patch")).get_result()
            child = (await handles(session, 1))[0]
            assert await asyncio.wait_for(child_output(session, child), 3) == "alpha complete"
            denial = json.dumps(peer.child_requests["alpha"][-1]).lower()
            assert "patch rejected" in denial
            assert target.read_text() == "original\n"
            assert not any(request.get("method") == "fs/writeFile" for request in executor.requests)
