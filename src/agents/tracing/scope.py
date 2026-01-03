# Holds the current active span
import contextvars
from typing import TYPE_CHECKING, Any

from ..logger import logger

if TYPE_CHECKING:
    from .spans import Span
    from .traces import Trace

_current_span: contextvars.ContextVar["Span[Any] | None"] = contextvars.ContextVar(
    "current_span", default=None
)

_current_trace: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar(
    "current_trace", default=None
)


class Scope:
    """
    Manages the current span and trace in the context.
    """

    @classmethod
    def get_current_span(cls) -> "Span[Any] | None":
        return _current_span.get()

    @classmethod
    def set_current_span(cls, span: "Span[Any] | None") -> "contextvars.Token[Span[Any] | None]":
        return _current_span.set(span)

    @classmethod
    def reset_current_span(
        cls,
        token: "contextvars.Token[Span[Any] | None]",
        prev_span: "Span[Any] | None" = None,
    ) -> None:
        try:
            _current_span.reset(token)
        except ValueError:
            # Token was created in a different Context. This can happen when multiple
            # Runner.run() calls execute concurrently via asyncio.gather().
            # Fall back to setting the previous value directly.
            # See: https://github.com/openai/openai-agents-python/issues/2246
            logger.warning(
                "Tracing context mismatch detected during concurrent execution. "
                "Span context was reset using fallback. This may affect trace hierarchy "
                "in concurrent scenarios. Consider using asyncio.create_task() for concurrent "
                "Runner.run() calls to ensure proper context isolation."
            )
            _current_span.set(prev_span)

    @classmethod
    def get_current_trace(cls) -> "Trace | None":
        return _current_trace.get()

    @classmethod
    def set_current_trace(cls, trace: "Trace | None") -> "contextvars.Token[Trace | None]":
        logger.debug(f"Setting current trace: {trace.trace_id if trace else None}")
        return _current_trace.set(trace)

    @classmethod
    def reset_current_trace(
        cls,
        token: "contextvars.Token[Trace | None]",
        prev_trace: "Trace | None" = None,
    ) -> None:
        """Reset the current trace to its previous value.

        Uses token-based reset when possible, with fallback to direct set for
        concurrent execution scenarios where Context objects may differ.
        See: https://github.com/openai/openai-agents-python/issues/2246
        """
        logger.debug("Resetting current trace")
        try:
            _current_trace.reset(token)
        except ValueError:
            # Token was created in a different Context. This can happen when multiple
            # Runner.run() calls execute concurrently via asyncio.gather().
            # Fall back to setting the previous value directly.
            # See: https://github.com/openai/openai-agents-python/issues/2246
            logger.warning(
                "Tracing context mismatch detected during concurrent execution. "
                "Trace context was reset using fallback. This may affect trace hierarchy "
                "in concurrent scenarios. Consider using asyncio.create_task() for concurrent "
                "Runner.run() calls to ensure proper context isolation."
            )
            _current_trace.set(prev_trace)
