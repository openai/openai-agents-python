from __future__ import annotations

import atexit
import threading
from typing import TYPE_CHECKING

from ..logger import logger

if TYPE_CHECKING:
    from .provider import TraceProvider

_DEFAULT_SHUTDOWN_TIMEOUT = 5.0
GLOBAL_TRACE_PROVIDER: TraceProvider | None = None
_GLOBAL_TRACE_PROVIDER_LOCK = threading.Lock()
_SHUTDOWN_HANDLER_REGISTERED = False


def _shutdown_global_trace_provider() -> None:
    provider = GLOBAL_TRACE_PROVIDER
    if provider is not None:
        from .provider import DefaultTraceProvider

        if isinstance(provider, DefaultTraceProvider):
            provider.shutdown(timeout=_DEFAULT_SHUTDOWN_TIMEOUT)
            return
        provider.shutdown()


def set_trace_provider(provider: TraceProvider) -> None:
    """Set the global trace provider used by tracing utilities.

    If a provider is already set and is being replaced, the previous provider
    is shut down so any background threads, network clients, or other resources
    held by its processors are released.
    """
    global GLOBAL_TRACE_PROVIDER
    global _SHUTDOWN_HANDLER_REGISTERED

    with _GLOBAL_TRACE_PROVIDER_LOCK:
        previous = GLOBAL_TRACE_PROVIDER
        GLOBAL_TRACE_PROVIDER = provider
        if not _SHUTDOWN_HANDLER_REGISTERED:
            atexit.register(_shutdown_global_trace_provider)
            _SHUTDOWN_HANDLER_REGISTERED = True

        # Shut down inside the lock so a concurrent `set_trace_provider(previous)`
        # cannot reinstall `previous` between releasing the lock and the shutdown
        # call, which would close the processors of the now-active provider.
        if previous is not None and previous is not provider:
            try:
                previous.shutdown()
            except Exception as exc:
                logger.error(f"Error shutting down previous trace provider: {exc}")


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
