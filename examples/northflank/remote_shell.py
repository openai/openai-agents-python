"""Example: a ShellTool whose every command runs *inside* a Northflank service.

Run::

    OPENAI_API_KEY=... NF_API_TOKEN=... \\
        python examples/northflank/remote_shell.py demo-app api
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence

from northflank import AsyncApiClient

from agents import Agent, ModelSettings, Runner, ShellTool
from agents.extensions.northflank import NorthflankCtx, NorthflankShellExecutor
from agents.items import ToolApprovalItem
from agents.run_context import RunContextWrapper
from agents.tool import ShellOnApprovalFunctionResult

AUTO_APPROVE = os.environ.get("SHELL_AUTO_APPROVE") == "1"


async def prompt_for_approval(commands: Sequence[str]) -> bool:
    if AUTO_APPROVE:
        return True
    print("Approve these commands?")
    for command in commands:
        print("  $", command)
    return input("[y/N] ").strip().lower() in {"y", "yes"}


async def on_shell_approval(
    _ctx: RunContextWrapper, item: ToolApprovalItem
) -> ShellOnApprovalFunctionResult:
    raw = item.raw_item
    commands: Sequence[str] = ()
    if isinstance(raw, dict):
        action = raw.get("action", {})
        if isinstance(action, dict):
            commands = action.get("commands", [])
    else:
        action_obj = getattr(raw, "action", None)
        if action_obj is not None and hasattr(action_obj, "commands"):
            commands = action_obj.commands
    approved = await prompt_for_approval(commands)
    return {"approve": approved, "reason": "ok" if approved else "user rejected"}


async def main(project_id: str, service_id: str) -> None:
    client = AsyncApiClient()
    executor = NorthflankShellExecutor(service_id=service_id, shell="sh")

    agent = Agent(
        name="northflank-shell",
        instructions=(
            "Diagnose a misbehaving Northflank service. Run a small number of "
            "shell commands inside the target container and explain the output."
        ),
        tools=[
            ShellTool(
                executor=executor,
                needs_approval=True,
                on_approval=on_shell_approval,
            )
        ],
        model_settings=ModelSettings(tool_choice="auto"),
    )

    result = await Runner.run(
        agent,
        "Check memory usage and list the most recent files in /tmp.",
        context=NorthflankCtx(client=client, project_id=project_id),
    )
    print("\n--- final ---")
    print(result.final_output)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} <project_id> <service_id>")
    asyncio.run(main(sys.argv[1], sys.argv[2]))
