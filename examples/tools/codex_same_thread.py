import asyncio
from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel

from agents import Agent, ModelSettings, Runner, gen_trace_id, trace

# This tool is still in experimental phase and the details could be changed until being GAed.
from agents.extensions.experimental.codex import (
    CodexToolStreamEvent,
    ThreadErrorEvent,
    ThreadOptions,
    ThreadStartedEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
    TurnStartedEvent,
    codex_tool,
)

# Derived from codex_tool(name="codex_engineer") when run_context_thread_id_key is omitted.
THREAD_ID_KEY = "codex_thread_id_engineer"


async def on_codex_stream(payload: CodexToolStreamEvent) -> None:
    event = payload.event

    if isinstance(event, ThreadStartedEvent):
        log(f"codex thread started: {event.thread_id}")
        return
    if isinstance(event, TurnStartedEvent):
        log("codex turn started")
        return
    if isinstance(event, TurnCompletedEvent):
        log(f"codex turn completed, usage: {event.usage}")
        return
    if isinstance(event, TurnFailedEvent):
        log(f"codex turn failed: {event.error.message}")
        return
    if isinstance(event, ThreadErrorEvent):
        log(f"codex stream error: {event.message}")


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    timestamp = _timestamp()
    lines = str(message).splitlines() or [""]
    for line in lines:
        print(f"{timestamp} {line}")


def read_context_value(context: Mapping[str, str] | BaseModel, key: str) -> str | None:
    # either dict or pydantic model
    if isinstance(context, Mapping):
        return context.get(key)
    return getattr(context, key, None)


async def main() -> None:
    agent = Agent(
        name="Codex Agent (same thread)",
        instructions=(
            "Always use the Codex tool answer the user's question. "
            "Even when you don't have enough context, the Codex tool may know. "
            "In that case, you can simply forward the question to the Codex tool."
        ),
        tools=[
            codex_tool(
                # Give each Codex tool a unique `codex_` name when you run multiple tools in one agent.
                # Name-based defaults keep their run-context thread IDs separated.
                name="codex_engineer",
                sandbox_mode="workspace-write",
                default_thread_options=ThreadOptions(
                    model="gpt-5.2-codex",
                    model_reasoning_effort="low",
                    network_access_enabled=True,
                    web_search_enabled=False,
                    approval_policy="never",
                ),
                on_stream=on_codex_stream,
                # Reuse the same Codex thread across runs that share this context object.
                use_run_context_thread_id=True,
            )
        ],
        model_settings=ModelSettings(tool_choice="required"),
    )

    class MyContext(BaseModel):
        something: str | None = None
        # the default is "codex_thread_id"; missing this works as well
        codex_thread_id_engineer: str | None = None  # aligns with run_context_thread_id_key

    context = MyContext()

    # Simple dict object works as well:
    # context: dict[str, str] = {}

    trace_id = gen_trace_id()
    log(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}")

    with trace("Codex same thread example", trace_id=trace_id):
        log("Turn 1: ask writing python code")
        first_prompt = "Write working python code example demonstrating how to call OpenAI's Responses API with web search tool."
        first_result = await Runner.run(agent, first_prompt, context=context)
        first_thread_id = read_context_value(context, THREAD_ID_KEY)
        log(first_result.final_output)
        log(f"thread id after turn 1: {first_thread_id}")

        log("Turn 2: continue with the same Codex thread.")
        second_prompt = "Write the same code in TypeScript."
        second_result = await Runner.run(agent, second_prompt, context=context)
        second_thread_id = read_context_value(context, THREAD_ID_KEY)
        log(second_result.final_output)
        log(f"thread id after turn 2: {second_thread_id}")
        log(
            "same thread reused: "
            + str(first_thread_id is not None and first_thread_id == second_thread_id)
        )


if __name__ == "__main__":
    asyncio.run(main())
