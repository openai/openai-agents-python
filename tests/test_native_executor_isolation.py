"""Real native executor transport, policy and simultaneous-client boundaries."""

from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, Environment, NativeProvider
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer, test_certificate as create_certificate
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider


def trust_certificates(
    monkeypatch: pytest.MonkeyPatch, directory: Path, *certificates: Path
) -> None:
    bundle = directory / "trusted.pem"
    bundle.write_bytes(b"".join(path.read_bytes() for path in certificates))
    empty = directory / "empty-cert-directory"
    empty.mkdir()
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.setenv("SSL_CERT_DIR", str(empty))


@pytest.mark.parametrize("failure", ["wrong-host", "untrusted-ca", "forbidden"])
async def test_executor_open_rejects_transport_identity_and_access_failures(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    sentinel = local / "untouched.txt"
    sentinel.write_text("local content")
    monkeypatch.chdir(local)
    names = "DNS:wrong-host.invalid" if failure == "wrong-host" else "DNS:localhost,IP:127.0.0.1"
    async with (
        timeout(20),
        ExecutorPeer(tmp_path / "remote", auth_status=403, certificate_names=names).serve() as peer,
    ):
        assert peer.ca is not None
        trusted_ca = peer.ca
        if failure == "untrusted-ca":
            unrelated = tmp_path / "unrelated-ca"
            unrelated.mkdir()
            trusted_ca, _ = await create_certificate(unrelated, "DNS:localhost")
        trust_certificates(monkeypatch, tmp_path, trusted_ca)
        token = "rejected-executor-credential" if failure == "forbidden" else peer.token
        selected = Environment(
            url=peer.url,
            cwd=str(peer.root),
            headers={"Authorization": "Bearer " + token},
            tools=["exec_command"],
            connect_timeout=0.5,
        )
        with pytest.raises(RuntimeError, match="remote executor failed to become ready") as caught:
            async with Agent(
                instructions="Use only the selected executor.",
                tools=[],
                provider=provider(responses),
            ).session(environment=selected):
                pytest.fail("A failed remote executor opened a usable Session")

        diagnostic = "".join(traceback.format_exception(caught.value)) + repr(selected)
        assert peer.url not in diagnostic
        assert peer.token not in diagnostic
        assert token not in diagnostic
        assert responses.requests == []
        assert peer.requests == []
        assert sentinel.read_text() == "local content"
        assert set(local.iterdir()) == {sentinel}
        if failure == "forbidden":
            assert peer.attempts > 0
        else:
            assert peer.attempts == 0


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("escape", ["relative", "workdir", "symlink", "metadata"])
async def test_executor_writes_only_assigned_workspace_without_local_fallback(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escape: str
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    (local / "protected.txt").write_text("local content")
    monkeypatch.chdir(local)
    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as peer:
        assert peer.ca is not None
        trust_certificates(monkeypatch, tmp_path, peer.ca)
        protected = peer.root.parent / "protected.txt"
        arguments = {"cmd": "printf changed > ../protected.txt"}
        if escape == "workdir":
            arguments = {"cmd": "printf changed > protected.txt", "workdir": str(peer.root.parent)}
        elif escape == "symlink":
            (peer.root / "escape").symlink_to(protected)
            arguments = {"cmd": "printf changed > escape"}
        elif escape == "metadata":
            (peer.root / ".git").mkdir()
            protected = peer.root / ".git" / "protected.txt"
            arguments = {"cmd": "printf changed > .git/protected.txt"}
        protected.write_text("remote content")
        selected = Environment(
            url=peer.url,
            cwd=str(peer.root),
            headers={"Authorization": "Bearer " + peer.token},
            tools=["exec_command"],
        )
        responses.respond(function_call("exec_command", arguments, "write"))
        responses.respond(assistant_output("write denied"))
        async with Agent(
            instructions="Use only the selected executor.",
            tools=[],
            provider=provider(responses),
        ).session(environment=selected) as session:
            handle = await session.submit("Try the write.")
            assert (await handle.get_result()).final_output == "write denied"
            output = tool_output(responses.requests[-1], "write")
            assert "Process exited with code 1" in output
            assert (
                "permission denied" in output.lower() or "read-only file system" in output.lower()
            )
            assert protected.read_text() == "remote content"
            assert (local / "protected.txt").read_text() == "local content"
            assert peer.token not in (await session.save_state()).to_json()
        assert any(request.get("method") == "process/start" for request in peer.requests)


@pytest.mark.requires_native_linux_sandbox
async def test_simultaneous_executor_sessions_keep_credentials_roots_and_results_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = [ResponsesFixture(), ResponsesFixture()]
    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    model_keys = ["synthetic-model-one-credential", "synthetic-model-two-credential"]
    app = web.Application()
    for index, model in enumerate(models):
        app.router.add_post(f"/{index}/v1/responses", model.handle)

    async def hold_first_call(index: int, event: dict[str, Any]) -> None:
        if event.get("item", {}).get("call_id") == "same-call-id":
            entered[index].set()
            await release.wait()

    async def hold_one(event: dict[str, Any]) -> None:
        await hold_first_call(0, event)

    async def hold_two(event: dict[str, Any]) -> None:
        await hold_first_call(1, event)

    models[0].before_event = hold_one
    models[1].before_event = hold_two
    async with (
        timeout(30),
        TestServer(app) as model_server,
        ExecutorPeer(tmp_path / "one", token="synthetic-executor-one-credential").serve() as one,
        ExecutorPeer(tmp_path / "two", token="synthetic-executor-two-credential").serve() as two,
    ):
        assert one.ca is not None and two.ca is not None
        trust_certificates(monkeypatch, tmp_path, one.ca, two.ca)
        peers = [one, two]
        agents = []
        environments = []
        for index, (model, peer) in enumerate(zip(models, peers, strict=True)):
            model.url = str(model_server.make_url(f"/{index}/v1"))
            (peer.root / "tenant.txt").write_text(f"tenant-{index}")
            model.respond(
                function_call(
                    "exec_command",
                    {"cmd": "pwd; cat tenant.txt; printf accepted > result.txt"},
                    "same-call-id",
                )
            )
            model.respond(assistant_output(f"done-{index}"))
            agents.append(
                Agent(
                    instructions="Use only the selected executor.",
                    provider=NativeProvider(base_url=model.url, api_key=model_keys[index]),
                    tools=[],
                )
            )
            environments.append(
                Environment(
                    url=peer.url,
                    cwd=str(peer.root),
                    headers={"Authorization": "Bearer " + peer.token},
                    tools=["exec_command"],
                )
            )

        async with (
            agents[0].session(environment=environments[0]) as first,
            agents[1].session(environment=environments[1]) as second,
        ):
            handles = [await first.submit("Run."), await second.submit("Run.")]
            waits = [
                asyncio.create_task((handles[0]).get_result()),
                asyncio.create_task((handles[1]).get_result()),
            ]
            try:
                await asyncio.gather(*(event.wait() for event in entered))
                assert all(not wait.done() for wait in waits)
                release.set()
                results = await asyncio.gather(*waits)
                assert [result.final_output for result in results] == ["done-0", "done-1"]
                for index, (model, peer, session) in enumerate(
                    zip(models, peers, [first, second], strict=True)
                ):
                    other = peers[1 - index]
                    output = tool_output(model.requests[-1], "same-call-id")
                    assert str(peer.root) in output and f"tenant-{index}" in output
                    assert str(other.root) not in output and f"tenant-{1 - index}" not in output
                    assert "Process exited with code 0" in output
                    assert (peer.root / "result.txt").read_text() == "accepted"
                    assert len(model.requests) == 2
                    assert all(
                        headers["authorization"] == "Bearer " + model_keys[index]
                        for headers in model.request_headers
                    )
                    state = (await session.save_state()).to_json()
                    assert str(other.root) not in state
                    for secret in [one.token, two.token, *model_keys, one.url, two.url]:
                        assert secret not in state
                        assert secret not in json.dumps(peer.requests)
                    assert any(
                        request.get("method") == "process/start" for request in peer.requests
                    )
            finally:
                release.set()
                for wait in waits:
                    if not wait.done():
                        wait.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
