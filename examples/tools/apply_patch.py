import argparse
import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from agents import Agent, ApplyPatchTool, ModelSettings, Runner, apply_diff, trace
from agents.editor import ApplyPatchOperation, ApplyPatchResult
from examples.auto_mode import confirm_with_fallback, is_auto_mode


class ApprovalTracker:
    def __init__(self) -> None:
        self._approved: set[str] = set()

    def fingerprint(self, operation: ApplyPatchOperation, relative_path: str) -> str:
        hasher = hashlib.sha256()
        hasher.update(operation.type.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(relative_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update((operation.diff or "").encode("utf-8"))
        return hasher.hexdigest()

    def remember(self, fingerprint: str) -> None:
        self._approved.add(fingerprint)

    def is_approved(self, fingerprint: str) -> bool:
        return fingerprint in self._approved


class WorkspaceEditor:
    def __init__(self, root: Path, approvals: ApprovalTracker, auto_approve: bool) -> None:
        self._root = root.resolve()
        self._approvals = approvals
        self._auto_approve = auto_approve or os.environ.get("APPLY_PATCH_AUTO_APPROVE") == "1"

    def create_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        relative = self._relative_path(operation.path)
        self._require_approval(operation, relative)
        target = self._resolve(operation.path, ensure_parent=True)
        diff = operation.diff or ""
        content = apply_diff("", diff, mode="create")
        target.write_text(content, encoding="utf-8")
        return ApplyPatchResult(output=f"Created {relative}")

    def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        relative = self._relative_path(operation.path)
        self._require_approval(operation, relative)
        target = self._resolve(operation.path)
        original = target.read_text(encoding="utf-8")
        diff = operation.diff or ""
        patched = apply_diff(original, diff)
        target.write_text(patched, encoding="utf-8")
        return ApplyPatchResult(output=f"Updated {relative}")

    def delete_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        relative = self._relative_path(operation.path)
        self._require_approval(operation, relative)
        target = self._resolve(operation.path)
        target.unlink(missing_ok=True)
        return ApplyPatchResult(output=f"Deleted {relative}")

    def _relative_path(self, value: str) -> str:
        resolved = self._resolve(value)
        return resolved.relative_to(self._root).as_posix()

    def _resolve(self, relative: str, ensure_parent: bool = False) -> Path:
        candidate = Path(relative)
        target = candidate if candidate.is_absolute() else (self._root / candidate)
        target = target.resolve()
        try:
            target.relative_to(self._root)
        except ValueError:
            raise RuntimeError(f"Operation outside workspace: {relative}") from None
        if ensure_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _require_approval(self, operation: ApplyPatchOperation, display_path: str) -> None:
        fingerprint = self._approvals.fingerprint(operation, display_path)
        if self._auto_approve or self._approvals.is_approved(fingerprint):
            self._approvals.remember(fingerprint)
            return

        print("\n[apply_patch] approval required")
        print(f"- type: {operation.type}")
        print(f"- path: {display_path}")
        if operation.diff:
            preview = operation.diff if len(operation.diff) < 400 else f"{operation.diff[:400]}…"
            print("- diff preview:\n", preview)
        approved = confirm_with_fallback("Proceed? [y/N] ", default=is_auto_mode())
        if not approved:
            raise RuntimeError("Apply patch operation rejected by user.")
        self._approvals.remember(fingerprint)


async def main(auto_approve: bool, model: str) -> None:
    with trace("apply_patch_example"):
        with tempfile.TemporaryDirectory(prefix="apply-patch-example-") as workspace:
            workspace_path = Path(workspace).resolve()
            approvals = ApprovalTracker()
            editor = WorkspaceEditor(workspace_path, approvals, auto_approve)
            tool = ApplyPatchTool(editor=editor)
            previous_response_id: str | None = None

            agent = Agent(
                name="Patch Assistant",
                model=model,
                instructions=(
                    f"You can edit files inside {workspace_path} using the apply_patch tool. "
                    "When modifying an existing file, include the file contents between "
                    "<BEGIN_FILES> and <END_FILES> in your prompt."
                ),
                tools=[tool],
                model_settings=ModelSettings(tool_choice="required"),
            )

            print(f"[info] Workspace root: {workspace_path}")
            print(f"[info] Using model: {model}")
            print("[run] Creating tasks.md")
            result = await Runner.run(
                agent,
                "Create tasks.md with a shopping checklist of 5 entries.",
                previous_response_id=previous_response_id,
            )
            previous_response_id = result.last_response_id
            print(f"[run] Final response #1:\n{result.final_output}\n")
            notes_path = workspace_path / "tasks.md"
            if not notes_path.exists():
                raise RuntimeError(f"{notes_path} was not created by the apply_patch tool.")
            updated_notes = notes_path.read_text(encoding="utf-8")
            print("[file] tasks.md after creation:\n")
            print(updated_notes)

            prompt = (
                "<BEGIN_FILES>\n"
                f"===== tasks.md\n{updated_notes}\n"
                "<END_FILES>\n"
                "Check off the last two items from the file."
            )
            print("\n[run] Updating tasks.md")
            result2 = await Runner.run(
                agent,
                prompt,
                previous_response_id=previous_response_id,
            )
            print(f"[run] Final response #2:\n{result2.final_output}\n")
            if not notes_path.exists():
                raise RuntimeError("tasks.md vanished unexpectedly before the second read.")
            print("[file] Final tasks.md:\n")
            print(notes_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        default=False,
        help="Skip manual confirmations for apply_patch operations.",
    )
    parser.add_argument(
        "--model",
        default="gpt-5.2",
        help="Model ID to use for the agent.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.auto_approve, args.model))
