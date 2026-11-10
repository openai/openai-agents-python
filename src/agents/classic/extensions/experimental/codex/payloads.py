"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from _agents_shared.codex_cli import payloads as _implementation
from _agents_shared.codex_cli.payloads import (
    _DictLike as _DictLike,
)

_sys.modules[__name__] = _implementation
