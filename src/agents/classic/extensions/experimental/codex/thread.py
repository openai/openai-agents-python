"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from agents._codex_cli import thread as _implementation
from agents._codex_cli.thread import (
    Input as Input,
    LocalImageInput as LocalImageInput,
    RunResult as RunResult,
    RunStreamedResult as RunStreamedResult,
    StreamedTurn as StreamedTurn,
    TextInput as TextInput,
    Thread as Thread,
    Turn as Turn,
    UserInput as UserInput,
    _aclosing as _aclosing,
    _normalize_input as _normalize_input,
    _parse_event as _parse_event,
)

_sys.modules[__name__] = _implementation
