"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from agents._codex_cli import payloads as _implementation
from agents._codex_cli.payloads import (
    _DictLike as _DictLike,
)

_sys.modules[__name__] = _implementation
