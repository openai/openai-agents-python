"""Tests for FaultInjectionHooks."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.extensions.fault_injection import (
    FaultInjectionHooks,
    FaultType,
    ToolFault,
)


def make_context():
    return MagicMock()


def make_agent():
    return MagicMock()


def make_tool(name: str):
    tool = MagicMock()
    tool.name = name
    return tool


# ── LATENCY ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_latency_fault_delays_execution():
    hooks = FaultInjectionHooks(
        faults=[ToolFault(tool_name="search", fault_type=FaultType.LATENCY, latency_seconds=0.05)],
        seed=42,
    )
    start = asyncio.get_event_loop().time()
    await hooks.on_tool_start(make_context(), make_agent(), make_tool("search"))
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed >= 0.04
    assert len(hooks.triggered_faults) == 1
    assert hooks.triggered_faults[0].fault_type == FaultType.LATENCY


# ── EXCEPTION ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exception_fault_raises():
    hooks = FaultInjectionHooks(
        faults=[ToolFault(tool_name="calculator", fault_type=FaultType.EXCEPTION)],
        seed=42,
    )
    with pytest.raises(RuntimeError, match="Simulated tool failure"):
        await hooks.on_tool_start(make_context(), make_agent(), make_tool("calculator"))

    assert len(hooks.triggered_faults) == 1
    assert hooks.triggered_faults[0].fault_type == FaultType.EXCEPTION


# ── RATE ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_zero_rate_never_triggers():
    hooks = FaultInjectionHooks(
        faults=[ToolFault(tool_name="search", fault_type=FaultType.EXCEPTION, rate=0.0)],
        seed=42,
    )
    # Should never raise regardless of how many times we call it
    for _ in range(20):
        await hooks.on_tool_start(make_context(), make_agent(), make_tool("search"))

    assert len(hooks.triggered_faults) == 0


@pytest.mark.asyncio
async def test_full_rate_always_triggers():
    hooks = FaultInjectionHooks(
        faults=[ToolFault(tool_name="search", fault_type=FaultType.EXCEPTION, rate=1.0)],
        seed=42,
    )
    with pytest.raises(RuntimeError):
        await hooks.on_tool_start(make_context(), make_agent(), make_tool("search"))

    assert len(hooks.triggered_faults) == 1


# ── UNAFFECTED TOOLS ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unregistered_tool_not_affected():
    hooks = FaultInjectionHooks(
        faults=[ToolFault(tool_name="search", fault_type=FaultType.EXCEPTION)],
        seed=42,
    )
    # "calculator" has no fault configured — should pass silently
    await hooks.on_tool_start(make_context(), make_agent(), make_tool("calculator"))
    assert len(hooks.triggered_faults) == 0


# ── MULTIPLE FAULTS ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_faults_independent():
    hooks = FaultInjectionHooks(
        faults=[
            ToolFault(tool_name="search", fault_type=FaultType.LATENCY, latency_seconds=0.01),
            ToolFault(tool_name="calculator", fault_type=FaultType.EXCEPTION),
        ],
        seed=42,
    )
    # Latency on search
    await hooks.on_tool_start(make_context(), make_agent(), make_tool("search"))

    # Exception on calculator
    with pytest.raises(RuntimeError):
        await hooks.on_tool_start(make_context(), make_agent(), make_tool("calculator"))

    assert len(hooks.triggered_faults) == 2


# ── CORRUPTION ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_corruption_fault_recorded_on_tool_end():
    hooks = FaultInjectionHooks(
        faults=[ToolFault(tool_name="search", fault_type=FaultType.CORRUPTION, rate=1.0)],
        seed=42,
    )
    await hooks.on_tool_end(make_context(), make_agent(), make_tool("search"), result="real output")
    assert len(hooks.triggered_faults) == 1
    assert hooks.triggered_faults[0].fault_type == FaultType.CORRUPTION


# ── REPORT ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_report_no_faults(capsys):
    hooks = FaultInjectionHooks(faults=[], seed=42)
    output = hooks.report()
    assert "No faults triggered" in output


@pytest.mark.asyncio
async def test_report_with_faults(capsys):
    hooks = FaultInjectionHooks(
        faults=[ToolFault(tool_name="search", fault_type=FaultType.EXCEPTION, rate=1.0)],
        seed=42,
    )
    with pytest.raises(RuntimeError):
        await hooks.on_tool_start(make_context(), make_agent(), make_tool("search"))

    output = hooks.report()
    assert "search" in output
    assert "EXCEPTION" in output


# ── SEED REPRODUCIBILITY ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_same_seed_produces_same_results():
    """Two hooks with the same seed and rate=0.5 should fire identically."""
    fault = ToolFault(tool_name="search", fault_type=FaultType.EXCEPTION, rate=0.5)

    results_a = []
    hooks_a = FaultInjectionHooks(faults=[fault], seed=99)
    for _ in range(10):
        try:
            await hooks_a.on_tool_start(make_context(), make_agent(), make_tool("search"))
            results_a.append(False)
        except RuntimeError:
            results_a.append(True)

    results_b = []
    hooks_b = FaultInjectionHooks(faults=[fault], seed=99)
    for _ in range(10):
        try:
            await hooks_b.on_tool_start(make_context(), make_agent(), make_tool("search"))
            results_b.append(False)
        except RuntimeError:
            results_b.append(True)

    assert results_a == results_b