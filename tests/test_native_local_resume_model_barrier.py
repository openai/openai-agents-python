"""Local resume must not publish a model read before another writer's update."""

import asyncio

import pytest

from agents import Agent, LocalStorage, NativeProvider, WebSearch
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_web_search import search_peer


async def test_hosted_resume_does_not_overwrite_intervening_writer_model(tmp_path, monkeypatch):
    from agents import _native

    async with timeout(35), search_peer() as (model, searches):
        storage = LocalStorage(tmp_path / "profile")
        provider = NativeProvider(base_url=model.url, api_key="resume-writer-snapshot")
        async with provider:
            source = Agent(instructions="Act", model="gpt-5.5", provider=provider)
            model.respond(assistant_output("original"))
            async with source.session(storage=storage) as session:
                identity = session.id
                assert (
                    await (await session.submit("first")).get_result()
                ).final_output == "original"

            ready, release = asyncio.Event(), asyncio.Event()
            create = _native.create_engine
            intercepted = False

            async def hold_create(*args, **kwargs):
                nonlocal intercepted
                engine = await create(*args, **kwargs)
                if not intercepted:
                    intercepted = True
                    ready.set()
                    await release.wait()
                return engine

            destination = Agent(
                instructions="Act", provider=provider, tools=[WebSearch(route="hosted")]
            )

            async def open_destination():
                async with destination.session(storage=storage, resume=identity):
                    pytest.fail("A stale hosted admission overwrote the latest writer's Lite model")

            with monkeypatch.context() as patch:
                patch.setattr(_native, "create_engine", hold_create)
                opening = asyncio.create_task(open_destination())
                try:
                    await ready.wait()
                    # The first opener has read the model but has not acquired
                    # its native writer. A second real owner can still commit.
                    latest = Agent(instructions="Act", model="gpt-5.6-sol", provider=provider)
                    model.respond(assistant_output("summarized"))
                    model.respond(assistant_output("latest"))
                    async with latest.session(storage=storage, resume=identity) as writer:
                        assert (
                            await (await writer.submit("change model")).get_result()
                        ).final_output == "latest"
                    assert model.requests[-1]["model"] == "gpt-5.6-sol"
                    release.set()
                    with pytest.raises((RuntimeError, ValueError), match="hosted"):
                        await opening
                finally:
                    release.set()
                    if not opening.done():
                        opening.cancel()
                    await asyncio.gather(opening, return_exceptions=True)
            assert not searches
            # A fresh implicit attempt sees the latest Lite model and refuses
            # hosted search before it can replace persisted settings.
            with pytest.raises((RuntimeError, ValueError), match="hosted"):
                async with destination.session(storage=storage, resume=identity):
                    pytest.fail("Fresh hosted resume ignored the latest Lite model")
            # Explicit override remains intentional, and can acquire the writer
            # after both refused attempts without inheriting a leaked lock.
            override = Agent(
                instructions="Act",
                model="gpt-5.5",
                provider=provider,
                tools=[WebSearch(route="hosted")],
            )
            model.respond(assistant_output("latest summary"))
            model.respond(assistant_output("explicit override"))
            async with override.session(storage=storage, resume=identity) as resumed:
                assert (
                    await (await resumed.submit("explicit")).get_result()
                ).final_output == "explicit override"
            assert model.requests[-1]["model"] == "gpt-5.5"
