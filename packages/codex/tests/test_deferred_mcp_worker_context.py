"""Exercise host callback admission through MCPTasksProvider under plain asyncio.run."""

import asyncio
import logging
import threading
from dataclasses import replace

import pytest

from tests import test_deferred_mcp_tasks as mcp


@pytest.mark.parametrize("require_task", [False, True], ids=["installed", "task-sensitive"])
def test_plain_asyncio_run_creates_and_observes(tmp_path, monkeypatch, require_task):
    if require_task:
        # Keep the regression active when the optional Sniffio package is absent.
        from anyio._core import _eventloop

        detect = _eventloop.current_async_library

        def detect_from_task():
            library = detect()
            if library == "asyncio" and asyncio.current_task() is None:
                return None
            return library

        monkeypatch.setattr(_eventloop, "current_async_library", detect_from_task)

    async def run():
        peer = mcp.Peer(tmp_path)
        claim = mcp.starting()
        async with mcp.serving(peer) as endpoint:
            provider = peer.provider(
                endpoint=endpoint, tool="render", authorize=mcp.authorize, timeout_seconds=2
            )
            created = await provider.start(claim, mcp.Args(pages=3))
            assert created.status == "pending" and created.receipt
            observed = await provider.observe(replace(claim, action="observe", update=created))
        assert observed.status == "completed" and observed.receipt == created.receipt
        assert peer.creates == 1
        assert peer.counts == {
            "server/discover": 2,
            "tools/list": 1,
            "tools/call": 1,
            "tasks/get": 1,
        }
        assert (tmp_path / "report.txt").read_text() == "page\n" * 3

    asyncio.run(run())


def test_cancel_before_host_admission(tmp_path, monkeypatch, caplog):
    async def run():
        peer = mcp.Peer(tmp_path)
        loop = asyncio.get_running_loop()
        host = threading.get_ident()
        posted = asyncio.Event()
        delayed = []
        authorized = []

        async def authorize(claim, action):
            authorized.append(action)
            return await mcp.authorize(claim, action)

        post = loop.call_soon_threadsafe

        def delay_first_post(callback, *args, context=None):
            if threading.get_ident() != host and not delayed:
                delayed.append(lambda: post(callback, *args, context=context))
                return post(posted.set)
            return post(callback, *args, context=context)

        async with mcp.serving(peer) as endpoint:
            provider = mcp.MCPTasksProvider(endpoint=endpoint, tool="render", authorize=authorize)
            before = asyncio.all_tasks()
            with monkeypatch.context() as patch:
                patch.setattr(loop, "call_soon_threadsafe", delay_first_post)
                sending = asyncio.create_task(provider.start(mcp.starting(), mcp.Args(pages=3)))
                try:
                    await asyncio.wait_for(posted.wait(), 3)
                finally:
                    sending.cancel("first")
                    with pytest.raises(asyncio.CancelledError):
                        await sending
            # Deliver the worker's first post after the owning exchange has closed.
            delayed[0]()
            await asyncio.sleep(0)
            assert asyncio.all_tasks() == before
        assert authorized == ["start"] and not peer.counts

    asyncio.run(run())
    assert not any(r.name == "asyncio" and r.levelno >= logging.ERROR for r in caplog.records)
