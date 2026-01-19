import asyncio
from datetime import datetime

from agents import Agent, Runner, gen_trace_id, trace

# This tool is still in experimental phase and the details could be changed until being GAed.
from agents.extensions.experimental.codex import (
    CodexToolStreamEvent,
    CommandExecutionItem,
    ErrorItem,
    FileChangeItem,
    ItemCompletedEvent,
    ItemStartedEvent,
    ItemUpdatedEvent,
    McpToolCallItem,
    ReasoningItem,
    ThreadErrorEvent,
    ThreadOptions,
    ThreadStartedEvent,
    TodoListItem,
    TurnCompletedEvent,
    TurnFailedEvent,
    TurnOptions,
    TurnStartedEvent,
    WebSearchItem,
    codex_tool,
)

# This example runs the Codex CLI via the Codex tool wrapper.
# You can configure the CLI path with CODEX_PATH or CodexOptions(codex_path_override="...").
# codex_tool accepts options as keyword arguments or a plain dict.
# For example: codex_tool(sandbox_mode="read-only") or codex_tool({"sandbox_mode": "read-only"}).
# The prompt below asks Codex to use the $openai-knowledge skill (Docs MCP) for API lookups.


async def on_codex_stream(payload: CodexToolStreamEvent) -> None:
    event = payload.event

    if isinstance(event, ThreadStartedEvent):
        log(f"codex thread started: {event.thread_id}")
        return
    if isinstance(event, TurnStartedEvent):
        log("codex turn started")
        return
    if isinstance(event, TurnCompletedEvent):
        usage = event.usage
        log(f"codex turn completed, usage: {usage}")
        return
    if isinstance(event, TurnFailedEvent):
        error = event.error.message
        log(f"codex turn failed: {error}")
        return
    if isinstance(event, ThreadErrorEvent):
        log(f"codex stream error: {event.message}")
        return

    if not isinstance(event, (ItemStartedEvent, ItemUpdatedEvent, ItemCompletedEvent)):
        return

    item = event.item

    if isinstance(item, ReasoningItem):
        text = item.text
        log(f"codex reasoning ({event.type}): {text}")
        return
    if isinstance(item, CommandExecutionItem):
        command = item.command
        output = item.aggregated_output
        output_preview = output[-200:] if isinstance(output, str) else ""
        status = item.status
        log(f"codex command {event.type}: {command} | status={status} | output={output_preview}")
        return
    if isinstance(item, McpToolCallItem):
        server = item.server
        tool = item.tool
        status = item.status
        log(f"codex mcp {event.type}: {server}.{tool} | status={status}")
        return
    if isinstance(item, FileChangeItem):
        changes = item.changes
        status = item.status
        log(f"codex file change {event.type}: {status} | {changes}")
        return
    if isinstance(item, WebSearchItem):
        log(f"codex web search {event.type}: {item.query}")
        return
    if isinstance(item, TodoListItem):
        items = item.items
        log(f"codex todo list {event.type}: {len(items)} items")
        return
    if isinstance(item, ErrorItem):
        log(f"codex error {event.type}: {item.message}")


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    timestamp = _timestamp()
    lines = str(message).splitlines() or [""]
    for line in lines:
        print(f"{timestamp} {line}")


async def main() -> None:
    agent = Agent(
        name="Codex Agent",
        instructions=(
            "Use the codex tool to inspect the workspace and answer the question. "
            "When skill names, which usually starts with `$`, are mentioned, "
            "you must rely on the codex tool to use the skill and answer the question.\n\n"
            "When you send the final answer, you must include the following info at the end:\n\n"
            "Run `codex resume <thread_id>` to continue the codex session."
        ),
        tools=[
            # Run local Codex CLI as a sub process
            codex_tool(
                sandbox_mode="workspace-write",
                default_thread_options=ThreadOptions(
                    # You can pass a Codex instance to customize CLI details
                    # codex=Codex(executable_path="/path/to/codex", base_url="..."),
                    model="gpt-5.2-codex",
                    model_reasoning_effort="low",
                    network_access_enabled=True,
                    web_search_enabled=False,
                    approval_policy="never",  # We'll update this example once the HITL is implemented
                ),
                default_turn_options=TurnOptions(
                    # Abort Codex CLI if no events arrive within this many seconds.
                    idle_timeout_seconds=60,
                ),
                on_stream=on_codex_stream,
            )
        ],
    )
    trace_id = gen_trace_id()
    log(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}")

    with trace("Codex tool example", trace_id=trace_id):
        # Use a skill that requires network access and MCP server settings
        log("Using $openai-knowledge skill to fetch the latest realtime model name...")
        result = await Runner.run(
            agent,
            "You must use `$openai-knowledge` skill to fetch the latest realtime model name.",
        )
        log(result.final_output)
        # The latest realtime model name, according to the $openai-knowledge skill, is gpt-realtime.

        # Use a skill that runs local command and analyzes the output
        log(
            "Using $test-coverage-improver skill to analyze the test coverage of the project and improve it..."
        )
        result = await Runner.run(
            agent,
            "You must use `$test-coverage-improver` skill to analyze the test coverage of the project and improve it.",
        )
        log(result.final_output)
        # (Aa few suggestions for improving the test coverage will be displayed.)


if __name__ == "__main__":
    asyncio.run(main())
