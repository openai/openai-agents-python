"""Typed user context shared by every Northflank tool.

Pass a :class:`NorthflankCtx` instance to ``Runner.run(context=...)``. Each
tool reads it via ``RunContextWrapper[NorthflankCtx].context``.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from northflank import AsyncApiClient
except ImportError as exc:  # pragma: no cover - import path depends on optional extras
    raise ImportError(
        "Northflank tools require the optional `northflank` extra.\n"
        "Install it with: pip install 'openai-agents[northflank]'"
    ) from exc


@dataclass
class NorthflankCtx:
    """Runtime context every Northflank tool expects.

    ``project_id`` is optional so the same context can drive read tools that
    list across projects, but most mutating tools will raise if it is not
    set (they accept it explicitly on the call too).
    """

    client: AsyncApiClient
    project_id: str | None = None
    team_id: str | None = None


__all__ = ["NorthflankCtx"]
