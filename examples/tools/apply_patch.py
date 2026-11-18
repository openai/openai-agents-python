import argparse
import asyncio
import hashlib
import os
import tempfile
from contextlib import closing, nullcontext
from pathlib import Path

from agents import Agent, ApplyPatchTool, RunConfig, Runner, SQLiteSession, apply_diff, trace
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


async def main(auto_approve: bool, model: str, use_codex: bool = False) -> None:
    config = None
    if use_codex:
        from agents.models.codex import CodexProvider

        config = RunConfig(model=os.environ.get("CODEX_MODEL"), model_provider=CodexProvider())
    # Hosted response IDs remain the default; Codex replays the SDK-managed history.
    session = SQLiteSession("apply-patch") if use_codex else None
    with closing(session) if session is not None else nullcontext(), trace("apply_patch_example"):
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
                    "<BEGIN_FILES> and <END_FILES> in your prompt. "
                    "For create_file, operation.diff contains only file-content lines prefixed "
                    "with '+'. For update_file, use @@ hunks and context/+/- lines. "
                    "Do not wrap operation.diff in *** Begin Patch, *** End Patch, filename "
                    "headers, Markdown fences, or <BEGIN_FILES>. "
                    "Use '- [ ] ' for unchecked items and '- [x] ' for checked items. "
                    "Only perform the edit requested in the current user message."
                ),
                tools=[tool],
            )

            print(f"[info] Workspace root: {workspace_path}")
            print(f"[info] Using model: {model}")
            print("[run] Creating tasks.md")
            result = await Runner.run(
                agent,
                "Create tasks.md with exactly 5 unchecked shopping items. Do not check off any items.",
                previous_response_id=None if use_codex else previous_response_id,
                session=session,
                run_config=config,
            )
            previous_response_id = result.last_response_id
            print(f"[run] Final response #1:\n{result.final_output}\n")
            notes_path = workspace_path / "tasks.md"
            if not notes_path.exists():
                raise RuntimeError(f"{notes_path} was not created by the apply_patch tool.")
            updated_notes = notes_path.read_text(encoding="utf-8")
            print("[file] tasks.md after creation:\n")
            print(updated_notes)
            original_items = [
                line.lstrip()
                for line in updated_notes.splitlines()
                if line.lstrip().startswith("- [")
            ]
            if len(original_items) != 5 or not all(
                line.startswith("- [ ] ") for line in original_items
            ):
                raise RuntimeError("Expected five unchecked shopping items after creation.")

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
                previous_response_id=None if use_codex else previous_response_id,
                session=session,
                run_config=config,
            )
            print(f"[run] Final response #2:\n{result2.final_output}\n")
            if not notes_path.exists():
                raise RuntimeError("tasks.md vanished unexpectedly before the second read.")
            print("[file] Final tasks.md:\n")
            final_notes = notes_path.read_text(encoding="utf-8")
            print(final_notes)
            checklist = [
                line.lstrip()
                for line in final_notes.splitlines()
                if line.lstrip().startswith("- [")
            ]
            expected = [
                line.replace("- [ ] ", "- [x] ", 1) if index >= 3 else line
                for index, line in enumerate(original_items)
            ]
            if checklist != expected:
                raise RuntimeError("Expected five checklist items with only the final two checked.")
            expected_items = iter(expected)
            expected_lines = [
                next(expected_items) if line.lstrip().startswith("- [") else line
                for line in updated_notes.split("\n")
            ]
            final_lines = [
                line.lstrip() if line.lstrip().startswith("- [") else line
                for line in final_notes.split("\n")
            ]
            if final_lines != expected_lines:
                raise RuntimeError("The update changed content outside the two checkbox markers.")


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
        default="gpt-5.6-sol",
        help="Model ID to use for the agent.",
    )
    parser.add_argument(
        "--codex", action="store_true", help="Use stock Codex with SDK-managed history."
    )
    args = parser.parse_args()
    asyncio.run(main(args.auto_approve, args.model, args.codex))
