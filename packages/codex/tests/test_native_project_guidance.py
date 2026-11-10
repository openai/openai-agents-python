"""Workspace image viewing through the public Python native session option."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from agents import Agent, Environment
from agents.observation import TurnStarted
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import function_declarations, provider

_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


async def test_project_guidance_requires_an_executor(responses) -> None:
    with pytest.raises(ValueError, match="requires an Environment"):
        async with Agent(instructions="Answer.", provider=provider(responses)).session(
            project_guidance=True
        ):
            pass
    assert responses.requests == []


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_project_guidance_reads_workspace_image_without_command_grants(
    tmp_path: Path, responses
) -> None:
    image = tmp_path / "pixel.png"
    image.write_bytes(_PIXEL_PNG)
    rules = tmp_path / ".agents" / "rules"
    rules.mkdir(parents=True)
    (rules / "general.md").write_text("SYNTHETIC_WORKSPACE_GUIDANCE")
    responses.replies.extend(
        [
            [function_call("view_image", {"path": str(image)}, "image-1")],
            [assistant_output("image read")],
        ]
    )
    async with Agent(instructions="Answer.", provider=provider(responses)).session(
        environment=Environment.local(cwd=tmp_path),
        project_guidance=True,
    ) as session:
        completed = await (await session.submit("Read the image.")).get_result()
        assert completed.final_output == "image read"
    request = json.dumps(responses.requests[1])
    assert "data:image/png;base64," in request
    assert "SYNTHETIC_WORKSPACE_GUIDANCE" in request
    assert not any(
        tool.get("name") == "exec_command" for tool in responses.requests[0].get("tools", [])
    )


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
async def test_project_guidance_matches_scoped_rule_with_aliased_cwd(
    tmp_path: Path, responses, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.native_executor_fixtures import ExecutorPeer

    async with ExecutorPeer(tmp_path / "workspace").serve() as peer:
        alias = tmp_path / "alias"
        alias.symlink_to(peer.root, target_is_directory=True)
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certificates"))
        (peer.root / "pixel.png").write_bytes(_PIXEL_PNG)
        rules = peer.root / ".agents" / "rules"
        rules.mkdir(parents=True)
        (rules / "pixel.md").write_text("---\npaths:\n  - pixel.png\n---\nSYNTHETIC_IMAGE_GUIDANCE")
        (rules / "other.md").write_text("---\npaths:\n  - other.png\n---\nSYNTHETIC_OTHER_GUIDANCE")
        responses.replies.extend(
            [
                [function_call("view_image", {"path": str(alias / "pixel.png")}, "image-1")],
                [assistant_output("image read")],
            ]
        )
        async with Agent(instructions="Answer.", provider=provider(responses)).session(
            environment=Environment(
                url=peer.url,
                cwd=str(alias),
                headers={"Authorization": "Bearer " + peer.token},
                writable_roots=(),
            ),
            project_guidance=True,
        ) as session:
            assert (await (await session.submit("Read the image.")).get_result()).final_output == (
                "image read"
            )

    first, second = responses.requests
    assert "SYNTHETIC_IMAGE_GUIDANCE" not in json.dumps(first)
    request = json.dumps(second)
    assert "data:image/png;base64," in request
    assert "SYNTHETIC_IMAGE_GUIDANCE" in request
    assert "SYNTHETIC_OTHER_GUIDANCE" not in request
    assert all(
        tool.get("name") != "exec_command"
        for request in responses.requests
        for tool in function_declarations(request)
    )


@pytest.mark.parametrize("project_guidance", [False, True])
async def test_native_agents_preserves_root_project_guidance(
    tmp_path: Path, responses, monkeypatch: pytest.MonkeyPatch, project_guidance: bool
) -> None:
    from agents import NativeAgents
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_executor_tools import declaration

    async with ExecutorPeer(tmp_path / "remote").serve() as executor:
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certificates"))
        responses.replies.append([assistant_output("done")])
        async with Agent(
            instructions="Answer.",
            model="gpt-5.5",
            tools=[NativeAgents()],
            provider=provider(responses),
        ).session(
            environment=declaration(executor),
            project_guidance=project_guidance,
            observation_retention="all",
        ) as session:
            assert (await (await session.submit("Answer.")).get_result()).final_output == "done"
        retained = [event async for event in session.events()]
        assert sum(isinstance(event, TurnStarted) for event in retained) == 1
        names = {tool.get("name") for tool in responses.requests[0]["tools"]}
        assert ("view_image" in names) is project_guidance
        assert "exec_command" in names


@pytest.mark.parametrize("name", ["read_text_file", "view_image"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_guidance_tool_refs_use_the_opening_grant(
    tmp_path: Path, responses, monkeypatch: pytest.MonkeyPatch, name: str, enabled: bool
) -> None:
    from agents import ToolRef
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_executor_tools import declaration

    async with ExecutorPeer(tmp_path / "remote").serve() as executor:
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certificates"))
        environment = declaration(executor)
        responses.replies.extend(
            [
                [assistant_output("selected")],
                [assistant_output("empty")],
                [assistant_output("workspace")],
                [assistant_output("updated")],
            ]
        )
        async with Agent(instructions="Answer.", provider=provider(responses)).session(
            environment=environment, project_guidance=enabled
        ) as session:
            if not enabled:
                with pytest.raises(ValueError, match=name):
                    await session.submit("Answer.", tools=[ToolRef(name, "functions")])
                assert responses.requests == []
                return
            completed = await (
                await session.submit("Answer.", tools=[ToolRef(name, "functions")])
            ).get_result()
            assert completed.final_output == "selected"
            assert {tool.get("name") for tool in function_declarations(responses.requests[0])} == {
                name
            }
            assert (
                await (await session.submit("Answer.", tools=[])).get_result()
            ).final_output == "empty"
            assert not function_declarations(responses.requests[1])
            assert (
                await (await session.submit("Answer.", tools=[environment])).get_result()
            ).final_output == "workspace"
            assert {"read_text_file", "view_image"}.issubset(
                {tool.get("name") for tool in function_declarations(responses.requests[2])}
            )
            await session.update(tools=[environment])
            assert (await (await session.submit("Answer.")).get_result()).final_output == "updated"
            assert {"read_text_file", "view_image"}.issubset(
                {tool.get("name") for tool in function_declarations(responses.requests[3])}
            )


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
@pytest.mark.parametrize(
    ("layout", "trust", "absolute", "readable", "guidance"),
    [
        ("sibling", None, False, True, True),
        ("sibling", None, True, False, False),
        ("inside", "trusted", False, True, True),
        ("inside", "untrusted", False, True, False),
        ("inside", "conflicting", False, True, False),
        ("canonical", "trusted", False, True, True),
    ],
)
async def test_local_project_guidance_alias_read_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses,
    layout: str,
    trust: str | None,
    absolute: bool,
    readable: bool,
    guidance: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = workspace
    if layout != "canonical":
        cwd = (workspace if layout == "inside" else tmp_path) / "alias"
        cwd.symlink_to(workspace, target_is_directory=True)
    home = tmp_path / "host-config"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    config = 'project_root_markers = [".project-root"]\n'
    if trust is not None:
        for path in dict.fromkeys([workspace, cwd]):
            level = (
                ("untrusted" if path == workspace else "trusted")
                if trust == "conflicting"
                else trust
            )
            config += f"[projects.{json.dumps(str(path))}]\ntrust_level = {json.dumps(level)}\n"
    (home / "config.toml").write_text(config)
    (workspace / ".project-root").write_text("")
    (workspace / "record.txt").write_text("SYNTHETIC_ALIAS_TEXT")
    (workspace / "pixel.png").write_bytes(_PIXEL_PNG)
    rules = workspace / ".agents" / "rules"
    rules.mkdir(parents=True)
    (rules / "text.md").write_text("---\npaths:\n  - record.txt\n---\nSYNTHETIC_TEXT_GUIDANCE")
    (rules / "image.md").write_text("---\npaths:\n  - pixel.png\n---\nSYNTHETIC_IMAGE_GUIDANCE")
    (rules / "other.md").write_text("---\npaths:\n  - other.txt\n---\nSYNTHETIC_OTHER_RULE")
    text_path = str(cwd / "record.txt") if absolute else "record.txt"
    image_path = str(cwd / "pixel.png") if absolute else "pixel.png"
    responses.replies.extend(
        [
            [function_call("read_text_file", {"path": text_path}, "text-1")],
            [function_call("view_image", {"path": image_path}, "image-1")],
            [assistant_output("reads complete")],
        ]
    )
    async with Agent(instructions="Answer.", provider=provider(responses)).session(
        environment=Environment.local(cwd=cwd, tools=(), writable_roots=(), write_policy="strict"),
        project_guidance=True,
    ) as session:
        assert (await (await session.submit("Read the files.")).get_result()).final_output == (
            "reads complete"
        )
    initial, after_text, after_image = map(json.dumps, responses.requests)
    assert "SYNTHETIC_TEXT_GUIDANCE" not in initial
    assert "SYNTHETIC_IMAGE_GUIDANCE" not in initial
    assert ("SYNTHETIC_ALIAS_TEXT" in after_text) is readable
    assert ("SYNTHETIC_TEXT_GUIDANCE" in after_text) is guidance
    assert "SYNTHETIC_IMAGE_GUIDANCE" not in after_text
    assert ("data:image/png;base64," in after_image) is readable
    assert ("SYNTHETIC_IMAGE_GUIDANCE" in after_image) is guidance
    assert "SYNTHETIC_OTHER_RULE" not in after_image
    assert all(
        tool.get("name") != "exec_command"
        for request in responses.requests
        for tool in function_declarations(request)
    )
