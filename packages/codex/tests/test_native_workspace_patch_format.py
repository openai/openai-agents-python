"""Custom patch formats run inside the native tool's acquired workspace."""

from contextlib import asynccontextmanager

import pytest

from agents import Agent, Environment, sandbox_apply_patch_tool
from agents.sandbox import Manifest, sandbox_environment
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_native_apply_patch import patch_call


class ReplaceFormat:
    def __init__(self):
        self.calls = []

    def apply_diff(self, input, diff, mode="default"):
        self.calls.append((input, diff, mode))
        return input + diff


async def test_native_workspace_editor_uses_resource_scope(responses, tmp_path, monkeypatch):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    root = tmp_path / "workspace"
    child = root / "nested"
    child.mkdir(parents=True)
    (child / "target.txt").write_text("before")
    format = ReplaceFormat()

    @asynccontextmanager
    async def connect(_resource):
        async with ExecutorPeer(tmp_path / "executor").serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            yield Environment(
                url=peer.url, headers={"Authorization": "Bearer " + peer.token}, cwd=str(child)
            )

    source = sandbox_environment(
        UnixLocalSandboxClient(),
        manifest=Manifest(root=str(root)),
        options=UnixLocalSandboxClientOptions(),
        connect=connect,
    )
    responses.respond(patch_call({"type": "update_file", "path": "target.txt", "diff": " after"}))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Update the file.",
        model="gpt-5.5",
        provider=provider(responses),
        tools=[sandbox_apply_patch_tool(format)],
    )
    async with agent.session(environment=source) as session:
        turn = await session.submit("Patch the workspace.")
        assert (await turn.get_result()).final_output == "done"
    assert format.calls == [("before", " after", "default")]
    assert (child / "target.txt").read_text() == "before after"
    assert not (root / "target.txt").exists()


async def test_native_workspace_editor_rejects_unowned_workspace(responses, tmp_path, monkeypatch):
    format = ReplaceFormat()
    responses.respond(patch_call({"type": "create_file", "path": "outside.txt", "diff": "new"}))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Update the file.",
        model="gpt-5.5",
        provider=provider(responses),
        tools=[sandbox_apply_patch_tool(format)],
    )
    async with ExecutorPeer(tmp_path / "executor").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        environment = Environment(
            url=peer.url, headers={"Authorization": "Bearer " + peer.token}, cwd=str(tmp_path)
        )
        async with agent.session(environment=environment) as session:
            with pytest.raises(RuntimeError, match="require the current sandbox environment"):
                await (await session.submit("Patch the workspace.")).get_result()
    assert not format.calls
    assert len(responses.requests) == 1
    assert not (tmp_path / "outside.txt").exists()
