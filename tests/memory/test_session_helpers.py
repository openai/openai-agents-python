import asyncio

import pytest
from pydantic import ValidationError

from agents import Agent, RunConfig, Runner, SessionSettings
from agents.memory import await_session_mutation, coerce_session_settings, resolve_session_limit
from agents.testing import ScriptedModel
from examples.memory.custom_session import CustomSession, HistoryStore
from tests.test_responses import get_text_message


def test_settings_normalization_and_limit_precedence() -> None:
    settings = coerce_session_settings({"limit": 2})
    assert coerce_session_settings(settings) is settings
    assert resolve_session_limit(None, settings) == 2
    assert resolve_session_limit(0, settings) == 0
    assert resolve_session_limit(-1, settings) == -1
    assert resolve_session_limit(3, settings) == 3
    assert resolve_session_limit(None, {"limit": 4}) == 4
    assert resolve_session_limit(None, None) is None
    assert resolve_session_limit(None, SessionSettings()) is None
    with pytest.raises(TypeError, match="Unknown session settings: unknown"):
        coerce_session_settings({"unknown": 1})
    with pytest.raises(ValidationError):
        coerce_session_settings({"limit": "not-a-number"})


@pytest.mark.asyncio
async def test_mutation_result_and_failure() -> None:
    async def succeed() -> int:
        return 42

    async def fail() -> None:
        raise ValueError("write failed")

    assert await await_session_mutation(succeed()) == 42
    with pytest.raises(ValueError, match="write failed"):
        await await_session_mutation(fail())


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_repeated_cancellation_waits_for_mutation(fail: bool) -> None:
    started = asyncio.Event()
    finish = asyncio.Event()
    settled = asyncio.Event()
    writes: list[str] = []
    cancellations: list[str] = []

    async def write() -> None:
        started.set()
        try:
            await finish.wait()
            if fail:
                raise ValueError("write failed")
            writes.append("committed")
        finally:
            settled.set()

    async def mutate() -> None:
        try:
            await await_session_mutation(write())
        except asyncio.CancelledError as exc:
            # Python 3.10 can discard this message when another task awaits us.
            cancellations.append(str(exc))
            raise

    task = asyncio.create_task(mutate())
    try:
        await started.wait()
        task.cancel("first")
        await asyncio.sleep(0)
        task.cancel("second")
        await asyncio.sleep(0)
        assert not task.done()
        assert not settled.is_set()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancellations == ["first"]
        assert settled.is_set()
        assert writes == ([] if fail else ["committed"])
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_custom_adapter_runner_and_storage_contract() -> None:
    store = HistoryStore()
    session = CustomSession("first", store, session_settings={"limit": 2})
    other = CustomSession("other", store)
    model = ScriptedModel([[get_text_message("Hello!")], [get_text_message("You said hello.")]])
    agent = Agent(name="Assistant", model=model)
    config = RunConfig(tracing_disabled=True)
    await Runner.run(agent, "Hello", session=session, run_config=config)
    await Runner.run(agent, "What did I say?", session=session, run_config=config)
    assert model.calls[-1].input[0]["content"] == "Hello"
    assert len(await session.get_items()) == 2
    assert len(await session.get_items(limit=4)) == 4
    assert await session.get_items(limit=0) == []
    assert await session.get_items(limit=-1) == []
    assert await other.get_items() == []
    last = (await session.get_items())[-1]
    assert await session.pop_item() == last
    await session.clear_session()
    assert await session.pop_item() is None
    assert await session.get_items() == []


@pytest.mark.asyncio
async def test_independently_cancelled_operation_propagates() -> None:
    operation: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_session_mutation(operation)
