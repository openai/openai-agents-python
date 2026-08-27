from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field

from ...tool import Tool
from ..workspace_paths import SandboxWorkspaceScope
from .capability import Capability
from .tools import SandboxApplyPatchTool, ViewImageTool


@dataclass
class FilesystemToolSet:
    """Mutable bundle of tools exposed by the filesystem capability."""

    view_image: ViewImageTool
    apply_patch: SandboxApplyPatchTool
    workspace_scope: SandboxWorkspaceScope = field(default_factory=SandboxWorkspaceScope)


FilesystemToolConfigurator = Callable[[FilesystemToolSet], None]


class Filesystem(Capability):
    type: Literal["filesystem"] = "filesystem"
    configure_tools: FilesystemToolConfigurator | None = Field(default=None, exclude=True)
    """Optional callback that can customize or replace bundled filesystem tools."""

    def tools(self) -> list[Tool]:
        if self.session is None:
            raise ValueError("Filesystem capability is not bound to a SandboxSession")

        toolset = FilesystemToolSet(
            view_image=ViewImageTool(
                session=self.session,
                user=self.run_as,
                workspace_scope=self.workspace_scope,
            ),
            apply_patch=SandboxApplyPatchTool(
                session=self.session,
                user=self.run_as,
                workspace_scope=self.workspace_scope,
            ),
            workspace_scope=self.workspace_scope,
        )
        if self.configure_tools is not None:
            self.configure_tools(toolset)

        return [toolset.view_image, toolset.apply_patch]
