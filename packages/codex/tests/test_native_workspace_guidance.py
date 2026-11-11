"""Automatic resource context through the real native instruction owner."""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager

import pytest

from agents import Agent, Environment
from agents.sandbox import Dir, Manifest, sandbox_environment
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_sandbox_environment import Client, Options, declaration


def _latest_user_instructions(request):
    return next(
        block["text"]
        for item in reversed(request["input"])
        if item.get("role") == "user"
        for block in item.get("content", [])
        if block.get("type") == "input_text" and "# AGENTS.md instructions" in block.get("text", "")
    )


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
async def test_native_workspace_appendix_preserves_providers_and_replaces_current_capture(
    tmp_path, monkeypatch, responses
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    global_file = home / "AGENTS.md"
    global_file.write_text("SYNTHETIC_GLOBAL_A")
    monkeypatch.setenv("CODEX_HOME", str(home))
    async with ExecutorPeer(tmp_path / "workspace").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certificates"))
        (peer.root / "AGENTS.md").write_text("SYNTHETIC_PROJECT_RULE")

        def environment():
            return Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=(),
            )

        @asynccontextmanager
        async def connect(selected):
            yield environment()

        def source(marker):
            return sandbox_environment(
                Client([]),
                manifest=Manifest(
                    root=str(peer.root),
                    entries={"data": declaration(description=marker)},
                    environment={"value": {"PRIVATE": "SYNTHETIC_ENV_SECRET"}},
                ),
                options=Options(),
                connect=connect,
            )

        agent = Agent(instructions="SYNTHETIC_APPLICATION", provider=provider(responses))
        responses.replies.extend([[assistant_output("done")]] * 4)
        async with agent.session(
            environment=source("SYNTHETIC_GUIDE_A"),
            project_guidance=True,
            discover_agents_md=True,
        ) as first:
            await (await first.submit("First capture")).get_result()
            global_file.write_text("SYNTHETIC_GLOBAL_B")
            await (await first.submit("Refresh original provider")).get_result()
            state = await first.save_state()
        first_text = _latest_user_instructions(responses.requests[0])
        assert "SYNTHETIC_GLOBAL_A" in first_text and "SYNTHETIC_PROJECT_RULE" in first_text
        assert first_text.count("SYNTHETIC_GUIDE_A") >= 1
        refreshed = _latest_user_instructions(responses.requests[1])
        assert "SYNTHETIC_GLOBAL_B" in refreshed and "SYNTHETIC_GLOBAL_A" not in refreshed
        assert refreshed.count("# Current SDK workspace snapshot") == 1
        async with agent.session(
            state=state,
            environment=source("SYNTHETIC_GUIDE_B"),
            project_guidance=True,
            discover_agents_md=True,
        ) as second:
            await (await second.submit("Current capture B")).get_result()
            state = await second.save_state()
        current = _latest_user_instructions(responses.requests[2])
        assert "SYNTHETIC_GUIDE_B" in current and "SYNTHETIC_GUIDE_A" not in current
        assert "SYNTHETIC_GLOBAL_B" in current and "SYNTHETIC_PROJECT_RULE" in current
        async with agent.session(
            state=state,
            environment=environment(),
            project_guidance=True,
            discover_agents_md=True,
        ) as cleared:
            await (await cleared.submit("Only current original instructions")).get_result()
        current = _latest_user_instructions(responses.requests[3])
        assert "# Current SDK workspace snapshot" not in current
        assert "SYNTHETIC_GUIDE_A" not in current and "SYNTHETIC_GUIDE_B" not in current
        for request in responses.requests:
            encoded = json.dumps(request)
            assert "SYNTHETIC_APPLICATION" in encoded
            assert "SYNTHETIC_ENV_SECRET" not in encoded
            assert "user_instructions_appendix" not in encoded


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@pytest.mark.parametrize(
    "scenario",
    ["unicode", "empty_allowlist", "oversized_allowlist", "path_priority", "dense_mounts"],
)
async def test_native_large_workspace_guidance_preserves_paths_and_policy(
    tmp_path, monkeypatch, responses, scenario
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "AGENTS.md").write_text("UNDISCOVERED_GLOBAL_RULE")
    monkeypatch.setenv("CODEX_HOME", str(home))
    commands = (
        Manifest().remote_mount_command_allowlist
        if scenario == "dense_mounts"
        else [f"fixture-mount-{index}" for index in range(2500)]
        if scenario == "oversized_allowlist"
        else []
        if scenario == "empty_allowlist"
        else ["fixture-mount", "fixture-unmount"]
    )
    root = "/workspace/fixtures/workspace-guidance-boundary"
    source_description = (
        "x" * 39_500 if scenario == "path_priority" else 'Source "code" 📚\nDirectory guidance.'
    )
    file_description = 'Entrée "test" 🧪\nFile guidance.'
    manifest = Manifest(
        root=root,
        remote_mount_command_allowlist=commands,
        entries={
            "data": declaration(description="📚" * 11_000),
            "output": declaration(read_only=False, description="Keep this short description."),
            "src": Dir(
                description=source_description,
                children={
                    "café.py": {
                        "type": "file",
                        "content": "pass\n",
                        "description": file_description,
                    }
                },
            ),
            **(
                {f"mount-{index:04d}": declaration() for index in range(600)}
                if scenario == "dense_mounts"
                else {}
            ),
        },
    )
    client = Client([])
    async with ExecutorPeer(tmp_path / "workspace").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certificates"))
        (peer.root / "AGENTS.md").write_text("UNDISCOVERED_PROJECT_RULE")

        @asynccontextmanager
        async def connect(selected):
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=(),
                read_only=scenario == "path_priority",
            )

        source = sandbox_environment(client, manifest=manifest, options=Options(), connect=connect)
        agent = Agent(instructions="SYNTHETIC_APPLICATION", provider=provider(responses))
        responses.replies.append([assistant_output("done")])
        async with agent.session(environment=source, discover_agents_md=False) as session:
            await (await session.submit("Read current workspace guidance")).get_result()
    text = _latest_user_instructions(responses.requests[0])
    assert "Some workspace details have been omitted" in text
    assert f"Executor read-only: {str(scenario == 'path_priority').lower()}" in text
    assert "untrusted external content" in text and "then copy back" in text
    assert "For any edit to a read-write mount, including apply_patch, copy" in text
    assert "Do not write to read-only mounts" in text
    assert "UNDISCOVERED_GLOBAL_RULE" not in text
    assert "UNDISCOVERED_PROJECT_RULE" not in text
    if scenario == "dense_mounts":
        assert "Do not run commands on remote-mounted paths." not in text
        assert (
            "Only use these commands on remote-mounted paths: "
            + json.dumps(commands, separators=(",", ":"))
            + "."
        ) in text
        assert "cp" in commands and "cat" in commands
    storage, _ = json.JSONDecoder().raw_decode(text.split("Declared storage access:\n")[1])
    if scenario == "dense_mounts":
        assert 2 < len(storage) < 602
        assert [{"path": entry["path"], "access": entry["access"]} for entry in storage[:2]] == [
            {"path": root + "/data", "access": "read_only"},
            {"path": root + "/output", "access": "read_write"},
        ]
        assert storage[2:] == [
            {"path": root + f"/mount-{index:04d}", "access": "read_only"}
            for index in range(len(storage) - 2)
        ]
        assert client.events == ["create", "start", "close", "delete"]
        return
    assert storage == [
        {"path": root + "/data", "access": "read_only"},
        {
            "path": root + "/output",
            "access": "read_write",
            "description": "Keep this short description.",
        },
    ]
    filesystem = json.loads(text.split("Filesystem layout:\n")[1].split("\n\n")[0])
    assert filesystem == [
        {
            "path": root + "/src/",
            **({"description": source_description} if scenario != "path_priority" else {}),
        },
        {"path": root + "/src/café.py", "description": file_description},
    ]
    if scenario in ("empty_allowlist", "oversized_allowlist"):
        assert "Do not run commands on remote-mounted paths." in text
        assert "Only use these commands on remote-mounted paths:" not in text
        assert "fixture-mount-0" not in text
    else:
        assert (
            'Only use these commands on remote-mounted paths: ["fixture-mount","fixture-unmount"].'
            in text
        )
    assert client.events == ["create", "start", "close", "delete"]
