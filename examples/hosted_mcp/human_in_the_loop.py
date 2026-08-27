import argparse
import asyncio
import json
from typing import Literal

from agents import (
    Agent,
    HostedMCPTool,
    ModelSettings,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
)
from agents.model_settings import MCPToolChoice
from examples.auto_mode import confirm_with_fallback


def prompt_for_interruption(
    tool_name: str | None, arguments: str | dict[str, object] | None
) -> bool:
    params: object = {}
    if arguments:
        if isinstance(arguments, str):
            try:
                params = json.loads(arguments)
            except json.JSONDecodeError:
                params = arguments
        else:
            params = arguments
    try:
        return confirm_with_fallback(
            f"Approve running tool (mcp: {tool_name or 'unknown'}, params: {json.dumps(params)})? (y/n) ",
            default=True,
        )
    except (EOFError, KeyboardInterrupt):
        return False


async def _drain_stream(
    result: RunResultStreaming,
    verbose: bool,
) -> RunResultStreaming:
    async for event in result.stream_events():
        if verbose:
            print(event)
        elif event.type == "raw_response_event" and event.data.type == "response.output_text.delta":
            print(event.data.delta, end="", flush=True)
    if not verbose:
        print()
    return result


async def main(verbose: bool, stream: bool) -> None:
    require_approval: Literal["always"] = "always"
    # Use the concise question tool first, then allow the model to answer after approval instead of
    # forcing the same tool again.
    agent = Agent(
        name="MCP Assistant",
        instructions=(
            "You must always use the MCP tools to answer questions. "
            "Use the DeepWiki hosted MCP server to answer questions and do not ask the user for "
            "additional configuration."
        ),
        model_settings=ModelSettings(
            tool_choice=MCPToolChoice(server_label="deepwiki", name="ask_question")
        ),
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "deepwiki",
                    "server_url": "https://mcp.deepwiki.com/mcp",
                    "require_approval": require_approval,
                }
            )
        ],
    )
    resume_config = RunConfig(model_settings=ModelSettings(tool_choice="auto"))

    question = "Which language is the repository openai/codex written in?"

    run_result: RunResult | RunResultStreaming
    if stream:
        stream_result = Runner.run_streamed(agent, question, max_turns=100)
        stream_result = await _drain_stream(stream_result, verbose)
        while stream_result.interruptions:
            state = stream_result.to_state()
            for interruption in stream_result.interruptions:
                approved = prompt_for_interruption(interruption.name, interruption.arguments)
                if approved:
                    state.approve(interruption)
                else:
                    state.reject(interruption)
            stream_result = Runner.run_streamed(
                agent,
                state,
                max_turns=100,
                run_config=resume_config,
            )
            stream_result = await _drain_stream(stream_result, verbose)
        print(f"Done streaming; final result: {stream_result.final_output}")
        run_result = stream_result
    else:
        run_result = await Runner.run(agent, question, max_turns=100)
        while run_result.interruptions:
            state = run_result.to_state()
            for interruption in run_result.interruptions:
                approved = prompt_for_interruption(interruption.name, interruption.arguments)
                if approved:
                    state.approve(interruption)
                else:
                    state.reject(interruption)
            run_result = await Runner.run(
                agent,
                state,
                max_turns=100,
                run_config=resume_config,
            )
        print(run_result.final_output)

    if verbose:
        for item in run_result.new_items:
            print(item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument("--stream", action="store_true", default=False)
    args = parser.parse_args()

    asyncio.run(main(args.verbose, args.stream))
