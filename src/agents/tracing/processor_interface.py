import abc
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .spans import Span
    from .traces import Trace


class TracingProcessor(abc.ABC):
    """Interface for processing spans."""

    @abc.abstractmethod
    def on_trace_start(self, trace: "Trace") -> None:
        """Called when a trace is started.

        Args:
            trace: The trace that started.
        """
        pass

    @abc.abstractmethod
    def on_trace_end(self, trace: "Trace") -> None:
        """Called when a trace is finished.

        Args:
            trace: The trace that finished.
        """
        pass

    @abc.abstractmethod
    def on_span_start(self, span: "Span[Any]") -> None:
        """Called when a span is started.

        Args:
            span: The span that started.
        """
        pass

    @abc.abstractmethod
    def on_span_end(self, span: "Span[Any]") -> None:
        """Called when a span is finished. Should not block or raise exceptions.

        Args:
            span: The span that finished.
        """
        pass

    @abc.abstractmethod
    def shutdown(self) -> None:
        """Called when the application stops."""
        pass

    @abc.abstractmethod
    def force_flush(self) -> None:
        """Forces an immediate flush of all queued spans/traces."""
        pass


class TracingExporter(abc.ABC):
    """Exports traces and spans. For example, could log them or send them to a backend."""

    @abc.abstractmethod
    def export(self, items: list["Trace | Span[Any]"]) -> None:
        """Exports a list of traces and spans.

        Args:
            items: The items to export.
        """
        pass
