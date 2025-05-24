from __future__ import annotations

import abc
import contextvars
from typing import Any

from ..logger import logger
from . import util
from .processor_interface import TracingProcessor
from .scope import Scope


class Trace:
    """
    A trace is the root level object that tracing creates. It represents a logical "workflow".
    """

    @abc.abstractmethod
    def __enter__(self) -> Trace:
        pass

    @abc.abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    @abc.abstractmethod
    def start(self, mark_as_current: bool = False):
        """
        Start the trace.

        Args:
            mark_as_current: If true, the trace will be marked as the current trace.
        """
        pass

    @abc.abstractmethod
    def finish(self, reset_current: bool = False):
        """
        Finish the trace.

        Args:
            reset_current: If true, the trace will be reset as the current trace.
        """
        pass

    @property
    @abc.abstractmethod
    def trace_id(self) -> str:
        """
        The trace ID.
        """
        pass

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """
        The name of the workflow being traced.
        """
        pass

    @abc.abstractmethod
    def to_dict(self) -> dict[str, Any] | None: # Renamed from export
        """
        Export the trace as a dictionary.
        """
        pass

    # Add properties that are expected by to_dict, assuming they'll be populated
    # by the tracing system before to_dict() is called on a completed trace.
    @property
    @abc.abstractmethod
    def start_time(self) -> Any: # Should be datetime, but TraceImpl doesn't have it yet
        pass

    @property
    @abc.abstractmethod
    def end_time(self) -> Any: # Should be datetime
        pass

    @property
    @abc.abstractmethod
    def status(self) -> Any: # Should be an Enum-like object with a .value
        pass

    @property
    @abc.abstractmethod
    def spans(self) -> list[Any]: # Should be list[Span]
        pass

    @property
    @abc.abstractmethod
    def usage(self) -> Any: # Should be an instance of Usage
        pass


class NoOpTrace(Trace):
    """
    A no-op trace that will not be recorded.
    """

    def __init__(self):
        self._started = False
        self._prev_context_token: contextvars.Token[Trace | None] | None = None

    def __enter__(self) -> Trace:
        if self._started:
            if not self._prev_context_token:
                logger.error("Trace already started but no context token set")
            return self

        self._started = True
        self.start(mark_as_current=True)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.finish(reset_current=True)

    def start(self, mark_as_current: bool = False):
        if mark_as_current:
            self._prev_context_token = Scope.set_current_trace(self)

    def finish(self, reset_current: bool = False):
        if reset_current and self._prev_context_token is not None:
            Scope.reset_current_trace(self._prev_context_token)
            self._prev_context_token = None

    @property
    def trace_id(self) -> str:
        return "no-op"

    @property
    def name(self) -> str:
        return "no-op"

    def to_dict(self) -> dict[str, Any] | None: # Renamed from export
        return None

    @property
    def start_time(self) -> Any: return None
    @property
    def end_time(self) -> Any: return None
    @property
    def status(self) -> Any: return None # Or a default status object
    @property
    def spans(self) -> list[Any]: return []
    @property
    def usage(self) -> Any: return None


NO_OP_TRACE = NoOpTrace()


class TraceImpl(Trace):
    """
    A trace that will be recorded by the tracing library.
    """

    __slots__ = (
        "_name",
        "_trace_id",
        "group_id",
        "metadata",
        "_prev_context_token",
        "_processor",
        "_started",
    )

    def __init__(
        self,
        name: str,
        trace_id: str | None,
        group_id: str | None,
        metadata: dict[str, Any] | None,
        processor: TracingProcessor,
    ):
        self._name = name
        self._trace_id = trace_id or util.gen_trace_id()
        self.group_id = group_id
        self.metadata = metadata
        self._prev_context_token: contextvars.Token[Trace | None] | None = None
        self._processor = processor
        self._started = False

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def name(self) -> str:
        return self._name

    def start(self, mark_as_current: bool = False):
        if self._started:
            return

        self._started = True
        self._processor.on_trace_start(self)

        if mark_as_current:
            self._prev_context_token = Scope.set_current_trace(self)

    def finish(self, reset_current: bool = False):
        if not self._started:
            return

        self._processor.on_trace_end(self)

        if reset_current and self._prev_context_token is not None:
            Scope.reset_current_trace(self._prev_context_token)
            self._prev_context_token = None

    def __enter__(self) -> Trace:
        if self._started:
            if not self._prev_context_token:
                logger.error("Trace already started but no context token set")
            return self

        self.start(mark_as_current=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.finish(reset_current=exc_type is not GeneratorExit)

    # These attributes will need to be set by the tracing system upon trace completion.
    # Initialize them to None or default values.
    _start_time_val: Any = None
    _end_time_val: Any = None
    _status_val: Any = None # e.g., an enum or string
    _spans_val: list[Any] = []
    _usage_val: Any = None


    @property
    def start_time(self) -> Any: return self._start_time_val
    @start_time.setter
    def start_time(self, value: Any): self._start_time_val = value

    @property
    def end_time(self) -> Any: return self._end_time_val
    @end_time.setter
    def end_time(self, value: Any): self._end_time_val = value

    @property
    def status(self) -> Any: return self._status_val
    @status.setter
    def status(self, value: Any): self._status_val = value
    
    @property
    def spans(self) -> list[Any]: return self._spans_val
    @spans.setter
    def spans(self, value: list[Any]): self._spans_val = value

    @property
    def usage(self) -> Any: return self._usage_val
    @usage.setter
    def usage(self, value: Any): self._usage_val = value


    def to_dict(self) -> dict[str, Any] | None: # Renamed from export
        # Ensure datetime objects are converted to ISO format strings
        start_time_iso = None
        if hasattr(self.start_time, 'isoformat'):
            start_time_iso = self.start_time.isoformat()
        elif isinstance(self.start_time, str): # If already a string
            start_time_iso = self.start_time

        end_time_iso = None
        if hasattr(self.end_time, 'isoformat'):
            end_time_iso = self.end_time.isoformat()
        elif isinstance(self.end_time, str): # If already a string
            end_time_iso = self.end_time
            
        status_value = None
        if hasattr(self.status, 'value'): # For enums
            status_value = self.status.value
        elif self.status is not None: # For strings or other direct values
            status_value = self.status

        spans_dict_list = []
        if self.spans: # self.spans should be a list of SpanImpl objects
            spans_dict_list = [span.to_dict() for span in self.spans if hasattr(span, 'to_dict')]


        usage_data = None
        if hasattr(self.usage, 'to_dict'):
            usage_data = self.usage.to_dict()
        elif self.usage: # Fallback for simple dataclasses or dicts
            try:
                usage_data = vars(self.usage)
            except TypeError: # vars() argument must have __dict__ attribute
                 usage_data = str(self.usage) # Or some other representation


        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "group_id": self.group_id,
            "start_time": start_time_iso,
            "end_time": end_time_iso,
            "status": status_value,
            "metadata": self.metadata,
            "spans": spans_dict_list,
            "usage": usage_data
        }
