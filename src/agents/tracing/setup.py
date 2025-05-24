from __future__ import annotations

import os
import threading
from typing import Any

from ..logger import logger
from . import util
from .processor_interface import TracingProcessor
from .scope import Scope
from .spans import NoOpSpan, Span, SpanImpl, TSpanData
from .traces import NoOpTrace, Trace, TraceImpl
from typing import Dict
from pathlib import Path # Added for Path
import json # Added for json


class SynchronousMultiTracingProcessor(TracingProcessor):
    """
    Forwards all calls to a list of TracingProcessors, in order of registration.
    """

    def __init__(self):
        # Use a tuple to avoid race conditions when iterating over processors.
        self._processors: tuple[TracingProcessor, ...] = ()
        self._lock = threading.Lock()

    def add_tracing_processor(self, tracing_processor: TracingProcessor):
        """
        Add a processor to the list of processors. Each processor will receive all traces/spans.
        """
        with self._lock:
            self._processors += (tracing_processor,)

    def set_processors(self, processors: list[TracingProcessor]):
        """
        Set the list of processors. This will replace the current list of processors.
        """
        with self._lock:
            self._processors = tuple(processors)

    def on_trace_start(self, trace: Trace) -> None:
        """
        Called when a trace is started.
        """
        for processor in self._processors:
            processor.on_trace_start(trace)

    def on_trace_end(self, trace: Trace) -> None:
        """
        Called when a trace is finished.
        """
        for processor in self._processors:
            processor.on_trace_end(trace)

    def on_span_start(self, span: Span[Any]) -> None:
        """
        Called when a span is started.
        """
        for processor in self._processors:
            processor.on_span_start(span)

    def on_span_end(self, span: Span[Any]) -> None:
        """
        Called when a span is finished.
        """
        for processor in self._processors:
            processor.on_span_end(span)

    def shutdown(self) -> None:
        """
        Called when the application stops.
        """
        for processor in self._processors:
            logger.debug(f"Shutting down trace processor {processor}")
            processor.shutdown()

    def force_flush(self):
        """
        Force the processors to flush their buffers.
        """
        for processor in self._processors:
            processor.force_flush()


class TraceProvider:
    def __init__(self, trace_storage_dir: str = "./trace_data"): # Added trace_storage_dir
        self._multi_processor = SynchronousMultiTracingProcessor()
        # Replace MemoryTraceCollector with FileTraceCollector
        self.file_collector = FileTraceCollector(storage_dir=trace_storage_dir)
        self._multi_processor.add_tracing_processor(self.file_collector)
        self._disabled = os.environ.get("OPENAI_AGENTS_DISABLE_TRACING", "false").lower() in (
            "true",
            "1",
        )

    def register_processor(self, processor: TracingProcessor):
        """
        Add a user-defined processor to the list of processors.
        The internal FileTraceCollector will always remain active.
        """
        # The FileTraceCollector is already added and will receive all events.
        # This method is for external processors.
        self._multi_processor.add_tracing_processor(processor)

    def set_processors(self, processors: list[TracingProcessor]):
        """
        Set the list of user-defined processors. This will replace the current list of
        user-defined processors. The internal FileTraceCollector will remain active.
        """
        # Ensure file_collector is always present
        all_processors = [self.file_collector] + processors
        self._multi_processor.set_processors(all_processors)

    # get_collected_traces is removed as traces are now on disk.
    # The Flask app will need to read from the directory.

    def get_current_trace(self) -> Trace | None:
        """
        Set the list of processors. This will replace the current list of processors.
        """
        self._multi_processor.set_processors(processors)

    def get_current_trace(self) -> Trace | None:
        """
        Returns the currently active trace, if any.
        """
        return Scope.get_current_trace()

    def get_current_span(self) -> Span[Any] | None:
        """
        Returns the currently active span, if any.
        """
        return Scope.get_current_span()

    def set_disabled(self, disabled: bool) -> None:
        """
        Set whether tracing is disabled.
        """
        self._disabled = disabled

    def create_trace(
        self,
        name: str,
        trace_id: str | None = None,
        group_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        disabled: bool = False,
    ) -> Trace:
        """
        Create a new trace.
        """
        if self._disabled or disabled:
            logger.debug(f"Tracing is disabled. Not creating trace {name}")
            return NoOpTrace()

        trace_id = trace_id or util.gen_trace_id()

        logger.debug(f"Creating trace {name} with id {trace_id}")

        return TraceImpl(
            name=name,
            trace_id=trace_id,
            group_id=group_id,
            metadata=metadata,
            processor=self._multi_processor,
        )

    def create_span(
        self,
        span_data: TSpanData,
        span_id: str | None = None,
        parent: Trace | Span[Any] | None = None,
        disabled: bool = False,
    ) -> Span[TSpanData]:
        """
        Create a new span.
        """
        if self._disabled or disabled:
            logger.debug(f"Tracing is disabled. Not creating span {span_data}")
            return NoOpSpan(span_data)

        if not parent:
            current_span = Scope.get_current_span()
            current_trace = Scope.get_current_trace()
            if current_trace is None:
                logger.error(
                    "No active trace. Make sure to start a trace with `trace()` first"
                    "Returning NoOpSpan."
                )
                return NoOpSpan(span_data)
            elif isinstance(current_trace, NoOpTrace) or isinstance(current_span, NoOpSpan):
                logger.debug(
                    f"Parent {current_span} or {current_trace} is no-op, returning NoOpSpan"
                )
                return NoOpSpan(span_data)

            parent_id = current_span.span_id if current_span else None
            trace_id = current_trace.trace_id

        elif isinstance(parent, Trace):
            if isinstance(parent, NoOpTrace):
                logger.debug(f"Parent {parent} is no-op, returning NoOpSpan")
                return NoOpSpan(span_data)
            trace_id = parent.trace_id
            parent_id = None
        elif isinstance(parent, Span):
            if isinstance(parent, NoOpSpan):
                logger.debug(f"Parent {parent} is no-op, returning NoOpSpan")
                return NoOpSpan(span_data)
            parent_id = parent.span_id
            trace_id = parent.trace_id

        logger.debug(f"Creating span {span_data} with id {span_id}")

        return SpanImpl(
            trace_id=trace_id,
            span_id=span_id,
            parent_id=parent_id,
            processor=self._multi_processor,
            span_data=span_data,
        )

    def shutdown(self) -> None:
        if self._disabled:
            return

        try:
            logger.debug("Shutting down trace provider")
            self._multi_processor.shutdown()
        except Exception as e:
            logger.error(f"Error shutting down trace provider: {e}")


# Define FileTraceCollector class here (replaces MemoryTraceCollector)
class FileTraceCollector(TracingProcessor):
    """Collects completed traces and saves them to JSON files."""

    def __init__(self, storage_dir: str = "./trace_data"):
        self.storage_dir = Path(storage_dir)
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"FileTraceCollector initialized. Storage directory: {self.storage_dir.resolve()}")
        except OSError as e:
            logger.error(f"Error creating storage directory {self.storage_dir.resolve()}: {e}")
            # Potentially re-raise or handle more gracefully depending on desired behavior
            raise

    def on_trace_start(self, trace: Trace) -> None:
        """Called when a trace is started."""
        # logger.debug(f"FileTraceCollector: Trace started - {trace.trace_id}")
        pass  # No-op, we only care about completed traces

    def on_trace_end(self, trace: Trace) -> None:
        """Called when a trace is finished. Serializes and saves the trace."""
        if not hasattr(trace, 'to_dict') or not callable(trace.to_dict):
            logger.error(f"FileTraceCollector: Trace object for trace_id {trace.trace_id} does not have a to_dict method. Cannot save.")
            return

        try:
            trace_dict = trace.to_dict()
            if trace_dict is None: # Should not happen if to_dict is implemented correctly for non-NoOp traces
                logger.warning(f"FileTraceCollector: to_dict() returned None for trace_id {trace.trace_id}. Skipping save.")
                return

            file_path = self.storage_dir / f"{trace.trace_id}.json"
            logger.debug(f"FileTraceCollector: Trace ended - {trace.trace_id}. Saving to {file_path.resolve()}")
            
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(trace_dict, f, indent=2)
            logger.debug(f"FileTraceCollector: Successfully saved trace {trace.trace_id} to {file_path.resolve()}")

        except Exception as e:
            # Catching a broad exception to ensure the collector doesn't crash the application
            logger.error(f"FileTraceCollector: Error saving trace {trace.trace_id} to file: {e}", exc_info=True)


    def on_span_start(self, span: Span[Any]) -> None:
        """Called when a span is started."""
        # logger.debug(f"FileTraceCollector: Span started - {span.span_id} in trace {span.trace_id}")
        pass  # No-op, span data is part of the Trace object when trace.to_dict() is called

    def on_span_end(self, span: Span[Any]) -> None:
        """Called when a span is finished."""
        # logger.debug(f"FileTraceCollector: Span ended - {span.span_id} in trace {span.trace_id}")
        pass  # No-op

    def shutdown(self) -> None:
        """Called when the application stops."""
        logger.debug(f"FileTraceCollector: Shutdown called. Traces are saved individually upon completion.")
        pass

    def force_flush(self) -> None:
        """Force the processor to flush its buffers."""
        # logger.debug("FileTraceCollector: Force flush called.")
        pass # No-op, as storage is synchronous in on_trace_end


GLOBAL_TRACE_PROVIDER = TraceProvider()
