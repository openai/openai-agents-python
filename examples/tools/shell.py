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


class ShellExecutor:
    """Executes shell commands with optional approval."""

    def __init__(self, cwd: Path | None = None):
        self.cwd = Path(cwd or Path.cwd())

    async def __call__(self, request: ShellCommandRequest) -> ShellResult:
        action = request.data.action
        await require_approval(action.commands)

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


async def require_approval(commands: Sequence[str]) -> None:
    if os.environ.get("SHELL_AUTO_APPROVE") == "1":
        return
    print("Shell command approval required:")
    for entry in commands:
        print(" ", entry)
    response = input("Proceed? [y/N] ").strip().lower()
    if response not in {"y", "yes"}:
        raise RuntimeError("Shell command execution rejected by user.")


async def main(prompt: str, model: str) -> None:
    with trace("shell_example"):
        print(f"[info] Using model: {model}")
        agent = Agent(
            name="Shell Assistant",
            model=model,
            instructions=(
                "You can run shell commands using the shell tool. "
                "Keep responses concise and include command output when helpful."
            ),
            tools=[ShellTool(executor=ShellExecutor())],
            model_settings=ModelSettings(tool_choice="required"),
        )

        result = await Runner.run(agent, prompt)
        print(f"\nFinal response:\n{result.final_output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompt",
        default="Show the list of files in the current directory.",
        help="Instruction to send to the agent.",
    )
    parser.add_argument(
        "--model",
        default="gpt-5.2",
    )
    args = parser.parse_args()
    asyncio.run(main(args.prompt, args.model))
