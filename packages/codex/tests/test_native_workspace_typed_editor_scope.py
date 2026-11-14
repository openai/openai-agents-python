"""Keep a real workspace and its custom editor on the selected native Agent."""

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from agents import Agent, Environment, NativeProvider, sandbox_apply_patch_tool
from agents.classic.sandbox.entries import File
from agents.sandbox import Manifest, sandbox_environment
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_apply_patch import patch_call
from tests.test_native_workspace_patch_format import ReplaceFormat

if sys.platform in ("linux", "darwin"):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="UnixLocal resource"
)


@pytest.mark.parametrize(("restore_block", "granted"), [(False, True), (True, True), (True, False)])
async def test_destination_guidance_follows_native_workspace(
    responses, tmp_path, monkeypatch, restore_block, granted
):
    root = tmp_path / "root"
    destination = tmp_path / "destination"
    for workspace in (root, destination):
        (workspace / "nested").mkdir(parents=True)
        (workspace / "nested/value.txt").write_text(workspace.name)
    editor = ReplaceFormat()

    def source(workspace):
        @asynccontextmanager
        async def connect(_resource):
            async with ExecutorPeer(tmp_path / uuid4().hex).serve() as peer:
                assert peer.ca is not None
                trust_bundle = tmp_path / "trusted.pem"
                with trust_bundle.open("ab") as certificates:
                    certificates.write(peer.ca.read_bytes())
                empty = tmp_path / "empty-certs"
                empty.mkdir(exist_ok=True)
                monkeypatch.setenv("SSL_CERT_FILE", str(trust_bundle))
                monkeypatch.setenv("SSL_CERT_DIR", str(empty))
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(workspace / "nested"),
                    tools=["apply_patch"] if granted else [],
                )

        return sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(
                root=str(workspace),
                entries={
                    Path("guidance.txt"): File(
                        content=b"fixture",
                        description=f"only-{workspace.name}-workspace-guide",
                    )
                },
            ),
            options=UnixLocalSandboxClientOptions(),
            connect=connect,
        )

    target = Agent(
        name="Editor",
        instructions="Edit in the destination.",
        environment=source(destination),
        tools=[sandbox_apply_patch_tool(editor)],
    )
    agent = Agent(
        name="Source",
        model="gpt-5.5",
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-workspace", supports_websockets=False
        ),
        instructions="Hand off to the editor.",
        handoffs=[target],
    )
    responses.respond(function_call("transfer_to_editor", {}, "to-editor"))
    responses.respond(patch_call({"type": "update_file", "path": "value.txt", "diff": " edited"}))
    if granted:
        responses.respond(assistant_output("done"))
        if not restore_block:
            responses.respond(assistant_output("updated"))

    async with agent.session(environment=source(root)) as session:
        if not granted:
            with pytest.raises(RuntimeError, match="[Uu]nsupported call|not permitted"):
                await (await session.submit("Route and edit.")).get_result()
            assert editor.calls == []
            assert (destination / "nested/value.txt").read_text() == "destination"
            assert (root / "nested/value.txt").read_text() == "root"
            assert all(
                "apply_patch" not in json.dumps(request.get("tools", []))
                for request in responses.requests
            )
            return
        assert (await (await session.submit("Route and edit.")).get_result()).final_output == "done"
        if restore_block:
            with pytest.raises(ValueError, match="Per-Agent Environment sources"):
                await session.save_state()
        else:
            await session.update(instructions="Changed application instruction.")
            result = await (await session.submit("Continue.")).get_result()
            assert result.final_output == "updated"

    assert editor.calls == [("destination", " edited", "default")]
    assert (destination / "nested/value.txt").read_text() == "destination edited"
    assert (root / "nested/value.txt").read_text() == "root"
    root_request, *destination_requests = (json.dumps(r) for r in responses.requests)
    assert root_request.count("only-root-workspace-guide") == 1
    assert "only-destination-workspace-guide" not in root_request
    assert len(destination_requests) == (2 if restore_block else 3)
    for request in destination_requests:
        assert request.count("only-destination-workspace-guide") == 1
        assert "only-root-workspace-guide" not in request
    if not restore_block:
        assert "Changed application instruction." in destination_requests[-1]
