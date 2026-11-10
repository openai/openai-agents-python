"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from _agents_shared.codex_cli import thread_options as _implementation
from _agents_shared.codex_cli.thread_options import (
    ApprovalMode as ApprovalMode,
    ModelReasoningEffort as ModelReasoningEffort,
    SandboxMode as SandboxMode,
    ThreadOptions as ThreadOptions,
    WebSearchMode as WebSearchMode,
    coerce_thread_options as coerce_thread_options,
)

_sys.modules[__name__] = _implementation
