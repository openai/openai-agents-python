"""Borrowed manifest descriptions are scoped to one native opening."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_sandbox_environment import Client, Options

from agents import Agent, Environment
from agents.classic.sandbox.entries import Dir, File
from agents.sandbox import Manifest, SandboxCapability, sandbox_environment


async def test_borrowed_file_description_changes_only_the_current_native_guidance(
    tmp_path, monkeypatch, responses
):
    root = tmp_path / "executor"
    rel = Path("records")
    name = Path("entry.txt")
    physical = root / rel / name
    old = File(content=b"declared bytes", description="original description")
    current = Manifest(root=str(root), entries={rel: Dir(children={name: old})})
    requested = Manifest(
        root=str(root),
        entries={
            rel: Dir(children={name: old.model_copy(update={"description": "new description"})})
        },
    )

    class ReadDescription(SandboxCapability):
        name = "description-guidance"

        async def instructions(self, manifest):
            return "Plugin sees " + manifest.entries[rel].children[name].description

    client = Client([])
    resource = await client.create(manifest=current, options=Options())
    client.backend_id = resource.state.type
    client.events.clear()
    responses.respond(assistant_output("changed"))
    responses.respond(assistant_output("original"))
    native = provider(responses)
    try:
        async with ExecutorPeer(root).serve() as peer:
            physical.parent.mkdir()
            physical.write_bytes(b"current physical contents")
            trust(peer, tmp_path, monkeypatch)

            @asynccontextmanager
            async def connect(selected):
                assert selected is resource and selected.state.manifest == current
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(root),
                    tools=(),
                )
                await peer.wait_disconnected()

            agent = Agent(instructions="Read the workspace guidance.", provider=native)
            for manifest, expected in ((requested, "changed"), (current, "original")):
                source = sandbox_environment(
                    client,
                    manifest=manifest,
                    options=Options(),
                    session=resource,
                    connect=connect,
                    capabilities=[ReadDescription()],
                )
                async with agent.session(environment=source) as session:
                    assert (
                        await (await session.submit("Read.")).get_result()
                    ).final_output == expected
                    assert resource.state.manifest == current
                assert physical.read_bytes() == b"current physical contents"

            wrong = requested.model_copy(deep=True)
            wrong.entries[rel].children[name].content = b"replacement bytes"
            with pytest.raises(ValueError, match="atomic provider transaction"):
                async with agent.session(
                    environment=sandbox_environment(
                        client, manifest=wrong, options=Options(), session=resource, connect=connect
                    )
                ):
                    pytest.fail("Borrowed physical file replacement was admitted")
        assert len(responses.requests) == 2
        first, second = [json.dumps(request["input"]) for request in responses.requests]
        assert "new description" in first and "original description" not in first
        assert "original description" in second and "new description" not in second
        assert "Plugin sees new description" in first
        assert "Plugin sees original description" in second
        assert not client.events
    finally:
        await native.aclose()
