"""Exercise the native SDK only through its public application API."""

from __future__ import annotations

import asyncio
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
        request_transport: str | None = None,
    ):
        if request_transport not in (None, "auto", "http", "websocket"):
            raise ValueError("Unknown native request transport")
        if request_transport is not None and (not default_host or provider_mode != "omitted"):
            raise ValueError("Request transport requires omitted Provider default-host calls")
        self._run_options = (
            {"model_settings": {"request_transport": request_transport}}
            if request_transport is not None
            else {}
        )
        if provider_mode not in ("caller-owned", "omitted"):
            raise ValueError("Unknown native Provider mode")
        if provider_mode == "omitted" and (
            not default_host
            or (with_tool and sync_tool)
            or (stream and not with_tool)
            or response_mode != "streaming"
        ):
            raise ValueError(
                "Omitted Provider mode: default-host fresh no-tool or one-async-tool calls only"
            )
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
        self._pending = {}
        self._implicit_tool = provider_mode == "omitted" and with_tool
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

        if self._implicit_tool:
            from agents import ToolContext

            async def contextual_echo(context, value):
                """Return the supplied value unchanged."""
                assert isinstance(context, ToolContext) and context.context is None
                call_id = context.call_id
                assert call_id.startswith("call_")
                operation, round_number, index = call_id[5:].rsplit("_", 2)
                assert round_number == index == "0"
                assert value == f"echo:{operation}:0:0"
                state = self._pending[operation]
                assert state["context"] is None
                state["context"] = context
                self.tool_calls += 1
                if self.stream:
                    state["published"].set_result(context.submission)
                    await state["ready"]
                return value

            # Resolve the locally imported public type before function_tool inspects it.
            contextual_echo.__annotations__ = {
                "context": ToolContext[None],
                "value": str,
                "return": str,
            }
            callback = contextual_echo
        else:
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
            assert not self._pending

    async def run(self, prompt: str) -> dict:
        if self._implicit_tool:
            return await self._fresh_tool_run(prompt)
        if self.stream:
            async with self.conversation() as conversation:
                return await conversation.run(prompt)
        result = await self.agent.run(prompt, **self._run_options)
        return {"text": result.final_output, "deltas": [], "first_delta_ns": None}

    async def _drain_turn(self, state):
        turn = await state["published"]
        deltas, first_delta_ns = [], None
        last_sequence = terminals = 0

        def observe(event):
            nonlocal last_sequence, terminals, first_delta_ns
            assert event.type != "gap"
            assert event.session_id == turn.session_id
            assert event.submission_id == turn.submission_id
            assert event.sequence > last_sequence
            last_sequence = event.sequence
            if event.type == "output_text_delta":
                if first_delta_ns is None:
                    first_delta_ns = perf_counter_ns()
                deltas.append(event.delta)
            if event.type == "turn_finished":
                assert event.status == "completed"
                terminals += 1

        try:
            stream = turn.events()
            try:
                first = await anext(stream)
                assert first.type != "output_text_delta"
                observe(first)
                state["ready"].set_result(None)
                async for event in stream:
                    observe(event)
            finally:
                await stream.aclose()
            await state["returned"]
            if terminals == 0:
                stream = turn.events(after=last_sequence)
                try:
                    async for event in stream:
                        observe(event)
                finally:
                    await stream.aclose()
            assert terminals == 1 and deltas and first_delta_ns is not None
            return {"deltas": deltas, "first_delta_ns": first_delta_ns}
        except BaseException as error:
            if not state["ready"].done():
                state["ready"].set_exception(error)
            raise

    async def _fresh_tool_run(self, prompt):
        assert prompt.startswith("BENCH_RUN=")
        operation = prompt.split("=", 1)[1].split(" ", 1)[0]
        assert operation not in self._pending and self.agent.provider is None
        state = {"context": None}
        task = None
        if self.stream:
            loop = asyncio.get_running_loop()
            state.update(
                {name: loop.create_future() for name in ("published", "ready", "returned")}
            )
            # Capture the caller's context before Agent.run enters its tool callback.
            task = asyncio.create_task(self._drain_turn(state))
        self._pending[operation] = state
        try:
            result = await self.agent.run(prompt, **self._run_options)
            assert self.agent.provider is None
            context = state["context"]
            assert context is not None and context.submission is result.turn
            observed = {"deltas": [], "first_delta_ns": None}
            if task is not None:
                assert state["published"].result() is result.turn
                state["returned"].set_result(None)
                observed = await task
            return {
                "text": result.final_output,
                **observed,
                "native_session_id": result.turn.session_id,
                "tool_context_validated": True,
                "context_submission_is_result_turn": True,
                "reader_ready": task is not None,
                "stream_fully_drained": task is not None,
            }
        finally:
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                for name in ("published", "ready", "returned"):
                    future = state[name]
                    if not future.done():
                        future.cancel()
                    elif not future.cancelled():
                        future.exception()
            del self._pending[operation]

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
