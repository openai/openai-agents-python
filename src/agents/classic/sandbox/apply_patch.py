"""Sandbox workspace editing compatibility exports."""

from _agents_shared.patch_format import PatchFormat as PatchFormat
from _agents_shared.workspace_editor import (
    V4AFormat as V4AFormat,
    WorkspaceEditor as WorkspaceEditor,
)

# Keep the released import and pickle paths for existing Classic callers.
for _type in (PatchFormat, V4AFormat, WorkspaceEditor):
    _type.__module__ = __name__
del _type

__all__ = ["PatchFormat", "V4AFormat", "WorkspaceEditor"]
