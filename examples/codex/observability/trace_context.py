"""Continue an explicit parent through native tools and a second worker.

Run with ``python -m examples.codex.observability.trace_context``. All provider and
collector traffic is synthetic and stays on loopback.
"""

from __future__ import annotations

import asyncio

from agents import Agent, ToolContext, TraceContext, function_tool, telemetry
from agents.testing import ScriptedResponses, text, tool_call

from .trace_walkthrough import Collector


async def exercise(collector_url: str) -> None:
    await telemetry.configure(
        traces=telemetry.OtlpHttp(endpoint=collector_url + "/v1/traces", protocol="json")
    )
    try:

        @function_tool
        async def delegate(ctx: ToolContext[None]) -> str:
            """Run synthetic work under the issuing native tool span."""
            # An application HTTP client can send these two headers, or create
            # its own client span with this explicit parent before injecting.
            # No Python provider or ambient ContextVar is adopted by the SDK.
            assert ctx.trace_context is not None
            headers = ctx.trace_context.to_headers()
            async with ScriptedResponses([text("Worker finished")]) as worker:
                result = await Agent(
                    instructions="Complete the synthetic task.", provider=worker.provider
                ).run("Work", parent_trace=headers)
                return result.final_output

        parent = TraceContext(traceparent="00-11111111111111111111111111111111-2222222222222222-01")
        async with ScriptedResponses(
            [tool_call("delegate", {}, call_id="delegate-1")], [text("Done")]
        ) as peer:
            agent = Agent(
                instructions="Delegate the synthetic task.",
                tools=[delegate],
                provider=peer.provider,
            )
            result = await agent.run("Start", parent_trace=parent)
            print(result.final_output)

        # Inherited original JSON, with a retained context after scope end.
        with telemetry.trace("Local metadata", options={"metadata": {"tenant": "example"}}) as root:
            with telemetry.span("Read metadata") as child:
                print(child.context.trace_metadata)
        snapshot = dict(root.trace_metadata or {})
        snapshot["tenant"] = "local copy"
        print(root.trace_metadata)  # {"tenant": "example"}; no native metadata update.
    finally:
        await telemetry.shutdown()


def main() -> None:
    # Telemetry initialization and shutdown belong to one process, outside all
    # Session lifetimes. Fresh workers receive fresh host carriers; checkpoints
    # contain history and accepted receipts, never an active trace context.
    with Collector().serve() as collector:
        asyncio.run(exercise(collector.url))
        count = sum(
            len(scope["spans"])
            for batch in collector.snapshot()
            for resource in batch["data"]["resourceSpans"]
            for scope in resource["scopeSpans"]
        )
        print(f"Received {count} native spans")


if __name__ == "__main__":
    main()
