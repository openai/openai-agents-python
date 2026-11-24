"""Choose a synthetic account in an application-owned desktop window."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mcp.types import Resource, TextContent, TextResourceContents

from agents import Agent, RunConfig, Runner, RunResult
from agents.decorators import tool
from agents.mcp import MCPServer, MCPServerStdio
from agents.testing import ScriptedModel, function_call
from agents.tool_context import ToolContext
from examples.mcp.account_picker.server import META, MIME, URI

# These are safe display labels. The source decides which ones the user may select.
LABELS = ("Everyday", "Savings", "Shared")


@dataclass(frozen=True, eq=False)
class Picker:
    conversation: str
    call_id: str
    resource: Resource
    choices: tuple[str, ...]


async def load_picker(server: MCPServer, conversation: str, call_id: str) -> Picker:
    # Use wire names for fields whose Python names differ between MCP versions.
    listing = await server.list_resources()
    matches = [resource for resource in listing.resources if str(resource.uri) == URI]
    if listing.model_dump(by_alias=True).get("nextCursor") is not None or len(matches) != 1:
        raise ValueError("Expected a single picker resource on one page")
    (resource,) = matches
    if resource.model_dump(by_alias=True).get("mimeType") != MIME or resource.meta != META:
        raise ValueError("Unexpected picker resource")

    result = await server.read_resource(URI)
    if len(result.contents) != 1:
        raise ValueError("Expected a single picker content block")
    (content,) = result.contents
    if (
        not isinstance(content, TextResourceContents)
        or str(content.uri) != URI
        or content.model_dump(by_alias=True).get("mimeType") != MIME
    ):
        raise ValueError("Unexpected picker content")
    choices = tuple(content.text.split("\n", len(LABELS)))
    if any(choice not in LABELS for choice in choices) or len(set(choices)) != len(choices):
        raise ValueError("Unexpected account labels")
    return Picker(conversation, call_id, resource, choices)


def show_picker(picker: Picker) -> tuple[Picker, str] | None:
    import tkinter as tk
    from tkinter import ttk

    choice = None
    window = tk.Tk()
    try:
        window.title("Choose a demo account")
        frame = ttk.Frame(window, padding=20)
        frame.pack()
        ttk.Label(frame, text=f"Conversation: {picker.conversation}").pack(anchor="w")
        ttk.Label(frame, text="Demo account").pack(anchor="w", pady=(16, 4))
        accounts = ttk.Combobox(frame, state="readonly", values=picker.choices, width=32)
        accounts.pack()
        accounts.focus_set()

        def confirm() -> None:
            nonlocal choice
            choice = (picker, accounts.get())
            window.quit()

        button = ttk.Button(frame, text="Use account", command=confirm, state="disabled")
        button.pack(anchor="e", pady=(16, 0))
        accounts.bind("<<ComboboxSelected>>", lambda _: button.state(["!disabled"]))
        window.protocol("WM_DELETE_WINDOW", window.quit)
        window.mainloop()
        return choice
    finally:
        window.destroy()


async def run_selection(
    server: MCPServer,
    conversation: str,
    show: Callable[[Picker], tuple[Picker, str] | None] = show_picker,
) -> RunResult | None:
    selected: str | None = None

    @tool(needs_approval=True, failure_error_function=None)
    async def choose_account(ctx: ToolContext) -> str:
        """Return the synthetic account selected by the user."""
        if selected is None or ctx.tool_call_id != picker.call_id:
            raise ValueError("No matching selection")
        confirmation = await server.call_tool("confirm_account", {"alias": selected})
        if (
            confirmation.model_dump(by_alias=True).get("isError")
            or len(confirmation.content) != 1
            or not isinstance(confirmation.content[0], TextContent)
            or confirmation.content[0].text != selected
        ):
            raise PermissionError("Account unavailable")
        return selected

    agent = Agent(
        name="Account assistant",
        tools=[choose_account],
        model=ScriptedModel([[function_call(choose_account.name, {}, call_id="pick-1")]]),
        tool_use_behavior="stop_on_first_tool",
    )
    config = RunConfig(tracing_disabled=True)
    paused = await Runner.run(agent, "Choose a demo account.", run_config=config)
    (call,) = paused.interruptions
    if call.name != choose_account.name or not call.call_id:
        raise ValueError("Unexpected tool call")
    picker = await load_picker(server, conversation, call.call_id)
    choice = show(picker)
    if choice is None:
        return None
    if type(choice) is not tuple or len(choice) != 2:
        raise ValueError("Expected one picker and account alias")
    request, alias = choice
    # A later run may reuse both the conversation and call ID.
    if request is not picker:
        raise ValueError("No matching pending request")
    if type(alias) is not str or not alias or alias not in picker.choices:
        raise PermissionError("Account unavailable")
    selected = alias

    state = paused.to_state()
    state.approve(call)
    return await Runner.run(agent, state, run_config=config)


def demo_server() -> MCPServerStdio:
    return MCPServerStdio(
        params={"command": sys.executable, "args": [str(Path(__file__).with_name("server.py"))]}
    )


async def main() -> None:
    async with demo_server() as server:
        result = await run_selection(server, "Demo")
        print(result.final_output if result else "Selection cancelled")


if __name__ == "__main__":
    if sys.argv[1:] == ["--pick"]:
        asyncio.run(main())
    else:
        print("Run with --pick to open the desktop account picker.")
