"""Workflow orchestration for multi-agent systems.

This module provides a declarative way to define and execute complex workflows
involving multiple agents with different connection patterns.
"""

from .connections import (
    ConditionalConnection,
    Connection,
    HandoffConnection,
    ParallelConnection,
    SequentialConnection,
    ToolConnection,
)
from .workflow import Workflow, WorkflowResult

__all__ = [
    "Connection",
    "ConditionalConnection",
    "HandoffConnection",
    "ParallelConnection",
    "SequentialConnection",
    "ToolConnection",
    "Workflow",
    "WorkflowResult",
]
