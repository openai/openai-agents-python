"""Export native ancestry across an actual official A2A HTTP boundary."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from a2a.helpers.proto_helpers import new_text_message
from a2a.types import a2a_pb2 as p

from agents import Agent, ToolContext, TraceContext, function_tool, telemetry
from agents.a2a import NativeA2AAgent, NativeA2AExecutor, connect_a2a
from agents.testing import ScriptedResponses, text, tool_call
from tests.a2a_helpers import HostStore
from tests.a2a_http import serve, service
from tests.test_a2a_client import completed, endpoint, token

PARENT = "00-11111111111111111111111111111111-2222222222222222-01"


async def main():
    collector, output, directory = sys.argv[1:]
    await telemetry.configure(
        traces=telemetry.OtlpHttp(endpoint=collector + "/v1/traces", protocol="json")
    )
    receipt = {}
    try:

        @function_tool
        def remote_identity(ctx: ToolContext) -> str:
            receipt["remote_turn"] = ctx.submission.turn_id
            assert ctx.trace_context is not None
            return "remote identity"

        async with ScriptedResponses(
            [tool_call("remote_identity", {}, call_id="remote-call")], [text("remote answer")]
        ) as remote_peer:
            remote = Agent(
                instructions="Synthetic remote specialist",
                tools=[remote_identity],
                provider=remote_peer.provider,
            )

            def factory(binding, context):
                receipt["received"] = dict(context.state["parent_trace"])
                return NativeA2AAgent(agent=remote, parent_trace=context.state["parent_trace"])

            store = HostStore(Path(directory))
            executor = NativeA2AExecutor(agent_factory=factory, store=store)
            async with serve(lambda url: service(executor, store, url)) as url:

                @function_tool
                async def outgoing(ctx: ToolContext) -> str:
                    assert ctx.trace_context is not None
                    receipt["outgoing"] = ctx.trace_context.to_headers()
                    async with connect_a2a(
                        endpoint(url), authorization=token, parent_trace=ctx.trace_context
                    ) as client:
                        async for event in client.send_message(
                            p.SendMessageRequest(
                                message=new_text_message("Synthetic remote task", role=p.ROLE_USER),
                                configuration=p.SendMessageConfiguration(return_immediately=True),
                            )
                        ):
                            if event.HasField("task"):
                                task = event.task
                                break
                        result = await completed(client, task.id)
                        return result.artifacts[0].parts[0].text

                async with ScriptedResponses(
                    [tool_call("outgoing", {}, call_id="outbound-call")],
                    [text("coordinator answer")],
                ) as coordinator_peer:
                    coordinator = Agent(
                        instructions="Synthetic coordinator",
                        tools=[outgoing],
                        provider=coordinator_peer.provider,
                    )
                    async with coordinator.session(
                        parent_trace=TraceContext(traceparent=PARENT, tracestate="synthetic=host")
                    ) as session:
                        turn = await session.submit("Call the remote specialist")
                        receipt["local_turn"] = turn.turn_id
                        assert (await turn.get_result()).final_output == "coordinator answer"
    finally:
        await telemetry.shutdown()
    Path(output).write_text(json.dumps(receipt))


if __name__ == "__main__":
    asyncio.run(main())
