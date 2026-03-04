"""Tests for ModelSettings.max_parallel_tool_calls concurrency limiting."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agents import Agent, ModelSettings, RunConfig
from agents.run_internal.tool_execution import _FunctionToolBatchExecutor


def make_executor(
    *,
    agent_max_parallel: int | None = None,
    config_max_parallel: int | None = None,
) -> _FunctionToolBatchExecutor:
    agent = MagicMock(spec=Agent)
    agent.model_settings = ModelSettings(max_parallel_tool_calls=agent_max_parallel)

    config = MagicMock(spec=RunConfig)
    config.model_settings = (
        ModelSettings(max_parallel_tool_calls=config_max_parallel)
        if config_max_parallel is not None
        else None
    )
    resolved = agent.model_settings.resolve(config.model_settings)
    agent.model_settings.resolve = MagicMock(return_value=resolved)

    return _FunctionToolBatchExecutor(
        agent=agent,
        tool_runs=[MagicMock() for _ in range(3)],
        hooks=MagicMock(),
        context_wrapper=MagicMock(),
        config=config,
        isolate_parallel_failures=False,
    )


async def test_no_limit_creates_no_semaphore():
    executor = make_executor(agent_max_parallel=None)
    assert executor._concurrency_semaphore is None


async def test_limit_creates_semaphore_with_correct_value():
    executor = make_executor(agent_max_parallel=2)
    sem = executor._concurrency_semaphore
    assert isinstance(sem, asyncio.Semaphore)
    # Can acquire 2 times before blocking
    assert await sem.acquire()
    assert await sem.acquire()
    assert sem.locked()  # 3rd acquire would block
    sem.release()
    sem.release()


async def test_config_level_overrides_agent_level():
    executor = make_executor(agent_max_parallel=5, config_max_parallel=2)
    sem = executor._concurrency_semaphore
    assert isinstance(sem, asyncio.Semaphore)
    await sem.acquire()
    await sem.acquire()
    assert sem.locked()
    sem.release()
    sem.release()


async def test_semaphore_limits_concurrency():
    """Verify that a semaphore(1) allows at most 1 concurrent execution."""
    semaphore = asyncio.Semaphore(1)
    concurrent_count = 0
    max_concurrent_seen = 0

    async def guarded_tool() -> None:
        nonlocal concurrent_count, max_concurrent_seen
        async with semaphore:
            concurrent_count += 1
            max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
            await asyncio.sleep(0.01)
            concurrent_count -= 1

    await asyncio.gather(*[guarded_tool() for _ in range(5)])
    assert max_concurrent_seen == 1


def test_model_settings_serialization_roundtrip():
    settings = ModelSettings(max_parallel_tool_calls=4)
    d = settings.to_json_dict()
    assert d["max_parallel_tool_calls"] == 4


def test_model_settings_resolve_inherits_max_parallel():
    base = ModelSettings(max_parallel_tool_calls=3)
    override = ModelSettings(temperature=0.5)
    resolved = base.resolve(override)
    assert resolved.max_parallel_tool_calls == 3


def test_model_settings_resolve_override_wins():
    base = ModelSettings(max_parallel_tool_calls=3)
    override = ModelSettings(max_parallel_tool_calls=1)
    resolved = base.resolve(override)
    assert resolved.max_parallel_tool_calls == 1


def test_model_settings_none_max_parallel_is_default():
    settings = ModelSettings()
    assert settings.max_parallel_tool_calls is None
