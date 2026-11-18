import argparse
import asyncio
import json
import os
from contextlib import AsyncExitStack
from datetime import datetime

from agents import (
    Agent,
    HostedMCPTool,
    ModelSettings,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
)
from agents.mcp import MCPServerStreamableHttp, create_static_tool_filter
from agents.models.codex import CodexProvider

# import logging
# logging.basicConfig(level=logging.DEBUG)


async def main(verbose: bool, stream: bool, sdk_managed: bool = False, codex: bool = False):
    if codex and not sdk_managed:
        raise ValueError(
            "Use --sdk-managed with --codex; Codex does not resolve hosted connector IDs."
        )
    # 1. Visit https://developers.google.com/oauthplayground/
    # 2. Input https://www.googleapis.com/auth/calendar.events as the required scope
    # 3. Grab the access token starting with "ya29."
    authorization = os.environ.get("GOOGLE_CALENDAR_AUTHORIZATION")
    if not authorization:
        raise ValueError("Set GOOGLE_CALENDAR_AUTHORIZATION before running this example.")
    run_config = RunConfig(model=os.environ.get("CODEX_MODEL" if codex else "OPENAI_MODEL"))
    if codex:
        run_config.model_provider = CodexProvider()
    async with AsyncExitStack() as stack:
        server = None
        if sdk_managed:
            server = await stack.enter_async_context(
                MCPServerStreamableHttp(
                    name="Google Calendar",
                    params={
                        "url": "https://calendarmcp.googleapis.com/mcp/v1",
                        "headers": {"Authorization": f"Bearer {authorization}"},
                    },
                    tool_filter=create_static_tool_filter(
                        allowed_tool_names=["calendar.list_events"]
                    ),
                )
            )
            if not any(tool.name == "calendar.list_events" for tool in await server.list_tools()):
                raise ValueError("The Calendar MCP server did not expose calendar.list_events.")
        agent = Agent(
            name="Assistant",
            instructions=(
                "You are a helpful assistant that can help a user with their calendar. "
                "Use the calendar tool to read the requested schedule."
            ),
            mcp_servers=[server] if server else [],
            model_settings=ModelSettings(tool_choice="required" if server else None),
            # Calendar's dotted MCP names need model-compatible public function names.
            mcp_config={"include_server_in_tool_names": True} if server else {},
            tools=[]
            if server
            else [
                HostedMCPTool(
                    tool_config={
                        "type": "mcp",
                        "server_label": "google_calendar",
                        # see https://platform.openai.com/docs/guides/tools-connectors-mcp#connectors
                        "connector_id": "connector_googlecalendar",
                        "authorization": authorization,
                        "require_approval": "never",
                    }
                )
            ],
        )

        today = datetime.now().strftime("%Y-%m-%d")
        run_result: RunResult | RunResultStreaming
        if stream:
            run_result = Runner.run_streamed(
                agent, f"What is my schedule for {today}?", run_config=run_config
            )
            async for event in run_result.stream_events():
                if event.type == "raw_response_event":
                    if event.data.type.startswith("response.output_item"):
                        print(json.dumps(event.data.to_dict(), indent=2))
                    if event.data.type.startswith("response.mcp"):
                        print(json.dumps(event.data.to_dict(), indent=2))
                    if event.data.type == "response.output_text.delta":
                        print(event.data.delta, end="", flush=True)
            print()
        else:
            run_result = await Runner.run(
                agent, f"What is my schedule for {today}?", run_config=run_config
            )
            print(run_result.final_output)

        if verbose:
            for item in run_result.new_items:
                # Print metadata only: items retain the agent's connector configuration.
                print(f"Generated item: {item.type}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument("--stream", action="store_true", default=False)
    parser.add_argument("--sdk-managed", action="store_true", default=False)
    parser.add_argument("--codex", action="store_true", default=False)
    args = parser.parse_args()

    asyncio.run(main(args.verbose, args.stream, args.sdk_managed, args.codex))
