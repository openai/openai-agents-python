from __future__ import annotations

import atexit
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .provider import TraceProvider

GLOBAL_TRACE_PROVIDER: TraceProvider | None = None
_GLOBAL_TRACE_PROVIDER_LOCK = threading.Lock()
_SHUTDOWN_HANDLER_REGISTERED = False


def _shutdown_global_trace_provider() -> None:
    provider = GLOBAL_TRACE_PROVIDER
    if provider is not None:
        provider.shutdown()


def set_trace_provider(provider: TraceProvider) -> None:
    """Set the global trace provider used by tracing utilities."""
    global GLOBAL_TRACE_PROVIDER
    global _SHUTDOWN_HANDLER_REGISTERED

    with _GLOBAL_TRACE_PROVIDER_LOCK:
        GLOBAL_TRACE_PROVIDER = provider
        if not _SHUTDOWN_HANDLER_REGISTERED:
            atexit.register(_shutdown_global_trace_provider)
            _SHUTDOWN_HANDLER_REGISTERED = True


def get_trace_provider() -> TraceProvider:
    """Get the global trace provider used by tracing utilities.

    The default provider and processor are initialized lazily on first access so
    importing the SDK does not create network clients or threading primitives.
    """
    global GLOBAL_TRACE_PROVIDER
    global _SHUTDOWN_HANDLER_REGISTERED

    provider = GLOBAL_TRACE_PROVIDER
    if provider is not None:
        return provider

    with _GLOBAL_TRACE_PROVIDER_LOCK:
        provider = GLOBAL_TRACE_PROVIDER
        if provider is None:
            from .processors import default_processor
            from .provider import DefaultTraceProvider

            provider = DefaultTraceProvider()
            provider.register_processor(default_processor())
            GLOBAL_TRACE_PROVIDER = provider

        if not _SHUTDOWN_HANDLER_REGISTERED:
            atexit.register(_shutdown_global_trace_provider)
            _SHUTDOWN_HANDLER_REGISTERED = True

    return provider
