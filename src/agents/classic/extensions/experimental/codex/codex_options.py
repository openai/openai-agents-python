"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from agents._codex_cli import codex_options as _implementation
from agents._codex_cli.codex_options import (
    CodexOptions as CodexOptions,
    coerce_codex_options as coerce_codex_options,
)

_sys.modules[__name__] = _implementation
