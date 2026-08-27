from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, RunHooks
from agents.items import ToolCallOutputItem
from agents.run import RunConfig
from agents.run_context import RunContextWrapper
from agents.run_internal.run_steps import ToolRunCustom
from agents.run_internal.tool_actions import CustomToolAction
from agents.sandbox.capabilities.tools import SandboxApplyPatchTool
from tests.sandbox._apply_patch_test_session import ApplyPatchSession
from tests.utils.hitl import make_context_wrapper


@pytest.mark.asyncio
async def test_invalid_later_path_does_not_mutate_valid_prefix() -> None:
    session = ApplyPatchSession()
    tool = SandboxApplyPatchTool(session=session, needs_approval=True)
    raw_input = (
        "*** Begin Patch\n"
        "*** Add File: safe.txt\n"
        "+safe\n"
        "*** Add File: ../escape.txt\n"
        "+escape\n"
        "*** End Patch\n"
    )

    result = await _execute_custom_tool_call(
        tool,
        context_wrapper=make_context_wrapper(),
        raw_input=raw_input,
    )

    assert isinstance(result, ToolCallOutputItem)
    assert session.files == {}


async def _execute_custom_tool_call(
    tool: SandboxApplyPatchTool,
    *,
    context_wrapper: RunContextWrapper[Any],
    raw_input: str,
    call_id: str = "call_apply",
) -> Any:
    return await CustomToolAction.execute(
        agent=Agent(name="patcher", tools=[tool]),
        call=ToolRunCustom(
            custom_tool=tool,
            tool_call={
                "type": "custom_tool_call",
                "name": tool.name,
                "call_id": call_id,
                "input": raw_input,
            },
        ),
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )
