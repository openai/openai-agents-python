from __future__ import annotations

import functools
from pathlib import Path

from .storage import PhaseTwoInputSelection

_PROMPTS_DIR = Path(__file__).parent / "prompts"


@functools.cache
def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text("utf-8")


MEMORY_CONSOLIDATION_PROMPT_TEMPLATE = _load_prompt("memory_consolidation_prompt.md")
MEMORY_READ_PROMPT_TEMPLATE = _load_prompt("memory_read_prompt.md")
ROLLOUT_EXTRACTION_PROMPT_TEMPLATE = _load_prompt("rollout_extraction_prompt.md")
ROLLOUT_EXTRACTION_USER_MESSAGE_TEMPLATE = _load_prompt("rollout_extraction_user_message.md")

_EXTRA_PROMPT_PLACEHOLDER = "{{ extra_prompt_section }}"
_PHASE_TWO_INPUT_SELECTION_PLACEHOLDER = "{{ phase_two_input_selection }}"
_EXTRA_PROMPT_SECTION_TEMPLATE = """============================================================
DEVELOPER-SPECIFIC EXTRA GUIDANCE
============================================================

The developer provided additional guidance for memory writing. Pay extra attention to
capturing these details when they would be useful for future runs, in addition to the
standard user preferences, failure recovery, and task summary signals. Keep following the
schema, safety, and evidence rules above.

{extra_prompt}
"""

MEMORY_READ_ONLY_INSTRUCTIONS = "Never update memories. You can only read them."
MEMORY_LIVE_UPDATE_INSTRUCTIONS = """When to update memory (automatic, same turn; required):

- Treat memory as guidance, not truth: if memory conflicts with current workspace
  state, tool outputs, environment, or user feedback, current evidence wins.
- Memory is writable. You are authorized to edit {memory_dir}/MEMORY.md when stale
  guidance is detected.
- If any memory fact conflicts with current evidence, you MUST update memory in the
  same turn. Do not wait for a separate user prompt.
- If you detect stale memory, updating {memory_dir}/MEMORY.md is part of task
  completion, not optional cleanup.
- Required behavior after detecting stale memory:
  1. Verify the correct replacement using local evidence.
  2. Continue the task using current evidence; do not rely on stale memory.
  3. Edit {memory_dir}/MEMORY.md later in the same turn, before your final response.
  4. Finalize the task after the memory update is written."""


def render_memory_read_prompt(
    *,
    memory_dir: str,
    memory_summary: str,
    live_update: bool = False,
) -> str:
    update_instructions = (
        MEMORY_LIVE_UPDATE_INSTRUCTIONS.replace("{memory_dir}", memory_dir)
        if live_update
        else MEMORY_READ_ONLY_INSTRUCTIONS
    )
    return (
        MEMORY_READ_PROMPT_TEMPLATE.replace("{memory_dir}", memory_dir)
        .replace("{memory_update_instructions}", update_instructions)
        .replace("{memory_summary}", memory_summary)
    )


def render_memory_consolidation_prompt(
    *,
    memory_root: str,
    selection: PhaseTwoInputSelection,
    extra_prompt: str | None = None,
) -> str:
    return (
        MEMORY_CONSOLIDATION_PROMPT_TEMPLATE.replace("{{ memory_root }}", memory_root)
        .replace(
            _PHASE_TWO_INPUT_SELECTION_PLACEHOLDER,
            _render_phase_two_input_selection(selection),
        )
        .replace(
            _EXTRA_PROMPT_PLACEHOLDER,
            _render_extra_prompt_section(extra_prompt),
        )
    )


def render_rollout_extraction_prompt(
    *,
    extra_prompt: str | None = None,
) -> str:
    return ROLLOUT_EXTRACTION_PROMPT_TEMPLATE.replace(
        _EXTRA_PROMPT_PLACEHOLDER,
        _render_extra_prompt_section(extra_prompt),
    )


def render_rollout_extraction_user_prompt(
    *,
    terminal_metadata_json: str,
    rollout_contents: str,
) -> str:
    return ROLLOUT_EXTRACTION_USER_MESSAGE_TEMPLATE.format(
        terminal_metadata_json=terminal_metadata_json,
        rollout_contents=rollout_contents,
    )


def _render_extra_prompt_section(extra_prompt: str | None) -> str:
    if extra_prompt is None or not extra_prompt.strip():
        return ""
    return "\n" + _EXTRA_PROMPT_SECTION_TEMPLATE.format(extra_prompt=extra_prompt.strip())


def _render_phase_two_input_selection(selection: PhaseTwoInputSelection) -> str:
    retained = len(selection.retained_rollout_ids)
    added = len(selection.selected) - retained
    selected_lines = (
        "\n".join(
            _render_selected_input_line(
                rollout_id=item.rollout_id,
                rollout_summary_file=item.rollout_summary_file,
                updated_at=item.updated_at,
                retained=item.rollout_id in selection.retained_rollout_ids,
            )
            for item in selection.selected
        )
        if selection.selected
        else "- none"
    )
    removed_lines = (
        "\n".join(
            _render_removed_input_line(
                rollout_id=item.rollout_id,
                rollout_summary_file=item.rollout_summary_file,
                updated_at=item.updated_at,
            )
            for item in selection.removed
        )
        if selection.removed
        else "- none"
    )
    return (
        f"- selected inputs this run: {len(selection.selected)}\n"
        f"- newly added since the last successful Phase 2 run: {added}\n"
        f"- retained from the last successful Phase 2 run: {retained}\n"
        f"- removed from the last successful Phase 2 run: {len(selection.removed)}\n\n"
        f"Current selected Phase 1 inputs:\n{selected_lines}\n\n"
        f"Removed from the last successful Phase 2 selection:\n{removed_lines}\n"
    )


def _render_selected_input_line(
    *,
    rollout_id: str,
    rollout_summary_file: str,
    updated_at: str,
    retained: bool,
) -> str:
    status = "retained" if retained else "added"
    return (
        f"- [{status}] rollout_id={rollout_id}, "
        f"rollout_summary_file={rollout_summary_file}, updated_at={updated_at or 'unknown'}"
    )


def _render_removed_input_line(
    *,
    rollout_id: str,
    rollout_summary_file: str,
    updated_at: str,
) -> str:
    return (
        f"- rollout_id={rollout_id}, "
        f"rollout_summary_file={rollout_summary_file}, updated_at={updated_at or 'unknown'}"
    )
