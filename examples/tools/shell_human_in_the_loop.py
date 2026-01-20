import argparse
import asyncio
import os
from collections.abc import Sequence
from pathlib import Path

from agents import (
    Agent,
    ModelSettings,
    Runner,
    ShellCallOutcome,
    ShellCommandOutput,
    ShellCommandRequest,
    ShellResult,
    ShellTool,
    trace,
)
from agents.items import ToolApprovalItem
from examples.auto_mode import confirm_with_fallback, is_auto_mode


class ShellExecutor:
    """Executes shell commands; approvals are handled manually via interruptions."""

    def __init__(self, cwd: Path | None = None):
        self.cwd = Path(cwd or Path.cwd())

    async def __call__(self, request: ShellCommandRequest) -> ShellResult:
        action = request.data.action

        outputs: list[ShellCommandOutput] = []
        for command in action.commands:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=self.cwd,
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            timed_out = False
            try:
                timeout = (action.timeout_ms or 0) / 1000 or None
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                stdout_bytes, stderr_bytes = await proc.communicate()
                timed_out = True

            stdout = stdout_bytes.decode("utf-8", errors="ignore")
            stderr = stderr_bytes.decode("utf-8", errors="ignore")
            outputs.append(
                ShellCommandOutput(
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                    outcome=ShellCallOutcome(
                        type="timeout" if timed_out else "exit",
                        exit_code=getattr(proc, "returncode", None),
                    ),
                )
            )

            if timed_out:
                break

        return ShellResult(
            output=outputs,
            provider_data={"working_directory": str(self.cwd)},
        )


async def prompt_shell_approval(commands: Sequence[str]) -> tuple[bool, bool]:
    """Prompt for approval and optional always-approve choice."""
    print("Shell command approval required:")
    for entry in commands:
        print(f"  {entry}")
    auto_mode = is_auto_mode()
    decision = confirm_with_fallback("Approve? [y/N]: ", default=auto_mode)
    always = False
    if decision:
        always = confirm_with_fallback(
            "Approve all future shell calls? [y/N]: ",
            default=auto_mode,
        )
    return decision, always


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
                approved, always = await prompt_shell_approval(commands)
                if approved:
                    state.approve(interruption, always_approve=always)
                else:
                    state.reject(interruption, always_reject=always)

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
        default="gpt-5.1",
    )
    args = parser.parse_args()
    asyncio.run(main(args.prompt, args.model))
