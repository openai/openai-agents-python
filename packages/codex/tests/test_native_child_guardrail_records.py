"""A child handle published before SDK event registration retains its own records."""

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, GuardrailResult, NativeAgents, OutputGuardrail, Session
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import ChildPeer, metadata, trust
from tests.test_native_executor_tools import declaration


async def test_early_child_turn_resolves_later_guardrail_owner(tmp_path, monkeypatch):
    release_reader = asyncio.Event()
    reader_waiting = asyncio.Event()
    children_requested = asyncio.Event()
    original_reader = Session._read_child_event

    async def delayed_reader(self, event):
        if event["msg"]["type"] in {"task_started", "turn_started"}:
            reader_waiting.set()
            await release_reader.wait()
        await original_reader(self, event)

    monkeypatch.setattr(Session, "_read_child_event", delayed_reader)

    class Peer(ChildPeer):
        async def hold(self, event):
            pass

        def response(self):
            body = self.requests[-1]
            path = metadata(body, self.request_headers[-1])["agent_name"]
            if path == "/root":
                return super().response()
            self.child_requests.setdefault(path, []).append(body)
            if len(self.child_requests) == 1:
                children_requested.set()
            return [assistant_output(path.rsplit("/", 1)[-1] + " accepted")]

    def check(context, source):
        assert source is agent
        return GuardrailResult(False, {"child": context.subagent_id})

    peer = Peer(("default",))
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Spawn alpha.",
            model="gpt-5.5",
            tools=[NativeAgents(max_children=1)],
            provider=peer.provider(websocket=False),
            output_guardrails=[OutputGuardrail("owner-check", check)],
        )
        async with agent.session(environment=declaration(executor)) as session:
            submission = asyncio.create_task(session.submit("start"))
            try:
                await reader_waiting.wait()
                await children_requested.wait()
                early = [child async for child in session.subagents()]
                assert len(early) == 1
                turns = [child.turn for child in early]
                assert all(turn is not None for turn in turns)
                assert session._child_callbacks == {}
                assert all(turn.guardrail_results == () for turn in turns)
                release_reader.set()
                root = await submission
                assert (await root.get_result()).final_output == "root complete"
                for child, turn in zip(early, turns, strict=True):
                    assert turn is not None
                    result = await turn.get_result()
                    assert len(turn.guardrail_results) == 1
                    assert result.guardrail_results == turn.guardrail_results
                    record = turn.guardrail_results[0]
                    assert record.agent is agent
                    assert record.subagent_id == child.id
                    assert record.result.output_info == {"child": child.id}
            finally:
                release_reader.set()
                await asyncio.gather(submission, return_exceptions=True)
        assert all(len(turn.guardrail_results) == 1 for turn in turns)
