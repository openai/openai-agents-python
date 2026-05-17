"""Internal helpers shared across the Northflank tool/shell modules."""

from __future__ import annotations

from typing import Any, Literal

from ...run_context import RunContextWrapper
from .context import NorthflankCtx

ShellMode = Literal["none", "sh", "bash"]


def wrap_shell_command(command: str, shell: ShellMode) -> tuple[str | list[str], Literal["none"]]:
    """Translate a one-line shell command into an SDK exec invocation.

    The Northflank exec proxy forwards the SDK's ``shell`` field verbatim
    and only recognises ``"none"`` for direct exec. So we always set
    ``shell="none"`` on the SDK side and, when a shell is requested, wrap
    the command in an explicit ``[shell, "-lc", command]`` argv ourselves.
    """
    if shell == "none":
        return command, "none"
    return [shell, "-lc", command], "none"


def resolve_project_id(ctx: RunContextWrapper[NorthflankCtx], project_id: str | None) -> str:
    """Resolve ``project_id`` from the call args, falling back to the context.

    Raises a clear error if neither is set so the model gets actionable
    feedback instead of a 404 from the API.
    """
    resolved = project_id or ctx.context.project_id
    if not resolved:
        raise ValueError(
            "project_id is required: pass it explicitly or set NorthflankCtx.project_id."
        )
    return resolved


def resolve_team_id(ctx: RunContextWrapper[NorthflankCtx], team_id: str | None) -> str | None:
    """Resolve ``team_id`` with context fallback. ``None`` is valid (uses the
    user-scoped routes)."""
    return team_id or ctx.context.team_id


def unwrap(response: Any) -> dict[str, Any]:
    """Return the SDK response's ``.data`` payload or the raw value.

    Northflank ``ApiCallResponse.data`` is always a TypedDict shape; tools
    surface it directly and the agents runtime takes care of JSON
    stringification.
    """
    payload = getattr(response, "data", response)
    return payload if isinstance(payload, dict) else {"value": payload}
