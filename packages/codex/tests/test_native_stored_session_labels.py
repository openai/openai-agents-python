"""Persisted native labels through the existing metadata owner."""

import asyncio
from typing import cast

import pytest

from agents import Agent, CustomStorage, LocalStorage, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.native_storage_fixtures import MemorySessionStore
from tests.test_native_manual_tools import next_request


async def _names(session):
    cursor = None
    labels = {}
    while True:
        page = await session.stored_sessions(limit=1, cursor=cursor)
        labels.update((item["id"], item["name"]) for item in page["sessions"])
        cursor = page["next_cursor"]
        if cursor is None:
            return labels


async def test_fresh_labels_clear_and_cold_sibling_identity(responses, tmp_path):
    storage = LocalStorage(tmp_path / "labels")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-labels") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as source:
            source_id = source.id
            assert await source.set_stored_name("  Alpha 😀  ") == "Alpha 😀"
            assert source.id == source_id
            assert (await _names(source))[source_id] == "Alpha 😀"
            with pytest.raises(RuntimeError, match="empty"):
                await source.set_stored_name(" \n ")
            with pytest.raises(TypeError):
                await source.set_stored_name(cast(str, 1))
            assert (await _names(source))[source_id] == "Alpha 😀"
            branch_id = await source.fork_stored()
        async with agent.session(storage=storage, resume=branch_id) as branch:
            assert await branch.set_stored_name("Beta") == "Beta"
            assert branch.id == branch_id
            labels = await _names(branch)
            assert labels[source_id] == "Alpha 😀" and labels[branch_id] == "Beta"
        async with agent.session(storage=storage, resume=source_id) as source:
            assert (await _names(source))[source_id] == "Alpha 😀"
            assert await source.set_stored_name(None) is None
            assert source.id == source_id
        async with agent.session(storage=storage, resume=source_id) as source:
            labels = await _names(source)
            assert labels[source_id] is None and labels[branch_id] == "Beta"
    assert responses.requests == []


@pytest.mark.parametrize("approval", [False, True])
async def test_label_during_callback_and_approval_preserves_execution(
    responses, tmp_path, approval
):
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @function_tool(needs_approval=approval)
    async def gate() -> str:
        """Held synthetic label control."""
        entered.set()
        await release.wait()
        effects.append("completed")
        return "done"

    responses.respond(function_call("gate", {}, "label-gate"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic-active-label") as provider,
        Agent(instructions="Use gate.", tools=[gate], provider=provider).session(
            storage=LocalStorage(tmp_path / "active")
        ) as session,
    ):
        turn = await session.submit("hold")
        request = None
        try:
            if approval:
                request = await next_request(session.interactions(), gate)
            else:
                await entered.wait()
            assert await session.set_stored_name("During hold") == "During hold"
            assert (await _names(session))[session.id] == "During hold"
            assert effects == [] and entered.is_set() is not approval
        finally:
            release.set()
            if request is not None:
                await request.approve()
        await turn.get_result()
        assert effects == ["completed"]
    assert len(responses.requests) == 2


async def test_unsupported_name_storage_preserves_ordinary_execution(responses):
    responses.respond(assistant_output("still usable"))
    async with (
        NativeProvider(base_url=responses.url, api_key="synthetic-unsupported-label") as provider,
        Agent(instructions="Answer.", provider=provider).session() as session,
    ):
        identity = session.id
        with pytest.raises(RuntimeError, match="LocalStorage"):
            await session.set_stored_name("Rejected")
        assert session.id == identity
        assert (await (await session.submit("hello")).get_result()).final_output == "still usable"
    assert len(responses.requests) == 1


async def test_custom_store_rejection_has_no_storage_callbacks_or_effects(responses, monkeypatch):
    backend = MemorySessionStore()
    calls = []
    for method in ("acquire", "read", "commit", "flush", "release"):
        original = getattr(backend, method)

        def counted(*args, _name=method, _method=original):
            calls.append(_name)
            return _method(*args)

        monkeypatch.setattr(backend, method, counted)
    responses.respond(assistant_output("custom store usable"))
    async with (
        NativeProvider(base_url=responses.url, api_key="synthetic-custom-label") as provider,
        Agent(instructions="Answer.", provider=provider).session(
            storage=CustomStorage(backend)
        ) as session,
    ):
        identity = session.id
        before = await session.history_snapshot()
        callbacks_before = tuple(calls)
        with pytest.raises(RuntimeError, match="LocalStorage"):
            await session.set_stored_name("Rejected")
        assert tuple(calls) == callbacks_before
        assert responses.requests == [] and session.id == identity
        assert await session.history_snapshot() == before
        result = await (await session.submit("hello")).get_result()
        assert result.final_output == "custom store usable"
    assert len(responses.requests) == 1
