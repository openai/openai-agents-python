from __future__ import annotations

import io
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agents import Agent, Environment, SessionState, function_tool
from agents.sandbox import Manifest, sandbox_environment
from agents.testing import (
    ExecResult,
    ScriptedResponses,
    UnconsumedSandboxSteps,
    scripted_sandbox_session,
    text,
    tool_call,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_sandbox_environment import Client, Options


async def test_scripted_commands_and_files_through_native_session_and_cold_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = scripted_sandbox_session(
        [
            {
                "method": "exec",
                "result": ExecResult(stdout=b"synthetic output", stderr=b"", exit_code=0),
            },
            {"method": "write", "result": None},
            {"method": "read", "result": io.BytesIO(b"synthetic file")},
        ]
    )
    effects: list[str] = []
    provider_events: list[str] = []
    client = Client(provider_events)
    client.backend_id = "scripted"

    @asynccontextmanager
    async def connect(resource):
        assert resource is fixture
        effects.append("connect")
        try:
            yield Environment(
                url=executor.url,
                cwd=executor.root.as_uri(),
                tools=(),
                headers={"Authorization": "Bearer " + executor.token},
            )
        finally:
            effects.append("disconnect")

    def environment():
        return sandbox_environment(
            client, manifest=Manifest(), options=Options(), session=fixture, connect=connect
        )

    @function_tool
    async def workflow() -> str:
        result = await fixture.exec("must-not-run-on-host")
        await fixture.write(Path("/workspace/note.txt"), io.BytesIO(b"synthetic file"))
        with await fixture.read(Path("/workspace/note.txt")) as stream:
            return result.stdout.decode() + ":" + stream.read().decode()

    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        ScriptedResponses(
            [tool_call("workflow", {}, call_id="scripted-sandbox-call")],
            [text("done")],
            [text("restored")],
        ) as peer,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certs"))
        agent = Agent(
            instructions="Use the workflow tool.", tools=[workflow], provider=peer.provider
        )
        async with agent.session(environment=environment()) as session:
            turn = await session.submit("run the scripted workflow")
            assert (await turn.get_result()).final_output == "done"
            saved = SessionState.from_json((await session.save_state()).to_json())
        fixture.assert_complete()
        assert peer.tool_output("scripted-sandbox-call") == "synthetic output:synthetic file"
        assert [call.method for call in fixture.calls] == ["exec", "write", "read"]
        assert fixture.calls[0].args == ("must-not-run-on-host",)
        assert fixture.calls[1].args[0] == Path("/workspace/note.txt")
        assert fixture.calls[1].args[1].getvalue() == b"synthetic file"
        fixture.calls[1].args[1].write(b"detached mutation")
        assert fixture.calls[1].args[1].getvalue() == b"synthetic file"
        async with agent.session(environment=environment(), state=saved) as restored:
            turn = await restored.submit("continue without repeating the tool")
            assert (await turn.get_result()).final_output == "restored"
        assert len(fixture.calls) == 3
    assert provider_events == []
    assert effects == ["connect", "disconnect", "connect", "disconnect"]
    assert not (tmp_path / "note.txt").exists()


async def test_scripted_resource_preserves_borrowed_backend_check(tmp_path: Path):
    fixture = scripted_sandbox_session()
    client = Client([])
    connected = False

    @asynccontextmanager
    async def connect(resource):
        nonlocal connected
        connected = True
        yield Environment.local(cwd=tmp_path)

    with pytest.raises(ValueError, match="session backend must match"):
        sandbox_environment(
            client, manifest=Manifest(), options=Options(), session=fixture, connect=connect
        )
    assert not connected
    assert fixture.calls == ()


def test_scripted_resource_does_not_advertise_unconfigured_or_durable_capabilities():
    fixture = scripted_sandbox_session([{"method": "exec", "result": None}])
    assert hasattr(fixture, "exec")
    for method in (
        "read",
        "write",
        "pty_exec_start",
        "pty_write_stdin",
        "hydrate_workspace",
        "persist_workspace",
    ):
        assert not hasattr(fixture, method)
        assert method not in dir(fixture)
    with pytest.raises(UnconsumedSandboxSteps) as remaining:
        fixture.assert_complete()
    assert remaining.value.remaining_steps == 1
