"""Tests for passing the run context wrapper to Session methods (issue #2072)."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
    SQLiteSession,
    TResponseInputItem,
    input_guardrail,
)
from agents.memory.session import SessionABC, session_method_accepts_wrapper
from agents.memory.session_settings import SessionSettings
from agents.run_context import RunContextWrapper
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message


@dataclass
class UserInfo:
    """Sample user-defined run context object."""

    user_id: str = "user-123"


class ContextAwareSession(SessionABC):
    """Session that opts into the wrapper parameter and records what it receives."""

    def __init__(self, session_id: str = "context-aware"):
        self.session_id = session_id
        self._items: list[TResponseInputItem] = []
        self.get_items_wrappers: list[RunContextWrapper[Any] | None] = []
        self.get_items_limits: list[int | None] = []
        self.add_items_wrappers: list[RunContextWrapper[Any] | None] = []

    async def get_items(
        self,
        limit: int | None = None,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> list[TResponseInputItem]:
        self.get_items_wrappers.append(wrapper)
        self.get_items_limits.append(limit)
        if limit is not None:
            return self._items[-limit:]
        return list(self._items)

    async def add_items(
        self,
        items: list[TResponseInputItem],
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None:
        self.add_items_wrappers.append(wrapper)
        self._items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()


class LegacySession(SessionABC):
    """Session with pre-wrapper signatures, as third-party implementations may still have."""

    def __init__(self, session_id: str = "legacy"):
        self.session_id = session_id
        self._items: list[TResponseInputItem] = []

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:  # type: ignore[override]
        if limit is not None:
            return self._items[-limit:]
        return list(self._items)

    async def add_items(self, items: list[TResponseInputItem]) -> None:  # type: ignore[override]
        self._items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()


class VarKwargsSession(LegacySession):
    """Session that accepts the wrapper through ``**kwargs`` rather than a named parameter."""

    def __init__(self, session_id: str = "var-kwargs"):
        super().__init__(session_id)
        self.received_kwargs: list[dict[str, Any]] = []

    async def get_items(self, limit: int | None = None, **kwargs: Any) -> list[TResponseInputItem]:
        self.received_kwargs.append(kwargs)
        return await super().get_items(limit)

    async def add_items(self, items: list[TResponseInputItem], **kwargs: Any) -> None:
        self.received_kwargs.append(kwargs)
        await super().add_items(items)


def _run_sync_wrapper(agent, input_data, **kwargs):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return Runner.run_sync(agent, input_data, **kwargs)
    finally:
        loop.close()


async def run_agent_async(runner_method: str, agent, input_data, **kwargs):
    if runner_method == "run":
        return await Runner.run(agent, input_data, **kwargs)
    elif runner_method == "run_sync":
        return await asyncio.to_thread(_run_sync_wrapper, agent, input_data, **kwargs)
    elif runner_method == "run_streamed":
        result = Runner.run_streamed(agent, input_data, **kwargs)
        async for _ in result.stream_events():
            pass
        return result
    else:
        raise ValueError(f"Unknown runner method: {runner_method}")


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_runner_passes_wrapper_to_context_aware_session(runner_method):
    """Sessions that opt in receive the run context wrapper from every runner entrypoint."""
    session = ContextAwareSession()
    context = UserInfo()

    model = FakeModel()
    agent = Agent(name="test", model=model)
    model.set_next_output([get_text_message("Hello")])

    result = await run_agent_async(
        runner_method, agent, "Hi there", session=session, context=context
    )
    assert result.final_output == "Hello"

    assert len(session.get_items_wrappers) > 0
    assert len(session.add_items_wrappers) > 0
    for wrapper in session.get_items_wrappers + session.add_items_wrappers:
        assert isinstance(wrapper, RunContextWrapper)
        assert wrapper.context is context


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_runner_keeps_legacy_session_working(runner_method):
    """Sessions without the wrapper parameter keep working unchanged."""
    session = LegacySession()

    model = FakeModel()
    agent = Agent(name="test", model=model)

    model.set_next_output([get_text_message("San Francisco")])
    result1 = await run_agent_async(
        runner_method, agent, "What city is the Golden Gate Bridge in?", session=session
    )
    assert result1.final_output == "San Francisco"

    model.set_next_output([get_text_message("California")])
    result2 = await run_agent_async(runner_method, agent, "What state is it in?", session=session)
    assert result2.final_output == "California"

    # The second turn must include the persisted history from the first turn.
    last_input = model.last_turn_args["input"]
    assert len(last_input) > 1


@pytest.mark.asyncio
async def test_runner_passes_wrapper_to_var_kwargs_session():
    """Sessions accepting **kwargs receive the wrapper through them."""
    session = VarKwargsSession()
    context = UserInfo()

    model = FakeModel()
    agent = Agent(name="test", model=model)
    model.set_next_output([get_text_message("Hello")])

    await Runner.run(agent, "Hi there", session=session, context=context)

    wrappers = [kwargs["wrapper"] for kwargs in session.received_kwargs if "wrapper" in kwargs]
    assert len(wrappers) > 0
    for wrapper in wrappers:
        assert isinstance(wrapper, RunContextWrapper)
        assert wrapper.context is context


@pytest.mark.asyncio
async def test_runner_without_explicit_context_passes_wrapper():
    """The wrapper is passed even when the caller does not provide a context object."""
    session = ContextAwareSession()

    model = FakeModel()
    agent = Agent(name="test", model=model)
    model.set_next_output([get_text_message("Hello")])

    await Runner.run(agent, "Hi there", session=session)

    assert len(session.add_items_wrappers) > 0
    for wrapper in session.add_items_wrappers:
        assert isinstance(wrapper, RunContextWrapper)


def test_session_method_accepts_wrapper_helper():
    """The capability check recognizes opt-in signatures and rejects legacy ones."""
    context_aware = ContextAwareSession()
    legacy = LegacySession()
    var_kwargs = VarKwargsSession()

    assert session_method_accepts_wrapper(context_aware.get_items) is True
    assert session_method_accepts_wrapper(context_aware.add_items) is True
    assert session_method_accepts_wrapper(var_kwargs.get_items) is True
    assert session_method_accepts_wrapper(var_kwargs.add_items) is True
    assert session_method_accepts_wrapper(legacy.get_items) is False
    assert session_method_accepts_wrapper(legacy.add_items) is False
    # Callables without an introspectable signature must not be treated as opted in.
    assert session_method_accepts_wrapper(max) is False


@pytest.mark.asyncio
async def test_sqlite_session_accepts_and_ignores_wrapper():
    """Built-in sessions accept the wrapper directly and behave the same with or without it."""
    session = SQLiteSession("direct-call-test")
    try:
        wrapper = RunContextWrapper(context=UserInfo())
        items: list[TResponseInputItem] = [{"role": "user", "content": "hello"}]

        await session.add_items(items, wrapper=wrapper)
        retrieved_with = await session.get_items(wrapper=wrapper)
        retrieved_without = await session.get_items()

        assert retrieved_with == retrieved_without
        assert len(retrieved_with) == 1
    finally:
        session.close()


_BUILTIN_SESSION_SPECS = [
    ("agents.memory.sqlite_session", "SQLiteSession", None),
    ("agents.memory.openai_conversations_session", "OpenAIConversationsSession", None),
    (
        "agents.memory.openai_responses_compaction_session",
        "OpenAIResponsesCompactionSession",
        None,
    ),
    ("agents.extensions.memory.async_sqlite_session", "AsyncSQLiteSession", None),
    ("agents.extensions.memory.advanced_sqlite_session", "AdvancedSQLiteSession", None),
    ("agents.extensions.memory.encrypt_session", "EncryptedSession", "cryptography"),
    ("agents.extensions.memory.redis_session", "RedisSession", "redis"),
    ("agents.extensions.memory.sqlalchemy_session", "SQLAlchemySession", "sqlalchemy"),
    ("agents.extensions.memory.dapr_session", "DaprSession", "dapr"),
    ("agents.extensions.memory.mongodb_session", "MongoDBSession", "pymongo"),
]


@pytest.mark.parametrize(
    "module_name,class_name,required_package",
    _BUILTIN_SESSION_SPECS,
    ids=[spec[1] for spec in _BUILTIN_SESSION_SPECS],
)
def test_builtin_sessions_expose_keyword_only_wrapper(module_name, class_name, required_package):
    """Every built-in session implementation exposes the keyword-only wrapper parameter."""
    if required_package is not None:
        pytest.importorskip(required_package)
    module = pytest.importorskip(module_name)
    session_cls = getattr(module, class_name)

    for method_name in ("get_items", "add_items"):
        signature = inspect.signature(getattr(session_cls, method_name))
        parameter = signature.parameters.get("wrapper")
        assert parameter is not None, f"{class_name}.{method_name} is missing wrapper"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None


@pytest.mark.asyncio
async def test_guardrail_trip_persists_input_with_wrapper():
    """The guardrail-trip persistence path forwards the wrapper to the session."""
    session = ContextAwareSession()
    context = UserInfo()

    @input_guardrail
    def always_trip(ctx, agent, input) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    model = FakeModel()
    agent = Agent(name="test", model=model, input_guardrails=[always_trip])
    model.set_next_output([get_text_message("never returned")])

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, "Hi there", session=session, context=context)

    assert len(session.add_items_wrappers) > 0
    for wrapper in session.add_items_wrappers:
        assert isinstance(wrapper, RunContextWrapper)
        assert wrapper.context is context


@pytest.mark.asyncio
async def test_session_input_callback_path_passes_wrapper():
    """The history-merge callback path still forwards the wrapper on get_items."""
    session = ContextAwareSession()
    context = UserInfo()

    def keep_everything(
        history: list[TResponseInputItem], new_items: list[TResponseInputItem]
    ) -> list[TResponseInputItem]:
        return history + new_items

    model = FakeModel()
    agent = Agent(name="test", model=model)

    model.set_next_output([get_text_message("first")])
    await Runner.run(agent, "Turn one", session=session, context=context)

    model.set_next_output([get_text_message("second")])
    await Runner.run(
        agent,
        "Turn two",
        session=session,
        context=context,
        run_config=RunConfig(session_input_callback=keep_everything),
    )

    assert len(session.get_items_wrappers) >= 2
    for wrapper in session.get_items_wrappers:
        assert isinstance(wrapper, RunContextWrapper)
        assert wrapper.context is context


@pytest.mark.asyncio
async def test_session_settings_limit_path_passes_wrapper():
    """The limited-history read passes both the limit and the wrapper to the session."""
    session = ContextAwareSession()
    context = UserInfo()

    model = FakeModel()
    agent = Agent(name="test", model=model)

    model.set_next_output([get_text_message("first")])
    await Runner.run(agent, "Turn one", session=session, context=context)

    model.set_next_output([get_text_message("second")])
    await Runner.run(
        agent,
        "Turn two",
        session=session,
        context=context,
        run_config=RunConfig(session_settings=SessionSettings(limit=1)),
    )

    # The second run's history read must use the limit and still carry the wrapper.
    assert session.get_items_limits[-1] == 1
    last_wrapper = session.get_items_wrappers[-1]
    assert isinstance(last_wrapper, RunContextWrapper)
    assert last_wrapper.context is context


@pytest.mark.asyncio
async def test_encrypted_session_forwards_wrapper_to_underlying_session():
    """EncryptedSession forwards the wrapper to underlying sessions that opt in."""
    pytest.importorskip("cryptography")
    from agents.extensions.memory.encrypt_session import EncryptedSession

    underlying = ContextAwareSession()
    session = EncryptedSession(
        session_id="enc-forward",
        underlying_session=underlying,
        encryption_key="test-key",
    )
    wrapper = RunContextWrapper(context=UserInfo())
    items: list[TResponseInputItem] = [{"role": "user", "content": "hello"}]

    await session.add_items(items, wrapper=wrapper)
    await session.get_items(wrapper=wrapper)

    assert underlying.add_items_wrappers == [wrapper]
    assert len(underlying.get_items_wrappers) > 0
    assert all(received is wrapper for received in underlying.get_items_wrappers)


@pytest.mark.asyncio
async def test_encrypted_session_does_not_break_legacy_underlying_session():
    """EncryptedSession never passes the wrapper to underlying sessions that predate it."""
    pytest.importorskip("cryptography")
    from agents.extensions.memory.encrypt_session import EncryptedSession

    underlying = LegacySession()
    session = EncryptedSession(
        session_id="enc-legacy",
        underlying_session=underlying,
        encryption_key="test-key",
    )
    wrapper = RunContextWrapper(context=UserInfo())
    items: list[TResponseInputItem] = [{"role": "user", "content": "hello"}]

    await session.add_items(items, wrapper=wrapper)
    retrieved = await session.get_items(wrapper=wrapper)

    assert len(retrieved) == 1


@pytest.mark.asyncio
async def test_compaction_session_accepts_but_does_not_forward_wrapper():
    """OpenAIResponsesCompactionSession accepts the wrapper but does not forward it.

    The decorator rewrites history via clear_session + add_items during compaction, which
    cannot be scoped consistently through the get_items/add_items wrapper contract, so it
    deliberately operates on the underlying session's default scope.
    """
    from agents.memory.openai_responses_compaction_session import (
        OpenAIResponsesCompactionSession,
    )

    underlying = ContextAwareSession()
    session = OpenAIResponsesCompactionSession("compaction-forward", underlying)
    wrapper = RunContextWrapper(context=UserInfo())
    items: list[TResponseInputItem] = [{"role": "user", "content": "hello"}]

    # Accepting the keyword-only wrapper keeps it protocol-compatible and still works.
    await session.add_items(items, wrapper=wrapper)
    retrieved = await session.get_items(wrapper=wrapper)

    assert len(retrieved) == 1
    # The wrapper is intentionally not propagated to the underlying session.
    assert underlying.add_items_wrappers == [None]
    assert underlying.get_items_wrappers == [None]


@pytest.mark.asyncio
async def test_rewind_session_items_does_not_forward_wrapper():
    """The retry-rewind helper removes items via pop_item, which is outside the wrapper
    contract, so it operates on the session's default scope and does not forward the wrapper."""
    from agents.run_internal.session_persistence import rewind_session_items

    session = ContextAwareSession()
    item: TResponseInputItem = {"role": "user", "content": "to be rewound"}
    await session.add_items([item])
    session.get_items_wrappers.clear()

    await rewind_session_items(session, [item])

    # The rewind still works (the matching item is popped) but no wrapper is forwarded.
    assert await session.get_items() == []
    assert all(received is None for received in session.get_items_wrappers)
