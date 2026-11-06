"""The native executor chooses its port without a parent-to-child bind race."""

import asyncio
import os
import socket
from urllib.parse import urlsplit

from tests import native_executor_fixtures as fixtures


async def test_executor_startup_does_not_reuse_a_parent_selected_port(tmp_path, monkeypatch):
    spawn = asyncio.create_subprocess_exec
    with socket.socket() as competitor:

        async def competing_spawn(*arguments, **kwargs):
            if arguments[0] == os.environ["CODEX_EXEC_SERVER_TEST_BINARY"]:
                # Take the actual requested port immediately before the real
                # child starts. Port zero instead selects an independent port.
                competitor.bind(("127.0.0.1", urlsplit(arguments[1]).port))
                competitor.listen()
            return await spawn(*arguments, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", competing_spawn)
        async with fixtures.ExecutorPeer(tmp_path / "remote").serve() as peer:
            assert competitor.getsockname()[1] > 0
            assert peer.url.startswith("wss://")
