"""Check the native controller's approval and launch request. The peer executes no commands."""

import json
import sys
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from tests.native_executor_fixtures import test_certificate as make_certificate
from tests.native_fixtures import ResponsesFixture
from tests.qualification.offline_ipc import public_command

from agents import Agent, Environment, NativeProvider, NetworkPolicy, _native
from agents.state import SessionState


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_os", ["linux", "macos"])
async def test_unix_peer_approval(peer_os, tmp_path, monkeypatch):
    assert Path(_native.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    ca, tls = await make_certificate(tmp_path, "IP:127.0.0.1")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    info = {
        "platformOs": peer_os,
        "cwd": "file:///job",
        "shell": {"name": "bash", "path": "/bin/bash"},
        "capabilities": {"networkProxyLaunch": True},
    }
    starts = []

    async def peer(request):
        if request.headers.get("Authorization") != "Bearer fixture":
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            rpc = json.loads(message.data)
            if "id" not in rpc:
                continue
            method = rpc["method"]
            if method == "initialize":
                reply = {"result": {"sessionId": "fixture", "environmentInfo": info}}
            elif method == "environment/info":
                reply = {"result": info}
            else:
                if method == "process/start":
                    starts.append(rpc["params"])
                reply = {"error": {"code": -32000, "message": "protocol fixture refused " + method}}
            await ws.send_json({"id": rpc["id"], **reply})
        return ws

    replies = ResponsesFixture()
    app = web.Application()
    app.router.add_get("/", peer)
    app.router.add_post("/v1/responses", replies.handle)
    server = TestServer(app, scheme="https")
    async with AsyncExitStack() as stack:
        await server.start_server(ssl=tls)
        stack.push_async_callback(server.close)
        provider = NativeProvider(base_url=str(server.make_url("/v1")), api_key="fixture")
        stack.push_async_callback(provider.aclose)
        agent = Agent(instructions="Use the selected job.", provider=provider)
        # The protocol-only peer has no filesystem RPCs for strict write admission.
        environment = Environment(
            url=str(server.make_url("/").with_scheme("wss")),
            cwd="/job",
            headers={"Authorization": "Bearer fixture"},
            tools=["exec_command"],
            write_policy="legacy",
            network=NetworkPolicy(allow_all_unix_sockets=True),
        )
        with pytest.raises(RuntimeError, match="POSIX executor workspace"):
            async with agent.session(environment=replace(environment, cwd="C:/job")):
                pytest.fail("incompatible workspace admitted")
        assert not starts

        for policy in (NetworkPolicy(), NetworkPolicy(allowed_domains=["fixture.invalid"])):
            selected = replace(environment, network=policy)
            if peer_os == "linux":
                with pytest.raises(
                    RuntimeError,
                    match=r"cannot enforce Unix-socket denial.*allow_all_unix_sockets=True",
                ):
                    async with agent.session(environment=selected):
                        pytest.fail("Linux executor admitted a policy denying Unix sockets")
            else:
                async with agent.session(environment=selected):
                    pass
            assert not starts

        async def check_target_rejected(state=None):
            before = len(starts)
            with pytest.raises(
                RuntimeError, match="Linux or macOS executor with managed proxy launch"
            ):
                async with agent.session(state=state, environment=environment):
                    pytest.fail("unsupported executor admitted")
            assert len(starts) == before

        with monkeypatch.context() as patch:
            patch.setitem(info, "platformOs", "windows")
            await check_target_rejected()
            patch.delitem(info, "platformOs")
            await check_target_rejected()
        with monkeypatch.context() as patch:
            patch.delitem(info["capabilities"], "networkProxyLaunch")
            await check_target_rejected()

        async def check_approval(session):
            before = len(starts)
            command = "/usr/bin/python3 /job/probe.py"
            result = await public_command(session, replies, command)
            assert "protocol fixture refused process/start" in result, result
            (start,) = starts[before:]
            assert command in " ".join(start["argv"])
            sandbox = start["sandbox"]
            assert sandbox["workspaceRoots"] == ["file:///job"]
            assert sandbox["windowsSandboxLevel"] == "disabled"
            assert sandbox["permissions"]["type"] == "managed"
            assert sandbox["permissions"]["file_system"]["type"] == "restricted"
            assert sandbox["permissions"]["network"] == "restricted"
            assert start["enforceManagedNetwork"]
            policy = start["networkProxy"]["proxy"]
            assert policy["enabled"] and policy["dangerouslyAllowAllUnixSockets"]
            assert not policy["domains"] and not policy["allowUpstreamProxy"]
            denied = await public_command(
                session, replies, command, sandbox_permissions="require_escalated"
            )
            assert "cannot ask for escalated permissions" in denied, denied
            assert len(starts) == before + 1, "explicit escalation reached the executor"

        async with agent.session(environment=environment) as session:
            await check_approval(session)
            saved = SessionState.from_json((await session.save_state()).to_json())
        async with agent.session(state=saved, environment=environment) as restored:
            await check_approval(restored)

        info["capabilities"]["networkProxyLaunch"] = False
        await check_target_rejected(saved)
