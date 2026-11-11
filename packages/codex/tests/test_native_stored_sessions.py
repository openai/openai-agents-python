"""Native application-session discovery, without loading original history."""

import asyncio
import json

import pytest

from agents import Agent, ApprovalRequested, LocalStorage, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import next_request


async def _pages(session):
    cursor = None
    seen = set()
    while True:
        page = await session.stored_sessions(limit=1, cursor=cursor)
        assert len(page["sessions"]) <= 1
        yield cursor, page
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert cursor not in seen
        seen.add(cursor)


async def test_stored_sessions_page_cold_native_ids_and_fork_links(responses, tmp_path):
    storage = LocalStorage(tmp_path / "discovery")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-discovery") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as source:
            source_id = source.id
            fork_id = await source.fork_stored()
        async with agent.session(storage=storage) as other:
            other_id = other.id
            await other.stored_sessions()
        async with agent.session(storage=storage) as reader:
            records = [item async for _, page in _pages(reader) for item in page["sessions"]]
            assert {item["id"] for item in records} == {source_id, fork_id, other_id, reader.id}
            indexed = {item["id"]: item for item in records}
            assert indexed[fork_id]["forked_from_id"] == source_id
            assert indexed[source_id]["forked_from_id"] is None
            assert all(item["parent_thread_id"] is None for item in records)
            assert all(isinstance(item["created_at_ms"], int) for item in records)
            assert all(isinstance(item["updated_at_ms"], int) for item in records)
            created = [item["created_at_ms"] for item in records]
            assert created == sorted(created)
            assert (await reader.stored_sessions(archived=True))["sessions"] == []
            first = await reader.stored_sessions(limit=1)
            original_id = first["sessions"][0]["id"]
            first["sessions"][0]["id"] = "application-change"
            assert (await reader.stored_sessions(limit=1))["sessions"][0]["id"] == original_id
            with pytest.raises(ValueError, match="limit"):
                await reader.stored_sessions(limit=0)
            with pytest.raises(TypeError, match="archived"):
                await reader.stored_sessions(archived=1)
            with pytest.raises(RuntimeError, match="cursor") as failure:
                await reader.stored_sessions(cursor="SYNTHETIC_SECRET_CURSOR")
            assert "SYNTHETIC_SECRET_CURSOR" not in str(failure.value)
    assert responses.requests == []


@pytest.mark.parametrize("approval", [False, True])
async def test_stored_sessions_during_callback_and_approval(responses, tmp_path, approval):
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @function_tool(needs_approval=approval)
    async def gate() -> str:
        """Synthetic held callback."""
        entered.set()
        await release.wait()
        effects.append("completed")
        return "finished"

    responses.respond(function_call("gate", {}, "discovery-gate"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic-discovery-active") as provider,
        Agent(instructions="Use gate.", tools=[gate], provider=provider).session(
            storage=LocalStorage(tmp_path / "active")
        ) as session,
    ):
        turn = await session.submit("hold")
        request = None
        try:
            if approval:
                request = await next_request(session.interactions(), gate)
                assert isinstance(request, ApprovalRequested)
            else:
                await entered.wait()
            page = await session.stored_sessions()
            assert [item["id"] for item in page["sessions"]] == [session.id]
            assert effects == []
        finally:
            release.set()
            if request is not None:
                await request.approve()
        assert (await turn.get_result()).final_output == "done"
    assert effects == ["completed"]
    assert len(responses.requests) == 2


def _rollout(home, identity):
    for path in home.rglob("*.jsonl"):
        with path.open("rb") as stream:
            header = json.loads(stream.readline())
        if header.get("type") == "session_meta" and header["payload"]["id"] == identity:
            return path
    raise AssertionError("Native stored rollout missing")


async def test_stored_sessions_reject_bad_header_and_skip_deleted_entry(responses, tmp_path):
    home = tmp_path / "headers"
    storage = LocalStorage(home)
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-discovery-header"
    ) as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as source:
            source_id = source.id
            fork_id = await source.fork_stored()
        async with agent.session(storage=storage) as reader:
            pages = [pair async for pair in _pages(reader)]
            cursor, original = next(
                pair for pair in pages if any(item["id"] == fork_id for item in pair[1]["sessions"])
            )
            assert original["sessions"][0]["forked_from_id"] == source_id
            path = _rollout(home, fork_id)
            retained = path.read_bytes()
            first, _, remaining = retained.partition(b"\n")
            header = json.loads(first)
            header["payload"]["id"] = "SYNTHETIC_HEADER_SECRET"
            try:
                path.write_bytes(json.dumps(header).encode() + b"\n" + remaining)
                with pytest.raises(RuntimeError, match="metadata") as failure:
                    await reader.stored_sessions(limit=1, cursor=cursor)
                assert "SYNTHETIC_HEADER_SECRET" not in str(failure.value)
            finally:
                path.write_bytes(retained)
            assert (await reader.stored_sessions(limit=1, cursor=cursor)) == original
            hidden = path.with_suffix(".hidden")
            path.rename(hidden)
            try:
                missing = await reader.stored_sessions(limit=1, cursor=cursor)
                assert missing["sessions"] == []
                assert missing["next_cursor"] == original["next_cursor"]
            finally:
                hidden.rename(path)
            assert (await reader.stored_sessions(limit=1, cursor=cursor)) == original
    assert responses.requests == []


async def test_stored_sessions_unsupported_store_remains_usable(responses):
    responses.respond(assistant_output("usable"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-discovery-memory"
    ) as provider:
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            with pytest.raises(RuntimeError, match="unsupported|LocalStorage"):
                await session.stored_sessions()
            assert (await (await session.submit("answer")).get_result()).final_output == "usable"
    assert len(responses.requests) == 1


async def test_stored_sessions_fresh_listing_excludes_rolled_back_fork(responses, tmp_path):
    from tests.test_native_stored_fork import _ForkDelivery

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-discovery-rollback"
    ) as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=LocalStorage(tmp_path / "rollback")) as source:
            delivery = _ForkDelivery(
                source._native, asyncio.Event(), asyncio.Event(), fail_delivery=True
            )
            source._native = delivery
            with pytest.raises(RuntimeError, match="delivery failure"):
                await source.fork_stored()
            assert delivery.fork_id is not None
            records = [item async for _, page in _pages(source) for item in page["sessions"]]
            assert source.id in {item["id"] for item in records}
            assert delivery.fork_id not in {item["id"] for item in records}
    assert responses.requests == []
