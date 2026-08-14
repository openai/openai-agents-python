from __future__ import annotations

import base64
import io
from pathlib import PurePosixPath
from typing import Any, cast

import pytest

from agents.exceptions import UserError
from agents.run_config import RunConfig, SandboxRunConfig
from agents.run_context import RunContextWrapper
from agents.sandbox import Manifest, SandboxAgent
from agents.sandbox.capabilities import Filesystem, Shell
from agents.sandbox.capabilities._run_cwd_tools import (
    RunCwdExecCommandTool,
    RunCwdSandboxApplyPatchTool,
    RunCwdViewImageTool,
)
from agents.sandbox.capabilities.tools import ExecCommandArgs, ViewImageArgs
from agents.sandbox.runtime import SandboxRuntime
from agents.sandbox.types import ExecResult
from agents.testing import scripted_sandbox_session
from agents.tool import ToolOutputImage

_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a84QAAAAASUVORK5CYII="
)
_PNG_BYTES = base64.b64decode(_PNG_BASE64)


@pytest.mark.asyncio
async def test_exec_command_defaults_to_run_cwd() -> None:
    session = scripted_sandbox_session(
        [{"method": "exec", "result": ExecResult(stdout=b"ok\n", stderr=b"", exit_code=0)}],
        manifest=Manifest(root="/workspace"),
    )
    tool = RunCwdExecCommandTool(session=session, cwd=PurePosixPath("tasks/a"))

    output = await tool.run(ExecCommandArgs(cmd="pwd"))

    assert session.calls[0].args == ("cd /workspace/tasks/a && pwd",)
    assert "ok" in output
    session.assert_complete()


@pytest.mark.asyncio
async def test_exec_command_rebases_relative_workdir_beneath_run_cwd() -> None:
    session = scripted_sandbox_session(
        [{"method": "exec", "result": ExecResult(stdout=b"", stderr=b"", exit_code=0)}],
        manifest=Manifest(root="/workspace"),
    )
    tool = RunCwdExecCommandTool(session=session, cwd=PurePosixPath("tasks/a"))

    await tool.run(ExecCommandArgs(cmd="pwd", workdir="nested"))

    assert session.calls[0].args == ("cd /workspace/tasks/a/nested && pwd",)
    session.assert_complete()


@pytest.mark.asyncio
async def test_exec_command_approval_sees_effective_workdir() -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
    seen: list[dict[str, Any]] = []

    async def needs_approval(
        _ctx: RunContextWrapper[Any], params: dict[str, Any], _call_id: str
    ) -> bool:
        seen.append(params)
        return True

    tool = RunCwdExecCommandTool(session=session, cwd=PurePosixPath("tasks/a"))
    tool.needs_approval = needs_approval
    tool.finalize_run_cwd()
    approval = cast(Any, tool.needs_approval)

    assert await approval(cast(RunContextWrapper[Any], None), {"cmd": "pwd"}, "call") is True
    assert seen == [{"cmd": "pwd", "workdir": "tasks/a"}]


@pytest.mark.asyncio
async def test_view_image_rebases_relative_path_beneath_run_cwd() -> None:
    session = scripted_sandbox_session(
        [{"method": "read", "result": io.BytesIO(_PNG_BYTES)}],
        manifest=Manifest(root="/workspace"),
    )
    tool = RunCwdViewImageTool(session=session, cwd=PurePosixPath("tasks/a"))

    output = await tool.run(ViewImageArgs(path="plot.png"))

    assert isinstance(output, ToolOutputImage)
    assert session.calls[0].args[0].as_posix() == "/workspace/tasks/a/plot.png"
    session.assert_complete()


@pytest.mark.asyncio
async def test_view_image_approval_sees_effective_path() -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
    seen: list[dict[str, Any]] = []

    async def needs_approval(
        _ctx: RunContextWrapper[Any], params: dict[str, Any], _call_id: str
    ) -> bool:
        seen.append(params)
        return True

    tool = RunCwdViewImageTool(session=session, cwd=PurePosixPath("tasks/a"))
    tool.needs_approval = needs_approval
    tool.finalize_run_cwd()
    approval = cast(Any, tool.needs_approval)

    assert await approval(
        cast(RunContextWrapper[Any], None), {"path": "plot.png"}, "call"
    ) is True
    assert seen == [{"path": "tasks/a/plot.png"}]


def test_apply_patch_rebases_paths_before_approval_and_execution() -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
    tool = RunCwdSandboxApplyPatchTool(session=session, cwd=PurePosixPath("tasks/a"))

    operations = tool.parse_custom_input(
        "*** Begin Patch\n"
        "*** Update File: notes.md\n"
        "*** Move to: archive/notes.md\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )

    assert len(operations) == 1
    assert operations[0].path == "tasks/a/notes.md"
    assert operations[0].move_to == "tasks/a/archive/notes.md"


def test_run_config_dict_coercion_accepts_cwd() -> None:
    config = RunConfig(sandbox={"cwd": "tasks/a"})

    assert isinstance(config.sandbox, SandboxRunConfig)
    assert config.sandbox.cwd == "tasks/a"


@pytest.mark.asyncio
async def test_runtime_rejects_cwd_outside_workspace() -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
    agent = SandboxAgent(name="sandbox", capabilities=[Shell()])
    runtime = SandboxRuntime[object](
        starting_agent=agent,
        run_config=RunConfig(sandbox=SandboxRunConfig(session=session, cwd="/tmp")),
        run_state=None,
    )

    with pytest.raises(UserError, match="sandbox.cwd must resolve within the sandbox workspace"):
        await runtime.prepare_agent(
            current_agent=agent,
            current_input="inspect the workspace",
            context_wrapper=cast(RunContextWrapper[object], None),
            is_resumed_state=False,
        )

    await runtime.cleanup()


@pytest.mark.asyncio
async def test_runtime_binds_cwd_to_per_run_capability_clones() -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
    agent = SandboxAgent(name="sandbox", capabilities=[Shell(), Filesystem()])
    runtime = SandboxRuntime[object](
        starting_agent=agent,
        run_config=RunConfig(sandbox=SandboxRunConfig(session=session, cwd="tasks/a")),
        run_state=None,
    )

    prepared = await runtime.prepare_agent(
        current_agent=agent,
        current_input="inspect the workspace",
        context_wrapper=cast(RunContextWrapper[object], None),
        is_resumed_state=False,
    )
    execution_agent = cast(SandboxAgent[object], prepared.bindings.execution_agent)

    assert [capability.run_cwd for capability in execution_agent.capabilities] == [
        PurePosixPath("tasks/a"),
        PurePosixPath("tasks/a"),
    ]
    assert [capability.run_cwd for capability in agent.capabilities] == [None, None]
    await runtime.cleanup()


@pytest.mark.asyncio
async def test_shared_live_session_keeps_run_cwds_independent() -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
    agent_a = SandboxAgent(name="a", capabilities=[Shell()])
    agent_b = SandboxAgent(name="b", capabilities=[Shell()])
    runtime_a = SandboxRuntime[object](
        starting_agent=agent_a,
        run_config=RunConfig(sandbox=SandboxRunConfig(session=session, cwd="tasks/a")),
        run_state=None,
    )
    runtime_b = SandboxRuntime[object](
        starting_agent=agent_b,
        run_config=RunConfig(sandbox=SandboxRunConfig(session=session, cwd="tasks/b")),
        run_state=None,
    )

    prepared_a = await runtime_a.prepare_agent(
        current_agent=agent_a,
        current_input="a",
        context_wrapper=cast(RunContextWrapper[object], None),
        is_resumed_state=False,
    )
    prepared_b = await runtime_b.prepare_agent(
        current_agent=agent_b,
        current_input="b",
        context_wrapper=cast(RunContextWrapper[object], None),
        is_resumed_state=False,
    )

    execution_a = cast(SandboxAgent[object], prepared_a.bindings.execution_agent)
    execution_b = cast(SandboxAgent[object], prepared_b.bindings.execution_agent)
    assert execution_a.capabilities[0].run_cwd == PurePosixPath("tasks/a")
    assert execution_b.capabilities[0].run_cwd == PurePosixPath("tasks/b")
    assert session.state.manifest.root == "/workspace"

    await runtime_a.cleanup()
    await runtime_b.cleanup()
