"""Run `python -m examples.basic.mutable_tool_hooks`. Add `--codex` to use Codex."""

import argparse
import asyncio
from typing import Any

from agents import Agent, ModelSettings, RunConfig, Runner, ToolCallHookContext, ToolExecutionConfig
from agents.decorators import tool
from agents.models.codex import CodexProvider


@tool
def echo(text: str) -> str:
    """Echo the supplied text."""
    print(f"Tool received {text!r}")
    return text


async def lowercase_input(call: ToolCallHookContext[None]) -> dict[str, Any]:
    if call.tool.name != echo.name:
        return call.arguments
    return {"text": call.arguments["text"].lower()}


async def quote_output(call: ToolCallHookContext[None], result: Any) -> Any:
    return f"Echoed {result!r}" if call.tool.name == echo.name else result


async def main(use_codex: bool) -> None:
    config = RunConfig(
        tool_execution=ToolExecutionConfig(
            # Hosted Responses tools execute outside both callbacks.
            prepare_tool_input=lowercase_input,
            prepare_tool_output=quote_output,
        ),
        trace_include_sensitive_data=False,
    )
    if use_codex:
        config.model_provider = CodexProvider()
    agent = Agent(
        name="Echo",
        tools=[echo],
        model_settings=ModelSettings(tool_choice="required"),
        tool_use_behavior="stop_on_first_tool",
    )
    try:
        result = await Runner.run(
            agent, "Call echo with the exact text 'Hello World'.", run_config=config
        )
        print(result.final_output)
    finally:
        await config.model_provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true", help="Use the Codex provider.")
    asyncio.run(main(parser.parse_args().codex))
