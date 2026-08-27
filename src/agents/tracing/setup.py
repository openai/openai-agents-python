from __future__ import annotations

import atexit
import threading
from typing import TYPE_CHECKING

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


def _reset_default_trace_provider_if_current(provider: TraceProvider) -> None:
    """Release the default stack when the registered default provider shuts down.

    Clears ``GLOBAL_TRACE_PROVIDER`` together with the module-owned default processor/exporter so a
    later trace rebuilds a fresh stack instead of reusing one whose exporter is already closed.
    Only resets when ``provider`` is the current global provider, so a caller's own provider is
    left untouched.
    """
    global GLOBAL_TRACE_PROVIDER

    from .processors import _close_default_exporter, _detach_default_processor

    exporter = None
    with _GLOBAL_TRACE_PROVIDER_LOCK:
        if GLOBAL_TRACE_PROVIDER is not provider:
            return
        # Clear the module singletons *before* publishing ``GLOBAL_TRACE_PROVIDER = None``, in the
        # same critical section. Otherwise a thread calling ``get_trace_provider()`` in the gap
        # would see no global provider and rebuild one from the stale processor singleton, adopting
        # the just-shut-down processor/closed exporter. ``_detach_default_processor`` acquires the
        # processor lock nested inside this one, matching the ``get_trace_provider`` lock order.
        exporter = _detach_default_processor()
        GLOBAL_TRACE_PROVIDER = None

    # Close outside both locks; ``close`` is idempotent and may do I/O.
    _close_default_exporter(exporter)


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
