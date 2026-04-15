"""
Mode Gate — enforces plan mode vs execute mode distinction.

Migrated from .claude/hooks/mode-gate.sh (vault).

In plan mode, certain high-impact tools are blocked regardless of content:
- Bash (no shell execution in plan)
- External comms (slack, gmail, sheet modify)

The mode is read from an environment file (`~/.local/share/tbc/caldero/mode`)
or falls back to "execute" if missing. This lets humans toggle plan mode
externally without restarting the Caldero.
"""
from __future__ import annotations

from pathlib import Path

from agents import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    tool_input_guardrail,
)

MODE_FILE = Path.home() / ".local" / "share" / "tbc" / "caldero" / "mode"
VALID_MODES = {"plan", "execute", "read-only"}

# Tools blocked in plan mode
PLAN_BLOCKED_TOOLS: set[str] = {
    "bash",
    "slack_send_message",
    "slack_schedule_message",
    "send_gmail_message",
    "draft_gmail_message",
    "modify_sheet_values",
    "write_file",
    "edit_file",
}

# Additional tools blocked in read-only mode (bash + all writes)
READONLY_BLOCKED_TOOLS: set[str] = PLAN_BLOCKED_TOOLS | {
    "create_drive_file",
    "manage_drive_access",
    "discord_reply",
    "telegram_reply",
}


def _read_mode() -> str:
    """Read current Caldero mode. Defaults to 'execute' if file missing."""
    try:
        if MODE_FILE.exists():
            mode = MODE_FILE.read_text().strip().lower()
            if mode in VALID_MODES:
                return mode
    except Exception:
        pass
    return "execute"


@tool_input_guardrail
def mode_input_guardrail(
    data: ToolInputGuardrailData,
) -> ToolGuardrailFunctionOutput:
    """
    Block tools that violate the current Caldero mode.

    Modes:
    - execute (default) — all tools allowed, other gates still apply
    - plan — blocks bash, external comms, write/edit tools
    - read-only — blocks everything except pure reads (files, queries, APIs)
    """
    tool_name = data.context.tool_name if data.context else ""
    mode = _read_mode()

    if mode == "execute":
        return ToolGuardrailFunctionOutput(
            output_info={"mode": "execute", "status": "passed"}
        )

    if mode == "plan":
        for blocked in PLAN_BLOCKED_TOOLS:
            if blocked in tool_name:
                return ToolGuardrailFunctionOutput.reject_content(
                    message=(
                        f"🛑 Mode gate: Caldero is in PLAN mode. "
                        f"Tool {tool_name!r} is blocked. "
                        f"Either finish planning or switch mode via "
                        f"`echo execute > {MODE_FILE}`"
                    ),
                    output_info={"mode": "plan", "blocked_tool": tool_name},
                )

    if mode == "read-only":
        for blocked in READONLY_BLOCKED_TOOLS:
            if blocked in tool_name:
                return ToolGuardrailFunctionOutput.reject_content(
                    message=(
                        f"🛑 Mode gate: Caldero is in READ-ONLY mode. "
                        f"Tool {tool_name!r} is blocked. "
                        f"Switch mode via `echo execute > {MODE_FILE}` "
                        f"to allow writes."
                    ),
                    output_info={"mode": "read-only", "blocked_tool": tool_name},
                )

    return ToolGuardrailFunctionOutput(output_info={"mode": mode, "status": "passed"})
