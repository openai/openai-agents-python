"""Public stable Agents SDK adapter for the shared local Responses benchmark."""

from __future__ import annotations

import time
from typing import Any

from openai import AsyncOpenAI

from agents import (
    Agent,
    ModelSettings,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
    set_tracing_disabled,
)
from agents.decorators import tool


class Adapter:
    def __init__(
        self,
        base_url: str,
        model: str = "gpt-5.1-codex",
        stream: bool = False,
        *,
        with_tool: bool = False,
        sync_tool: bool = False,
        default_host: bool = False,
    ) -> None:
        set_tracing_disabled(True)
        self.stream = stream
        self.tool_calls = 0
        self.client = AsyncOpenAI(
            api_key="benchmark-placeholder",
            **({} if default_host else {"base_url": base_url}),
            max_retries=0,
            timeout=30,
        )
        tools = []
        if with_tool:
            if sync_tool:

                def echo(value: str) -> str:
                    """Return the supplied value unchanged."""
                    self.tool_calls += 1
                    return value
            else:

                async def echo(value: str) -> str:
                    """Return the supplied value unchanged."""
                    self.tool_calls += 1
                    return value

            tools.append(tool(echo, failure_error_function=None))
        self.agent = Agent(
            name="benchmark",
            instructions="Follow the benchmark request and return the result.",
            model=OpenAIResponsesModel(model=model, openai_client=self.client),
            model_settings=ModelSettings(parallel_tool_calls=True),
            tools=tools,
        )
        self.config = RunConfig(tracing_disabled=True)

    async def __aenter__(self) -> Adapter:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.client.close()

    async def _run(self, input: Any):
        deltas: list[str] = []
        first_delta_ns = None
        if self.stream:
            result = Runner.run_streamed(self.agent, input, run_config=self.config)
            async for event in result.stream_events():
                if (
                    event.type == "raw_response_event"
                    and event.data.type == "response.output_text.delta"
                ):
                    if first_delta_ns is None:
                        first_delta_ns = time.perf_counter_ns()
                    deltas.append(event.data.delta)
        else:
            result = await Runner.run(self.agent, input, run_config=self.config)
        if not isinstance(result.final_output, str):
            raise TypeError(f"Expected text output, got {type(result.final_output).__name__}")
        return result, {
            "text": result.final_output,
            "deltas": deltas,
            "first_delta_ns": first_delta_ns,
        }

    async def run(self, prompt: str) -> dict:
        _, output = await self._run(prompt)
        return output

    def conversation(self) -> Conversation:
        return Conversation(self)


class Conversation:
    def __init__(self, adapter: Adapter) -> None:
        self.adapter = adapter
        self.history: list = []

    async def __aenter__(self) -> Conversation:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.history.clear()

    async def run(self, prompt: str) -> dict:
        result, output = await self.adapter._run(
            self.history + [{"role": "user", "content": prompt}]
        )
        self.history = result.to_input_list()
        return output
