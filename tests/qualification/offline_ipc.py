"""Fresh-wheel Linux qualification. The parent namespace contains only local controls."""

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import socket
import sys
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

import agents
from agents import Agent, Environment, NativeProvider, NetworkPolicy
from agents.state import SessionState
from tests.native_executor_fixtures import test_certificate
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call, tool_output


@asynccontextmanager
async def gateway(launch, tls, root):
    """Only JSON-RPC crosses the job boundary. The job never receives the TLS socket."""
    starts = []

    async def relay(request):
        if request.headers.get("Authorization") != "Bearer job-fixture":
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        with (root / "executor.log").open("ab") as errors:
            process = await asyncio.create_subprocess_exec(
                *launch,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=errors,
                close_fds=True,
                limit=4 * 1024 * 1024,
            )
            assert process.stdin and process.stdout
            methods = {}

            async def incoming():
                async for message in ws:
                    assert message.type == WSMsgType.TEXT
                    value = json.loads(message.data)
                    if "method" in value and "id" in value:
                        methods[value["id"]] = value["method"]
                    if value.get("method") == "process/start":
                        starts.append(value["params"])
                    process.stdin.write(message.data.encode() + b"\n")
                    await process.stdin.drain()

            async def outgoing():
                async for line in process.stdout:
                    value = json.loads(line)
                    result = value.get("result")
                    method = methods.pop(value.get("id"), None) if "method" not in value else None
                    if isinstance(result, dict):
                        info = (
                            result.get("environmentInfo")
                            if method == "initialize"
                            else (result if method == "environment/info" else None)
                        )
                        if info is not None:
                            expected_os = "macos" if sys.platform == "darwin" else "linux"
                            assert info["platformOs"] == expected_os
                            assert info["capabilities"]["networkProxyLaunch"] is True
                    await ws.send_json(value)

            tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/", relay)
    server = TestServer(app, scheme="https")
    await server.start_server(ssl=tls)
    try:
        yield str(server.make_url("/")).replace("https:", "wss:"), starts
    finally:
        await server.close()


def executor_command(root, helper, fixture, proxy_port):
    # Privileged provisioning ends before the exec-server reads its first RPC.
    return [
        "sudo",
        "-n",
        "unshare",
        "--mount",
        "--net",
        "--ipc",
        "--uts",
        "--pid",
        "--fork",
        "--kill-child",
        "--propagation",
        "private",
        "--",
        "/bin/bash",
        str(Path(__file__).with_name("offline_executor.sh")),
        str(root),
        str(helper),
        str(fixture),
        str(os.getuid()),
        str(os.getgid()),
        str(proxy_port),
    ]


async def public_command(session, responses, command, **extra):
    call = uuid4().hex
    responses.respond(
        function_call("exec_command", {"cmd": command, "yield_time_ms": 30000, **extra}, call)
    )
    responses.respond(assistant_output("complete"))
    assert (
        await (await session.submit("Run the job command.")).get_result()
    ).final_output == "complete"
    return tool_output(responses.requests[-1], call)


class DatagramPeer(asyncio.DatagramProtocol):
    def __init__(self):
        self.packets = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.packets.append(data)
        self.transport.sendto(data, addr)


async def qualify(root, fixture, dns_log):
    helper = (Path(agents.__file__).parent / "bin" / "codex-linux-sandbox").resolve(strict=True)
    assert helper.is_relative_to(Path(sys.prefix).resolve()), (
        "helper must come from installed wheel"
    )
    from agents import _native

    for binary in (helper, Path(_native.__file__), fixture):
        with binary.open("rb") as stream:
            print(binary, hashlib.file_digest(stream, "sha256").hexdigest(), flush=True)
    assert Path(_native.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    token = uuid4().hex
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    for name in ("outside", "image"):
        (root / name).mkdir()
    (root / "outside" / "positive").write_text(token)
    shutil.copyfile(Path(__file__).with_name("unix_socket_command.py"), workspace / "probe.py")
    ca, tls = await test_certificate(root, "IP:127.0.0.1,DNS:localhost")
    os.environ["SSL_CERT_FILE"] = str(ca)
    seen = []

    async def accept(reader, writer):
        index = len(seen)
        seen.append(None)
        seen[index] = await reader.read(64)
        writer.write(b"control")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    hidden = str(root / "host.sock")
    launcher_fd, peer_fd = socket.socketpair()
    with launcher_fd, peer_fd:
        launcher_fd.set_inheritable(True)
        launcher_fd.sendall(b"control")
        assert peer_fd.recv(32) == b"control"
        inherited = os.readlink(f"/proc/self/fd/{launcher_fd.fileno()}")
        async with AsyncExitStack() as stack:
            v4 = await stack.enter_async_context(await asyncio.start_server(accept, "127.0.0.1", 0))
            v6 = await stack.enter_async_context(await asyncio.start_server(accept, "::1", 0))
            await stack.enter_async_context(await asyncio.start_unix_server(accept, hidden))
            await stack.enter_async_context(await asyncio.start_unix_server(accept, "\0" + token))
            ports = [server.sockets[0].getsockname()[1] for server in (v4, v6)]
            loop = asyncio.get_running_loop()
            udp, datagrams = await loop.create_datagram_endpoint(
                DatagramPeer, local_addr=("127.0.0.1", ports[0])
            )
            stack.callback(udp.close)
            for connect in (
                asyncio.open_connection("127.0.0.1", ports[0]),
                asyncio.open_connection("::1", ports[1]),
                asyncio.open_unix_connection(hidden),
                asyncio.open_unix_connection("\0" + token),
            ):
                reader, writer = await connect
                writer.write(b"control")
                assert await reader.readexactly(7) == b"control"
                writer.close()
                await writer.wait_closed()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.setblocking(False)
                await loop.sock_sendto(sender, b"control", ("127.0.0.1", ports[0]))
                assert (await loop.sock_recvfrom(sender, 64))[0] == b"control"
            assert socket.gethostbyname(token + ".control.ipc.test") == "127.0.0.1"
            assert token + ".control.ipc.test" in dns_log.read_text()
            replies = ResponsesFixture()
            app = web.Application()
            app.router.add_post("/v1/responses", replies.handle)
            responses_server = await stack.enter_async_context(TestServer(app))
            provider = NativeProvider(
                base_url=str(responses_server.make_url("/v1")), api_key="fixture"
            )
            stack.push_async_callback(provider.aclose)
            agent = Agent(instructions="Use the selected job.", provider=provider)
            launch = executor_command(root, helper, fixture, ports[0])
            async with gateway(launch, tls, root) as (url, starts):
                restricted = Environment(
                    url=url,
                    cwd="/job",
                    tools=["exec_command"],
                    headers={"Authorization": "Bearer job-fixture"},
                )
                permitted = replace(restricted, network=NetworkPolicy(allow_all_unix_sockets=True))

                async def probe(session, mode):
                    challenge = uuid4().hex
                    args = [
                        "/usr/bin/python3",
                        "-I",
                        "/job/probe.py",
                        mode,
                        token,
                        challenge,
                        *map(str, ports),
                        hidden,
                        inherited,
                    ]
                    before = len(starts)
                    output = await public_command(session, replies, shlex.join(args))
                    assert len(starts) == before + 1, "expected one new process/start"
                    assert json.dumps({"mode": mode, "challenge": challenge}) in output, output
                    assert (workspace / "ordinary").read_text() == challenge
                    settings = starts[before].get("networkProxy")
                    if mode == "restricted":
                        assert settings is None
                    else:
                        policy = settings["proxy"]
                        assert policy["enabled"]
                        assert policy["dangerouslyAllowAllUnixSockets"] is True
                        assert not policy["domains"] and not policy["allowUpstreamProxy"]
                        assert not policy["allowLocalBinding"]
                    print("public exec_command", mode, challenge, flush=True)

                async with agent.session(environment=restricted) as session:
                    await probe(session, "restricted")
                    legacy = await session.save_state()
                assert "network_policy_digest" not in legacy.to_json()
                empty = replace(restricted, network=NetworkPolicy())
                before = len(starts)
                try:
                    async with agent.session(environment=empty):
                        raise AssertionError("Linux executor admitted an empty network policy")
                except RuntimeError as error:
                    assert "cannot enforce Unix-socket denial" in str(error), error
                assert len(starts) == before
                async with agent.session(environment=permitted) as session:
                    await probe(session, "permitted")
                    saved = SessionState.from_json((await session.save_state()).to_json())
                    assert saved.network_policy_digest
                    assert saved.root_metadata["approval_mode"] == {
                        "granular": {
                            "sandbox_approval": False,
                            "rules": True,
                            "skill_approval": True,
                            "request_permissions": True,
                            "mcp_elicitations": True,
                        }
                    }
                    count = len(starts)
                    await public_command(
                        session,
                        replies,
                        "touch /job/escalated",
                        sandbox_permissions="require_escalated",
                    )
                    assert len(starts) == count and not (workspace / "escalated").exists()
                for state, selected in [(saved, restricted), (saved, None), (legacy, permitted)]:
                    count = len(starts)
                    try:
                        async with agent.session(state=state, environment=selected):
                            raise AssertionError("mismatched restore admitted")
                    except ValueError as error:
                        assert "Checkpoint and Environment must agree" in str(error), error
                    assert len(starts) == count
                async with agent.session(state=saved, environment=permitted) as restored:
                    await probe(restored, "permitted")
                # Only the current declaration can grant access when history has been forged.
                forged = json.loads(saved.to_json())
                forged.pop("network_policy_digest")
                forged["agent_options"] = {"environment": permitted._to_native()}
                forged["root_metadata"]["permission_profile"] = {"type": "disabled"}
                contexts = [
                    item["payload"] for item in forged["history"] if item["type"] == "turn_context"
                ]
                assert contexts
                for context in contexts:
                    context["permission_profile"] = {"type": "disabled"}
                count = len(starts)
                async with AsyncExitStack() as forged_stack:
                    try:
                        restored = await forged_stack.enter_async_context(
                            agent.session(
                                state=SessionState.from_json(json.dumps(forged)),
                                environment=restricted,
                            )
                        )
                    except (ValueError, RuntimeError) as error:
                        assert len(starts) == count
                        assert any(
                            word in str(error).lower()
                            for word in ("checkpoint", "history", "permission", "sandbox")
                        ), error
                    else:
                        await probe(restored, "restricted")
            assert seen == [b"control"] * 4 and datagrams.packets == [b"control"]
            assert ".job.ipc.test" not in dns_log.read_text(), "DNS request escaped job"
            assert not (root / "outside" / "forbidden").exists()
            peer_fd.setblocking(False)
            with suppress(BlockingIOError):
                raise AssertionError(f"unexpected inherited socket traffic: {peer_fd.recv(64)!r}")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ipc-") as directory:
        local_root = Path(directory)
        try:
            asyncio.run(
                asyncio.wait_for(qualify(local_root, Path(sys.argv[1]), Path(sys.argv[2])), 150)
            )
        except BaseException:
            log = local_root / "executor.log"
            if log.exists():
                print(log.read_text()[-6000:], file=sys.stderr)
            raise
