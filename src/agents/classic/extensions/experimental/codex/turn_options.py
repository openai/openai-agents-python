"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from _agents_shared.codex_cli import turn_options as _implementation
from _agents_shared.codex_cli.turn_options import (
    AbortSignal as AbortSignal,
    TurnOptions as TurnOptions,
    coerce_turn_options as coerce_turn_options,
)

_sys.modules[__name__] = _implementation
