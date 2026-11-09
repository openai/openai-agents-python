"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from agents._codex_cli import events as _implementation
from agents._codex_cli.events import (
    ItemCompletedEvent as ItemCompletedEvent,
    ItemStartedEvent as ItemStartedEvent,
    ItemUpdatedEvent as ItemUpdatedEvent,
    ThreadError as ThreadError,
    ThreadErrorEvent as ThreadErrorEvent,
    ThreadEvent as ThreadEvent,
    ThreadStartedEvent as ThreadStartedEvent,
    TurnCompletedEvent as TurnCompletedEvent,
    TurnFailedEvent as TurnFailedEvent,
    TurnStartedEvent as TurnStartedEvent,
    Usage as Usage,
    _coerce_thread_error as _coerce_thread_error,
    _UnknownThreadEvent as _UnknownThreadEvent,
    coerce_thread_event as coerce_thread_event,
    coerce_usage as coerce_usage,
)

_sys.modules[__name__] = _implementation
