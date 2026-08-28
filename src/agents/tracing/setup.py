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
_DEFAULT_PROCESSOR: TracingProcessor | None = None
_SDK_DEFAULT_PROVIDER: TraceProvider | None = None
_SDK_DEFAULT_PROCESSOR: TracingProcessor | None = None
_ATEXIT_SHUTDOWN_STARTED = False


def _default_processor_is_terminal() -> bool:
    """Return whether the SDK-owned processor needs supported-use recovery."""
    if _ATEXIT_SHUTDOWN_STARTED:
        return False
    return getattr(_DEFAULT_PROCESSOR, "is_shut_down", False) is True


def _recover_default_processor(provider: TraceProvider) -> None:
    """Replace only the SDK-owned terminal processor in the existing provider."""
    global _DEFAULT_PROCESSOR
    global _SDK_DEFAULT_PROCESSOR

    from .processors import default_processor
    from .provider import DefaultTraceProvider

    stale = _DEFAULT_PROCESSOR
    fresh = default_processor()
    if fresh is stale:
        return
    if stale is not None and isinstance(provider, DefaultTraceProvider):
        provider._replace_processor(stale, fresh)
    _DEFAULT_PROCESSOR = fresh
    _SDK_DEFAULT_PROCESSOR = fresh


def _shutdown_global_trace_provider() -> None:
    global _ATEXIT_SHUTDOWN_STARTED

    from .processors import _begin_default_stack_atexit_shutdown

    with _GLOBAL_TRACE_PROVIDER_LOCK:
        _ATEXIT_SHUTDOWN_STARTED = True
        _begin_default_stack_atexit_shutdown()
        provider = GLOBAL_TRACE_PROVIDER
    try:
        if provider is not None:
            from .provider import DefaultTraceProvider

            if isinstance(provider, DefaultTraceProvider):
                provider.shutdown(timeout=_DEFAULT_SHUTDOWN_TIMEOUT)
            else:
                provider.shutdown()
    finally:
        from .processors import (
            _close_unattached_default_exporter,
            _wait_for_retiring_default_processors,
        )

        _wait_for_retiring_default_processors(_DEFAULT_SHUTDOWN_TIMEOUT)
        _close_unattached_default_exporter()


def set_trace_provider(provider: TraceProvider) -> None:
    """Set the global trace provider used by tracing utilities."""
    global GLOBAL_TRACE_PROVIDER
    global _SHUTDOWN_HANDLER_REGISTERED
    global _DEFAULT_PROCESSOR
    global _SDK_DEFAULT_PROVIDER
    global _SDK_DEFAULT_PROCESSOR

    retired_default: TracingProcessor | None = None
    with _GLOBAL_TRACE_PROVIDER_LOCK:
        if provider is not GLOBAL_TRACE_PROVIDER:
            retired_default = _DEFAULT_PROCESSOR
            if retired_default is not None:
                from .processors import _detach_owned_default_processor

                _detach_owned_default_processor(retired_default)
        GLOBAL_TRACE_PROVIDER = provider
        if retired_default is not None:
            _DEFAULT_PROCESSOR = None
        if provider is _SDK_DEFAULT_PROVIDER:
            _DEFAULT_PROCESSOR = _SDK_DEFAULT_PROCESSOR
        if not _SHUTDOWN_HANDLER_REGISTERED:
            atexit.register(_shutdown_global_trace_provider)
            _SHUTDOWN_HANDLER_REGISTERED = True

    if retired_default is not None:
        from .processors import _retire_owned_default_processor

        _retire_owned_default_processor(retired_default)


def get_trace_provider() -> TraceProvider:
    """Get the global trace provider used by tracing utilities.

    The default provider and processor are initialized lazily on first access so
    importing the SDK does not create network clients or threading primitives.
    """
    global GLOBAL_TRACE_PROVIDER
    global _SHUTDOWN_HANDLER_REGISTERED
    global _DEFAULT_PROCESSOR
    global _SDK_DEFAULT_PROVIDER
    global _SDK_DEFAULT_PROCESSOR

    provider = GLOBAL_TRACE_PROVIDER
    if provider is not None and not _default_processor_is_terminal():
        return provider

    with _GLOBAL_TRACE_PROVIDER_LOCK:
        provider = GLOBAL_TRACE_PROVIDER
        if provider is None:
            from .provider import DefaultTraceProvider

            provider = DefaultTraceProvider()
            GLOBAL_TRACE_PROVIDER = provider
            if not _ATEXIT_SHUTDOWN_STARTED:
                from .processors import default_processor

                processor = default_processor()
                provider.register_processor(processor)
                _DEFAULT_PROCESSOR = processor
                _SDK_DEFAULT_PROVIDER = provider
                _SDK_DEFAULT_PROCESSOR = processor
        elif _default_processor_is_terminal():
            _recover_default_processor(provider)

        if not _SHUTDOWN_HANDLER_REGISTERED:
            atexit.register(_shutdown_global_trace_provider)
            _SHUTDOWN_HANDLER_REGISTERED = True

    return provider


def replace_trace_processors(processors: list[TracingProcessor]) -> None:
    """Replace processors and retire a removed SDK-owned default processor."""
    get_trace_provider().set_processors(processors)


def _contains_processor_identity(
    processors: list[TracingProcessor], target: TracingProcessor
) -> bool:
    """Return whether the exact SDK-owned processor remains registered."""
    return any(processor is target for processor in processors)


def replace_trace_processors_for_provider(
    provider: TraceProvider, processors: list[TracingProcessor]
) -> bool:
    """Handle direct replacement on the currently registered SDK default provider."""
    global _DEFAULT_PROCESSOR
    global _SDK_DEFAULT_PROCESSOR

    with _GLOBAL_TRACE_PROVIDER_LOCK:
        from .provider import DefaultTraceProvider

        if not isinstance(provider, DefaultTraceProvider):
            return False
        if provider is not GLOBAL_TRACE_PROVIDER:
            if (
                provider is _SDK_DEFAULT_PROVIDER
                and _SDK_DEFAULT_PROCESSOR is not None
                and not _contains_processor_identity(processors, _SDK_DEFAULT_PROCESSOR)
            ):
                _SDK_DEFAULT_PROCESSOR = None
            return False
        if _DEFAULT_PROCESSOR is None:
            return False
        provider._set_processors(processors)
        default_processor = _DEFAULT_PROCESSOR
        default_removed = default_processor is not None and not _contains_processor_identity(
            processors, default_processor
        )
        if default_removed:
            from .processors import _detach_owned_default_processor

            _detach_owned_default_processor(default_processor)
            _DEFAULT_PROCESSOR = None
            _SDK_DEFAULT_PROCESSOR = None

    if default_removed:
        from .processors import _retire_owned_default_processor

        _retire_owned_default_processor(default_processor)
    return True
