from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from agents import Agent, RunContextWrapper, Runner, SessionSettings, TResponseInputItem
from agents.exceptions import InputGuardrailTripwireTriggered
from agents.guardrail import GuardrailFunctionOutput, InputGuardrail
from agents.memory import OpenAIResponsesCompactionSession
from agents.memory.session import _session_accepts_wrapper, _session_method_accepts_wrapper
from agents.run_internal.session_persistence import rewind_session_items
from agents.tool import function_tool
from tests.fake_model import FakeModel
from tests.test_responses import get_function_tool_call, get_text_message


@dataclass
class TenantContext:
    tenant_id: str


class ContextAwareSession:
    def __init__(self) -> None:
        self.session_id = "context-aware"
        self.session_settings: SessionSettings | None = None
        self.items_by_scope: dict[str, list[TResponseInputItem]] = {"default": []}
        self.calls: list[tuple[str, RunContextWrapper[Any] | None]] = []

    def _scope(self, wrapper: RunContextWrapper[Any] | None) -> str:
        if wrapper is None:
            return "default"
        context = cast(TenantContext, wrapper.context)
        return context.tenant_id

    async def get_items(
        self,
        limit: int | None = None,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> list[TResponseInputItem]:
        self.calls.append(("get_items", wrapper))
        items = self.items_by_scope.setdefault(self._scope(wrapper), [])
        if limit is None:
            return list(items)
        return list(items[-limit:])

    async def add_items(
        self,
        items: list[TResponseInputItem],
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None:
        self.calls.append(("add_items", wrapper))
        self.items_by_scope.setdefault(self._scope(wrapper), []).extend(items)

    async def pop_item(
        self,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> TResponseInputItem | None:
        self.calls.append(("pop_item", wrapper))
        items = self.items_by_scope.setdefault(self._scope(wrapper), [])
        return items.pop() if items else None

    async def clear_session(
        self,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None:
        self.calls.append(("clear_session", wrapper))
        self.items_by_scope[self._scope(wrapper)] = []


class LegacySession:
    def __init__(self) -> None:
        self.session_id = "legacy"
        self.session_settings = None
        self.items: list[TResponseInputItem] = []
        self.get_calls = 0

    async def get_items(self) -> list[TResponseInputItem]:
        self.get_calls += 1
        return list(self.items)

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self.items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        return self.items.pop() if self.items else None

    async def clear_session(self) -> None:
        self.items.clear()


class LegacyKwargsSession:
    def __init__(self) -> None:
        self.session_id = "legacy-kwargs"
        self.session_settings: SessionSettings | None = None
        self.items: list[TResponseInputItem] = []
        self.kwargs_calls: list[dict[str, Any]] = []

    async def get_items(self, limit: int | None = None, **kwargs: Any) -> list[TResponseInputItem]:
        self.kwargs_calls.append(kwargs)
        if limit is None:
            return list(self.items)
        return list(self.items[-limit:])

    async def add_items(self, items: list[TResponseInputItem], **kwargs: Any) -> None:
        self.kwargs_calls.append(kwargs)
        self.items.extend(items)

    async def pop_item(self, **kwargs: Any) -> TResponseInputItem | None:
        self.kwargs_calls.append(kwargs)
        return self.items.pop() if self.items else None

    async def clear_session(self, **kwargs: Any) -> None:
        self.kwargs_calls.append(kwargs)
        self.items.clear()


class UninspectableAsyncMethod:
    def __init__(self, method: Any) -> None:
        self.method = method

    @property
    def __signature__(self) -> Any:
        raise RuntimeError("signature unavailable")

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return await self.method(*args, **kwargs)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_runner_passes_same_wrapper_to_context_aware_session(streamed: bool) -> None:
    session = ContextAwareSession()
    model = FakeModel(initial_output=[get_text_message("ok")])
    agent = Agent(name="test", model=model)
    context = TenantContext(tenant_id="tenant-a")

    if streamed:
        result: Any = Runner.run_streamed(agent, "hello", context=context, session=session)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "hello", context=context, session=session)

    assert result.final_output == "ok"
    assert [name for name, _ in session.calls] == [
        "get_items",
        "add_items",
        "add_items",
    ]
    assert all(wrapper is result.context_wrapper for _, wrapper in session.calls)
    assert session.items_by_scope["default"] == []
    assert len(session.items_by_scope["tenant-a"]) == 2


@pytest.mark.asyncio
async def test_runner_preserves_legacy_session_call_shapes() -> None:
    session = LegacySession()
    model = FakeModel(initial_output=[get_text_message("ok")])
    agent = Agent(name="test", model=model)

    result = await Runner.run(
        agent,
        "hello",
        context=TenantContext(tenant_id="tenant-a"),
        session=cast(Any, session),
    )

    assert result.final_output == "ok"
    assert session.get_calls == 1
    assert len(session.items) == 2


@pytest.mark.asyncio
async def test_runner_does_not_treat_legacy_kwargs_as_wrapper_opt_in() -> None:
    session = LegacyKwargsSession()
    model = FakeModel(initial_output=[get_text_message("ok")])

    result = await Runner.run(
        Agent(name="test", model=model),
        "hello",
        context=TenantContext(tenant_id="tenant-a"),
        session=session,
    )

    assert result.final_output == "ok"
    assert session.kwargs_calls == [{}, {}, {}]
    assert len(session.items) == 2


@pytest.mark.asyncio
async def test_runner_preserves_legacy_calls_when_signature_inspection_fails() -> None:
    session = cast(Any, LegacySession())
    session.get_items = UninspectableAsyncMethod(session.get_items)
    model = FakeModel(initial_output=[get_text_message("ok")])

    result = await Runner.run(
        Agent(name="test", model=model),
        "hello",
        context=TenantContext(tenant_id="tenant-a"),
        session=session,
    )

    assert result.final_output == "ok"
    assert session.get_calls == 1
    assert len(session.items) == 2


@pytest.mark.asyncio
async def test_runner_does_not_partially_enable_context_aware_session() -> None:
    class PartialSession:
        def __init__(self) -> None:
            self.session_id = "partial"
            self.session_settings: SessionSettings | None = None
            self.items: list[TResponseInputItem] = []
            self.wrappers: list[RunContextWrapper[Any] | None] = []

        async def get_items(
            self,
            limit: int | None = None,
            *,
            wrapper: RunContextWrapper[Any] | None = None,
        ) -> list[TResponseInputItem]:
            self.wrappers.append(wrapper)
            return list(self.items if limit is None else self.items[-limit:])

        async def add_items(
            self,
            items: list[TResponseInputItem],
            *,
            wrapper: RunContextWrapper[Any] | None = None,
        ) -> None:
            self.wrappers.append(wrapper)
            self.items.extend(items)

        async def pop_item(self) -> TResponseInputItem | None:
            return None

        async def clear_session(self) -> None:
            pass

    session = PartialSession()
    model = FakeModel(initial_output=[get_text_message("ok")])

    result = await Runner.run(
        Agent(name="test", model=model),
        "hello",
        context=TenantContext(tenant_id="tenant-a"),
        session=session,
    )

    assert result.final_output == "ok"
    assert session.wrappers == [None, None, None]
    assert len(session.items) == 2


@pytest.mark.asyncio
async def test_retry_rewind_uses_same_context_scope_for_reads_pops_and_cleanup() -> None:
    session = ContextAwareSession()
    wrapper = RunContextWrapper(context=TenantContext(tenant_id="tenant-a"))
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    session.items_by_scope["default"] = [{"role": "user", "content": "keep"}]
    await session.add_items(items, wrapper=wrapper)
    session.calls.clear()

    await rewind_session_items(session, items, wrapper=wrapper)

    assert session.items_by_scope["tenant-a"] == []
    assert session.items_by_scope["default"] == [{"role": "user", "content": "keep"}]
    assert [name for name, _ in session.calls] == [
        "get_items",
        "pop_item",
        "pop_item",
        "get_items",
    ]
    assert all(call_wrapper is wrapper for _, call_wrapper in session.calls)


@pytest.mark.asyncio
async def test_retry_rewind_restores_partial_pops_in_the_same_context_scope() -> None:
    class FailingSecondPopSession(ContextAwareSession):
        def __init__(self) -> None:
            super().__init__()
            self.pop_count = 0

        async def pop_item(
            self,
            *,
            wrapper: RunContextWrapper[Any] | None = None,
        ) -> TResponseInputItem | None:
            self.calls.append(("pop_item", wrapper))
            self.pop_count += 1
            if self.pop_count == 2:
                raise RuntimeError("pop failed")
            items = self.items_by_scope.setdefault(self._scope(wrapper), [])
            return items.pop() if items else None

    session = FailingSecondPopSession()
    wrapper = RunContextWrapper(context=TenantContext(tenant_id="tenant-a"))
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    session.items_by_scope["tenant-a"] = list(items)
    session.items_by_scope["default"] = [{"role": "user", "content": "keep"}]

    await rewind_session_items(session, items, wrapper=wrapper)

    assert session.items_by_scope["tenant-a"] == items
    assert session.items_by_scope["default"] == [{"role": "user", "content": "keep"}]
    assert [name for name, _ in session.calls] == [
        "get_items",
        "pop_item",
        "pop_item",
        "add_items",
    ]
    assert all(call_wrapper is wrapper for _, call_wrapper in session.calls)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_input_guardrail_persists_in_the_context_scope(streamed: bool) -> None:
    def guardrail_function(
        _context: RunContextWrapper[Any], _agent: Agent[Any], _input: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    session = ContextAwareSession()
    session.items_by_scope["default"] = [{"role": "user", "content": "keep"}]
    context = TenantContext(tenant_id="tenant-a")
    agent = Agent(
        name="test",
        model=FakeModel(initial_output=[get_text_message("not persisted")]),
        input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        if streamed:
            result = Runner.run_streamed(agent, "hello", context=context, session=session)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "hello", context=context, session=session)

    assert session.items_by_scope["default"] == [{"role": "user", "content": "keep"}]
    assert session.items_by_scope["tenant-a"] == [{"role": "user", "content": "hello"}]
    assert all(wrapper is not None and wrapper.context is context for _, wrapper in session.calls)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_resumed_run_persists_in_the_context_scope(streamed: bool) -> None:
    async def test_tool() -> str:
        return "tool result"

    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("test_tool", "{}", call_id="call-resume")],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[tool])
    session = ContextAwareSession()
    session.items_by_scope["default"] = [{"role": "user", "content": "keep"}]
    context = TenantContext(tenant_id="tenant-a")

    if streamed:
        first: Any = Runner.run_streamed(agent, "hello", context=context, session=session)
        async for _ in first.stream_events():
            pass
    else:
        first = await Runner.run(agent, "hello", context=context, session=session)

    assert len(first.interruptions) == 1
    state = first.to_state()
    state.approve(first.interruptions[0])
    session.calls.clear()

    if streamed:
        resumed: Any = Runner.run_streamed(agent, state, session=session)
        async for _ in resumed.stream_events():
            pass
    else:
        resumed = await Runner.run(agent, state, session=session)

    assert resumed.final_output == "done"
    assert session.items_by_scope["default"] == [{"role": "user", "content": "keep"}]
    assert all(wrapper is resumed.context_wrapper for _, wrapper in session.calls)
    assert any(
        isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == "call-resume"
        for item in session.items_by_scope["tenant-a"]
    )


@pytest.mark.asyncio
async def test_compaction_session_keeps_context_aware_underlying_on_legacy_scope() -> None:
    underlying = ContextAwareSession()
    underlying.items_by_scope["default"] = [{"role": "user", "content": "existing"}]
    session = OpenAIResponsesCompactionSession(
        session_id="compaction",
        underlying_session=underlying,
        should_trigger_compaction=lambda _: False,
    )

    result = await Runner.run(
        Agent(name="test", model=FakeModel(initial_output=[get_text_message("done")])),
        "hello",
        context=TenantContext(tenant_id="tenant-a"),
        session=session,
    )

    assert result.final_output == "done"
    assert not _session_accepts_wrapper(session)
    assert "tenant-a" not in underlying.items_by_scope
    assert len(underlying.items_by_scope["default"]) == 3
    assert underlying.calls
    assert all(wrapper is None for _, wrapper in underlying.calls)


def test_session_wrapper_method_requires_named_wrapper_parameter() -> None:
    class Methods:
        async def legacy(self) -> None:
            pass

        async def positional_only(self, wrapper: Any, /) -> None:
            pass

        async def keyword(self, *, wrapper: Any = None) -> None:
            pass

        async def kwargs(self, **kwargs: Any) -> None:
            pass

    methods = Methods()

    assert not _session_method_accepts_wrapper(methods.legacy)
    assert not _session_method_accepts_wrapper(methods.positional_only)
    assert _session_method_accepts_wrapper(methods.keyword)
    assert not _session_method_accepts_wrapper(methods.kwargs)


def test_session_wrapper_opt_in_requires_all_history_operations() -> None:
    session = ContextAwareSession()
    assert _session_accepts_wrapper(session)

    cast(Any, session).clear_session = LegacySession().clear_session
    assert not _session_accepts_wrapper(session)
