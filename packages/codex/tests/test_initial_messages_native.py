from __future__ import annotations

import asyncio
import os
import threading

import pytest

from agents import Agent, CustomStorage, NativeProvider, SQLiteSessionStore
from tests.native_fixtures import responses as responses

SEEDS = [
    {"role": "assistant", "content": ""},
    {"role": "assistant", "content": "duplicate"},
    {"role": "user", "content": [{"type": "input_text", "text": "duplicate"}]},
]


def messages(snapshot):
    return [
        item
        for item in snapshot.items
        if item.get("type") == "message" and item.get("role") in {"user", "assistant"}
    ]


async def test_initial_messages_portable_round_trip_has_no_turn_or_request(responses):
    async with NativeProvider(base_url=responses.url, api_key="synthetic-seeds") as provider:
        agent = Agent(instructions="Keep the conversation.", provider=provider)
        async with agent.session(initial_messages=SEEDS) as session:
            initial = messages(await session.model_context())
            assert [item["role"] for item in initial] == ["assistant", "assistant", "user"]
            assert [item["content"][0]["text"] for item in initial] == [
                "",
                "duplicate",
                "duplicate",
            ]
            assert len({item["id"] for item in initial}) == 3
            assert [turn async for turn in session.turns()] == []
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            assert messages(await restored.model_context()) == initial
            assert [turn async for turn in restored.turns()] == []
        assert responses.requests == []


class UnopenedStore:
    """Portable spy: these validation paths must never acquire any storage."""

    acquisitions = 0

    def acquire(self, thread_id):
        self.acquisitions += 1
        raise AssertionError("invalid seed opening acquired storage")

    def unexpected(self, *args):
        raise AssertionError("invalid seed opening touched storage")

    read = commit = flush = release = unexpected


class SeedFenceStore(SQLiteSessionStore):
    def __init__(self, path, *, fail=False):
        super().__init__(path)
        self.seed_committed = False
        self.entered = threading.Event()
        self.allow = threading.Event()
        self.released = threading.Event()
        self.fail = fail
        self.fired = False
        self.thread_id = None
        self.seed_fence = None

    def commit(self, thread_id, token, expected_revision, write_id, payload):
        super().commit(thread_id, token, expected_revision, write_id, payload)
        if "seed-barrier-sentinel" in payload:
            self.seed_committed = True
            self.thread_id = thread_id
            self.seed_fence = token

    def flush(self, thread_id, token):
        if self.seed_committed and not self.fired:
            self.fired = True
            self.entered.set()
            if not self.allow.wait(15):
                raise TimeoutError("seed fence was not released")
            if self.fail:
                raise OSError("injected seed flush failure")
        return super().flush(thread_id, token)

    def release(self, thread_id, token):
        super().release(thread_id, token)
        if thread_id == self.thread_id and token == self.seed_fence:
            self.released.set()


@pytest.mark.skipif(os.name != "posix", reason="Reference adapter requires POSIX locks")
@pytest.mark.parametrize("cancel", [False, True])
async def test_initial_messages_opening_waits_for_real_flush_and_cancel_joins(
    tmp_path, responses, cancel
):
    store = SeedFenceStore(tmp_path / "seed.sqlite")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-seeds") as provider:
        agent = Agent(instructions="Keep the conversation.", provider=provider)
        manager = agent.session(
            storage=CustomStorage(store),
            initial_messages=[{"role": "user", "content": "seed-barrier-sentinel"}],
        )
        opening = asyncio.create_task(manager.__aenter__())
        try:
            assert await asyncio.to_thread(store.entered.wait, 10)
            assert not opening.done()
            if cancel:
                opening.cancel()
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(opening), 0.05)
                assert not store.released.is_set()
            store.allow.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(opening, 10)
                assert store.released.is_set()
            else:
                session = await asyncio.wait_for(opening, 10)
                assert (
                    messages(await session.model_context())[0]["content"][0]["text"]
                    == "seed-barrier-sentinel"
                )
                await manager.__aexit__(None, None, None)
        finally:
            store.allow.set()
            if not opening.done():
                opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
        assert responses.requests == []


@pytest.mark.skipif(os.name != "posix", reason="Reference adapter requires POSIX locks")
async def test_initial_messages_failed_flush_does_not_publish_or_replay(tmp_path, responses):
    store = SeedFenceStore(tmp_path / "seed.sqlite", fail=True)
    store.allow.set()
    async with NativeProvider(base_url=responses.url, api_key="synthetic-seeds") as provider:
        agent = Agent(instructions="Keep the conversation.", provider=provider)
        with pytest.raises(RuntimeError, match="Canonical storage failed"):
            async with agent.session(
                storage=CustomStorage(store),
                initial_messages=[{"role": "user", "content": "seed-barrier-sentinel"}],
            ):
                pytest.fail("a Session escaped the failed seed barrier")
        assert store.fired and store.released.is_set()
        # The real SQLite commit succeeded before the injected flush failure.
        # Explicit recovery reads that accepted prefix, without resubmitting seeds.
        async with agent.session(storage=CustomStorage(store), resume=store.thread_id) as restored:
            seeds = messages(await restored.model_context())
            assert len(seeds) == 1
            assert seeds[0]["content"][0]["text"] == "seed-barrier-sentinel"
        assert responses.requests == []


async def test_initial_messages_inline_image_uses_canonical_preparation(responses):
    image = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    async with NativeProvider(base_url=responses.url, api_key="synthetic-seeds") as provider:
        agent = Agent(instructions="Keep the conversation.", model="gpt-5.5", provider=provider)
        async with agent.session(
            initial_messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "before-image"},
                        {"type": "input_image", "image_url": image},
                        {"type": "input_text", "text": "after-image"},
                    ],
                }
            ]
        ) as session:
            [seed] = messages(await session.model_context())
            assert seed["content"][0] == {"type": "input_text", "text": "before-image"}
            assert seed["content"][2] == {"type": "input_text", "text": "after-image"}
            assert seed["content"][1]["type"] == "input_image"
            # Detail-based preparation uses high processing quality internally,
            # while retaining the caller's omitted detail in canonical content.
            assert "detail" not in seed["content"][1]
            assert [turn async for turn in session.turns()] == []
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            assert messages(await restored.model_context()) == [seed]
        assert responses.requests == []


@pytest.mark.parametrize("opening", [{"resume": "existing"}, {"state": {}}])
async def test_initial_messages_public_open_rejects_empty_with_restore_before_storage(
    responses, opening
):
    store = UnopenedStore()
    async with NativeProvider(base_url=responses.url, api_key="synthetic-seeds") as provider:
        with pytest.raises(ValueError, match="fresh"):
            async with Agent(instructions="Keep the conversation.", provider=provider).session(
                storage=CustomStorage(store), initial_messages=[], **opening
            ):
                pytest.fail("invalid seed opening escaped")
        assert store.acquisitions == 0
        assert responses.requests == []


@pytest.mark.parametrize(
    "source",
    [
        {"file_id": "file-seed", "filename": "report.pdf"},
        {"file_data": "data:text/plain;base64,YQ==", "filename": "note.txt"},
        {"file_url": "https://example.invalid/report.pdf", "filename": "report.pdf"},
    ],
)
async def test_initial_messages_file_sources_round_trip_as_opaque_data(responses, source):
    part = {"type": "input_file", **source}
    async with NativeProvider(base_url=responses.url, api_key="synthetic-seeds") as provider:
        agent = Agent(instructions="Keep the conversation.", model="gpt-5.5", provider=provider)
        async with agent.session(initial_messages=[{"role": "user", "content": [part]}]) as session:
            [seed] = messages(await session.model_context())
            assert seed["content"] == [part]
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            assert messages(await restored.model_context()) == [seed]
        assert responses.requests == []


async def test_initial_messages_file_rejects_lite_before_thread_acquisition(responses):
    store = UnopenedStore()
    async with NativeProvider(base_url=responses.url, api_key="synthetic-seeds") as provider:
        agent = Agent(instructions="Keep the conversation.", model="gpt-6.1-sol", provider=provider)
        with pytest.raises(Exception, match="qualified standard Responses"):
            async with agent.session(
                storage=CustomStorage(store),
                initial_messages=[
                    {"role": "user", "content": [{"type": "input_file", "file_id": "file-seed"}]}
                ],
            ):
                pytest.fail("unsupported startup model accepted a file seed")
        assert store.acquisitions == 0
        assert responses.requests == []


async def test_initial_messages_empty_fresh_batch_creates_no_turn(responses):
    async with NativeProvider(base_url=responses.url, api_key="synthetic-seeds") as provider:
        async with Agent(instructions="Keep the conversation.", provider=provider).session(
            initial_messages=[]
        ) as session:
            assert messages(await session.model_context()) == []
            assert [turn async for turn in session.turns()] == []
        assert responses.requests == []


async def test_initial_file_retains_an_explicit_empty_filename(responses):
    content = {"type": "input_file", "file_id": "opaque-seed-file", "filename": ""}
    async with NativeProvider(base_url=responses.url, api_key="synthetic-seeds") as provider:
        agent = Agent(instructions="Keep the file.", model="gpt-5.5", provider=provider)
        async with agent.session(
            initial_messages=[{"role": "user", "content": [content]}]
        ) as session:
            [seed] = messages(await session.model_context())
            assert seed["content"] == [content]
        assert responses.requests == []
