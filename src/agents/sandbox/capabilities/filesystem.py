from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from ...tool import Tool
from ._run_cwd_tools import RunCwdSandboxApplyPatchTool, RunCwdViewImageTool
from .capability import Capability
from .tools import SandboxApplyPatchTool, ViewImageTool


@dataclass
class FilesystemToolSet:
    """Mutable bundle of tools exposed by the filesystem capability."""

    view_image: ViewImageTool
    apply_patch: SandboxApplyPatchTool


FilesystemToolConfigurator = Callable[[FilesystemToolSet], None]


class Filesystem(Capability):
    type: Literal["filesystem"] = "filesystem"
    configure_tools: FilesystemToolConfigurator | None = Field(default=None, exclude=True)
    """Optional callback that can customize or replace bundled filesystem tools."""

    def tools(self) -> list[Tool]:
        if self.session is None:
            raise ValueError("Filesystem capability is not bound to a SandboxSession")

        toolset = FilesystemToolSet(
            view_image=RunCwdViewImageTool(
                session=self.session,
                user=self.run_as,
                cwd=self.run_cwd,
            ),
            apply_patch=RunCwdSandboxApplyPatchTool(
                session=self.session,
                user=self.run_as,
                cwd=self.run_cwd,
            ),
        )
        if self.configure_tools is not None:
            self.configure_tools(toolset)
        if isinstance(toolset.view_image, RunCwdViewImageTool):
            toolset.view_image.finalize_run_cwd()

        return [toolset.view_image, toolset.apply_patch]
