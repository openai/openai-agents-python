"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from _agents_shared.codex_cli import codex as _implementation
from _agents_shared.codex_cli.codex import (
    _UNSET as _UNSET,
    Codex as Codex,
    _normalize_env as _normalize_env,
    _UnsetType as _UnsetType,
)

_sys.modules[__name__] = _implementation
