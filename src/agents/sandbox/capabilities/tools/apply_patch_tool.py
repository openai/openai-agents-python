from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ....editor import ApplyPatchEditor, ApplyPatchOperation, ApplyPatchResult
from ....run_context import RunContextWrapper
from ....tool import (
    ApplyPatchApprovalFunction,
    ApplyPatchOnApprovalFunction,
    CustomTool,
    CustomToolApprovalFunction,
)
from ....tool_context import ToolContext
from ....util._approvals import evaluate_needs_approval_setting
from ...apply_patch import WorkspaceEditor
from ...session.base_sandbox_session import BaseSandboxSession
from ...types import User

_APPLY_PATCH_CUSTOM_TOOL_GRAMMAR = r"""
start: begin_patch hunk+ end_patch
begin_patch: "*** Begin Patch" LF
end_patch: "*** End Patch" LF?

hunk: add_hunk | delete_hunk | update_hunk
add_hunk: "*** Add File: " filename LF add_line+
delete_hunk: "*** Delete File: " filename LF
update_hunk: "*** Update File: " filename LF change_move? change?

filename: /(.+)/
add_line: "+" /(.*)/ LF -> line

change_move: "*** Move to: " filename LF
change: (change_context | change_line)+ eof_line?
change_context: ("@@" | "@@ " /(.+)/) LF
change_line: ("+" | "-" | " ") /(.*)/ LF
eof_line: "*** End of File" LF

%import common.LF
""".strip()

_APPLY_PATCH_CUSTOM_TOOL_DESCRIPTION = r"""
Use the `apply_patch` tool to edit files. This is a FREEFORM tool, so do not wrap the patch in JSON.
Your patch language is a stripped-down, file-oriented diff format designed to be easy to
parse and safe to apply. You can think of it as a high-level envelope:

*** Begin Patch
[ one or more file sections ]
*** End Patch

Within that envelope, you get a sequence of file operations.
You MUST include a header to specify the action you are taking.
Each operation starts with one of three headers:

*** Add File: <path> - create a new file. Every following line is a + line (the initial contents).
*** Delete File: <path> - remove an existing file. Nothing follows.
*** Update File: <path> - patch an existing file in place (optionally with a rename).

May be immediately followed by *** Move to: <new path> if you want to rename the file.
Then one or more hunks, each introduced by @@ (optionally followed by a hunk header).
Within a hunk, each line starts with a space, -, or +.

For context lines:
- By default, show 3 lines of code immediately above and 3 lines immediately below each
change. If a change is within 3 lines of a previous change, do NOT duplicate the first
change's post-context lines in the second change's pre-context lines.
- If 3 lines of context is insufficient to uniquely identify the snippet of code within the
file, use the @@ operator to indicate the class or function to which the snippet belongs.
For instance:
@@ class BaseClass
[3 lines of pre-context]
-[old_code]
+[new_code]
[3 lines of post-context]

- If a code block is repeated so many times in a class or function that a single @@ statement
and 3 lines of context cannot uniquely identify the snippet, use multiple @@ statements to
jump to the right context. For instance:

@@ class BaseClass
@@ def method():
[3 lines of pre-context]
-[old_code]
+[new_code]
[3 lines of post-context]

The full grammar definition is below:
Patch := Begin { FileOp } End
Begin := "*** Begin Patch" NEWLINE
End := "*** End Patch" NEWLINE
FileOp := AddFile | DeleteFile | UpdateFile
AddFile := "*** Add File: " path NEWLINE { "+" line NEWLINE }
DeleteFile := "*** Delete File: " path NEWLINE
UpdateFile := "*** Update File: " path NEWLINE [ MoveTo ] { Hunk }
MoveTo := "*** Move to: " newPath NEWLINE
Hunk := "@@" [ header ] NEWLINE { HunkLine } [ "*** End of File" NEWLINE ]
HunkLine := (" " | "-" | "+") text NEWLINE

A full patch can combine several operations:

*** Begin Patch
*** Add File: hello.txt
+Hello world
*** Update File: src/app.py
*** Move to: src/main.py
@@ def greet():
-print("Hi")
+print("Hello, world!")
*** Delete File: obsolete.txt
*** End Patch

Important:
- You must include a header with your intended action (Add/Delete/Update).
- You must prefix new lines with + even when creating a new file.
- File references can only be relative, NEVER ABSOLUTE.
""".strip()

_APPLY_PATCH_CUSTOM_TOOL_CONFIG: dict[str, Any] = {
    "type": "custom",
    "name": "apply_patch",
    "description": _APPLY_PATCH_CUSTOM_TOOL_DESCRIPTION,
    "format": {
        "type": "grammar",
        "syntax": "lark",
        "definition": _APPLY_PATCH_CUSTOM_TOOL_GRAMMAR,
    },
}

_BEGIN_PATCH = "*** Begin Patch"
_END_PATCH = "*** End Patch"
_ADD_FILE = "*** Add File: "
_DELETE_FILE = "*** Delete File: "
_UPDATE_FILE = "*** Update File: "
_MOVE_TO = "*** Move to: "


class SandboxApplyPatchEditor(ApplyPatchEditor):
    def __init__(self, session: BaseSandboxSession, *, user: str | User | None = None) -> None:
        self.session = session
        self.user = user

    async def create_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return await WorkspaceEditor(self.session, user=self.user).apply_operation(operation)

    async def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return await WorkspaceEditor(self.session, user=self.user).apply_operation(operation)

    async def delete_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return await WorkspaceEditor(self.session, user=self.user).apply_operation(operation)


class SandboxApplyPatchTool(CustomTool):
    # `CustomTool` stores raw-input approval callbacks, but this sandbox wrapper exposes
    # operation-typed approval callbacks publicly and adapts them at runtime.
    needs_approval: bool | ApplyPatchApprovalFunction = False  # type: ignore[assignment]
    on_approval: ApplyPatchOnApprovalFunction | None = None

    def __init__(
        self,
        *,
        session: BaseSandboxSession,
        user: str | User | None = None,
        needs_approval: bool | ApplyPatchApprovalFunction = False,
        on_approval: ApplyPatchOnApprovalFunction | None = None,
    ) -> None:
        self.session = session
        self.editor = SandboxApplyPatchEditor(session, user=user)
        super().__init__(
            name="apply_patch",
            description=_APPLY_PATCH_CUSTOM_TOOL_DESCRIPTION,
            format=_APPLY_PATCH_CUSTOM_TOOL_CONFIG["format"],
            on_invoke_tool=self._on_invoke_tool,
            needs_approval=False,
            on_approval=on_approval,
        )
        self.needs_approval = needs_approval
        self.on_approval = on_approval

    @property
    def operation_needs_approval(self) -> bool | ApplyPatchApprovalFunction:
        return self.needs_approval

    @operation_needs_approval.setter
    def operation_needs_approval(self, value: bool | ApplyPatchApprovalFunction) -> None:
        self.needs_approval = value

    def runtime_needs_approval(self) -> CustomToolApprovalFunction:
        return self._needs_custom_approval

    def parse_custom_input(self, raw_input: str) -> list[ApplyPatchOperation]:
        return _parse_custom_tool_input(raw_input)

    async def _needs_custom_approval(
        self, ctx_wrapper: RunContextWrapper[Any], raw_input: str, call_id: str
    ) -> bool:
        try:
            operations = self.parse_custom_input(raw_input)
        except ValueError:
            # Let malformed patches flow through normal tool execution so the model gets a
            # recoverable tool error instead of aborting the whole run during approval pre-checks.
            return False

        for operation in operations:
            if await evaluate_needs_approval_setting(
                self.needs_approval,
                ctx_wrapper,
                operation,
                call_id,
            ):
                return True
        return False

    async def _on_invoke_tool(self, ctx: ToolContext[Any], raw_input: str) -> str:
        operation_outputs: list[str] = []
        for operation in self.parse_custom_input(raw_input):
            operation.ctx_wrapper = ctx
            if operation.type == "create_file":
                result = await self.editor.create_file(operation)
            elif operation.type == "update_file":
                result = await self.editor.update_file(operation)
            elif operation.type == "delete_file":
                result = await self.editor.delete_file(operation)
            else:
                raise ValueError(f"Unsupported apply_patch operation: {operation.type}")
            if result.output:
                operation_outputs.append(result.output)
        return "\n".join(operation_outputs)


def _parse_custom_tool_input(raw_input: str) -> list[ApplyPatchOperation]:
    stripped_input = raw_input.lstrip()
    if stripped_input.startswith(("{", "[")):
        return _parse_apply_patch_json(raw_input)
    return _parse_apply_patch_input(raw_input)


def _parse_apply_patch_json(raw_input: str) -> list[ApplyPatchOperation]:
    payload = json.loads(raw_input)
    if isinstance(payload, Mapping):
        operations = payload.get("operations")
        if isinstance(operations, Sequence) and not isinstance(operations, str | bytes):
            return [_parse_apply_patch_operation_json(operation) for operation in operations]
        operation = payload.get("operation")
        if operation is not None:
            return [_parse_apply_patch_operation_json(operation)]
        return [_parse_apply_patch_operation_json(payload)]
    if isinstance(payload, Sequence) and not isinstance(payload, str | bytes):
        return [_parse_apply_patch_operation_json(operation) for operation in payload]
    raise ValueError("apply_patch JSON input must be an object or array")


def _parse_apply_patch_operation_json(operation: object) -> ApplyPatchOperation:
    if not isinstance(operation, Mapping):
        raise ValueError("apply_patch operation must be an object")

    raw_type = operation.get("type")
    raw_path = operation.get("path")
    raw_diff = operation.get("diff")
    if raw_type not in {"create_file", "update_file", "delete_file"}:
        raise ValueError(f"Invalid apply_patch operation type: {raw_type}")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("apply_patch operation is missing a path")
    if raw_type in {"create_file", "update_file"} and not isinstance(raw_diff, str):
        raise ValueError(f"apply_patch operation {raw_type} is missing a diff")
    if raw_type == "delete_file":
        raw_diff = None

    raw_move_to = operation.get("move_to")
    if raw_move_to is not None and not isinstance(raw_move_to, str):
        raise ValueError("apply_patch operation move_to must be a string")

    return ApplyPatchOperation(
        type=raw_type,
        path=raw_path,
        diff=raw_diff,
        move_to=raw_move_to,
    )


def _parse_apply_patch_input(raw_input: str) -> list[ApplyPatchOperation]:
    lines = raw_input.splitlines()
    if not lines or lines[0] != _BEGIN_PATCH:
        raise ValueError("apply_patch input must start with '*** Begin Patch'")
    if len(lines) < 2 or lines[-1] != _END_PATCH:
        raise ValueError("apply_patch input must end with '*** End Patch'")

    operations: list[ApplyPatchOperation] = []
    index = 1
    while index < len(lines) - 1:
        line = lines[index]
        if line.startswith(_ADD_FILE):
            parsed, index = _parse_add_file(lines, index)
        elif line.startswith(_DELETE_FILE):
            parsed, index = _parse_delete_file(lines, index)
        elif line.startswith(_UPDATE_FILE):
            parsed, index = _parse_update_file(lines, index)
        else:
            raise ValueError(f"Invalid apply_patch file operation header: {line}")
        operations.append(parsed)

    if not operations:
        raise ValueError("apply_patch input must include at least one file operation")
    return operations


def _parse_add_file(lines: list[str], index: int) -> tuple[ApplyPatchOperation, int]:
    path = _parse_path_header(lines[index], _ADD_FILE)
    index += 1
    diff_lines: list[str] = []
    while index < len(lines) - 1 and not _is_file_operation_header(lines[index]):
        line = lines[index]
        if not line.startswith("+"):
            raise ValueError(f"Invalid Add File line: {line}")
        diff_lines.append(line)
        index += 1
    if not diff_lines:
        raise ValueError(f"Add File patch for {path} must include at least one + line")
    return (
        ApplyPatchOperation(type="create_file", path=path, diff=_join_diff(diff_lines)),
        index,
    )


def _parse_delete_file(lines: list[str], index: int) -> tuple[ApplyPatchOperation, int]:
    path = _parse_path_header(lines[index], _DELETE_FILE)
    index += 1
    if index < len(lines) - 1 and not _is_file_operation_header(lines[index]):
        raise ValueError(f"Delete File patch for {path} must not include a diff")
    return ApplyPatchOperation(type="delete_file", path=path), index


def _parse_update_file(lines: list[str], index: int) -> tuple[ApplyPatchOperation, int]:
    path = _parse_path_header(lines[index], _UPDATE_FILE)
    index += 1
    move_to: str | None = None
    if index < len(lines) - 1 and lines[index].startswith(_MOVE_TO):
        move_to = _parse_path_header(lines[index], _MOVE_TO)
        index += 1

    diff_lines: list[str] = []
    while index < len(lines) - 1 and not _is_file_operation_header(lines[index]):
        diff_lines.append(lines[index])
        index += 1
    if not diff_lines:
        raise ValueError(f"Update File patch for {path} must include a hunk")
    return (
        ApplyPatchOperation(
            type="update_file",
            path=path,
            diff=_join_diff(diff_lines),
            move_to=move_to,
        ),
        index,
    )


def _parse_path_header(line: str, prefix: str) -> str:
    path = line.removeprefix(prefix).strip()
    if not path:
        raise ValueError(f"Missing path in apply_patch header: {line}")
    return path


def _is_file_operation_header(line: str) -> bool:
    return line.startswith((_ADD_FILE, _DELETE_FILE, _UPDATE_FILE))


def _join_diff(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"
