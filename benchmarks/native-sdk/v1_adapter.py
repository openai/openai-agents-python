"""Exercise the native SDK only through its public application API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from time import perf_counter_ns

from agents import Agent, NativeProvider, function_tool, telemetry


class Adapter:
    def __init__(
        self,
        base_url: str,
        model: str = "gpt-5.1-codex",
        stream: bool = False,
        *,
        with_tool: bool = False,
        sync_tool: bool = False,
        response_mode: str = "streaming",
        transport: str = "http",
        default_host: bool = False,
        provider_mode: str = "caller-owned",
    ):
        if provider_mode not in ("caller-owned", "omitted"):
            raise ValueError("Unknown native Provider mode")
        if provider_mode == "omitted" and (
            not default_host or with_tool or stream or response_mode != "streaming"
        ):
            raise ValueError("Omitted Provider mode: default-host final-only no-tool calls only")
        if transport not in ("http", "websocket") or (
            transport == "websocket" and response_mode != "streaming"
        ):
            raise ValueError("WebSocket transport requires streaming response mode")
        if response_mode not in ("streaming", "buffered"):
            raise ValueError("Unknown native response mode")
        if response_mode == "buffered" and stream:
            raise ValueError("Buffered benchmarks require final-result workloads")
        # Omit the new keyword by default so older native SDK wheels still work.
        provider_options = {"response_mode": "buffered"} if response_mode == "buffered" else {}
        self.stream = stream
        self.tool_calls = 0
        if not default_host:
            provider_options.update(base_url=base_url, supports_websockets=transport == "websocket")
        self.provider = (
            NativeProvider(api_key="synthetic-benchmark-key", **provider_options)
            if provider_mode == "caller-owned"
            else None
        )

        def synchronous_echo(value: str) -> str:
            """Return the supplied value unchanged."""
            self.tool_calls += 1
            return value

        async def asynchronous_echo(value: str) -> str:
            """Return the supplied value unchanged."""
            self.tool_calls += 1
            return value

        callback = synchronous_echo if sync_tool else asynchronous_echo
        callback.__name__ = "echo"
        tools = [function_tool(callback)] if with_tool else []
        self.agent = Agent(
            name="benchmark",
            instructions="Follow the benchmark request and return the result.",
            model=model,
            tools=tools,
            **({"provider": self.provider} if self.provider is not None else {}),
        )

    async def __aenter__(self):
        # All exporters default to None. Own this process-wide configuration
        # explicitly, matching the stable adapter's disabled tracing.
        await telemetry.configure()
        if self.provider is not None:
            await self.provider.__aenter__()
        return self

    async def __aexit__(self, *exc):
        try:
            if self.provider is not None:
                await self.provider.__aexit__(*exc)
        finally:
            await telemetry.shutdown()

    async def run(self, prompt: str) -> dict:
        if self.stream:
            async with self.conversation() as conversation:
                return await conversation.run(prompt)
        result = await self.agent.run(prompt)
        return {"text": result.final_output, "deltas": [], "first_delta_ns": None}

    @asynccontextmanager
    async def conversation(self):
        if self.provider is None:
            raise ValueError("Omitted Provider mode: fresh Agent.run calls only")
        async with self.agent.session() as session:
            yield Conversation(session, self.stream)


class Conversation:
    def __init__(self, session, stream):
        self.session = session
        self.stream = stream
        self.after = 0

    async def run(self, prompt: str) -> dict:
        turn = await self.session.submit(prompt)
        deltas = []
        first_delta_ns = None
        if self.stream:
            async for event in turn.events(after=self.after):
                if event.type == "gap":
                    raise AssertionError("Native stream dropped observations")
                self.after = event.sequence
                if event.type == "output_text_delta":
                    if first_delta_ns is None:
                        first_delta_ns = perf_counter_ns()
                    deltas.append(event.delta)
        result = await turn.get_result()
        return {
            "text": result.final_output,
            "deltas": deltas,
            "first_delta_ns": first_delta_ns,
        }
