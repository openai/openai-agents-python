import asyncio
import json
import shutil
import subprocess

from agents import (
    Agent,
    Runner,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
)
from agents.decorators import tool, tool_input_guardrail

"""Inspect shell commands with HOL Guard before a FunctionTool executes.

This example demonstrates the pre-execution boundary of `FunctionTool` input tool
guardrails. A guardrail invokes the external `hol-guard` CLI in its side-effect-free
`command test` mode before the wrapped function runs, and only an explicitly safe
classification lets the call through.

Important boundary: `hol-guard command test` inspects the command string only. It does
not execute the command, evaluate final Guard policy, create approvals, or record
receipts. Treat it as one layer of defense, not as comprehensive runtime enforcement.

Install HOL Guard before running this example:

    pipx install hol-guard

The example fails closed: if `hol-guard` is missing, times out, returns malformed JSON,
or does not clearly classify the command as safe, the tool call is rejected and the
wrapped function is never invoked.
"""

# Counts how many times the protected function body actually ran. The main() demo uses
# this to prove that blocked commands never reach the tool implementation.
EXECUTED_COMMANDS = 0

HOL_GUARD_TIMEOUT_SECONDS = 10.0


def _classify_with_hol_guard(command: str) -> dict[str, object] | None:
    """Run `hol-guard command test` and return its parsed JSON verdict, or None.

    Returning None means the inspection itself failed (missing binary, timeout,
    non-zero exit, or malformed output), which the guardrail treats as fail closed.
    """
    binary = shutil.which("hol-guard")
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "command", "test", command, "--json"],
            capture_output=True,
            text=True,
            timeout=HOL_GUARD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None

    if completed.returncode != 0:
        return None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_explicitly_safe(verdict: dict[str, object]) -> bool:
    """Return True only when HOL Guard explicitly classifies the command as safe.

    Adjust the accepted field names and values here to match the JSON shape produced by
    your installed HOL Guard version. Anything unrecognized counts as not safe.
    """
    classification = verdict.get("classification") or verdict.get("verdict")
    return isinstance(classification, str) and classification.strip().lower() == "safe"


@tool_input_guardrail
def inspect_command_with_hol_guard(
    data: ToolInputGuardrailData,
) -> ToolGuardrailFunctionOutput:
    """Reject any tool call whose command does not pass HOL Guard command inspection."""
    try:
        args = json.loads(data.context.tool_arguments) if data.context.tool_arguments else {}
    except json.JSONDecodeError:
        return ToolGuardrailFunctionOutput.reject_content(
            message="Tool call blocked: arguments are not valid JSON.",
            output_info={"reason": "malformed_arguments"},
        )

    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return ToolGuardrailFunctionOutput.reject_content(
            message="Tool call blocked: no shell command was provided.",
            output_info={"reason": "missing_command"},
        )

    verdict = _classify_with_hol_guard(command)
    if verdict is None:
        # Fail closed: inspection unavailability must never open the execution path.
        return ToolGuardrailFunctionOutput.reject_content(
            message=(
                "Tool call blocked: HOL Guard command inspection is unavailable or could "
                "not classify this command."
            ),
            output_info={"reason": "inspection_unavailable", "command": command},
        )

    if not _is_explicitly_safe(verdict):
        return ToolGuardrailFunctionOutput.reject_content(
            message=f"Tool call blocked: HOL Guard did not classify '{command}' as safe.",
            output_info={"reason": "not_classified_safe", "verdict": verdict},
        )

    return ToolGuardrailFunctionOutput(
        output_info={"reason": "classified_safe", "verdict": verdict}
    )


@tool
def run_command(command: str) -> str:
    """Run a shell command and return its output."""
    global EXECUTED_COMMANDS
    EXECUTED_COMMANDS += 1
    result = subprocess.run(command, shell=True, capture_output=True, text=True, check=False)
    return result.stdout or result.stderr or "(no output)"


# Attach the input guardrail so every `run_command` call is inspected before execution.
run_command.tool_input_guardrails = [inspect_command_with_hol_guard]

agent = Agent(
    name="Command Assistant",
    instructions=(
        "You can run shell commands with the run_command tool. "
        "When the user asks to run a command, call the tool with their exact command."
    ),
    tools=[run_command],
)


async def main():
    print("=== HOL Guard command inspection example ===\n")

    print("1. Asking the agent to run a harmless command:")
    result = await Runner.run(agent, "Run the command 'echo hello from hol-guard'")
    executed_before = EXECUTED_COMMANDS
    print(f"   Final output: {result.final_output}")
    print(f"   Tool executions so far: {executed_before}\n")

    print("2. Asking the agent to run a destructive command:")
    result = await Runner.run(agent, "Run the command 'rm -rf /tmp/important-data'")
    print(f"   Final output: {result.final_output}")
    print(f"   Tool executions so far: {EXECUTED_COMMANDS}")

    if EXECUTED_COMMANDS == executed_before:
        print("   ✅ The blocked command never reached the tool implementation.")
    else:
        print("   ❌ Unexpected: the blocked command reached the tool implementation.")


if __name__ == "__main__":
    asyncio.run(main())
