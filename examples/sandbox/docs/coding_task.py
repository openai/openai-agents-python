"""Runnable sandbox coding example used by docs/sandbox_agents.md.

This example gives the model a tiny repo plus one lazy-loaded skill, then
verifies that the agent edited the repo and ran the targeted test command.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from agents import ModelSettings, Runner
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import LocalDirLazySkillSource, Skills
from agents.sandbox.capabilities.capabilities import Capabilities
from agents.sandbox.entries import LocalDir
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

DEFAULT_MODEL = "gpt-5.4"
TARGET_TEST_CMD = "sh tests/test_credit_note.sh"
DEFAULT_PROMPT = (
    "Open `repo/task.md`, use the `$credit-note-fixer` skill, fix the bug, run "
    f"`{TARGET_TEST_CMD}`, and summarize the change."
)
EXAMPLE_DIR = Path(__file__).resolve().parent

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def build_agent(model: str) -> SandboxAgent[None]:
    return SandboxAgent(
        name="Sandbox engineer",
        model=model,
        instructions=(
            "Inspect the repo, make the smallest correct change, run the most relevant checks, "
            "and summarize the file changes and risks. "
            "Read `repo/task.md` before editing files. Stay grounded in the repository, preserve "
            "existing behavior, and use the `$credit-note-fixer` skill before editing files. "
            "When using `apply_patch`, remember that paths are relative to the sandbox workspace "
            "root, not the shell working directory, so edit files as `repo/credit_note.sh` and "
            "`repo/tests/test_credit_note.sh`. "
            f"Run the exact verification command `{TARGET_TEST_CMD}` from `repo/`, then mention "
            "that command in the final answer."
        ),
        default_manifest=Manifest(
            entries={
                "repo": LocalDir(src=EXAMPLE_DIR / "repo"),
            }
        ),
        capabilities=Capabilities.default()
        + [
            Skills(
                lazy_from=LocalDirLazySkillSource(
                    source=LocalDir(src=EXAMPLE_DIR / "skills"),
                )
            ),
        ],
        model_settings=ModelSettings(tool_choice="required"),
    )


async def _read_workspace_text(session, path: Path) -> str:
    handle = await session.read(path)
    try:
        payload = handle.read()
    finally:
        handle.close()

    if isinstance(payload, str):
        return payload
    return bytes(payload).decode("utf-8", errors="replace")


def _tool_call_name(item: ToolCallItem) -> str:
    raw_item = item.raw_item
    if isinstance(raw_item, dict):
        raw_type = raw_item.get("type")
        name = raw_item.get("name")
    else:
        raw_type = getattr(raw_item, "type", None)
        name = getattr(raw_item, "name", None)

    if raw_type == "apply_patch_call":
        return "apply_patch"
    if isinstance(name, str) and name:
        return name
    if isinstance(raw_type, str) and raw_type:
        return raw_type
    return ""


def _tool_call_arguments(item: ToolCallItem) -> dict[str, object]:
    raw_item = item.raw_item
    if isinstance(raw_item, dict):
        arguments = raw_item.get("arguments")
    else:
        arguments = getattr(raw_item, "arguments", None)

    if not isinstance(arguments, str) or arguments == "":
        return {}

    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {"_raw": arguments}

    if isinstance(parsed, dict):
        return parsed
    return {"_value": parsed}


def _saw_target_test_command(tool_calls: list[ToolCallItem]) -> bool:
    for item in tool_calls:
        if _tool_call_name(item) != "exec_command":
            continue

        arguments = _tool_call_arguments(item)
        cmd = arguments.get("cmd")
        workdir = arguments.get("workdir")
        if cmd == TARGET_TEST_CMD and workdir == "repo":
            return True
        if isinstance(cmd, str) and TARGET_TEST_CMD in cmd:
            return True
        if isinstance(cmd, str) and workdir == "repo" and TARGET_TEST_CMD in cmd:
            return True

    return False


def _tool_call_debug_lines(tool_calls: list[ToolCallItem]) -> list[str]:
    lines: list[str] = []
    for item in tool_calls:
        lines.append(
            f"{_tool_call_name(item)}: {json.dumps(_tool_call_arguments(item), sort_keys=True)}"
        )
    return lines


def _tool_output_debug_lines(new_items: Sequence[object]) -> list[str]:
    lines: list[str] = []
    for item in new_items:
        if not isinstance(item, ToolCallOutputItem):
            continue
        output = item.output
        if isinstance(output, str):
            rendered = output
        else:
            rendered = str(output)
        lines.append(rendered[:400] if len(rendered) > 400 else rendered)
    return lines


def _saw_target_test_success(new_items: Sequence[object]) -> bool:
    awaiting_target_output = False

    for item in new_items:
        if isinstance(item, ToolCallItem):
            if _tool_call_name(item) != "exec_command":
                awaiting_target_output = False
                continue

            arguments = _tool_call_arguments(item)
            cmd = arguments.get("cmd")
            if isinstance(cmd, str) and TARGET_TEST_CMD in cmd:
                awaiting_target_output = True
                continue

            awaiting_target_output = False
            continue

        if awaiting_target_output and isinstance(item, ToolCallOutputItem):
            output = item.output
            if isinstance(output, str) and "2 passed" in output:
                return True
            awaiting_target_output = False

    return False


async def main(model: str, prompt: str) -> None:
    agent = build_agent(model)
    client = UnixLocalSandboxClient()
    sandbox = await client.create(manifest=agent.default_manifest)

    try:
        async with sandbox:
            result = await Runner.run(
                agent,
                prompt,
                max_turns=12,
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(session=sandbox),
                    tracing_disabled=True,
                    workflow_name="Sandbox docs coding example",
                ),
            )

            tool_calls = [item for item in result.new_items if isinstance(item, ToolCallItem)]
            tool_names = [_tool_call_name(item) for item in tool_calls]

            if "load_skill" not in tool_names:
                raise RuntimeError(f"Expected load_skill call, saw: {tool_names}")
            if "apply_patch" not in tool_names:
                raise RuntimeError(f"Expected apply_patch call, saw: {tool_names}")
            if not _saw_target_test_command(tool_calls):
                raise RuntimeError(
                    "Expected the agent to run the targeted test command.\n"
                    + "\n".join(_tool_call_debug_lines(tool_calls))
                )

            if not _saw_target_test_success(result.new_items):
                raise RuntimeError(
                    "Expected the targeted test command to report `2 passed`.\n"
                    "Tool calls:\n"
                    + "\n".join(_tool_call_debug_lines(tool_calls))
                    + "\nTool outputs:\n"
                    + "\n".join(_tool_output_debug_lines(result.new_items))
                )

            verification = await sandbox.exec(
                f"cd repo && {TARGET_TEST_CMD}",
                shell=True,
            )
            verification_text = verification.stdout.decode(
                "utf-8", errors="replace"
            ) + verification.stderr.decode("utf-8", errors="replace")
            if verification.exit_code != 0 or "2 passed" not in verification_text:
                raise RuntimeError(f"Post-run verification failed:\n{verification_text}")

            updated_module = await _read_workspace_text(sandbox, Path("repo/credit_note.sh"))

            print("=== Final summary ===")
            print("final_output:", result.final_output)
            print("tool_calls:", ", ".join(tool_names))
            print("verification_command:", TARGET_TEST_CMD)
            print("verification_result: observed target test output with `2 passed`")
            print("updated_credit_note.sh:")
            print(updated_module, end="" if updated_module.endswith("\n") else "\n")
    finally:
        await client.delete(sandbox)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run a self-validating sandbox coding example used by the docs."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt to send to the agent.")
    args = parser.parse_args()

    asyncio.run(main(args.model, args.prompt))
