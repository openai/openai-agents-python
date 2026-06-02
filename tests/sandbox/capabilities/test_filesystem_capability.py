from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from agents.editor import ApplyPatchOperation
from agents.sandbox import Manifest
from agents.sandbox.capabilities import Filesystem, FilesystemToolSet
from agents.sandbox.capabilities.tools import SandboxApplyPatchTool, ViewImageTool
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxSession,
    UnixLocalSandboxSessionState,
)
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import User
from agents.tool import CustomTool, FunctionTool


def _make_session(tmp_path: Path) -> UnixLocalSandboxSession:
    return UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(tmp_path / "workspace")),
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
            workspace_root_owned=False,
        )
    )


class TestFilesystemCapability:
    def test_tools_requires_bound_session(self) -> None:
        capability = Filesystem()

        with pytest.raises(
            ValueError,
            match="Filesystem capability is not bound to a SandboxSession",
        ):
            capability.tools()

    def test_tools_exposes_view_image_and_apply_patch_after_bind(self, tmp_path: Path) -> None:
        capability = Filesystem()
        capability.bind(_make_session(tmp_path))

        tools = capability.tools()

        assert len(tools) == 2
        assert isinstance(tools[0], ViewImageTool)
        assert isinstance(tools[1], SandboxApplyPatchTool)
        assert isinstance(tools[0], FunctionTool)
        assert isinstance(tools[1], CustomTool)
        assert tools[0].name == "view_image"
        assert tools[1].name == "apply_patch"

    def test_configure_tools_can_customize_approvals_after_clone(self, tmp_path: Path) -> None:
        async def view_image_needs_approval(
            _ctx: Any, params: dict[str, Any], _call_id: str
        ) -> bool:
            return str(params["path"]).startswith("sensitive/")

        async def apply_patch_needs_approval(
            _ctx: Any, operation: ApplyPatchOperation, _call_id: str
        ) -> bool:
            return operation.type != "create_file"

        def configure_tools(toolset: FilesystemToolSet) -> None:
            toolset.view_image.needs_approval = view_image_needs_approval
            toolset.apply_patch.needs_approval = apply_patch_needs_approval

        capability = Filesystem(configure_tools=configure_tools).clone()
        capability.bind(_make_session(tmp_path))

        tools = capability.tools()
        view_image_tool = cast(ViewImageTool, tools[0])
        apply_patch_tool = cast(SandboxApplyPatchTool, tools[1])

        assert isinstance(view_image_tool, ViewImageTool)
        assert isinstance(apply_patch_tool, SandboxApplyPatchTool)
        assert cast(object, view_image_tool.needs_approval) is view_image_needs_approval
        assert cast(object, apply_patch_tool.needs_approval) is apply_patch_needs_approval

    def test_configure_tools_can_replace_tool_instances(self, tmp_path: Path) -> None:
        replacement_view_image: ViewImageTool | None = None

        def configure_tools(toolset: FilesystemToolSet) -> None:
            nonlocal replacement_view_image
            replacement_view_image = ViewImageTool(
                session=toolset.view_image.session,
                needs_approval=True,
            )
            toolset.view_image = replacement_view_image

        capability = Filesystem(configure_tools=configure_tools)
        capability.bind(_make_session(tmp_path))

        tools = capability.tools()
        view_image_tool = cast(ViewImageTool, tools[0])

        assert replacement_view_image is not None
        assert view_image_tool is replacement_view_image
        assert view_image_tool.needs_approval is True
        assert isinstance(tools[1], SandboxApplyPatchTool)

    def test_tools_passes_bound_run_as_to_file_tools(self, tmp_path: Path) -> None:
        run_as = User(name="sandbox-user")
        capability = Filesystem()
        capability.bind(_make_session(tmp_path))
        capability.bind_run_as(run_as)

        tools = capability.tools()

        assert isinstance(tools[0], ViewImageTool)
        assert isinstance(tools[1], SandboxApplyPatchTool)
        assert tools[0].user == run_as
        assert tools[1].editor.user == run_as

    @pytest.mark.asyncio
    async def test_instructions_default_to_none(self) -> None:
        capability = Filesystem()

        instructions = await capability.instructions(Manifest(root="/workspace"))

        assert instructions is None
