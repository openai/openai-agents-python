from __future__ import annotations

import asyncio
import base64
import io
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from openai.types.responses import ResponseCustomToolCall

from agents import RunConfig, Runner, ToolOutputImage
from agents.items import ToolCallOutputItem
from agents.run_state import RunState
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, FilesystemToolSet, Shell, ShellToolSet
from agents.sandbox.errors import WorkspaceReadNotFoundError
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a84QAAAAASUVORK5CYII="
)
_SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'
_IMAGE_BY_TASK = {
    "task-a": ("image/png", _PNG_BYTES),
    "task-b": ("image/svg+xml", _SVG_BYTES),
}


async def _read_bytes(session: BaseSandboxSession, path: str) -> bytes:
    file_obj = await session.read(Path(path))
    try:
        payload = file_obj.read()
    finally:
        file_obj.close()
    return payload if isinstance(payload, bytes) else payload.encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Unix local sandbox is unavailable on Windows")
async def test_concurrent_runs_scope_relative_paths_with_shared_live_session() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    both_runs_ready = asyncio.Event()
    ready_count = 0
    ready_lock = asyncio.Lock()

    def first_step(task_name: str) -> Callable[[Any], Awaitable[list[Any]]]:
        async def respond(_call: Any) -> list[Any]:
            nonlocal ready_count
            async with ready_lock:
                ready_count += 1
                if ready_count == 2:
                    both_runs_ready.set()
            await asyncio.wait_for(both_runs_ready.wait(), timeout=5)
            return [
                function_call(
                    "exec_command",
                    {"cmd": "cp seed.png plot.png", "login": False},
                    call_id=f"{task_name}_shell",
                )
            ]

        return respond

    def build_model(task_name: str) -> ScriptedModel:
        return ScriptedModel(
            [
                ModelStep.respond(first_step(task_name)),
                [
                    function_call(
                        "view_image",
                        {"path": "plot.png"},
                        call_id=f"{task_name}_image",
                    )
                ],
                [
                    ResponseCustomToolCall(
                        id=f"{task_name}_patch_item",
                        type="custom_tool_call",
                        name="apply_patch",
                        call_id=f"{task_name}_patch",
                        input=(
                            "*** Begin Patch\n"
                            "*** Add File: notes.md\n"
                            f"+{task_name}\n"
                            "*** End Patch\n"
                        ),
                    )
                ],
                [assistant_message("done", item_id=f"{task_name}_message")],
            ]
        )

    models = {task_name: build_model(task_name) for task_name in ("task-a", "task-b")}
    agents = {
        task_name: SandboxAgent(
            name=task_name,
            model=models[task_name],
            capabilities=[Shell(), Filesystem()],
        )
        for task_name in models
    }

    try:
        async with session:
            for task_name in models:
                task_dir = f"tasks/{task_name}"
                await session.mkdir(task_dir, parents=True)
                await session.write(
                    Path(task_dir) / "seed.png",
                    io.BytesIO(_IMAGE_BY_TASK[task_name][1]),
                )

            results = await asyncio.gather(
                *(
                    Runner.run(
                        agents[task_name],
                        "Create the requested task-local artifacts.",
                        run_config=RunConfig(
                            sandbox=SandboxRunConfig(
                                session=session,
                                cwd=f"tasks/{task_name}",
                            )
                        ),
                    )
                    for task_name in models
                )
            )

            for task_name, result in zip(models, results, strict=True):
                assert result.final_output == "done"
                outputs = {
                    item.call_id: item.output
                    for item in result.new_items
                    if isinstance(item, ToolCallOutputItem)
                }
                image_output = outputs[f"{task_name}_image"]
                assert isinstance(image_output, ToolOutputImage)
                mime_type, image_bytes = _IMAGE_BY_TASK[task_name]
                assert image_output.image_url == (
                    f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
                )
                assert outputs[f"{task_name}_patch"] == "Created notes.md"
                assert await _read_bytes(session, f"tasks/{task_name}/plot.png") == image_bytes
                assert (
                    await _read_bytes(session, f"tasks/{task_name}/notes.md") == task_name.encode()
                )
                models[task_name].assert_complete()

            for root_relative_path in ("plot.png", "notes.md"):
                with pytest.raises(WorkspaceReadNotFoundError):
                    await session.read(Path(root_relative_path))
    finally:
        await client.delete(session)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Unix local sandbox is unavailable on Windows")
async def test_resumed_run_rebinds_cwd_to_pending_sandbox_tool() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())

    def require_exec_approval(toolset: ShellToolSet) -> None:
        toolset.exec_command.needs_approval = True

    model = ScriptedModel(
        [
            [
                function_call(
                    "exec_command",
                    {"cmd": "printf resumed > marker.txt", "login": False},
                    call_id="resumed_shell",
                )
            ],
            [assistant_message("done", item_id="resumed_message")],
        ]
    )
    agent = SandboxAgent(
        name="resumed-task",
        model=model,
        capabilities=[Shell(configure_tools=require_exec_approval)],
    )
    run_config = RunConfig(sandbox=SandboxRunConfig(session=session, cwd="tasks/resumed-task"))

    try:
        async with session:
            await session.mkdir("tasks/resumed-task", parents=True)

            first = await Runner.run(agent, "Create the marker.", run_config=run_config)
            assert len(first.interruptions) == 1
            state = first.to_state()
            state.approve(first.interruptions[0])

            resumed = await Runner.run(agent, state, run_config=run_config)

            assert resumed.final_output == "done"
            assert await _read_bytes(session, "tasks/resumed-task/marker.txt") == b"resumed"
            with pytest.raises(WorkspaceReadNotFoundError):
                await session.read(Path("marker.txt"))
            model.assert_complete()
    finally:
        await client.delete(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("serialize_state", [False, True], ids=["in-memory", "json"])
@pytest.mark.skipif(sys.platform == "win32", reason="Unix local sandbox is unavailable on Windows")
async def test_resumed_apply_patch_uses_current_run_cwd(serialize_state: bool) -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())

    def require_apply_patch_approval(toolset: FilesystemToolSet) -> None:
        toolset.apply_patch.needs_approval = True

    model = ScriptedModel(
        [
            [
                ResponseCustomToolCall(
                    id="patch_item",
                    type="custom_tool_call",
                    name="apply_patch",
                    call_id="patch_call",
                    input=("*** Begin Patch\n*** Add File: marker.txt\n+resumed\n*** End Patch\n"),
                )
            ],
            [assistant_message("done", item_id="resumed_patch_message")],
        ]
    )
    agent = SandboxAgent(
        name="resumed-patch-task",
        model=model,
        capabilities=[Filesystem(configure_tools=require_apply_patch_approval)],
    )

    try:
        async with session:
            await session.mkdir("tasks/a", parents=True)
            await session.mkdir("tasks/b", parents=True)

            first = await Runner.run(
                agent,
                "Create the marker.",
                run_config=RunConfig(sandbox=SandboxRunConfig(session=session, cwd="tasks/a")),
            )
            assert len(first.interruptions) == 1
            state = first.to_state()
            if serialize_state:
                state = await RunState.from_json(agent, state.to_json())
            state.approve(state.get_interruptions()[0])

            resumed = await Runner.run(
                agent,
                state,
                run_config=RunConfig(sandbox=SandboxRunConfig(session=session, cwd="tasks/b")),
            )

            assert resumed.final_output == "done"
            assert await _read_bytes(session, "tasks/b/marker.txt") == b"resumed"
            with pytest.raises(WorkspaceReadNotFoundError):
                await session.read(Path("tasks/a/marker.txt"))
            model.assert_complete()
    finally:
        await client.delete(session)
