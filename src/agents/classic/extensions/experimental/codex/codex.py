"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from agents._codex_cli import codex as _implementation
from agents._codex_cli.codex import (
    _UNSET as _UNSET,
    Codex as Codex,
    _normalize_env as _normalize_env,
    _UnsetType as _UnsetType,
)

_sys.modules[__name__] = _implementation
