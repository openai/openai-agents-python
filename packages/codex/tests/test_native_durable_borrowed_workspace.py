"""A caller-owned live resource retains edited seed files through idle restore."""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from agents import Agent, Environment, NativeProvider, SessionState, sandbox_apply_patch_tool
from agents.classic.sandbox.entries import File
from agents.sandbox import Manifest, sandbox_environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_apply_patch import patch_call
from tests.test_native_workspace_patch_format import ReplaceFormat


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift", ["none", "grant-remove", "grant-add", "cwd", "closed", "restarted", "new-live-owner"]
)
async def test_caller_borrowed_seed_is_not_reapplied_on_idle_reopen(
    responses, tmp_path, monkeypatch, drift
):
    if os.name != "posix":
        pytest.skip("UnixLocal caller resources require POSIX")
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    client = UnixLocalSandboxClient()
    resources = []
    all_resources = []
    acquisitions = []
    exits = []
    tokens = []
    editor = ReplaceFormat()
    try:
        for name in ("root", "destination"):
            path = tmp_path / name
            resource = await client.create(
                manifest=Manifest(
                    root=str(path), entries={Path("value.txt"): File(content=name.encode())}
                ),
                options=UnixLocalSandboxClientOptions(),
            )
            await resource.start()
            resources.append(resource)
            all_resources.append(resource)
            other = path / "other"
            other.mkdir()
            (other / "value.txt").write_text("unselected")

        def source(resource, label):
            @asynccontextmanager
            async def connect(current):
                assert current is resource
                async with ExecutorPeer(tmp_path / uuid4().hex).serve() as peer:
                    trust_bundle = tmp_path / "trusted.pem"
                    with trust_bundle.open("ab") as certificates:
                        certificates.write(peer.ca.read_bytes())
                    empty = tmp_path / "empty-certs"
                    empty.mkdir(exist_ok=True)
                    monkeypatch.setenv("SSL_CERT_FILE", str(trust_bundle))
                    monkeypatch.setenv("SSL_CERT_DIR", str(empty))
                    acquisitions.append(label)
                    tokens.append(peer.token)
                    try:
                        yield Environment(
                            url=peer.url,
                            headers={"Authorization": "Bearer " + peer.token},
                            cwd=str(Path(current.state.manifest.root) / "other")
                            if label == "CURRENT_TARGET" and drift == "cwd"
                            else str(current.state.manifest.root),
                            tools=[]
                            if label == "CURRENT_TARGET" and drift == "grant-remove"
                            else ["apply_patch", "exec_command", "write_stdin"]
                            if label == "CURRENT_TARGET" and drift == "grant-add"
                            else ["apply_patch"],
                            write_policy="legacy",
                        )
                    finally:
                        exits.append(label)

            manifest = resource.state.manifest.model_copy(deep=True)
            manifest.entries[Path("value.txt")].description = label
            return sandbox_environment(
                client,
                session=resource,
                manifest=manifest,
                options=UnixLocalSandboxClientOptions(),
                connect=connect,
            )

        async with (
            timeout(60),
            NativeProvider(
                base_url=responses.url,
                api_key="synthetic-borrowed-workspace",
                supports_websockets=False,
            ) as provider,
        ):

            def graph(generation):
                target = Agent(
                    name="Editor",
                    instructions="Edit.",
                    environment=source(resources[1], generation + "_TARGET"),
                    tools=[sandbox_apply_patch_tool(editor)],
                )
                return Agent(
                    name="Root",
                    model="gpt-5.5",
                    instructions="Transfer.",
                    provider=provider,
                    handoffs=[target],
                ), source(resources[0], generation + "_ROOT")

            responses.respond(function_call("transfer_to_editor", {}, "handoff"))
            responses.respond(
                patch_call({"type": "update_file", "path": "value.txt", "diff": " first"})
            )
            responses.respond(assistant_output("first"))
            responses.respond(
                patch_call({"type": "update_file", "path": "value.txt", "diff": " second"})
            )
            responses.respond(assistant_output("second"))
            root, environment = graph("FIRST")
            async with root.session(environment=environment) as session:
                assert (await (await session.submit("Edit.")).get_result()).final_output == "first"
                assert (tmp_path / "destination/value.txt").read_text() == "destination first"
                print(
                    "BORROWED_NATIVE_FIRST="
                    + json.dumps({"requests": len(responses.requests), "edits": editor.calls}),
                    flush=True,
                )
                saved = (await session.save_state()).to_json()
            assert sorted(acquisitions) == sorted(exits)
            assert all([await resource.running() for resource in resources])
            assert (tmp_path / "destination/value.txt").read_text() == "destination first"
            assert all(token not in saved for token in tokens)
            assert all(label not in saved for label in acquisitions)
            expected_contents = "destination first"
            if drift == "closed":
                await resources[1].shutdown()
            elif drift == "restarted":
                await resources[1].shutdown()
                await resources[1].start()
                assert await resources[1].running()
                expected_contents = "destination"
            elif drift == "new-live-owner":
                previous = resources[1]
                manifest = previous.state.manifest.model_copy(deep=True)
                manifest.entries[Path("value.txt")].content = b"destination first"
                current_state = previous.state.model_copy(update={"manifest": manifest})
                replacement = await client.resume(current_state)
                await replacement.start()
                assert replacement.state.session_id == previous.state.session_id
                assert replacement is not previous
                assert await replacement.running()
                resources[1] = replacement
                all_resources.append(replacement)
            current, environment = graph("CURRENT")
            if drift != "none":
                with pytest.raises(
                    (ValueError, RuntimeError),
                    match=(
                        "recorded Agent environment grants|provider state restoration"
                        "|workspace identity"
                    ),
                ):
                    async with current.session(
                        environment=environment, state=SessionState.from_json(saved)
                    ):
                        pytest.fail(
                            "Changed live resource or grants must refuse before resumed effects"
                        )
                assert len(responses.requests) == 3
                assert editor.calls == [("destination", " first", "default")]
                assert (tmp_path / "destination/value.txt").read_text() == expected_contents
                assert sorted(acquisitions) == sorted(exits)
                for resource in all_resources:
                    expected_running = not (drift == "closed" and resource is resources[1])
                    assert await resource.running() is expected_running
                return

            async with current.session(
                environment=environment, state=SessionState.from_json(saved)
            ) as session:
                assert (await session.get_active_agent()).name == "Editor"
                assert (tmp_path / "destination/value.txt").read_text() == "destination first"
                assert (
                    await (await session.submit("Again.")).get_result()
                ).final_output == "second"
            assert len(responses.requests) == 5
            for request in responses.requests[3:]:
                serialized = json.dumps(request["input"])
                assert serialized.count("CURRENT_TARGET") == 1
                assert "FIRST_TARGET" not in serialized
                assert "CURRENT_ROOT" not in serialized
            assert editor.calls == [
                ("destination", " first", "default"),
                ("destination first", " second", "default"),
            ]
            assert sorted(acquisitions) == sorted(exits)
            assert all([await resource.running() for resource in resources])
            assert (tmp_path / "destination/value.txt").read_text() == "destination first second"
            assert (tmp_path / "root/value.txt").read_text() == "root"
    finally:
        for resource in all_resources:
            await resource.aclose()
            await client.delete(resource)
