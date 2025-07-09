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
    def export(self) -> dict[str, Any] | None:
        """
        Export the trace as a dictionary.
        """
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

    def export(self) -> dict[str, Any] | None:
        return None


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

    def export(self) -> dict[str, Any] | None:
        return {
            "object": "trace",
            "id": self.trace_id,
            "workflow_name": self.name,
            "group_id": self.group_id,
            "metadata": self.metadata,
        }
