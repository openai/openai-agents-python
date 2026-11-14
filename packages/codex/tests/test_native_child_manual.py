"""Manual child replies retain their exact native owner after the root settles."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel, model_validator

from agents import (
    Agent,
    FunctionTool,
    InteractionExpiredError,
    NativeAgents,
    NativeError,
    NativeRole,
    Session,
)
from agents.observation import TurnFinished, TurnStarted
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_agent_observation import collect
from tests.test_native_child_agents import (
    ChildPeer,
    agent_for,
    captured_turn,
    collaboration,
    handles,
    metadata,
    trust,
)
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request


class ManualChildren(ChildPeer):
    def __init__(self, *, invalid_alpha: bool = False):
        super().__init__(("default", "default"))
        self.invalid_alpha = invalid_alpha

    def response(self):
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        if meta["agent_name"] == "/root":
            return super().response()
        name = meta["agent_name"].rsplit("/", 1)[-1]
        self.child_metadata.setdefault(name, []).append(meta)
        requests = self.child_requests.setdefault(name, [])
        requests.append(body)
        if len(requests) == 1:
            return [
                function_call(
                    "lookup",
                    {"key": 42 if self.invalid_alpha and name == "alpha" else name},
                    "same-manual-id",
                )
            ]
        return [assistant_output(name + " complete")]

    def application(self):
        app = web.Application()
        app.router.add_post("/v1/responses", self.handle)
        app.router.add_get("/v1/responses", self.websocket)
        app.on_shutdown.append(self.close_sockets)
        return app


def declaration_for(peer, executor, lookup):
    return Agent(
        instructions="Start children and let them read their own record.",
        model=agent_for(peer, executor).model,
        provider=peer.provider(websocket=False),
        tools=[
            lookup,
            NativeAgents(
                additional_roles=[
                    NativeRole(name="reader", instructions="Read a record.", description="Reader")
                ],
                max_children=2,
            ),
        ],
    )


@pytest.mark.parametrize("outcome", ["release", "close", "lease_observed", "lease_lookup"])
async def test_child_manual_reply_before_turn_observation(tmp_path, monkeypatch, outcome):
    validated: list[str] = []

    class Answer(BaseModel):
        value: str

        @model_validator(mode="after")
        def record_validation(self):
            validated.append(self.value)
            return self

    peer = ManualChildren()
    release = asyncio.Event()
    release_lookup = asyncio.Event()
    lease_events = []
    read_events = Session._read_events

    async def hold_reader(session):
        await release.wait()
        await read_events(session)

    monkeypatch.setattr(Session, "_read_events", hold_reader)
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=Answer
    )
    async with (
        timeout(30),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)

        @asynccontextmanager
        async def environment_source():
            lease_events.append("acquired")
            try:
                yield declaration(executor)
            finally:
                lease_events.append("released")

        needs_lease = outcome in {"lease_observed", "lease_lookup"}
        environment = environment_source() if needs_lease else declaration(executor)
        async with declaration_for(peer, executor, lookup).session(
            environment=environment
        ) as session:
            try:
                await session.submit("Start both children")
                # Opening a fresh cursor must work even if no native event
                # was delivered to Python before the tools became pending.
                while len(session._native._pending_manual_tools()) < 2:
                    await asyncio.sleep(0)
                stream = session.interactions()
                requests = [await next_request(stream, lookup), await next_request(stream, lookup)]
                by_name = {request.arguments.key: request for request in requests}
                assert set(by_name) == {"alpha", "beta"}
                assert all(session._native._check_manual_tool(req._grant) for req in requests)
                alpha = by_name["alpha"]
                assert await session._native._child_turn(alpha.subagent_id) is None
                lookup_entered = asyncio.Event()
                if outcome == "lease_lookup":
                    native = session._native

                    class HoldLookup:
                        def __getattr__(self, name):
                            return getattr(native, name)

                        async def _child_turn(self, child_id):
                            current = await native._child_turn(child_id)
                            if asyncio.current_task() is pending and child_id == alpha.subagent_id:
                                lookup_entered.set()
                                await release_lookup.wait()
                            return current

                    monkeypatch.setattr(session, "_native", HoldLookup())
                pending = asyncio.create_task(alpha.respond({"value": "answer-alpha"}))
                # Core has admitted both real manual calls, but the sole native
                # event reader cannot yet produce the child Turn receipt.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(pending), 0.1)
                assert validated == []
                assert all(len(items) == 1 for items in peer.child_requests.values())
                if outcome in {"lease_observed", "lease_lookup"}:
                    lease = session._environment_lease
                    assert lease is not None
                    assert lease_events == ["acquired"]
                    if outcome == "lease_lookup":
                        release.set()
                        await lookup_entered.wait()
                    # Hold teardown at its first real effect: the lease is
                    # revoked while the separately owned reply is awaiting
                    # native ownership. Its grant has not been retired yet.
                    lease._interrupted = True
                    assert session._native._check_manual_tool(alpha._grant)
                    release.set()
                    release_lookup.set()
                    with pytest.raises(InteractionExpiredError, match="expired"):
                        await pending
                    assert validated == []
                    assert all(len(items) == 1 for items in peer.child_requests.values())
                elif outcome == "close":
                    closing = asyncio.create_task(session._aclose())
                    while not session._closing:
                        await asyncio.sleep(0)
                    release.set()
                    with pytest.raises((ValueError, RuntimeError), match="closed|expired"):
                        await pending
                    await closing
                    assert validated == []
                    assert all(len(items) == 1 for items in peer.child_requests.values())
                else:
                    release.set()
                    assert await pending is None
                    await by_name["beta"].respond({"value": "answer-beta"})
                    turns = {child.id: child.turn async for child in session.subagents()}
                    alpha_turn = turns[alpha.subagent_id]
                    beta_turn = turns[by_name["beta"].subagent_id]
                    assert alpha_turn is not None and beta_turn is not None
                    assert alpha_turn.child_id == alpha.subagent_id
                    assert alpha_turn.turn_id == alpha.turn_id
                    assert beta_turn.child_id == by_name["beta"].subagent_id
                    results = await asyncio.gather(alpha_turn.get_result(), beta_turn.get_result())
                    assert {result.final_output for result in results} == {
                        "alpha complete",
                        "beta complete",
                    }
                    assert validated == ["answer-alpha", "answer-beta"]
                    with pytest.raises(InteractionExpiredError, match="expired"):
                        await alpha.respond({"value": "duplicate"})
                    assert validated == ["answer-alpha", "answer-beta"]
            finally:
                release.set()
                release_lookup.set()
        assert lease_events == (["acquired", "released"] if needs_lease else [])


async def test_manual_siblings_after_root_completion_keep_unique_grants(tmp_path, monkeypatch):
    peer = ManualChildren()
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with (
        timeout(30),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with declaration_for(peer, executor, lookup).session(
            environment=declaration(executor)
        ) as session:
            root = await session.submit("Start both children")
            assert (await (root).get_result()).final_output == "root complete"
            stream = session.interactions()
            requests = [await next_request(stream, lookup), await next_request(stream, lookup)]
            by_name = {request.arguments.key: request for request in requests}
            await handles(session, 2)
            listed = {child.id: child async for child in session.subagents()}
            started = {}
            for name, request in by_name.items():
                turn = listed[request.subagent_id].turn
                assert turn is not None
                await turn.steer("Continue this child's lookup.")
                started[name] = turn
            assert set(by_name) == {"alpha", "beta"}
            assert len({request._invocation for request in requests}) == 2
            assert all(request.session_id == session.id for request in requests)
            assert all(request.subagent_id is not None for request in requests)
            assert len({request.subagent_id for request in requests}) == 2
            with pytest.raises(RuntimeError, match="child"):
                await session.save_state()
            with pytest.raises(RuntimeError, match="pending manual-tool boundary"):
                await session.suspend()
            with pytest.raises(ValueError, match="validation"):
                await by_name["alpha"].respond(42)
            assert all(len(items) == 1 for items in peer.child_requests.values())
            alias_stream = session.interactions()
            aliases = [
                await next_request(alias_stream, lookup),
                await next_request(alias_stream, lookup),
            ]
            selected = {
                name: [
                    request for request in aliases if request.submission_id == turn.submission_id
                ]
                for name, turn in started.items()
            }
            assert {name: len(matches) for name, matches in selected.items()} == {
                "alpha": 1,
                "beta": 1,
            }
            assert {name: matches[0].arguments.key for name, matches in selected.items()} == {
                "alpha": "alpha",
                "beta": "beta",
            }
            assert all(
                listed[request.subagent_id].turn.submission_id == started[name].submission_id
                for name, request in by_name.items()
            )
            alpha_alias = selected["alpha"][0]
            competing = await asyncio.gather(
                by_name["alpha"].respond("answer-alpha"),
                alpha_alias.respond("answer-alpha"),
                return_exceptions=True,
            )
            assert sum(value is None for value in competing) == 1
            assert sum(isinstance(value, InteractionExpiredError) for value in competing) == 1
            await by_name["beta"].respond("answer-beta")
            alpha_turn = listed[by_name["alpha"].subagent_id].turn
            beta_turn = listed[by_name["beta"].subagent_id].turn
            assert alpha_turn is not None and beta_turn is not None
            assert alpha_turn.child_id == by_name["alpha"].subagent_id
            assert beta_turn.child_id == by_name["beta"].subagent_id
            assert alpha_turn.submission_id == started["alpha"].submission_id
            assert beta_turn.submission_id == started["beta"].submission_id
            results = await asyncio.gather((alpha_turn).get_result(), (beta_turn).get_result())
            assert {result.final_output for result in results} == {
                "alpha complete",
                "beta complete",
            }
            for name in ("alpha", "beta"):
                assert (
                    tool_output(peer.child_requests[name][1], "same-manual-id") == "answer-" + name
                )
            saved = await session.save_state()
            assert len(saved.descendants) == 2
            with pytest.raises(InteractionExpiredError, match="expired"):
                await by_name["beta"].respond("duplicate")


@pytest.mark.parametrize("close_session", [False, True])
async def test_manual_child_grants_expire_on_native_cancel_or_close(
    tmp_path, monkeypatch, close_session
):
    peer = ManualChildren()
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with (
        timeout(30),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with declaration_for(peer, executor, lookup).session(
            environment=declaration(executor)
        ) as session:
            await (await session.submit("Start both children")).get_result()
            stream = session.interactions()
            requests = [await next_request(stream, lookup), await next_request(stream, lookup)]
            turns = await handles(session, 2)
            if close_session:
                await session._aclose()
            else:
                assert all(
                    await asyncio.gather(
                        *(session._native._cancel_child_turn(turn._native) for turn in turns)
                    )
                )
            for request in requests:
                with pytest.raises((ValueError, RuntimeError), match="closed|expired"):
                    await request.respond("late")
            assert all(len(items) == 1 for items in peer.child_requests.values())
            for turn in turns:
                events = await asyncio.wait_for(collect(turn.events()), 2)
                assert sum(isinstance(event, TurnStarted) for event in events) == 1
                assert isinstance(events[-1], TurnFinished)
                assert events[-1].status == "cancelled"


async def test_invalid_manual_child_input_fails_only_its_owner_without_a_reader(
    tmp_path, monkeypatch
):
    peer = ManualChildren(invalid_alpha=True)
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with (
        timeout(30),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with declaration_for(peer, executor, lookup).session(
            environment=declaration(executor)
        ) as session:
            await (await session.submit("Start both children")).get_result()
            turns = await handles(session, 2)
            while "alpha" not in peer.child_metadata:
                await asyncio.sleep(0)
            alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
            alpha = next(turn for turn in turns if turn.child_id == alpha_id)
            with pytest.raises(NativeError, match="Manual tool arguments"):
                await (alpha).get_result()
            beta = await next_request(session.interactions(), lookup)
            assert beta.arguments.key == "beta"
            await beta.respond("answer-beta")
            turn = next(turn for turn in turns if turn.child_id == beta.subagent_id)
            assert (await turn.get_result()).final_output == "beta complete"


async def test_listing_cannot_graft_a_reloaded_manual_child_onto_its_old_turn(
    tmp_path, monkeypatch
):
    inspections: list[str] = []
    submitting_task = asyncio.current_task()

    class Arguments(BaseModel):
        key: str

        @model_validator(mode="after")
        def validate_key(self):
            inspections.append("input:" + self.key)
            return self

        def __deepcopy__(self, memo=None):
            # A passive reader may copy the live replacement request in parallel.
            if asyncio.current_task() is submitting_task:
                inspections.append("copy:" + self.key)
            return super().__deepcopy__(memo)

    class Answer(BaseModel):
        value: str

        @model_validator(mode="after")
        def validate_value(self):
            inspections.append("output:" + self.value)
            return self

    class ReusableManualChild(ManualChildren):
        def response(self):
            body = self.requests[-1]
            meta = metadata(body, self.request_headers[-1])
            if meta["agent_name"] == "/root":
                return ChildPeer.response(self)
            name = meta["agent_name"].rsplit("/", 1)[-1]
            self.child_metadata.setdefault(name, []).append(meta)
            requests = self.child_requests.setdefault(name, [])
            requests.append(body)
            if name == "alpha" and len(requests) in (1, 3):
                return [function_call("lookup", {"key": "alpha"}, "same-manual-id")]
            output = assistant_output(name + " complete")
            if name == "beta":
                output["item"]["id"] = "done-beta"
            return [output]

    peer = ReusableManualChild()
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=Arguments, output_type=Answer
    )
    async with (
        timeout(45),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = declaration_for(peer, executor, lookup)
        async with agent.session(environment=declaration(executor)) as session:
            try:
                await (await session.submit("Start alpha and beta")).get_result()
                await peer.entered["beta"].wait()
                request = await next_request(session.interactions(), lookup)
                alpha_id = request.subagent_id
                assert alpha_id is not None
                await request.respond({"value": "original"})
                turns = {child.id: child.turn async for child in session.subagents()}
                original = turns[alpha_id]
                assert original is not None
                old_result = await original.get_result()
                assert old_result.final_output == "alpha complete"
                observed = asyncio.Event()
                release_snapshot = asyncio.Event()
                native = session._native

                async def read_alpha():
                    async for child in session.subagents():
                        if child.id == alpha_id:
                            return child
                    pytest.fail("Original child is missing from the durable list")

                # Delay only the first real getter in this specific listing.
                # Every reload, reply, and Core operation still uses the actual
                # native session. No getter synthesizes or changes a receipt.
                class Capture:
                    def __getattr__(self, name):
                        return getattr(native, name)

                    async def _child_turn(self, child_id):
                        result = await native._child_turn(child_id)
                        if (
                            asyncio.current_task() is listing
                            and child_id == alpha_id
                            and not observed.is_set()
                        ):
                            observed.set()
                            await release_snapshot.wait()
                        return result

                    async def _child_instance(self, child_id):
                        result = await native._child_instance(child_id)
                        if (
                            asyncio.current_task() is listing
                            and child_id == alpha_id
                            and not observed.is_set()
                        ):
                            observed.set()
                            await release_snapshot.wait()
                        return result

                monkeypatch.setattr(session, "_native", Capture())
                listing = asyncio.create_task(read_alpha())
                await observed.wait()
                # With beta running, only finished alpha is unloadable. Gamma
                # consumes the second native slot; its completion permits a
                # real followup to reload the same durable alpha thread.
                peer.action = collaboration(
                    "spawn_agent",
                    {"task_name": "gamma", "message": "Finish now", "fork_turns": "none"},
                    "evict-idle-alpha",
                )
                await (await session.submit("Start gamma")).get_result()
                while (
                    "gamma" not in peer.child_metadata
                    or (
                        gamma_turn := await captured_turn(
                            session, peer.child_metadata["gamma"][0]["thread_id"]
                        )
                    )
                    is None
                ):
                    await asyncio.sleep(0)
                assert (await gamma_turn.get_result()).final_output == "gamma complete"
                peer.action = collaboration(
                    "followup_task",
                    {"target": "/root/alpha", "message": "Look up alpha again"},
                    "reload-alpha",
                )
                await (await session.submit("Reload alpha")).get_result()
                current_request = await next_request(session.interactions(), lookup)
                assert (current_request.subagent_id, current_request.call_id) == (
                    alpha_id,
                    request.call_id,
                )
                release_snapshot.set()
                stale = await listing
                assert stale.turn is not None
                assert stale.turn.submission_id == original.submission_id
                assert stale.turn.instance_id == stale._instance_id
                inspections.clear()
                with pytest.raises((RuntimeError, ValueError)):
                    await stale.start("MUST_NOT_REACH_THE_REPLACEMENT")
                with pytest.raises((RuntimeError, ValueError)):
                    await stale.start(
                        {
                            "type": "function_call_output",
                            "call_id": "same-manual-id",
                            "output": '{"value":"MUST_NOT_VALIDATE"}',
                        }
                    )
                with pytest.raises((RuntimeError, ValueError, TypeError)):
                    await stale.start(current_request)
                assert inspections == []
                while True:
                    current = next(
                        child
                        for child in [item async for item in session.subagents()]
                        if child.id == alpha_id
                    )
                    if current.turn is not None:
                        break
                    await asyncio.sleep(0)
                assert current.turn is not None
                assert current.turn.instance_id != original.instance_id
                assert current.turn.submission_id == current_request.submission_id
                await current_request.respond({"value": "current"})
                assert (await current.turn.get_result()).final_output == "alpha complete"
                assert (await original.get_result()).turn is original
                outputs = [
                    tool_output({"input": [item]}, "same-manual-id")
                    for item in peer.child_requests["alpha"][3]["input"]
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == "same-manual-id"
                ]
                assert outputs == ['{"value":"original"}', '{"value":"current"}']
                assert "MUST_NOT" not in str(peer.child_requests)
                peer.release["beta"].set()
                beta_id = peer.child_metadata["beta"][0]["thread_id"]
                beta = next(
                    child
                    for child in [item async for item in session.subagents()]
                    if child.id == beta_id
                )
                assert beta.turn is not None
                assert (await beta.turn.get_result()).final_output == "beta complete"
            finally:
                for release in peer.release.values():
                    release.set()
