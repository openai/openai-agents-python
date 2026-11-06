"""Public lifetime gates; local default substitution tests ownership, not routing."""

from __future__ import annotations

import asyncio
import json
import pickle
from contextlib import asynccontextmanager
from copy import copy, deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeProvider, _native
from tests._asyncio import timeout

TEXT = "PROVIDER_RESOURCE_OK"


@dataclass
class Peer:
    url: str = ""
    requests: list[dict[str, Any]] = field(default_factory=list)
    transports: list[Any] = field(default_factory=list)
    active_handlers: int = 0
    bodies_finished: int = 0

    async def handle(self, request: web.Request) -> web.Response:
        self.active_handlers += 1
        try:
            if request.transport not in self.transports:
                self.transports.append(request.transport)
            body = await request.json()
            assert body["model"] == "gpt-5.1-codex"
            assert type(body["stream"]) is bool
            self.requests.append(
                {"body": body, "authorization": request.headers.get("Authorization")}
            )
            number = len(self.requests)
            item = {
                "type": "message",
                "id": f"msg_{number}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": TEXT, "annotations": []}],
            }
            response = {
                "id": f"resp_{number}",
                "status": "completed",
                "output": [item],
                "usage": {
                    "input_tokens": 11,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 3,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 14,
                },
            }
            if body["stream"] is False:
                assert request.headers.get("Accept") == "application/json"
                self.bodies_finished += 1
                return web.json_response(response)
            events = [
                {
                    "type": "response.created",
                    "response": {
                        "id": response["id"],
                        "status": "in_progress",
                        "output": [],
                    },
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": dict(item, content=[]),
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "delta": TEXT,
                },
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            self.bodies_finished += 1
            return web.Response(
                body="".join("data: " + json.dumps(e) + "\n\n" for e in events).encode(),
                content_type="text/event-stream",
            )
        finally:
            self.active_handlers -= 1


@asynccontextmanager
async def peer():
    value = Peer()
    app = web.Application()
    app.router.add_post("/v1/responses", value.handle)
    async with TestServer(app) as server:
        value.url = str(server.make_url("/v1"))
        yield value


class _TaggedProvider(NativeProvider):
    __slots__ = ("tag",)


def _pickle_copy(provider):
    return pickle.loads(pickle.dumps(provider))


@pytest.mark.asyncio
@pytest.mark.parametrize("clone", [copy, deepcopy, _pickle_copy])
@pytest.mark.parametrize("provider_type", [NativeProvider, _TaggedProvider])
async def test_used_provider_copy_owns_independent_resources(clone, provider_type):
    async with peer() as remote:
        provider = provider_type(base_url=remote.url, api_key="local", supports_websockets=False)
        if isinstance(provider, _TaggedProvider):
            object.__setattr__(provider, "tag", ["slot metadata"])
            object.__setattr__(provider, "annotation", ["dictionary metadata"])
        agent = Agent(instructions="answer", model="gpt-5.1-codex", provider=provider)
        duplicate = None
        try:
            async with agent.session():
                pass
            duplicate = clone(provider)
            assert type(duplicate) is type(provider)
            assert duplicate is not provider and duplicate == provider
            if isinstance(provider, _TaggedProvider):
                for name in ("tag", "annotation"):
                    assert getattr(duplicate, name) == getattr(provider, name)
                    assert (getattr(duplicate, name) is getattr(provider, name)) == (clone is copy)
            copied_agent = Agent(instructions="answer", model="gpt-5.1-codex", provider=duplicate)
            async with copied_agent.session() as session:
                handle = await session.submit("copied provider")
                assert (await (handle).get_result()).final_output == TEXT
            await duplicate.aclose()
            async with agent.session() as session:
                handle = await session.submit("original survives")
                assert (await (handle).get_result()).final_output == TEXT
            assert len(remote.requests) == 2
        finally:
            if duplicate is not None:
                await duplicate.aclose()
            await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("clone", [copy, deepcopy, _pickle_copy])
async def test_closed_provider_copy_stays_closed(clone):
    provider = NativeProvider(api_key="local", supports_websockets=False)
    await provider.aclose()
    duplicate = clone(provider)
    try:
        with pytest.raises(RuntimeError, match="closed"):
            async with Agent(instructions="answer", provider=duplicate).session():
                pytest.fail("a copied closed provider reopened")
    finally:
        await duplicate.aclose()


@pytest.mark.asyncio
async def test_default_providers_are_not_allocated_by_agent_declarations():
    a = Agent(instructions="a")
    b = Agent(instructions="b")
    assert a.provider is None
    assert b.provider is None
    c = Agent(instructions="c")
    assert c.provider is None


@pytest.mark.asyncio
async def test_shared_explicit_owner_keeps_fresh_histories_and_explicit_shutdown():
    async with peer() as remote:
        provider = NativeProvider(base_url=remote.url, api_key="local", supports_websockets=False)
        closed_sessions = []
        ids = []
        for index in range(3):
            agent = Agent(instructions=f"agent-{index}", model="gpt-5.1-codex", provider=provider)
            assert agent.provider is provider
            async with agent.session() as session:
                ids.append(session.id)
                handle = await session.submit(f"input-{index}")
                assert (await (handle).get_result()).final_output == TEXT
                with pytest.raises(RuntimeError, match="open or opening"):
                    await provider.aclose()
            closed_sessions.append(session)
        assert len(set(ids)) == 3
        assert all(session._closed for session in closed_sessions)
        await asyncio.gather(provider.aclose(), provider.aclose())
        async with timeout(5):
            while any(not transport.is_closing() for transport in remote.transports):
                await asyncio.sleep(0.001)
        assert len(remote.requests) == 3
        assert "input-0" not in str(remote.requests[-1]["body"]["input"])
        assert NativeProvider(base_url=remote.url, api_key="local") is not provider


@pytest.mark.asyncio
@pytest.mark.parametrize("output_type", [None, int], ids=["text", "structured"])
async def test_busy_close_covers_plain_and_structured_without_poisoning(output_type):
    async with peer() as remote:
        provider = NativeProvider(base_url=remote.url, api_key="local", supports_websockets=False)
        agent = Agent(
            instructions="answer",
            model="gpt-5.1-codex",
            provider=provider,
            output_type=output_type,
        )
        async with agent.session() as session:
            with pytest.raises(RuntimeError, match="open or opening"):
                await provider.aclose()
            assert [entry async for entry in session.items()] == []
        await provider.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            async with agent.session():
                pytest.fail("closed Provider reopened")


@pytest.mark.asyncio
async def test_cancelled_open_keeps_lease_until_real_acquisition_joins(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = _native.create_engine
    owners = []

    def held(*args, **kwargs):
        operation = original(*args, **kwargs)

        async def run():
            owner = await operation
            owners.append(owner)
            entered.set()
            await release.wait()
            return owner

        return run()

    monkeypatch.setattr(_native, "create_engine", held)
    async with peer() as remote:
        provider = NativeProvider(base_url=remote.url, api_key="local", supports_websockets=False)
        agent = Agent(instructions="answer", model="gpt-5.1-codex", provider=provider)
        context = agent.session()
        opening = asyncio.create_task(context.__aenter__())
        await entered.wait()
        with pytest.raises(RuntimeError, match="open or opening"):
            await provider.aclose()
        opening.cancel()
        await asyncio.sleep(0)
        opening.cancel()
        await asyncio.sleep(0)
        assert not opening.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert len(owners) == 1
        with pytest.raises(RuntimeError, match="closed"):
            await owners[0].start_thread()
        await provider.aclose()
        assert not remote.requests


@pytest.mark.asyncio
async def test_failed_session_close_retains_lease_for_retry(monkeypatch):
    async with peer() as remote:
        provider = NativeProvider(base_url=remote.url, api_key="local", supports_websockets=False)
        agent = Agent(instructions="answer", model="gpt-5.1-codex", provider=provider)
        context = agent.session()
        session = await context.__aenter__()
        original = _native.Engine.close
        calls = 0

        async def fail_one_owned_close(engine):
            nonlocal calls
            if engine is session._engine:
                calls += 1
                if calls == 1:
                    raise RuntimeError("controlled close failure")
            await original(engine)

        monkeypatch.setattr(_native.Engine, "close", fail_one_owned_close)
        with pytest.raises(RuntimeError, match="controlled close failure"):
            await session._aclose()
        with pytest.raises(RuntimeError, match="open or opening"):
            await provider.aclose()
        await session._aclose()
        await context.__aexit__(None, None, None)
        await provider.aclose()


@pytest.mark.asyncio
async def test_provider_across_threads_and_fresh_event_loops(monkeypatch):
    original = _native.create_engine
    owners = []

    async def capture(*args, **kwargs):
        owner = await original(*args, **kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(_native, "create_engine", capture)
    async with peer() as remote:
        provider = NativeProvider(base_url=remote.url, api_key="local", supports_websockets=False)

        def run_one(index):
            agent = Agent(instructions=f"thread-{index}", model="gpt-5.1-codex", provider=provider)
            return agent.run_sync(f"input-{index}").final_output

        assert await asyncio.gather(*(asyncio.to_thread(run_one, i) for i in range(2))) == [
            TEXT,
            TEXT,
        ]
        assert len(owners) == 2
        for owner in owners:
            with pytest.raises(RuntimeError, match="closed"):
                await owner.start_thread()
        await provider.aclose()
        assert len(remote.requests) == 2


@pytest.mark.asyncio
async def test_failed_open_cleanup_retains_exact_owner_and_lease(monkeypatch):
    original = _native.create_engine
    owners = []

    class FailedPublication:
        def __init__(self, owner):
            self.owner = owner
            self.close_calls = 0

        def __getattr__(self, name):
            if name == "_start_initial_replaceable_thread":
                raise RuntimeError("controlled publication failure")
            return getattr(self.owner, name)

        async def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("controlled cleanup failure")
            await self.owner.close()

    def factory(*args, **kwargs):
        operation = original(*args, **kwargs)

        async def acquire():
            owner = await operation
            owners.append(owner)
            return FailedPublication(owner)

        return acquire()

    monkeypatch.setattr(_native, "create_engine", factory)
    async with peer() as remote:
        provider = NativeProvider(base_url=remote.url, api_key="local", supports_websockets=False)
        agent = Agent(instructions="answer", model="gpt-5.1-codex", provider=provider)
        with pytest.raises(RuntimeError, match="controlled publication failure") as caught:
            async with agent.session():
                pytest.fail("failed publication entered")
        assert not hasattr(caught.value, "_session_cleanup")
        del caught
        await provider.aclose()
        assert len(owners) == 1
        with pytest.raises(RuntimeError, match="closed"):
            await owners[0].start_thread()
        assert not remote.requests


@pytest.mark.asyncio
async def test_provider_retries_native_orphan_home_after_failed_core_open(monkeypatch):
    """Real native cleanup failure, with no private exception recovery handle."""
    import json

    original = _native.create_engine
    saved = {}

    class FailThreadPublication:
        def __init__(self, engine):
            self.engine = engine

        def _start_initial_replaceable_thread(self, *args):
            raise RuntimeError("controlled thread publication failure")

    def create(config, runtime, **kwargs):
        operation = original(config, runtime, **kwargs)

        async def acquire():
            engine = await operation
            home = Path(json.loads(runtime)["codex_home"])
            backup = home.with_name(home.name + "-held")
            await asyncio.to_thread(home.rename, backup)
            await asyncio.to_thread(home.write_text, "block directory cleanup")
            saved.update(home=home, backup=backup)
            return FailThreadPublication(engine)

        return acquire()

    monkeypatch.setattr(_native, "create_engine", create)
    async with peer() as remote:
        provider = NativeProvider(base_url=remote.url, api_key="local", supports_websockets=False)
        agent = Agent(
            instructions="answer",
            model="gpt-5.1-codex",
            provider=provider,
            output_type=int,
        )
        try:
            with pytest.raises(RuntimeError, match="controlled thread publication failure"):
                async with agent.session():
                    pytest.fail("failed Core open entered")
            with pytest.raises(RuntimeError) as failed_close:
                await provider.aclose()
            assert "directory" in str(failed_close.value).lower()
            assert not remote.requests
        finally:
            saved["home"].unlink()
            saved["backup"].rename(saved["home"])
        await provider.aclose()
        assert not saved["home"].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_mcp_policy", [False, True])
async def test_reader_arm_failure_joins_unpublished_session_before_provider_close(
    monkeypatch,
    with_mcp_policy,
):
    from agents import HttpMCP, Session
    from tests.test_native_mcp_tools import Peer as MCPPeer

    original = Session._start_readers
    readers = []

    def fail_after_readers(session):
        original(session)
        readers.append(session._reader)
        if session._mcp_reader is not None:
            readers.append(session._mcp_reader)
        if session._interception is not None:
            readers.append(session._interception.reader)
        raise RuntimeError("controlled reader arm failure")

    monkeypatch.setattr(Session, "_start_readers", fail_after_readers)
    mcp = MCPPeer()
    async with peer() as remote, mcp.serve() as url:
        provider = NativeProvider(base_url=remote.url, api_key="local", supports_websockets=False)
        tools = (
            [
                HttpMCP(
                    name="docs",
                    url=url,
                    tools=["search"],
                    transform_result=lambda context, result: result,
                )
            ]
            if with_mcp_policy
            else []
        )
        agent = Agent(
            instructions="answer",
            model="gpt-5.1-codex",
            provider=provider,
            tools=tools,
            programmatic_tool_calling=with_mcp_policy,
        )
        try:
            with pytest.raises(RuntimeError, match="controlled reader arm failure"):
                async with agent.session():
                    pytest.fail("partially armed Session entered")
            await provider.aclose()
            # No MCP policy reader belongs to a cold, unadmitted server.
            assert len(readers) == 1
            assert all(reader.done() for reader in readers)
            assert not remote.requests
            assert not mcp.requests and not mcp.calls
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
