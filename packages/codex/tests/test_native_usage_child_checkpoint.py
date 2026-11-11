"""Settled native child charges across a supported root manual checkpoint."""

from dataclasses import replace

from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import FunctionTool, ToolContext, function_tool
from agents.state import SessionState
from agents.usage import TokenUsage
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_child_agents import (
    ChildPeer,
    agent_for,
    child_usage,
    collaboration,
    handles,
    metadata,
    trust,
)
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request


async def test_settled_child_usage_survives_root_manual_checkpoint(tmp_path, monkeypatch):
    class CheckpointPeer(ChildPeer):
        def __init__(self):
            super().__init__(("default",))
            self.root_requests = 0

        def response(self):
            body = self.requests[-1]
            meta = metadata(body, self.request_headers[-1])
            if meta["agent_name"] == "/root":
                self.root_requests += 1
                if self.root_requests == 1:
                    return [
                        collaboration(
                            "spawn_agent",
                            {"task_name": "alpha", "message": "Run metered."},
                            "spawn",
                        )
                    ]
                if self.root_requests == 2:
                    return [function_call("lookup", {"key": "A"}, "root-manual")]
                return [assistant_output("root complete")]
            self.child_metadata.setdefault("alpha", []).append(meta)
            calls = self.child_requests.setdefault("alpha", [])
            calls.append(body)
            if len(calls) == 1:
                return [function_call("metered", {}, "child-meter")]
            return [assistant_output("child complete")]

    effects = []

    @function_tool
    def metered(context: ToolContext[None]) -> str:
        effects.append(context.submission.child_id)
        context.report_usage(TokenUsage(3, 1, 0, 2, 0, 5))
        return "known"

    peer = CheckpointPeer()
    child_usage(peer, {"/root": (101, 7, 9, 2), "/root/alpha": (7, 3, 2, 1)})
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(40),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = agent_for(peer, executor)
        agent = replace(agent, tools=(*agent.tools, lookup, metered))
        async with agent.session(environment=declaration(executor)) as source:
            root = await source.submit("Begin")
            await next_request(source.interactions(), lookup)
            child = (await handles(source, 1))[0]
            completed = await child.get_result()
            assert completed.run_usage.requests == 3
            assert completed.run_usage.total_tokens == 25
            assert len(effects) == 1
            assert root.run_usage.requests == 5
            assert root.run_usage.total_tokens == 241
            saved = SessionState.from_json((await source.suspend()).to_json())
            assert saved.run_usage == root.run_usage
            assert saved.run_usage.requests == 5
            assert saved.run_usage.total_tokens == 241
        async with agent.session(state=saved, environment=declaration(executor)) as restored:
            request = await next_request(restored.interactions(), lookup)
            assert request.call_id == "root-manual"
            await request.respond("answer")
            result = await restored.restored_turn.get_result()
            assert result.final_output == "root complete"
            assert result.run_usage.requests == 6
            assert result.run_usage.total_tokens == 349
            assert len(effects) == 1
            assert completed.run_usage.requests == 3
            assert completed.run_usage.total_tokens == 25
