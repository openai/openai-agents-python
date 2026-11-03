"""Exercise native macOS command policy. Deployment offline isolation is a separate prerequisite."""

import asyncio
import hashlib
import json
import os
import platform
import shlex
import shutil
import sys
import tempfile
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, Environment, NativeProvider, NetworkPolicy, _native
from agents.state import SessionState
from tests.native_executor_fixtures import test_certificate
from tests.native_fixtures import ResponsesFixture
from tests.qualification.offline_ipc import gateway, public_command


async def qualify(root, fixture):
    assert sys.platform == "darwin" and platform.machine() == "arm64"
    assert Path(_native.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    for path in (Path(_native.__file__), fixture):
        with path.open("rb") as stream:
            print(path, hashlib.file_digest(stream, "sha256").hexdigest(), flush=True)
    workspace = root / "workspace"
    (workspace / ".git").mkdir(parents=True)
    protected = (workspace / ".git" / "protected", root / "protected")
    for path in protected:
        path.write_text("control")
    shutil.copyfile(Path(__file__).with_name("unix_socket_command.py"), workspace / "probe.py")
    body = b"job-local-control"
    seen = []

    async def respond(reader, writer):
        # Count connections on accept, including any direct connect that sends no request.
        seen.append(writer.get_extra_info("sockname"))
        try:
            try:
                await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError:
                return
            headers = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            writer.write(headers.encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with AsyncExitStack() as stack:
        v4 = await stack.enter_async_context(await asyncio.start_server(respond, "127.0.0.1", 0))
        v6 = await stack.enter_async_context(await asyncio.start_server(respond, "::1", 0))
        unix = workspace / "control.sock"
        await stack.enter_async_context(await asyncio.start_unix_server(respond, unix))
        v4_address, v6_address = (s.sockets[0].getsockname() for s in (v4, v6))
        ports = [v4_address[1], v6_address[1]]
        for connection in (
            asyncio.open_connection("127.0.0.1", ports[0]),
            asyncio.open_connection("::1", ports[1]),
            asyncio.open_unix_connection(unix),
        ):
            reader, writer = await connection
            writer.write(b"GET /control HTTP/1.0\r\n\r\n")
            await writer.drain()
            assert (await reader.read()).endswith(b"\r\n\r\n" + body)
            writer.close()
            await writer.wait_closed()
        expected = [v4_address, v6_address, str(unix)]
        assert seen == expected

        ca, tls = await test_certificate(root, "IP:127.0.0.1,DNS:localhost")
        os.environ["SSL_CERT_FILE"] = str(ca)
        url, starts = await stack.enter_async_context(gateway([fixture, "stdio"], tls, root))
        replies = ResponsesFixture()
        app = web.Application()
        app.router.add_post("/v1/responses", replies.handle)
        server = await stack.enter_async_context(TestServer(app))
        provider = NativeProvider(base_url=str(server.make_url("/v1")), api_key="fixture")
        stack.push_async_callback(provider.aclose)
        agent = Agent(instructions="Run the fixed job command.", provider=provider)
        restricted = Environment(
            url=url,
            cwd=str(workspace),
            headers={"Authorization": "Bearer job-fixture"},
            tools=["exec_command"],
        )
        permitted = replace(restricted, network=NetworkPolicy(allow_all_unix_sockets=True))
        domains = NetworkPolicy(
            allowed_domains=["*.0.0.1", "**.localhost"],
            denied_domains=["localhost"],
            allow_local_binding=True,
        )

        async def run(session, mode, *connections):
            before = len(starts)
            challenge = uuid4().hex
            command = shlex.join(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(workspace / "probe.py"),
                    mode,
                    challenge,
                    *map(str, ports),
                ]
            )
            output = await public_command(session, replies, command)
            assert json.dumps({"mode": mode, "challenge": challenge}) in output, output
            assert (workspace / "ordinary").read_text() == challenge
            assert all(path.read_text() == "control" for path in protected)
            assert len(starts) > before
            expected.extend(connections)
            assert seen == expected
            print(f"native macOS exec_command {mode} passed", flush=True)

        async with agent.session(environment=restricted) as session:
            await run(session, "restricted")
        async with agent.session(environment=permitted) as session:
            await run(session, "permitted", str(unix))
            saved = SessionState.from_json((await session.save_state()).to_json())
        assert saved.network_policy_digest
        before = len(starts)
        try:
            async with agent.session(state=saved, environment=restricted):
                raise AssertionError("mismatched restore admitted")
        except ValueError as error:
            assert "Checkpoint and Environment must agree" in str(error), error
        assert len(starts) == before
        async with agent.session(state=saved, environment=permitted) as session:
            await run(session, "permitted", str(unix))
        async with agent.session(environment=replace(restricted, network=domains)) as session:
            await run(session, "domains", v4_address, v4_address)
        denied = replace(domains, denied_domains=[*domains.denied_domains, "::ffff:7f00:1"])
        async with agent.session(environment=replace(restricted, network=denied)) as session:
            await run(session, "domains-deny-mapped", v4_address)
        deny_only = replace(domains, allowed_domains=())
        async with agent.session(environment=replace(restricted, network=deny_only)) as session:
            await run(session, "domains-deny-only")
        print(
            "Native macOS IPC policy and restore passed. Deployment isolation is unverified.",
            flush=True,
        )


if __name__ == "__main__":
    # Keep AF_UNIX pathnames below Darwin's 104-byte limit.
    with tempfile.TemporaryDirectory(prefix="ipc-", dir="/private/tmp") as directory:
        root = Path(directory).resolve()
        try:
            asyncio.run(
                asyncio.wait_for(qualify(root, Path(sys.argv[1]).resolve(strict=True)), 110)
            )
        except BaseException:
            log = root / "executor.log"
            if log.exists():
                print(log.read_text()[-6000:], file=sys.stderr)
            raise
