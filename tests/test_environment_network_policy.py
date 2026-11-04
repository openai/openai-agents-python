"""Command network declarations and checkpoint restore authority."""

from __future__ import annotations

import shlex
import sys
from contextlib import AsyncExitStack
from dataclasses import FrozenInstanceError, replace
from functools import partial
from pathlib import Path
from typing import Any, Literal

import pytest
from aiohttp import ClientTimeout, web
from aiohttp.test_utils import RawTestServer, TestClient

import agents
from agents import Agent, Environment, NetworkPolicy
from agents._options import RuntimeNetworkPolicy
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_provider_websockets import peer as model_peer


def test_empty_policy_is_distinct_from_the_default() -> None:
    assert "NetworkPolicy" in agents.__all__
    default = Environment(url="wss://executor.invalid", cwd="/job", tools=["exec_command"])
    assert default.network is None
    assert "network" not in default._to_native()
    empty = replace(default, network={})
    assert empty.network == NetworkPolicy()
    assert empty._to_native()["network"] == {
        "allowed_domains": [],
        "denied_domains": [],
        "allow_all_unix_sockets": False,
        "allow_local_binding": False,
    }


@pytest.mark.parametrize("typed", [True, False], ids=["policy", "dictionary"])
def test_environment_captures_network_policy(typed: bool) -> None:
    allowed = ["api.example.invalid", "BLOCKED.example.invalid."]
    denied = ["blocked.example.invalid"]
    fields: RuntimeNetworkPolicy = {
        "allowed_domains": allowed,
        "denied_domains": denied,
        "allow_local_binding": True,
    }
    policy = NetworkPolicy(**fields) if typed else fields
    if typed:
        allowed.append("changed-before-environment.example.invalid")
    selected = Environment(
        url="wss://executor.invalid",
        cwd="/job",
        tools=["exec_command"],
        network=policy,
    )
    allowed.append("later.example.invalid")
    denied.clear()
    fields["allow_local_binding"] = False
    assert isinstance(selected.network, NetworkPolicy)
    with pytest.raises(FrozenInstanceError):
        selected.network.allow_local_binding = False  # type: ignore[misc]
    assert selected._to_native()["network"] == {
        "allowed_domains": ["api.example.invalid"],
        "denied_domains": ["blocked.example.invalid"],
        "allow_all_unix_sockets": False,
        "allow_local_binding": True,
    }


@pytest.mark.parametrize("address", ["::ffff:192.0.2.1", "::ffff:c000:201", "[::ffff:c000:201]"])
def test_mapped_ipv6_policy_has_stable_wire_and_checkpoint_identity(address: str) -> None:
    environment = Environment(
        url="wss://executor.invalid",
        cwd="/job",
        tools=["exec_command"],
        network=NetworkPolicy(allowed_domains=["*.*"], denied_domains=[address]),
    )
    assert environment._to_native()["network"] == {
        "allowed_domains": ["*.*"],
        "denied_domains": ["::ffff:192.0.2.1"],
        "allow_all_unix_sockets": False,
        "allow_local_binding": False,
    }
    assert (
        environment._network_policy_digest()
        == "a7ffe370b28f51bcf208970d80428d867bb55e4462627e7d8d95c85531b15ae8"
    )
    denied = NetworkPolicy(allowed_domains=[address], denied_domains=["::ffff:192.0.2.1"])
    assert denied.allowed_domains == ()


@pytest.mark.parametrize(
    "network",
    [
        ["api.example.invalid"],
        {"proxy_port": 8080},
        {"allowed_domains": "api.example.invalid"},
        {"allowed_domains": ["https://api.example.invalid/job"]},
        {"allowed_domains": ["api.example.invalid:443"]},
        {"allowed_domains": ["::ffff:192.0.2.1%eth0"]},
        {"allowed_domains": ["*.[api].example.invalid"]},
        {"denied_domains": [True]},
        {"allow_local_binding": "false"},
    ],
)
def test_invalid_policy_rejected_at_declaration(network: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        Environment(
            url="wss://executor.invalid", cwd="/job", tools=["exec_command"], network=network
        )


@pytest.mark.parametrize("typed", [True, False], ids=["policy", "dictionary"])
@pytest.mark.parametrize("field", ["allowed_domains", "denied_domains"])
@pytest.mark.parametrize(
    "rule",
    [
        "\0private.example.invalid",
        "private.\0example.invalid",
        "private.example.invalid\0",
        "private.example.invalid\x1b",
        "private.example.invalid\x7f",
        "\tprivate.example.invalid",
        "private.exämple.invalid",
    ],
)
def test_malformed_host_rejected_at_declaration(
    rule: str, field: Literal["allowed_domains", "denied_domains"], typed: bool
) -> None:
    network: RuntimeNetworkPolicy = {"allowed_domains": ["*.example.invalid"]}
    network[field] = [rule]
    with pytest.raises(ValueError, match="ASCII host or host pattern") as failure:
        if typed:
            NetworkPolicy(**network)
        else:
            Environment(
                url="wss://executor.invalid", cwd="/job", tools=["exec_command"], network=network
            )
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("typed", [True, False], ids=["policy", "dictionary"])
@pytest.mark.parametrize("paths", [[], ["/run/job.sock"]], ids=["empty", "nonempty"])
def test_removed_socket_paths_rejected_at_declaration(typed: bool, paths: list[str]) -> None:
    network: Any = {"allow_unix_sockets": paths}
    with pytest.raises(TypeError, match="unexpected keyword argument 'allow_unix_sockets'"):
        if typed:
            NetworkPolicy(**network)
        else:
            Environment(
                url="wss://executor.invalid", cwd="/job", tools=["exec_command"], network=network
            )


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
async def test_checkpoint_requires_the_current_effective_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        provider_key = "synthetic-model-credential"
        agent = Agent(
            instructions="Answer.", provider=model.provider(websocket=False, key=provider_key)
        )
        policy = NetworkPolicy(
            allowed_domains=["b.example.invalid", "A.example.invalid.", "blocked.example.invalid"],
            denied_domains=["blocked.example.invalid", "::ffff:192.0.2.1"],
            allow_all_unix_sockets=True,
            allow_local_binding=True,
        )
        selected = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            network=policy,
        )
        async with agent.session(environment=selected) as session:
            model.respond(assistant_output("ready"))
            assert (await (await session.submit("Start.")).get_result()).final_output == "ready"
            encoded = (await session.save_state()).to_json()
            [receipt] = [turn["receipt"] async for turn in session.turns() if "receipt" in turn]
            assert receipt["executors"]
            network = receipt["executors"][0]["network_policy"]
            assert network is not None and network["domains"] is not None
            assert network["domains"]["a.example.invalid"] == "allow"
        for private_value in ("example.invalid", "192.0.2.1", executor.token, provider_key):
            assert private_value not in encoded
        saved = SessionState.from_json(encoded)
        equivalent = replace(
            policy,
            allowed_domains=["a.example.invalid", "b.example.invalid", "a.example.invalid"],
            denied_domains=["::ffff:c000:201", "blocked.example.invalid"],
        )
        async with agent.session(
            state=saved, environment=replace(selected, network=equivalent)
        ) as restored:
            assert restored.id == saved.source_session_id
            assert [turn["receipt"] async for turn in restored.turns() if "receipt" in turn] == [
                {**receipt, "executors": None}
            ]
        attempts = executor.attempts
        changes = [
            replace(policy, denied_domains=[]),
            replace(policy, allow_local_binding=False),
            replace(policy, allow_all_unix_sockets=False),
        ]
        for changed in changes:
            with pytest.raises(ValueError, match="Checkpoint and Environment must agree"):
                async with agent.session(
                    state=saved, environment=replace(selected, network=changed)
                ):
                    pytest.fail("Opened a session with a different network policy")
            assert executor.attempts == attempts


async def test_checkpoint_without_network_policy_restores_only_with_the_default() -> None:
    async with timeout(20), model_peer() as model:
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        async with agent.session() as session:
            encoded = (await session.save_state()).to_json()
        assert "network_policy_digest" not in encoded
        legacy = SessionState.from_json(encoded)
        async with agent.session(state=legacy) as restored:
            assert restored.id == legacy.source_session_id
        empty = Environment(
            url="wss://unused.invalid", cwd="/job", tools=["exec_command"], network={}
        )
        with pytest.raises(ValueError, match="Checkpoint and Environment must agree"):
            async with agent.session(state=legacy, environment=empty):
                pytest.fail("Opened a legacy checkpoint with an explicit policy")


_LINUX_NETWORK_PROBE = """\
import errno
import os
import socket
import sys
from contextlib import closing
from http.client import HTTPConnection
from urllib.parse import urlsplit

allowed, overlap, unlisted = sys.argv[1:]
proxy = urlsplit(os.environ["HTTP_PROXY"])
for url, expected in [(allowed, 200), (overlap, 403), (unlisted, 403)]:
    with closing(HTTPConnection(proxy.hostname, proxy.port, timeout=3)) as connection:
        connection.request("GET", url)
        response = connection.getresponse()
        assert response.status == expected, (url, response.status)
        if expected == 200:
            assert response.read() == b"127.0.0.1"

target = urlsplit(allowed)
try:
    with socket.create_connection((target.hostname, target.port), timeout=3):
        raise AssertionError("Direct connection to the allowed destination succeeded")
except OSError as error:
    assert error.errno in (
        errno.EPERM, errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH
    ), error
"""


@pytest.mark.requires_native_linux_sandbox
async def test_linux_network_policy_enforces_proxy_and_blocks_direct_access(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed, overlap, unlisted = "127.0.0.1", "127.0.0.2", "127.0.0.3"
    received: list[str] = []

    async def record(host: str, _request: web.BaseRequest) -> web.Response:
        received.append(host)
        return web.Response(text=host)

    async with (
        timeout(30),
        AsyncExitStack() as listeners,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        urls = []
        for host in (allowed, overlap, unlisted):
            server = RawTestServer(partial(record, host), host=host)
            control = await listeners.enter_async_context(TestClient(server))
            async with control.get("/", timeout=ClientTimeout(total=3)) as response:
                assert response.status == 200 and await response.text() == host
            urls.append(str(control.make_url("/")))
        received.clear()

        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        environment = Environment(
            url=executor.url,
            cwd=str(executor.root),
            headers={"Authorization": "Bearer " + executor.token},
            tools=["exec_command"],
            network=NetworkPolicy(
                allowed_domains=[allowed, overlap],
                denied_domains=[overlap],
                allow_all_unix_sockets=True,
            ),
        )
        command = shlex.join(["python3", "-I", "-c", _LINUX_NETWORK_PROBE, *urls])
        responses.respond(
            function_call("exec_command", {"cmd": command, "yield_time_ms": 20_000}, "probe")
        )
        responses.respond(assistant_output("done"))
        agent = Agent(instructions="Probe the network policy.", provider=provider(responses))
        await agent.run("Run the probe.", environment=environment)
        assert "Process exited with code 0" in tool_output(responses.requests[-1], "probe")
        assert received == [allowed]
