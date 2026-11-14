"""A current application graph reacquires distinct editor workspaces on cold reopen."""

import shutil
import sys
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from agents import Agent, Environment, NativeProvider, SessionState, sandbox_apply_patch_tool
from agents.sandbox import Manifest, sandbox_environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_apply_patch import patch_call
from tests.test_native_workspace_patch_format import ReplaceFormat
from tests.test_native_workspace_sampling_snapshot import input_text

if sys.platform in ("linux", "darwin"):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="UnixLocal resource"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    ["none", "grant", "cwd", "replaced", "cwd_replaced", "live_replaced", "live_cwd_replaced"],
)
async def test_fresh_application_workspaces_reopen_the_selected_editor(
    responses, tmp_path, monkeypatch, drift
):
    root, destination = (tmp_path / leaf for leaf in ("root", "destination"))
    for workspace in (root, destination):
        for relative in ("nested", "other"):
            folder = workspace / relative
            folder.mkdir(parents=True)
            (folder / "value.txt").write_text(workspace.name)
    editor = ReplaceFormat()
    acquisitions = []
    retirements = []
    tokens = []

    def source(workspace, label, *, grant, relative="nested"):
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
                acquisitions.append(label)
                tokens.append(peer.token)
                try:
                    yield Environment(
                        url=peer.url,
                        headers={"Authorization": "Bearer " + peer.token},
                        cwd=str(workspace / relative),
                        tools=["apply_patch"] if grant else [],
                        write_policy="legacy",
                    )
                finally:
                    retirements.append(label)

        return sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(workspace)),
            options=UnixLocalSandboxClientOptions(),
            connect=connect,
        )

    async with (
        timeout(60),
        NativeProvider(
            base_url=responses.url,
            api_key="synthetic-durable-workspace",
            supports_websockets=False,
        ) as provider,
    ):

        def graph(generation):
            cold = generation == "COLD"
            target = Agent(
                name="Editor",
                instructions="Edit in the selected workspace.",
                environment=source(
                    destination,
                    f"{generation}_DESTINATION_GUIDE",
                    grant=not (cold and drift == "grant"),
                    relative="other" if cold and drift == "cwd" else "nested",
                ),
                tools=[sandbox_apply_patch_tool(editor)],
            )
            root_agent = Agent(
                name="Source",
                model="gpt-5.5",
                instructions="Transfer.",
                provider=provider,
                handoffs=[target],
            )
            return root_agent, source(root, f"{generation}_ROOT_GUIDE", grant=False)

        responses.respond(function_call("transfer_to_editor", {}, "to-editor"))
        responses.respond(
            patch_call({"type": "update_file", "path": "value.txt", "diff": " first"})
        )
        responses.respond(assistant_output("before"))
        responses.respond(
            patch_call({"type": "update_file", "path": "value.txt", "diff": " second"})
        )
        responses.respond(assistant_output("after"))
        opened, environment = graph("FIRST")
        async with opened.session(environment=environment) as session:
            result = await (await session.submit("Route and edit.")).get_result()
            assert result.final_output == "before"
            assert (await session.get_active_agent()).name == "Editor"
            assert (await session.checkpoint_status(kind="idle")).status == "ready"
            with pytest.raises(ValueError, match="Per-Agent Environment sources"):
                await session.fork()
            if drift in {"live_replaced", "live_cwd_replaced"}:
                changed = destination / "nested" if drift == "live_cwd_replaced" else destination
                changed.rename(tmp_path / "previous")
                shutil.copytree(tmp_path / "previous", changed)
                status = await session.checkpoint_status(kind="idle")
                assert status.status == "not_ready"
                with pytest.raises(ValueError, match="changed Agent workspace identity"):
                    await session.save_state()
                assert (destination / "nested/value.txt").read_text() == "destination first"
                old_path = "value.txt" if drift == "live_cwd_replaced" else "nested/value.txt"
                assert (tmp_path / "previous" / old_path).read_text() == "destination first"
                assert len(responses.requests) == 3
                return
            state = (await session.save_state()).to_json()
        assert len(acquisitions) == len(retirements) == 2
        assert all(token not in state for token in tokens)
        assert "# Current SDK workspace snapshot" not in state
        if drift in {"replaced", "cwd_replaced"}:
            # Keep the old inode allocated so the new directory cannot reuse it.
            changed = destination / "nested" if drift == "cwd_replaced" else destination
            changed.rename(tmp_path / "previous")
            shutil.copytree(tmp_path / "previous", changed)
        current, environment = graph("COLD")
        checkpoint = SessionState.from_json(state)
        if drift == "none":
            async with current.session(environment=environment, state=checkpoint) as session:
                assert (await session.get_active_agent()).name == "Editor"
                assert (
                    await (await session.submit("Edit again.")).get_result()
                ).final_output == "after"
            assert len(responses.requests) == 5
            for request in responses.requests[3:]:
                snapshots = [
                    item
                    for item in request["input"]
                    if "# Current SDK workspace snapshot" in input_text(item)
                ]
                assert len(snapshots) == 1 and snapshots[0]["role"] == "user"
                selected = input_text(snapshots[0])
                assert str(destination / "nested") in selected
                assert str(root / "nested") not in selected
            assert editor.calls == [
                ("destination", " first", "default"),
                ("destination first", " second", "default"),
            ]
        else:
            with pytest.raises(
                (ValueError, RuntimeError), match="recorded Agent environment grants"
            ):
                async with current.session(environment=environment, state=checkpoint):
                    pytest.fail("Changed current executor authority must refuse at opening")
            assert len(responses.requests) == 3
            assert editor.calls == [("destination", " first", "default")]
        assert sorted(acquisitions) == sorted(retirements)
        assert len(acquisitions) == 4
        assert (root / "nested/value.txt").read_text() == "root"
        expected = "destination first second" if drift == "none" else "destination first"
        assert (destination / "nested/value.txt").read_text() == expected
        assert (destination / "other/value.txt").read_text() == "destination"
