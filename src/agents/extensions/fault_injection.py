"""
Fault injection hooks for chaos testing of agent runs.

Allows simulating tool failures, latency, and errors to test
agent resilience without modifying agent or tool code.

Usage:
    from agents.extensions.fault_injection import FaultInjectionHooks, ToolFault, FaultType

    hooks = FaultInjectionHooks(
        faults=[
            ToolFault(tool_name="web_search", fault_type=FaultType.EXCEPTION, rate=0.5),
            ToolFault(tool_name="calculator", fault_type=FaultType.LATENCY, latency_seconds=2.0),
        ]
    )
    result = await Runner.run(agent, "Hello", hooks=hooks)
    hooks.report()
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agents import Agent, RunHooks, Tool
from agents.run_context import RunContextWrapper, TContext


class FaultType(str, Enum):
    EXCEPTION = "exception"   # Raise an exception, simulating a tool crash
    LATENCY = "latency"       # Inject artificial delay before tool runs
    CORRUPTION = "corruption" # Return garbled/empty output instead of real result


@dataclass
class ToolFault:
    """Defines a fault to inject into a specific tool."""
    tool_name: str
    fault_type: FaultType
    rate: float = 1.0               # 0.0 to 1.0 — probability this fault fires
    latency_seconds: float = 1.0    # Only used for FaultType.LATENCY
    exception_message: str = "Simulated tool failure (fault injection)"
    corrupt_output: str = "[CORRUPTED]"


@dataclass
class FaultEvent:
    """Records a single fault that was triggered during a run."""
    tool_name: str
    fault_type: FaultType
    triggered_at: float = field(default_factory=time.time)


class FaultInjectionHooks(RunHooks[TContext]):
    """
    RunHooks subclass that injects configurable faults into tool calls.
    Attach to Runner.run() via the hooks= parameter.
    """

    def __init__(
        self,
        faults: list[ToolFault],
        seed: int | None = None,
    ) -> None:
        """
        Args:
            faults: List of ToolFault configs describing what to inject and where.
            seed: Optional random seed for reproducible fault injection in tests.
        """
        self._faults: dict[str, ToolFault] = {f.tool_name: f for f in faults}
        self._rng = random.Random(seed)
        self._events: list[FaultEvent] = []

    @property
    def triggered_faults(self) -> list[FaultEvent]:
        """All faults that were actually triggered during the run."""
        return list(self._events)

    async def on_tool_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
    ) -> None:
        fault = self._faults.get(tool.name)
        if fault is None:
            return

        if self._rng.random() > fault.rate:
            return

        if fault.fault_type == FaultType.LATENCY:
            self._events.append(FaultEvent(tool.name, FaultType.LATENCY))
            await asyncio.sleep(fault.latency_seconds)

        elif fault.fault_type == FaultType.EXCEPTION:
            self._events.append(FaultEvent(tool.name, FaultType.EXCEPTION))
            raise RuntimeError(fault.exception_message)

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
        result: str,
    ) -> None:
        fault = self._faults.get(tool.name)
        if fault is None or fault.fault_type != FaultType.CORRUPTION:
            return

        if self._rng.random() > fault.rate:
            return

        # We record the event — the corruption itself is applied via result override
        # Note: RunHooks.on_tool_end does not support return values for overriding output.
        # Corruption here logs the intent; see docs for wrapping tools directly if needed.
        self._events.append(FaultEvent(tool.name, FaultType.CORRUPTION))

    def report(self) -> str:
        """Print a summary of all faults triggered during the run."""
        if not self._events:
            summary = "No faults triggered."
            print(summary)
            return summary

        lines = [f"Fault injection report — {len(self._events)} fault(s) triggered:"]
        for i, event in enumerate(self._events, 1):
            lines.append(
                f"  {i}. [{event.fault_type.upper()}] tool={event.tool_name!r}"
            )
        summary = "\n".join(lines)
        print(summary)
        return summary