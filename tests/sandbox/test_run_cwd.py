from __future__ import annotations

import asyncio
import base64
import io
import shlex
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from openai.types.responses import ResponseCustomToolCall

from agents import RunConfig, Runner, ToolOutputImage
from agents.items import ToolCallOutputItem
from agents.run_state import RunState
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import (
    Filesystem,
    FilesystemToolSet,
    Shell,
    ShellToolSet,
    Skill,
    Skills,
)
from agents.sandbox.entries import File
from agents.sandbox.errors import WorkspaceReadNotFoundError
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

if TYPE_CHECKING or sys.platform != "win32":
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

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
async def test_python_skill_uses_absolute_root_from_nested_workdir() -> None:
    skill_script = (
        b"from pathlib import Path\n"
        b"skill_root = Path(__file__).parent.parent\n"
        b"suffix = (skill_root / 'assets' / 'suffix.txt').read_text(encoding='utf-8')\n"
        b"source = Path('input.txt').read_text(encoding='utf-8')\n"
        b"Path('output.txt').write_text(source + suffix, encoding='utf-8')\n"
    )
    skills = Skills(
        skills=[
            Skill(
                name="python-proof",
                description="Proves shared Python skills keep task files local.",
                content="# Python proof\n",
                scripts={"prove.py": File(content=skill_script)},
                assets={"suffix.txt": File(content=b"-from-shared-skill")},
            )
        ]
    )
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=skills.process_manifest(Manifest()))

    try:
        async with session:
            await session.mkdir("tasks/task-a/nested", parents=True)
            await session.write(
                Path("tasks/task-a/nested/input.txt"),
                io.BytesIO(b"task-a"),
            )
            workspace_root = session.state.manifest.root
            skill_root = f"{workspace_root}/.agents/python-proof"
            script_path = f"{skill_root}/scripts/prove.py"
            model = ScriptedModel(
                [
                    [
                        function_call(
                            "exec_command",
                            {
                                "cmd": (
                                    f"{shlex.quote(sys.executable)} {shlex.quote(script_path)}"
                                ),
                                "workdir": "nested",
                                "login": False,
                            },
                            call_id="python_skill",
                        )
                    ],
                    [assistant_message("done", item_id="python_skill_message")],
                ]
            )
            agent = SandboxAgent(
                name="python-skill-task",
                model=model,
                capabilities=[Shell(), skills],
            )

            result = await Runner.run(
                agent,
                "Use the available Python skill.",
                run_config=RunConfig(sandbox=SandboxRunConfig(session=session, cwd="tasks/task-a")),
            )

            assert result.final_output == "done"
            assert await _read_bytes(session, "tasks/task-a/nested/output.txt") == (
                b"task-a-from-shared-skill"
            )
            assert (
                await _read_bytes(session, ".agents/python-proof/scripts/prove.py") == skill_script
            )
            instructions = model.calls[0].system_instructions
            assert instructions is not None
            assert f"(file: {skill_root})" in instructions
            assert "Treat each listed path as the skill root" in instructions
            assert "Files outside the working directory may be visible to or shared" in instructions
            model.assert_complete()
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
