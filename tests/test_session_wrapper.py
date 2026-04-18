from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from agents import Agent, Runner
from agents.items import TResponseInputItem
from agents.run_context import RunContextWrapper
from agents.run_internal.session_persistence import (
    prepare_input_with_session,
    rewind_session_items,
    save_result_to_session,
)
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message
from tests.utils.simple_session import SimpleListSession


def _run_sync_wrapper(agent: Agent[Any], input_data: str, **kwargs: Any) -> Any:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return Runner.run_sync(agent, input_data, **kwargs)
    finally:
        loop.close()


async def _run_agent_async(runner_method: str, agent: Agent[Any], input_data: str, **kwargs: Any):
    if runner_method == "run":
        return await Runner.run(agent, input_data, **kwargs)
    if runner_method == "run_sync":
        return await asyncio.to_thread(_run_sync_wrapper, agent, input_data, **kwargs)
    if runner_method == "run_streamed":
        result = Runner.run_streamed(agent, input_data, **kwargs)
        async for _event in result.stream_events():
            pass
        return result
    raise ValueError(f"Unknown runner method: {runner_method}")


class WrapperRecordingSession(SimpleListSession):
    def __init__(
        self,
        session_id: str = "test",
        history: list[TResponseInputItem] | None = None,
    ) -> None:
        super().__init__(session_id=session_id, history=history)
        self.get_wrappers: list[RunContextWrapper[Any] | None] = []
        self.add_wrappers: list[RunContextWrapper[Any] | None] = []
        self.pop_wrappers: list[RunContextWrapper[Any] | None] = []

    async def get_items(
        self,
        limit: int | None = None,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> list[TResponseInputItem]:
        self.get_wrappers.append(wrapper)
        return await super().get_items(limit=limit, wrapper=wrapper)

    async def add_items(
        self,
        items: list[TResponseInputItem],
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None:
        self.add_wrappers.append(wrapper)
        await super().add_items(items, wrapper=wrapper)

    async def pop_item(
        self,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> TResponseInputItem | None:
        self.pop_wrappers.append(wrapper)
        return await super().pop_item(wrapper=wrapper)


class LegacySession:
    session_id = "legacy"
    session_settings = None

    def __init__(self) -> None:
        self.items: list[TResponseInputItem] = []
        self.get_calls = 0
        self.add_calls = 0
        self.pop_calls = 0

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        self.get_calls += 1
        if limit is None:
            return list(self.items)
        if limit <= 0:
            return []
        return self.items[-limit:]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self.add_calls += 1
        self.items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        self.pop_calls += 1
        if not self.items:
            return None
        return self.items.pop()

    async def clear_session(self) -> None:
        self.items.clear()


@pytest.mark.asyncio
async def test_prepare_input_with_session_passes_wrapper_to_get_items() -> None:
    wrapper = RunContextWrapper(context={"tenant": "acme"})
    history = [cast(TResponseInputItem, {"role": "assistant", "content": "Earlier"})]
    session = WrapperRecordingSession(history=history)

    prepared, session_items = await prepare_input_with_session(
        "Hello",
        session,
        None,
        wrapper=wrapper,
    )

    assert session.get_wrappers == [wrapper]
    prepared_item = cast(dict[str, Any], prepared[-1])
    session_item = cast(dict[str, Any], session_items[-1])
    assert prepared_item["content"] == "Hello"
    assert session_item["content"] == "Hello"


@pytest.mark.asyncio
async def test_save_result_to_session_passes_wrapper_to_add_items() -> None:
    wrapper = RunContextWrapper(context={"tenant": "acme"})
    session = WrapperRecordingSession()

    await save_result_to_session(
        session,
        "Hello",
        [],
        None,
        wrapper=wrapper,
    )

    assert session.add_wrappers == [wrapper]


@pytest.mark.asyncio
async def test_rewind_session_items_passes_wrapper_to_pop_and_cleanup() -> None:
    wrapper = RunContextWrapper(context={"tenant": "acme"})
    target = cast(TResponseInputItem, {"role": "user", "content": "Hello"})
    session = WrapperRecordingSession(history=[target])

    await rewind_session_items(session, [target], wrapper=wrapper)

    assert session.pop_wrappers == [wrapper]
    assert session.get_wrappers[-1] == wrapper


@pytest.mark.asyncio
async def test_session_helpers_remain_compatible_with_legacy_sessions() -> None:
    wrapper = RunContextWrapper(context={"tenant": "legacy"})
    legacy = LegacySession()

    prepared, session_items = await prepare_input_with_session(
        "Hello",
        cast(Any, legacy),
        None,
        wrapper=wrapper,
    )
    await save_result_to_session(
        cast(Any, legacy),
        prepared,
        [],
        None,
        wrapper=wrapper,
    )
    await rewind_session_items(cast(Any, legacy), session_items, wrapper=wrapper)

    assert legacy.get_calls >= 1
    assert legacy.add_calls == 1
    assert legacy.pop_calls >= 1


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_runner_passes_context_wrapper_to_session_methods(runner_method: str) -> None:
    context = {"tenant": "acme"}
    session = WrapperRecordingSession()
    model = FakeModel()
    model.set_next_output([get_text_message("ok")])
    agent = Agent(name="test", model=model)

    result = await _run_agent_async(
        runner_method,
        agent,
        "Hello",
        context=context,
        session=session,
    )

    assert result.final_output == "ok"
    assert session.get_wrappers
    assert session.add_wrappers
    assert session.get_wrappers[0] is not None
    assert session.add_wrappers[0] is not None
    assert session.get_wrappers[0].context is context
    assert session.add_wrappers[0].context is context
