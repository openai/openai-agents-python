from __future__ import annotations

import atexit
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .provider import TraceProvider  # pragma: no cover

GLOBAL_TRACE_PROVIDER: TraceProvider | None = None


def set_trace_provider(provider: TraceProvider) -> None:
    """Set the global trace provider used by tracing utilities."""
    global GLOBAL_TRACE_PROVIDER
    GLOBAL_TRACE_PROVIDER = provider


def get_trace_provider() -> TraceProvider:
    """Get the global trace provider used by tracing utilities."""
    global GLOBAL_TRACE_PROVIDER
    if GLOBAL_TRACE_PROVIDER is None:
        # Lazily initialize defaults on first tracing API usage to avoid
        # import-time side effects while keeping historical call behavior.
        from .processors import default_processor
        from .provider import DefaultTraceProvider

        provider = DefaultTraceProvider()
        GLOBAL_TRACE_PROVIDER = provider
        provider.register_processor(default_processor())
        atexit.register(provider.shutdown)
    return GLOBAL_TRACE_PROVIDER
