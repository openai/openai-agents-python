"""Export native ancestry across an actual official A2A HTTP boundary."""

from __future__ import annotations

import asyncio
import json
import os
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


PROMPT = "PRIVATE_A2A_PROMPT_CANARY"
ARGUMENT = "PRIVATE_A2A_ARGUMENT_CANARY"
RESULT = "PRIVATE_A2A_RESULT_CANARY"


async def configure_private(collector, service):
    telemetry.set_sensitive_data_logging_enabled(False)
    await telemetry.configure(
        traces=telemetry.OtlpHttp(endpoint=collector + "/v1/traces", protocol="json"),
        log_user_prompt=False,
        log_agent_responses=False,
        tool_result_max_bytes=0,
        span_attributes={"business.service": service, "business.specialist": "approved-specialist"},
    )


async def privacy_receiver(collector, output, directory):
    await configure_private(collector, "specialist")
    receipt = {"pid": os.getpid()}

    @function_tool
    def private_probe(ctx: ToolContext, argument: str) -> str:
        assert argument == ARGUMENT
        receipt["remote_turn"] = ctx.submission.turn_id
        with telemetry.span("receiver.child", options={"include_sensitive_data": True}) as child:
            receipt["child_sensitive"] = child.context.include_sensitive_data
        return RESULT

    try:
        async with ScriptedResponses(
            [tool_call("private_probe", {"argument": ARGUMENT}, call_id="remote-private-call")],
            [text(RESULT)],
        ) as peer:
            remote = Agent(
                instructions="Synthetic specialist", tools=[private_probe], provider=peer.provider
            )
            store = HostStore(Path(directory))

            def factory(binding, context):
                receipt["received"] = dict(context.state["parent_trace"])
                receipt["binding_principal"] = binding.principal
                return NativeA2AAgent(agent=remote, parent_trace=context.state["parent_trace"])

            executor = NativeA2AExecutor(agent_factory=factory, store=store)
            async with serve(lambda url: service(executor, store, url)) as url:
                print(json.dumps({"url": url}), flush=True)
                await asyncio.to_thread(sys.stdin.readline)
            peer.assert_exhausted()
            with store.db() as db:
                snapshot = db.execute("SELECT snapshot FROM public").fetchone()[0]
            receipt["private_application_state"] = RESULT.encode() in snapshot
    finally:
        flush = await telemetry.force_flush()
        receipt["flush"] = flush.status
        await telemetry.shutdown()
        Path(output).write_text(json.dumps(receipt))


async def privacy_root(collector, output, directory):
    await configure_private(collector, "coordinator")
    remote_output = str(Path(output).with_suffix(".receiver.json"))
    receiver = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.a2a_trace_worker",
        collector,
        remote_output,
        directory,
        "receiver",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    receipt = {"pid": os.getpid()}
    try:
        line = await asyncio.wait_for(receiver.stdout.readline(), 20)
        if not line:
            raise AssertionError((await receiver.stderr.read()).decode())
        url = json.loads(line)["url"]
        registry = {"approved-specialist": endpoint(url)}

        @function_tool
        async def outgoing(ctx: ToolContext, argument: str) -> str:
            assert argument == ARGUMENT
            receipt["outgoing"] = ctx.trace_context.to_headers()
            with telemetry.span(
                "coordinator.child", options={"include_sensitive_data": True}
            ) as child:
                receipt["child_sensitive"] = child.context.include_sensitive_data
            async with connect_a2a(
                registry["approved-specialist"], authorization=token, parent_trace=ctx.trace_context
            ) as client:
                async for event in client.send_message(
                    p.SendMessageRequest(
                        message=new_text_message(PROMPT, role=p.ROLE_USER),
                        configuration=p.SendMessageConfiguration(return_immediately=True),
                    )
                ):
                    if event.HasField("task"):
                        task = event.task
                        break
                final = await completed(client, task.id)
                assert final.artifacts[0].parts[0].text == RESULT
            return RESULT

        async with ScriptedResponses(
            [tool_call("outgoing", {"argument": ARGUMENT}, call_id="outbound-private-call")],
            [text("public coordinator answer")],
        ) as peer:
            coordinator = Agent(
                instructions="Synthetic coordinator", tools=[outgoing], provider=peer.provider
            )
            with telemetry.trace(
                "private A2A workflow",
                options={
                    "include_sensitive_data": False,
                    "metadata": {"business.route": "standard"},
                },
            ):
                result = await coordinator.run(PROMPT)
            assert result.final_output == "public coordinator answer"
            peer.assert_exhausted()
    finally:
        if receiver.returncode is None:
            receiver.stdin.write(b"stop\n")
            await receiver.stdin.drain()
        _, errors = await asyncio.wait_for(receiver.communicate(), 20)
        assert receiver.returncode == 0, errors.decode()
        receipt["receiver"] = json.loads(Path(remote_output).read_text())
        flush = await telemetry.force_flush()
        receipt["flush"] = flush.status
        await telemetry.shutdown()
        Path(output).write_text(json.dumps(receipt))


async def main():
    collector, output, directory, *mode = sys.argv[1:]
    if mode:
        if mode[0] == "receiver":
            await privacy_receiver(collector, output, directory)
        else:
            await privacy_root(collector, output, directory)
        return
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
