from __future__ import annotations

from collections.abc import Awaitable
from pathlib import Path
from typing import Any, cast

import pytest

from agents import Agent, CustomTool, RunHooks
from agents.editor import ApplyPatchOperation, ApplyPatchResult
from agents.items import ToolApprovalItem, ToolCallOutputItem
from agents.models.openai_responses import Converter
from agents.run import RunConfig
from agents.run_context import RunContextWrapper
from agents.run_internal.run_steps import ToolRunCustom
from agents.run_internal.tool_actions import CustomToolAction
from agents.sandbox.capabilities.tools import SandboxApplyPatchTool
from agents.sandbox.types import User
from tests.sandbox._apply_patch_test_session import (
    ApplyPatchSession,
    UserRecordingApplyPatchSession,
)
from tests.utils.hitl import make_context_wrapper


class TestSandboxApplyPatchTool:
    def test_exposes_custom_apply_patch_tool(self) -> None:
        tool = SandboxApplyPatchTool(session=ApplyPatchSession())

        assert isinstance(tool, CustomTool)
        assert tool.name == "apply_patch"
        assert tool.tool_config["type"] == "custom"
        assert tool.tool_config["name"] == "apply_patch"
        assert tool.tool_config["format"]["type"] == "grammar"
        assert tool.tool_config["format"]["syntax"] == "lark"

    def test_converter_uses_sandbox_custom_apply_patch_tool_config(self) -> None:
        tool = SandboxApplyPatchTool(session=ApplyPatchSession())

        converted = Converter.convert_tools([tool], handoffs=[])

        assert converted.tools[0]["type"] == "custom"
        assert converted.tools[0]["name"] == "apply_patch"
        description = converted.tools[0]["description"]
        assert isinstance(description, str)
        assert "This is a FREEFORM tool" in description
        assert "A full patch can combine several operations" in description
        tool_format = cast(dict[str, Any], converted.tools[0]["format"])
        assert tool_format["syntax"] == "lark"

    def test_needs_approval_exposes_operation_typed_setting(self) -> None:
        async def needs_approval(
            _ctx: RunContextWrapper[Any], operation: ApplyPatchOperation, _call_id: str
        ) -> bool:
            return operation.type != "create_file"

        tool = SandboxApplyPatchTool(session=ApplyPatchSession(), needs_approval=needs_approval)

        assert cast(object, tool.needs_approval) is needs_approval
        assert cast(object, tool.operation_needs_approval) is needs_approval

    @pytest.mark.asyncio
    async def test_public_needs_approval_assignment_drives_runtime_approval(self) -> None:
        async def needs_approval(
            _ctx: RunContextWrapper[Any], operation: ApplyPatchOperation, _call_id: str
        ) -> bool:
            return operation.type == "delete_file"

        tool = SandboxApplyPatchTool(session=ApplyPatchSession())
        tool.needs_approval = needs_approval

        result = await _execute_custom_tool_call(
            tool,
            context_wrapper=make_context_wrapper(),
            raw_input="*** Begin Patch\n*** Delete File: notes.txt\n*** End Patch\n",
        )

        assert isinstance(result, ToolApprovalItem)

    @pytest.mark.asyncio
    async def test_invalid_patch_input_surfaces_tool_error_after_approval_precheck(self) -> None:
        tool = SandboxApplyPatchTool(session=ApplyPatchSession(), needs_approval=True)

        result = await _execute_custom_tool_call(
            tool,
            context_wrapper=make_context_wrapper(),
            raw_input="not a valid patch",
        )

        assert isinstance(result, ToolCallOutputItem)
        assert "apply_patch input must start with '*** Begin Patch'" in result.output

    @pytest.mark.asyncio
    async def test_editor_create_update_delete_round_trip(self) -> None:
        session = ApplyPatchSession()
        tool = SandboxApplyPatchTool(session=session)

        create_result = await cast(
            Awaitable[ApplyPatchResult],
            tool.editor.create_file(
                ApplyPatchOperation(
                    type="create_file",
                    path="notes.txt",
                    diff="+hello\n+world\n",
                )
            ),
        )
        assert isinstance(create_result, ApplyPatchResult)
        assert create_result.output == "Created notes.txt"
        assert session.files[Path("/workspace/notes.txt")] == b"hello\nworld"

        update_result = await cast(
            Awaitable[ApplyPatchResult],
            tool.editor.update_file(
                ApplyPatchOperation(
                    type="update_file",
                    path="notes.txt",
                    diff="@@\n-hello\n+hi\n world\n",
                )
            ),
        )
        assert isinstance(update_result, ApplyPatchResult)
        assert update_result.output == "Updated notes.txt"
        assert session.files[Path("/workspace/notes.txt")] == b"hi\nworld"

        delete_result = await cast(
            Awaitable[ApplyPatchResult],
            tool.editor.delete_file(
                ApplyPatchOperation(
                    type="delete_file",
                    path="notes.txt",
                )
            ),
        )
        assert isinstance(delete_result, ApplyPatchResult)
        assert delete_result.output == "Deleted notes.txt"
        assert Path("/workspace/notes.txt") not in session.files

    @pytest.mark.asyncio
    async def test_editor_runs_file_operations_as_bound_user(self) -> None:
        session = UserRecordingApplyPatchSession()
        session.files[Path("/workspace/existing.txt")] = b"old\n"
        tool = SandboxApplyPatchTool(session=session, user=User(name="sandbox-user"))

        await cast(
            Awaitable[ApplyPatchResult],
            tool.editor.update_file(
                ApplyPatchOperation(
                    type="update_file",
                    path="existing.txt",
                    diff="@@\n-old\n+new\n",
                )
            ),
        )
        await cast(
            Awaitable[ApplyPatchResult],
            tool.editor.create_file(
                ApplyPatchOperation(
                    type="create_file",
                    path="created.txt",
                    diff="+created\n",
                )
            ),
        )
        await cast(
            Awaitable[ApplyPatchResult],
            tool.editor.delete_file(
                ApplyPatchOperation(
                    type="delete_file",
                    path="existing.txt",
                )
            ),
        )

        assert session.read_users == ["sandbox-user", "sandbox-user"]
        assert session.mkdir_users == ["sandbox-user", "sandbox-user"]
        assert session.write_users == ["sandbox-user", "sandbox-user"]
        assert session.rm_users == ["sandbox-user"]

    @pytest.mark.asyncio
    async def test_custom_tool_input_create_update_move_delete(self) -> None:
        session = ApplyPatchSession()
        tool = SandboxApplyPatchTool(session=session)
        context_wrapper = make_context_wrapper()

        await _execute_custom_tool_call(
            tool,
            context_wrapper=context_wrapper,
            raw_input=("*** Begin Patch\n*** Add File: notes.txt\n+hello\n+world\n*** End Patch\n"),
        )
        assert session.files[Path("/workspace/notes.txt")] == b"hello\nworld"

        result = await _execute_custom_tool_call(
            tool,
            context_wrapper=context_wrapper,
            raw_input=(
                "*** Begin Patch\n"
                "*** Update File: notes.txt\n"
                "*** Move to: moved.txt\n"
                "@@\n"
                "-hello\n"
                "+hi\n"
                " world\n"
                "*** End Patch\n"
            ),
        )
        assert "Updated notes.txt" in result.output
        assert "Moved notes.txt to moved.txt" in result.output
        assert Path("/workspace/notes.txt") not in session.files
        assert session.files[Path("/workspace/moved.txt")] == b"hi\nworld"

        await _execute_custom_tool_call(
            tool,
            context_wrapper=context_wrapper,
            raw_input="*** Begin Patch\n*** Delete File: moved.txt\n*** End Patch\n",
        )
        assert Path("/workspace/moved.txt") not in session.files


async def _execute_custom_tool_call(
    tool: SandboxApplyPatchTool,
    *,
    context_wrapper: RunContextWrapper[Any],
    raw_input: str,
) -> Any:
    result = await CustomToolAction.execute(
        agent=Agent(name="patcher", tools=[tool]),
        call=ToolRunCustom(
            custom_tool=tool,
            tool_call={
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "call_apply",
                "input": raw_input,
            },
        ),
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )
    return result
