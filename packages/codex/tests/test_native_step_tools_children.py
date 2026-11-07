"""Native child selections retain their own callbacks, ceilings and nonces."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    ChildInvocation,
    ChildStepContext,
    FunctionTool,
    NativeAgents,
    NativeError,
    StopTurn,
    ToolContext,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_child_agents import ChildPeer, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_tools import declarations


class SelectedChildrenPeer(ChildPeer):
    def __init__(self):
        super().__init__(("default", "worker"))
        self.steps = {}

    async def hold(self, event):
        pass

    def response(self):
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        path = meta["agent_name"]
        if path == "/root":
            return super().response()
        name = path.rsplit("/", 1)[-1]
        self.child_requests.setdefault(name, []).append(body)
        self.child_metadata.setdefault(name, []).append(meta)
        index = self.steps.get(path, 0)
        self.steps[path] = index + 1
        if index == 0:
            selected = set(declarations(body)) & {("functions", "first"), ("functions", "second")}
            assert len(selected) == 1
            return [function_call(next(iter(selected))[1], {}, "same-child-call-id")]
        return [assistant_output(name + " complete")]


@asynccontextmanager
async def selected_children_peer():
    peer = SelectedChildrenPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with TestServer(app, shutdown_timeout=1) as server:
        peer.url = str(server.make_url("/v1"))
        yield peer


async def test_child_step_stop_preserves_sibling_and_allows_successor(tmp_path, monkeypatch):
    context = {"tenant": "synthetic-tenant"}
    owners = {}
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    allow_successor = False

    @function_tool
    def first(ctx: ToolContext[dict[str, str]]) -> str:
        calls.append(ctx.submission)
        return "record found"

    async def select(step: ChildStepContext[dict[str, str]]):
        child = step.child
        assert step.context is context
        if child in calls:
            return []
        owners.setdefault(child.child_id, child)
        if len(owners) == 2:
            entered.set()
        await release.wait()
        if not allow_successor and child.child_id == next(iter(owners)):
            return StopTurn("synthetic-private-child-policy")
        return [first]

    async with (
        timeout(40),
        selected_children_peer() as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with Agent(
            instructions="Spawn the children.",
            model="gpt-5.5",
            tools=[first, NativeAgents(before_step=select, max_children=2)],
            provider=peer.provider(websocket=False),
        ).session(context=context, environment=declaration(executor)) as session:
            root = await session.submit("Spawn alpha and beta")
            try:
                await entered.wait()
                assert (await root.get_result()).final_output == "root complete"
                # Selector entry can precede native child-turn publication.
                await handles(session, 2)
                children = [child async for child in session.subagents()]
                stopped, surviving = sorted(
                    children, key=lambda child: child.id != next(iter(owners))
                )
                assert stopped.turn is not None and surviving.turn is not None
                assert not peer.child_requests
                release.set()
                with pytest.raises(NativeError, match="^Turn stopped by policy$"):
                    await stopped.turn.get_result()
                assert (await surviving.turn.get_result()).final_output.endswith("complete")
                assert calls == [owners[surviving.id]]
                assert all(
                    item["thread_id"] != stopped.id
                    for requests in peer.child_metadata.values()
                    for item in requests
                )
                allow_successor = True
                successor = await stopped.start("Read the record now")
                assert successor.turn_id != stopped.turn.turn_id
                assert (await successor.get_result()).final_output.endswith("complete")
                assert len(calls) == 2
                assert calls[-1].child_id == stopped.id
                assert calls[-1].turn_id == successor.turn_id
                assert (await root.get_result()).final_output == "root complete"
            finally:
                release.set()


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("cancel_one", [False, True])
async def test_overlapping_native_and_independent_children_keep_own_selected_plans(
    responses,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_one: bool,
) -> None:
    owners = {}
    calls = []
    entered = asyncio.Event()
    releases = {}
    joined = set()
    callback_exited = {}

    @function_tool
    def first(ctx: ToolContext[None]) -> str:
        calls.append(("first", ctx.submission))
        return "first accepted"

    @function_tool
    def second(ctx: ToolContext[None]) -> str:
        calls.append(("second", ctx.submission))
        return "second accepted"

    async def select(step: ChildStepContext[None]):
        child = step.child
        assert step.context is None
        assert isinstance(child, ChildInvocation)
        if any(identity == child for _, identity in calls):
            return []
        if child.child_id not in owners:
            owners[child.child_id] = (child, first if not owners else second)
        else:
            assert owners[child.child_id][0] == child
        # Preparations can retire before sampling. Keep the selected leaf until
        # this child actually calls it, and track the currently waiting callback.
        callback_exited[child.child_id] = asyncio.Event()
        releases.setdefault(child.child_id, asyncio.Event())
        if len(owners) == 2:
            entered.set()
        try:
            await releases[child.child_id].wait()
            return [owners[child.child_id][1]]
        finally:
            joined.add(child.child_id)
            callback_exited[child.child_id].set()

    native = NativeAgents(before_step=select, max_children=2)
    async with (
        timeout(60),
        selected_children_peer() as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with Agent(
            instructions="Spawn the children",
            model="gpt-5.5",
            tools=[first, second, native],
            provider=peer.provider(websocket=False),
        ).session(environment=declaration(executor)) as session:
            root = await session.submit(
                "Spawn alpha and beta",
                before_step=lambda turn, step_index: [native] if not step_index else [],
            )
            await entered.wait()
            assert (await root.get_result()).final_output == "root complete"
            child_turns = await handles(session, 2)
            assert not peer.child_requests

            @function_tool
            def independent() -> str:
                return "independent receipt"

            responses.respond(function_call("independent", {}, "own-catalog"))
            responses.respond(assistant_output("independent complete"))
            async with Agent(
                instructions="Independent Session",
                tools=[independent],
                provider=provider(responses),
            ).session() as other:
                await (
                    await other.submit("Own catalog", before_step=lambda turn, step_index: None)
                ).get_result()
                assert all(
                    set(declarations(request)) == {("functions", "independent")}
                    for request in responses.requests
                )
            cancelled = next(iter(owners)) if cancel_one else None
            if cancelled is not None:
                turn = next(turn for turn in child_turns if turn.child_id == cancelled)
                subagent = next(
                    child
                    for child in [item async for item in session.subagents()]
                    if child.id == cancelled
                )
                native_entered = asyncio.Event()
                native_release = asyncio.Event()
                selected_name = owners[cancelled][1].name

                async def hold_selected_call(event):
                    item = event.get("item", {})
                    if (
                        item.get("type") == "function_call"
                        and item.get("call_id") == "same-child-call-id"
                        and item.get("name") == selected_name
                    ):
                        request_meta = metadata(peer.requests[-1], peer.request_headers[-1])
                        assert request_meta["thread_id"] == cancelled
                        native_entered.set()
                        await native_release.wait()

                peer.before_event = hold_selected_call
                releases[cancelled].set()
                try:
                    await native_entered.wait()
                    assert owners[cancelled][0].turn_id == turn.turn_id
                    assert not calls
                    assert all(
                        not gate.is_set()
                        for child_id, gate in releases.items()
                        if child_id != cancelled
                    )
                    assert await subagent.try_cancel(turn)
                    with pytest.raises(NativeError, match="interrupted"):
                        await turn.get_result()
                    await callback_exited[cancelled].wait()
                    assert cancelled in joined
                    assert all(
                        not callback_exited[child_id].is_set()
                        for child_id in owners
                        if child_id != cancelled
                    )
                finally:
                    native_release.set()
                    for gate in releases.values():
                        gate.set()
            for gate in releases.values():
                gate.set()
            surviving = [turn for turn in child_turns if turn.child_id != cancelled]
            await asyncio.gather(*(turn.get_result() for turn in surviving))
            assert len(calls) == len(surviving)
            for tool_name, identity in calls:
                assert isinstance(identity, ChildInvocation)
                owner, selected = owners[identity.child_id]
                assert owner == identity and selected.name == tool_name
                assert identity.child_id != cancelled
            for name, requests in peer.child_requests.items():
                identity = peer.child_metadata[name][0]["thread_id"]
                assert set(declarations(requests[0])) == {("functions", owners[identity][1].name)}
                if identity == cancelled:
                    assert len(requests) == 1
                else:
                    assert len(requests) == 2
                    assert not declarations(requests[1])


@pytest.mark.requires_native_linux_sandbox
async def test_manual_restore_enables_destination_child_selector_with_implicit_defaults(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read before spawning", input_type=LookupArgs, output_type=str
    )
    selected = []

    async def choose(step: ChildStepContext[None]):
        child = step.child
        step_index = step.step_index
        selected.append((child, step_index))
        return []

    async with (
        timeout(45),
        selected_children_peer() as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.roles = ("default",)
        ordinary = peer.response

        def respond():
            body = peer.requests[-1]
            meta = metadata(body, peer.request_headers[-1])
            if meta["agent_name"] == "/root":
                return ordinary()
            name = meta["agent_name"].rsplit("/", 1)[-1]
            peer.child_requests.setdefault(name, []).append(body)
            peer.child_metadata.setdefault(name, []).append(meta)
            return [assistant_output("child complete")]

        peer.response = respond
        peer.action = function_call("lookup", {"key": "before-child"}, "manual-before-child")
        agent = Agent(
            instructions="Read then spawn a child",
            model="gpt-5.5",
            tools=[lookup, NativeAgents(max_children=1)],
            provider=peer.provider(websocket=False),
        )
        async with agent.session(environment=declaration(executor)) as source:
            await source.submit("Read before spawning")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
            assert saved.operation_tools is None
        destination = replace(
            agent, tools=[lookup, NativeAgents(before_step=choose, max_children=1)]
        )
        async with destination.session(state=saved, environment=declaration(executor)) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("accepted")
            assert (await restored.restored_turn.get_result()).final_output == "root complete"
            (child,) = await handles(restored, 1)
            assert (await child.get_result()).final_output == "child complete"
            assert selected
            assert [index for _, index in selected] == list(range(len(selected)))
            assert all(
                isinstance(identity, ChildInvocation) and identity.child_id == child.child_id
                for identity, _ in selected
            )
            assert all(identity == selected[0][0] for identity, _ in selected)
            assert len(peer.child_requests["alpha"]) == 1
            assert not declarations(peer.child_requests["alpha"][0])
