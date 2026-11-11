"""Input-only declarations retain completion ownership without replaying input checks."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    FunctionTool,
    GuardrailResult,
    GuardrailTripwire,
    InputGuardrail,
    NativeAgents,
    OutputGuardrail,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_child_agents import ChildPeer, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_profiles import save_at_manual_boundary
from tests.test_native_manual_tools import LookupArgs, next_request


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("block_output", [False, True])
async def test_input_only_manual_restore_completes_without_replaying_initial_checks(
    responses, parallel, block_output
):
    checked = []
    converted = []

    def input_check(turn, context, agent, value):
        checked.append(value)
        return GuardrailResult(False)

    def output_check(request, agent):
        converted.append(request.final_output)
        return GuardrailResult(True)

    lookup = FunctionTool(
        name="lookup", description="Read a record.", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=provider(responses),
        input_guardrails=[InputGuardrail("initial", input_check, run_in_parallel=parallel)],
        output_guardrails=[OutputGuardrail("final", output_check)] if block_output else [],
    )
    responses.respond(function_call("lookup", {"key": "record"}, "manual-call"))
    async with timeout(30):
        async with agent.session() as original:
            await original.submit("original-input")
            await next_request(original.interactions(), lookup)
            saved = await save_at_manual_boundary(original)
        assert checked == ["original-input"]
        async with agent.session(state=saved) as restored:
            request = await next_request(restored.interactions(), lookup)
            responses.respond(assistant_output("restored complete"))
            await request.respond("record-value")
            if block_output:
                with pytest.raises(GuardrailTripwire):
                    await restored.restored_turn.get_result()
                assert converted == ["restored complete"]
            else:
                assert (
                    await restored.restored_turn.get_result()
                ).final_output == "restored complete"
        assert checked == ["original-input"]
        assert len(responses.requests) == 2


@pytest.mark.parametrize("parallel", [False, True])
async def test_root_only_agent_input_guard_does_not_own_child_checks(
    tmp_path, monkeypatch, parallel
):
    roots = []

    def root_check(turn, context, agent, value):
        roots.append(value)
        return GuardrailResult(False)

    class Peer(ChildPeer):
        async def hold(self, event):
            pass

        def response(self):
            body = self.requests[-1]
            path = metadata(body, self.request_headers[-1])["agent_name"]
            if path == "/root":
                return super().response()
            self.child_requests.setdefault(path, []).append(body)
            return [assistant_output("child complete")]

    peer = Peer(("default", "default"))
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
            instructions="Spawn alpha and beta.",
            model="gpt-5.5",
            tools=[NativeAgents(max_children=2)],
            provider=peer.provider(websocket=False),
            input_guardrails=[InputGuardrail("root-only", root_check, run_in_parallel=parallel)],
        )
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("root-only-input")
            assert (await root.get_result()).final_output == "root complete"
            await handles(session, 2)
            children = [child async for child in session.subagents()]
            assert len(children) == 2
            for child in children:
                assert child.turn is not None
                assert (await child.turn.get_result()).final_output == "child complete"
            assert roots == ["root-only-input"]
