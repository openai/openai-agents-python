"""Run two trusted agents in separate working directories of one live sandbox.

Run-scoped working directories make relative paths consistent; they are not confinement.
Use separate sandbox sessions for untrusted agents or workloads that need compute isolation.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path

from agents import ModelSettings, Runner, RunResult, ToolOutputImage
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Shell
from agents.sandbox.entries import BaseEntry, Dir, File
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.sandbox.session import BaseSandboxSession


async def main(*, model: str) -> None:
    client = UnixLocalSandboxClient()
    agent_a = _build_agent(name="Task A worker", model=model)
    agent_b = _build_agent(name="Task B worker", model=model)
    shared_sandbox = await client.create(manifest=_build_manifest())

    try:
        # The session is shared, but each run gets a different base for relative paths.
        run_a_config = RunConfig(
            sandbox=SandboxRunConfig(
                session=shared_sandbox,
                cwd="tasks/task-a",
            ),
            workflow_name="Shared sandbox task A",
        )
        run_b_config = RunConfig(
            sandbox=SandboxRunConfig(
                session=shared_sandbox,
                cwd="tasks/task-b",
            ),
            workflow_name="Shared sandbox task B",
        )

        async with shared_sandbox:
            run_tasks = [
                asyncio.create_task(
                    Runner.run(
                        agent_a,
                        _task_prompt("task-a"),
                        run_config=run_a_config,
                        max_turns=10,
                    )
                ),
                asyncio.create_task(
                    Runner.run(
                        agent_b,
                        _task_prompt("task-b"),
                        run_config=run_b_config,
                        max_turns=10,
                    )
                ),
            ]
            result_a, result_b = await _run_concurrently(run_tasks)

            # These checks make the demo self-verifying; applications do not need them.
            await _verify_task(shared_sandbox, task_name="task-a", result=result_a)
            await _verify_task(shared_sandbox, task_name="task-b", result=result_b)
    finally:
        await client.delete(shared_sandbox)

    print("\nVerified: distinct run-scoped cwd values worked without session-global cwd mutation.")


# Demo setup. Applications can create task directories and inputs however they prefer.

DEFAULT_MODEL = "gpt-5.6-sol"

# The two task directories intentionally contain the same relative filenames.
TASKS = {
    "task-a": (
        "red",
        "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKklEQVR4nGO4o6ZGU8QwasGoBaMWjFowasGoBaMWjFowasGoBaMWDBULAIjyoD0k0I5JAAAAAElFTkSuQmCC",
    ),
    "task-b": (
        "blue",
        "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKklEQVR4nGNQTX5NU8QwasGoBaMWjFowasGoBaMWjFowasGoBaMWDBULAMMtzEwilX6gAAAAAElFTkSuQmCC",
    ),
}

SHELL_COMMAND = "cp reference.png page.png && cat brief.md"


def _task_prompt(task_name: str) -> str:
    return f"""
Complete {task_name} in your current run working directory. Use only these relative paths:

1. Call `exec_command` with `{SHELL_COMMAND}`. Do not pass `workdir`.
2. Call `view_image` with `page.png` and identify its color.
3. Use `apply_patch` on `notes.md` to change only `status=pending-{task_name}` to
   `status=complete-{task_name}`.
4. Reply with the task name and image color from the files you inspected.

Do not prefix paths with `tasks/`, use absolute paths, or use the shell to edit `notes.md`.
""".strip()


def _build_manifest() -> Manifest:
    task_dirs: dict[str | Path, BaseEntry] = {}
    for task_name, (color, image_base64) in TASKS.items():
        task_dirs[task_name] = Dir(
            children={
                "brief.md": File(
                    content=f"# {task_name}\n\nThe reference image is {color}.\n".encode()
                ),
                "reference.png": File(content=base64.b64decode(image_base64)),
                "notes.md": File(
                    content=f"task={task_name}\nstatus=pending-{task_name}\n".encode()
                ),
            }
        )
    return Manifest(entries={"tasks": Dir(children=task_dirs)})


def _build_agent(*, name: str, model: str) -> SandboxAgent:
    # These settings only make the demo's dependent tool sequence deterministic.
    # Run-scoped cwd does not require them.
    return SandboxAgent(
        name=name,
        model=model,
        instructions=(
            "Follow the requested tool sequence exactly. Treat the run working directory as "
            "the base for every relative path."
        ),
        capabilities=[Shell(), Filesystem()],
        model_settings=ModelSettings(tool_choice="required", parallel_tool_calls=False),
    )


async def _run_concurrently(
    run_tasks: list[asyncio.Task[RunResult]],
) -> tuple[RunResult, RunResult]:
    """Keep both runs terminal before the caller closes their shared session."""
    try:
        result_a, result_b = await asyncio.gather(*run_tasks)
    except BaseException:
        for run_task in run_tasks:
            if not run_task.done():
                run_task.cancel()
        await asyncio.gather(*run_tasks, return_exceptions=True)
        raise
    return result_a, result_b


# Demo verification. Applications do not need to inspect raw tool calls this way.


def _tool_call_name(item: ToolCallItem) -> str:
    raw_item = item.raw_item
    if isinstance(raw_item, dict):
        type_name = raw_item.get("type")
        name = raw_item.get("name")
    else:
        type_name = getattr(raw_item, "type", None)
        name = getattr(raw_item, "name", None)

    if type_name == "apply_patch_call":
        return "apply_patch"
    if isinstance(name, str):
        return name
    return type_name if isinstance(type_name, str) else ""


def _function_call_arguments(item: ToolCallItem) -> dict[str, object]:
    raw_item = item.raw_item
    if isinstance(raw_item, dict):
        arguments = raw_item.get("arguments")
    else:
        arguments = getattr(raw_item, "arguments", None)
    if not isinstance(arguments, str):
        raise RuntimeError(f"{_tool_call_name(item)} did not provide JSON arguments")

    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{_tool_call_name(item)} provided invalid JSON arguments") from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{_tool_call_name(item)} arguments were not an object")
    return parsed


def _apply_patch_input(item: ToolCallItem) -> str:
    raw_item = item.raw_item
    if isinstance(raw_item, dict):
        raw_input = raw_item.get("input")
    else:
        raw_input = getattr(raw_item, "input", None)
    if not isinstance(raw_input, str):
        raise RuntimeError("apply_patch did not provide patch input")
    return raw_input


async def _read_workspace_bytes(session: BaseSandboxSession, path: Path) -> bytes:
    handle = await session.read(path)
    try:
        payload = handle.read()
    finally:
        handle.close()
    return payload.encode() if isinstance(payload, str) else bytes(payload)


async def _verify_task(
    session: BaseSandboxSession,
    *,
    task_name: str,
    result: RunResult,
) -> None:
    color, image_base64 = TASKS[task_name]
    task_root = Path("tasks") / task_name

    # Direct session operations remain workspace-root-relative, so verification uses full paths.
    page = await _read_workspace_bytes(session, task_root / "page.png")
    notes = await _read_workspace_bytes(session, task_root / "notes.md")
    expected_page = base64.b64decode(image_base64)
    expected_notes = f"task={task_name}\nstatus=complete-{task_name}\n".encode()
    if page != expected_page:
        raise RuntimeError(f"{task_name} copied the wrong relative reference.png")
    if notes != expected_notes:
        raise RuntimeError(f"{task_name} did not patch its own relative notes.md")

    tool_call_items = [item for item in result.new_items if isinstance(item, ToolCallItem)]
    tool_calls = [_tool_call_name(item) for item in tool_call_items]
    expected_tool_calls = ["exec_command", "view_image", "apply_patch"]
    if tool_calls != expected_tool_calls:
        raise RuntimeError(
            f"{task_name} used {tool_calls}; expected the ordered calls {expected_tool_calls}"
        )

    shell_arguments = _function_call_arguments(tool_call_items[0])
    if shell_arguments.get("cmd") != SHELL_COMMAND or "workdir" in shell_arguments:
        raise RuntimeError(f"{task_name} did not use the task-relative shell command")

    image_arguments = _function_call_arguments(tool_call_items[1])
    if image_arguments.get("path") != "page.png":
        raise RuntimeError(f"{task_name} did not view relative page.png")

    patch_input = _apply_patch_input(tool_call_items[2])
    patch_lines = patch_input.splitlines()
    file_directives = [
        line
        for line in patch_lines
        if line.startswith(
            ("*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: ")
        )
    ]
    expected_old = f"-status=pending-{task_name}"
    expected_new = f"+status=complete-{task_name}"
    if (
        file_directives != ["*** Update File: notes.md"]
        or expected_old not in patch_lines
        or expected_new not in patch_lines
    ):
        raise RuntimeError(f"{task_name} did not patch relative notes.md with its own marker")

    expected_image_url = f"data:image/png;base64,{image_base64}"
    image_urls = [
        item.output.image_url
        for item in result.new_items
        if isinstance(item, ToolCallOutputItem) and isinstance(item.output, ToolOutputImage)
    ]
    if expected_image_url not in image_urls:
        raise RuntimeError(f"{task_name} viewed an image outside its run working directory")

    print(f"\n[{task_name}] cwd={task_root.as_posix()} color={color}")
    print(f"tool calls: {', '.join(tool_calls)}")
    print(f"notes.md: {notes.decode().strip()}")
    print(f"final output: {result.final_output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run two task-local agents concurrently in one live sandbox session."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    args = parser.parse_args()
    asyncio.run(main(model=args.model))
