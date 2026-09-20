"""Handle host shell approvals through run interruptions.

Each command batch requires an interactive operator decision. Approved commands
retain host filesystem and network access; this example does not provide isolation.
"""

import argparse
import asyncio
from collections.abc import Sequence

from agents import Agent, ModelSettings, Runner, ShellTool, trace
from agents.items import ToolApprovalItem
from examples.tools.shell import ShellExecutor, prompt_shell_approval


def _extract_commands(approval_item: ToolApprovalItem) -> Sequence[str]:
    raw = approval_item.raw_item
    if isinstance(raw, dict):
        action = raw.get("action", {})
        if isinstance(action, dict):
            commands = action.get("commands", [])
            if isinstance(commands, Sequence):
                return [str(cmd) for cmd in commands]
    action_obj = getattr(raw, "action", None)
    if action_obj and hasattr(action_obj, "commands"):
        return list(action_obj.commands)
    return ()


async def main(prompt: str, model: str) -> None:
    with trace("shell_hitl_example"):
        print(f"[info] Using model: {model}")

        agent = Agent(
            name="Shell HITL Assistant",
            model=model,
            instructions=(
                "You can run shell commands using the shell tool. "
                "Ask for approval before running commands."
            ),
            tools=[
                ShellTool(
                    executor=ShellExecutor(),
                    needs_approval=True,
                )
            ],
            model_settings=ModelSettings(tool_choice="required"),
        )

        result = await Runner.run(agent, prompt)

        while result.interruptions:
            print("\n== Pending approvals ==")
            state = result.to_state()
            for interruption in result.interruptions:
                commands = _extract_commands(interruption)
                approved = await prompt_shell_approval(commands)
                if approved:
                    state.approve(interruption)
                else:
                    state.reject(interruption)

            result = await Runner.run(agent, state)

        print(f"\nFinal response:\n{result.final_output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompt",
        default="List the files in the current directory and show the current working directory.",
        help="Instruction to send to the agent.",
    )
    parser.add_argument(
        "--model",
        default="gpt-5.6-sol",
    )
    args = parser.parse_args()
    asyncio.run(main(args.prompt, args.model))
