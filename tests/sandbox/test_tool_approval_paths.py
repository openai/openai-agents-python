from __future__ import annotations

from typing import Any

import pytest

from agents.run_context import RunContextWrapper
from agents.sandbox import Manifest
from agents.sandbox.capabilities.tools.shell_tool import ExecCommandTool
from agents.sandbox.workspace_paths import SandboxWorkspaceScope
from agents.testing import scripted_sandbox_session


@pytest.mark.asyncio
async def test_exec_command_approval_receives_effective_workdir() -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
    seen: dict[str, Any] = {}

    async def needs_approval(
        _ctx: RunContextWrapper[Any], parameters: dict[str, Any], _call_id: str
    ) -> bool:
        seen.update(parameters)
        return False

    tool = ExecCommandTool(
        session=session,
        needs_approval=needs_approval,
        workspace_scope=SandboxWorkspaceScope.from_cwd("tasks/task-a"),
    )
    approval = tool.needs_approval
    assert callable(approval)

    assert not await approval(
        RunContextWrapper(context=None),
        {"cmd": "pwd", "workdir": r"nested\project"},
        "call-1",
    )
    assert seen["workdir"] == "/workspace/tasks/task-a/nested/project"


@pytest.mark.asyncio
async def test_exec_command_approval_receives_run_cwd_when_workdir_is_omitted() -> None:
    session = scripted_sandbox_session(manifest=Manifest(root="/workspace"))
    seen: dict[str, Any] = {}

    async def needs_approval(
        _ctx: RunContextWrapper[Any], parameters: dict[str, Any], _call_id: str
    ) -> bool:
        seen.update(parameters)
        return False

    tool = ExecCommandTool(
        session=session,
        needs_approval=needs_approval,
        workspace_scope=SandboxWorkspaceScope.from_cwd("tasks/task-a"),
    )
    approval = tool.needs_approval
    assert callable(approval)

    assert not await approval(RunContextWrapper(context=None), {"cmd": "pwd"}, "call-2")
    assert seen["workdir"] == "/workspace/tasks/task-a"
