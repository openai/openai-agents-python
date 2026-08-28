from __future__ import annotations

import atexit
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .processor_interface import TracingProcessor
    from .provider import TraceProvider

_DEFAULT_SHUTDOWN_TIMEOUT = 5.0
GLOBAL_TRACE_PROVIDER: TraceProvider | None = None
_GLOBAL_TRACE_PROVIDER_LOCK = threading.Lock()
_SHUTDOWN_HANDLER_REGISTERED = False
# The default processor wired into a provider we bootstrapped ourselves, kept so a
# terminal shutdown can be spotted and re-initialized. None whenever the provider came
# from `set_trace_provider`: that lifecycle belongs to whoever set it.
_DEFAULT_PROCESSOR: TracingProcessor | None = None
# Set before the atexit shutdown runs, so a span closed during interpreter teardown
# cannot resurrect the stack and build a fresh HTTP client on the way out.
_ATEXIT_SHUTDOWN_STARTED = False


def _default_stack_is_terminal() -> bool:
    """Whether the bootstrapped provider's default processor has been shut down.

    Shutdown is terminal for the whole default stack -- provider, processor and exporter
    alike -- so the next use re-initializes instead of feeding spans to a processor that
    will never drain them again, or applying a freshly configured API key to an exporter
    nothing exports through.
    """
    if _ATEXIT_SHUTDOWN_STARTED:
        return False
    return getattr(_DEFAULT_PROCESSOR, "is_shut_down", False) is True


def _shutdown_global_trace_provider() -> None:
    global _ATEXIT_SHUTDOWN_STARTED

    _ATEXIT_SHUTDOWN_STARTED = True
    provider = GLOBAL_TRACE_PROVIDER
    if provider is not None:
        from .provider import DefaultTraceProvider

        if isinstance(provider, DefaultTraceProvider):
            provider.shutdown(timeout=_DEFAULT_SHUTDOWN_TIMEOUT)
            return
        provider.shutdown()


def set_trace_provider(provider: TraceProvider) -> None:
    """Set the global trace provider used by tracing utilities."""
    global GLOBAL_TRACE_PROVIDER
    global _SHUTDOWN_HANDLER_REGISTERED
    global _DEFAULT_PROCESSOR

    with _GLOBAL_TRACE_PROVIDER_LOCK:
        GLOBAL_TRACE_PROVIDER = provider
        _DEFAULT_PROCESSOR = None
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
    global _DEFAULT_PROCESSOR

    provider = GLOBAL_TRACE_PROVIDER
    if provider is not None and not _default_stack_is_terminal():
        return provider

    with _GLOBAL_TRACE_PROVIDER_LOCK:
        provider = GLOBAL_TRACE_PROVIDER
        if provider is None or _default_stack_is_terminal():
            from .processors import default_processor
            from .provider import DefaultTraceProvider

            processor = default_processor()
            provider = DefaultTraceProvider()
            provider.register_processor(processor)
            GLOBAL_TRACE_PROVIDER = provider
            _DEFAULT_PROCESSOR = processor

        if not _SHUTDOWN_HANDLER_REGISTERED:
            atexit.register(_shutdown_global_trace_provider)
            _SHUTDOWN_HANDLER_REGISTERED = True

    return provider
