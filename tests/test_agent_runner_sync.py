import asyncio
from collections.abc import Generator

import pytest

from agents.run import AgentRunner


@pytest.fixture
def fresh_event_loop_policy() -> Generator[asyncio.AbstractEventLoopPolicy, None, None]:
    policy_before = asyncio.get_event_loop_policy()
    new_policy = asyncio.DefaultEventLoopPolicy()
    asyncio.set_event_loop_policy(new_policy)
    try:
        yield new_policy
    finally:
        asyncio.set_event_loop_policy(policy_before)


def test_run_sync_reuses_existing_default_loop(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    test_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(test_loop)

    try:
        runner.run_sync(object(), "input")
        assert observed_loops and observed_loops[0] is test_loop
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        test_loop.close()


def test_run_sync_creates_default_loop_when_missing(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    fresh_event_loop_policy.set_event_loop(None)

    runner.run_sync(object(), "input")
    created_loop = observed_loops[0]
    assert created_loop is fresh_event_loop_policy.get_event_loop()

    fresh_event_loop_policy.set_event_loop(None)
    created_loop.close()


def test_run_sync_errors_when_loop_already_running(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()

    async def fake_run(self, *_args, **_kwargs):
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    async def invoke():
        with pytest.raises(RuntimeError):
            runner.run_sync(object(), "input")

    asyncio.run(invoke())
