from __future__ import annotations

import abc
import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Generic, TypeVar

from typing_extensions import TypedDict

from ..logger import logger
from . import util
from .processor_interface import TracingProcessor
from .scope import Scope
from .span_data import OperationMetadata, SpanData

TSpanData = TypeVar("TSpanData", bound=SpanData)
_SPAN_METADATA_ROUTING_KEYS = ("agent_harness_id",)


class SpanError(TypedDict):
    """Represents an error that occurred during span execution.

    Attributes:
        message: A human-readable error description
        data: Optional dictionary containing additional error context
    """

    message: str
    data: dict[str, Any] | None


def _finish_on_generator_exit(span: Span[Any]) -> None:
    """Finish a span whose ``with`` block is unwinding because of ``GeneratorExit``.

    A generator closed from the task that advanced it resets normally, which is the common
    case. An abandoned async generator is instead finalized from whichever task happens to
    run its ``aclose``, so the body resumes in a context that never set the token and
    ``ContextVar.reset`` raises ``ValueError``. Nothing can be done about that from here:
    a ``Token`` is only valid in the ``Context`` that created it, so the task doing the
    finalizing cannot rewrite the caller's context, and the caller keeps seeing this span
    as current until its own scope ends. Raising would only add a crash on top of that.

    The tolerance is deliberately limited to this path, and within it to the reset itself.
    An explicit ``finish`` from the wrong context is a context-ownership violation rather
    than an unavoidable one, so it still raises, and a processor that fails during
    ``finish`` still surfaces rather than being mistaken for a foreign token.
    """
    span.finish(reset_current=False)

    if isinstance(span, SpanImpl):
        span._exit_processor_scope()

    token: contextvars.Token[Span[Any] | None] | None = span._prev_span_token  # type: ignore[attr-defined]
    if token is None:
        return
    span._prev_span_token = None  # type: ignore[attr-defined]
    try:
        Scope.reset_current_span(token)
    except ValueError:
        logger.debug("Skipping span context reset, token belongs to another context")


class Span(abc.ABC, Generic[TSpanData]):
    """Base class for representing traceable operations with timing and context.

    A span represents a single operation within a trace (e.g., an LLM call, tool execution,
    or agent run). Spans track timing, relationships between operations, and operation-specific
    data.

    Type Args:
        TSpanData: The type of span-specific data this span contains.

    Example:
        ```python
        # Creating a custom span
        with custom_span("database_query", {
            "operation": "SELECT",
            "table": "users"
        }) as span:
            results = await db.query("SELECT * FROM users")
            span.span_data.data["output"] = {"count": len(results)}

        # Handling errors in spans
        with custom_span("risky_operation") as span:
            try:
                result = perform_risky_operation()
            except Exception as e:
                span.set_error({
                    "message": str(e),
                    "data": {"operation": "risky_operation"}
                })
                raise
        ```

        Notes:
        - Spans automatically nest under the current trace
        - Use context managers for reliable start/finish
        - Include relevant data but avoid sensitive information
        - Handle errors properly using set_error()
    """

    @property
    @abc.abstractmethod
    def trace_id(self) -> str:
        """The ID of the trace this span belongs to.

        Returns:
            str: Unique identifier of the parent trace.
        """
        pass

    @property
    @abc.abstractmethod
    def span_id(self) -> str:
        """Unique identifier for this span.

        Returns:
            str: The span's unique ID within its trace.
        """
        pass

    @property
    @abc.abstractmethod
    def span_data(self) -> TSpanData:
        """Operation-specific data for this span.

        Returns:
            TSpanData: Data specific to this type of span (e.g., LLM generation data).
        """
        pass

    @abc.abstractmethod
    def start(self, mark_as_current: bool = False):
        """
        Start the span.

        Args:
            mark_as_current: If true, the span will be marked as the current span.
        """
        pass

    @abc.abstractmethod
    def finish(self, reset_current: bool = False) -> None:
        """
        Finish the span.

        Args:
            reset_current: If true, the span will be reset as the current span.
        """
        pass

    def start_at(self, started_at: str) -> None:
        """Start an external operation at its source ISO 8601 timestamp."""
        self.start()

    def finish_at(self, ended_at: str) -> None:
        """Finish an external operation at its source ISO 8601 timestamp."""
        self.finish()

    def clear_error(self) -> None:
        """Clear a transient error when retrying the same operation."""

    @contextmanager
    def as_current(self) -> Iterator[Span[TSpanData]]:
        """Activate an already-started span without changing its lifetime.

        Use this scope when one source-timed or retried operation spans several execution
        intervals. Start and finish remain the caller's responsibility.
        """
        token = Scope.set_current_span(self)
        processor = getattr(self, "_processor", None)
        callback = getattr(processor, "on_span_scope_enter", None)
        scope_token: object | None = None
        if callback is not None:
            try:
                scope_token = callback(self)
            except Exception:
                logger.warning("Error in trace processor during scope entry")
        generator_exit = False
        try:
            yield self
        except GeneratorExit:
            generator_exit = True
            raise
        finally:
            callback = getattr(processor, "on_span_scope_exit", None)
            if callback is not None and scope_token is not None:
                try:
                    callback(self, scope_token)
                except Exception:
                    logger.warning("Error in trace processor during scope exit")
            try:
                Scope.reset_current_span(token)
            except ValueError:
                if not generator_exit:
                    raise
                logger.debug("Skipping span context reset, token belongs to another context")

    @abc.abstractmethod
    def __enter__(self) -> Span[TSpanData]:
        pass

    @abc.abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    @property
    @abc.abstractmethod
    def parent_id(self) -> str | None:
        """ID of the parent span, if any.

        Returns:
            str | None: The parent span's ID, or None if this is a root span.
        """
        pass

    @abc.abstractmethod
    def set_error(self, error: SpanError) -> None:
        pass

    @property
    @abc.abstractmethod
    def error(self) -> SpanError | None:
        """Any error that occurred during span execution.

        Returns:
            SpanError | None: Error details if an error occurred, None otherwise.
        """
        pass

    @abc.abstractmethod
    def export(self) -> dict[str, Any] | None:
        pass

    @property
    @abc.abstractmethod
    def started_at(self) -> str | None:
        """When the span started execution.

        Returns:
            str | None: ISO format timestamp of span start, None if not started.
        """
        pass

    @property
    @abc.abstractmethod
    def ended_at(self) -> str | None:
        """When the span finished execution.

        Returns:
            str | None: ISO format timestamp of span end, None if not finished.
        """
        pass

    @property
    @abc.abstractmethod
    def tracing_api_key(self) -> str | None:
        """The API key to use when exporting this span."""
        pass

    @property
    def trace_metadata(self) -> dict[str, Any] | None:
        """Trace-level metadata inherited by this span, if available."""
        return None


class NoOpSpan(Span[TSpanData]):
    """A no-op implementation of Span that doesn't record any data.

    Used when tracing is disabled but span operations still need to work.

    Args:
        span_data: The operation-specific data for this span.
    """

    __slots__ = ("_span_data", "_prev_span_token")

    def __init__(self, span_data: TSpanData):
        self._span_data = span_data
        self._prev_span_token: contextvars.Token[Span[TSpanData] | None] | None = None

    @property
    def trace_id(self) -> str:
        return "no-op"

    @property
    def span_id(self) -> str:
        return "no-op"

    @property
    def span_data(self) -> TSpanData:
        return self._span_data

    @property
    def parent_id(self) -> str | None:
        return None

    def start(self, mark_as_current: bool = False):
        if mark_as_current:
            self._prev_span_token = Scope.set_current_span(self)

    def finish(self, reset_current: bool = False) -> None:
        if reset_current and self._prev_span_token is not None:
            Scope.reset_current_span(self._prev_span_token)
            self._prev_span_token = None

    def __enter__(self) -> Span[TSpanData]:
        self.start(mark_as_current=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is GeneratorExit:
            _finish_on_generator_exit(self)
        else:
            self.finish(reset_current=True)

    def set_error(self, error: SpanError) -> None:
        pass

    @property
    def error(self) -> SpanError | None:
        return None

    def export(self) -> dict[str, Any] | None:
        return None

    @property
    def started_at(self) -> str | None:
        return None

    @property
    def ended_at(self) -> str | None:
        return None

    @property
    def tracing_api_key(self) -> str | None:
        return None


class SpanImpl(Span[TSpanData]):
    __slots__ = (
        "_trace_id",
        "_span_id",
        "_parent_id",
        "_started_at",
        "_ended_at",
        "_error",
        "_prev_span_token",
        "_processor",
        "_span_data",
        "_tracing_api_key",
        "_trace_metadata",
        "_processor_scope_token",
    )

    def __init__(
        self,
        trace_id: str,
        span_id: str | None,
        parent_id: str | None,
        processor: TracingProcessor,
        span_data: TSpanData,
        tracing_api_key: str | None,
        trace_metadata: dict[str, Any] | None = None,
    ):
        self._trace_id = trace_id
        self._span_id = span_id or util.gen_span_id()
        self._parent_id = parent_id
        self._started_at: str | None = None
        self._ended_at: str | None = None
        self._processor = processor
        self._error: SpanError | None = None
        self._prev_span_token: contextvars.Token[Span[TSpanData] | None] | None = None
        self._span_data = span_data
        self._tracing_api_key = tracing_api_key
        self._trace_metadata = trace_metadata
        self._processor_scope_token: object | None = None

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def span_id(self) -> str:
        return self._span_id

    @property
    def span_data(self) -> TSpanData:
        return self._span_data

    @property
    def parent_id(self) -> str | None:
        return self._parent_id

    def start(self, mark_as_current: bool = False):
        self._start(util.time_iso(), mark_as_current)

    def start_at(self, started_at: str) -> None:
        self._start(started_at, mark_as_current=False)

    def _start(self, started_at: str, mark_as_current: bool) -> None:
        if self.started_at is not None:
            logger.warning("Span already started")
            return

        self._started_at = started_at
        self._processor.on_span_start(self)
        if mark_as_current:
            self._prev_span_token = Scope.set_current_span(self)
            callback = getattr(self._processor, "on_span_scope_enter", None)
            if callback is not None:
                try:
                    self._processor_scope_token = callback(self)
                except Exception:
                    logger.warning("Error in trace processor during scope entry")

    def finish(self, reset_current: bool = False) -> None:
        self._finish(util.time_iso(), reset_current)

    def finish_at(self, ended_at: str) -> None:
        self._finish(ended_at, reset_current=False)

    def _finish(self, ended_at: str, reset_current: bool) -> None:
        if self.ended_at is not None:
            logger.warning("Span already finished")
            return

        self._ended_at = ended_at
        self._processor.on_span_end(self)
        if reset_current and self._prev_span_token is not None:
            self._exit_processor_scope()
            Scope.reset_current_span(self._prev_span_token)
            self._prev_span_token = None

    def _exit_processor_scope(self) -> None:
        token = self._processor_scope_token
        self._processor_scope_token = None
        if token is not None:
            callback = getattr(self._processor, "on_span_scope_exit", None)
            if callback is not None:
                try:
                    callback(self, token)
                except Exception:
                    logger.warning("Error in trace processor during scope exit")

    def __enter__(self) -> Span[TSpanData]:
        self.start(mark_as_current=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is GeneratorExit:
            _finish_on_generator_exit(self)
        else:
            self.finish(reset_current=True)

    def set_error(self, error: SpanError) -> None:
        self._error = error

    def clear_error(self) -> None:
        self._error = None

    @property
    def error(self) -> SpanError | None:
        return self._error

    @property
    def started_at(self) -> str | None:
        return self._started_at

    @property
    def ended_at(self) -> str | None:
        return self._ended_at

    @property
    def tracing_api_key(self) -> str | None:
        return self._tracing_api_key

    @property
    def trace_metadata(self) -> dict[str, Any] | None:
        return self._trace_metadata

    def export(self) -> dict[str, Any] | None:
        payload = {
            "object": "trace.span",
            "id": self.span_id,
            "trace_id": self.trace_id,
            "parent_id": self._parent_id,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "span_data": self.span_data.export(),
            "error": self._error,
        }
        metadata: dict[str, Any] = {}
        if self._trace_metadata is not None:
            metadata.update(
                {
                    key: self._trace_metadata[key]
                    for key in _SPAN_METADATA_ROUTING_KEYS
                    if key in self._trace_metadata
                }
            )
        span_data_metadata = getattr(self.span_data, "metadata", None)
        if isinstance(span_data_metadata, dict):
            metadata.update(
                {key: value for key, value in span_data_metadata.items() if key not in metadata}
            )
        operation = getattr(self.span_data, "operation", None)
        if isinstance(operation, OperationMetadata) and operation.detail is not None:
            metadata["operation"] = operation.export()
        if metadata:
            payload["metadata"] = metadata
        return payload
