from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from agents.exceptions import UserError

AbortSignal = asyncio.Event


@dataclass(frozen=True)
class TurnOptions:
    # JSON schema used by Codex for structured output.
    output_schema: dict[str, Any] | None = None
    # Cancellation signal for the Codex CLI subprocess.
    signal: AbortSignal | None = None
    # Abort the Codex CLI if no events arrive within this many seconds.
    idle_timeout_seconds: float | None = None


def coerce_turn_options(
    options: TurnOptions | Mapping[str, Any] | None,
) -> TurnOptions | None:
    if options is None or isinstance(options, TurnOptions):
        return options
    if not isinstance(options, Mapping):
        raise UserError("TurnOptions must be a TurnOptions or a mapping.")

    allowed = {field.name for field in fields(TurnOptions)}
    unknown = set(options.keys()) - allowed
    if unknown:
        raise UserError(f"Unknown TurnOptions field(s): {sorted(unknown)}")

    return TurnOptions(**dict(options))
