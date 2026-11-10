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
from tests.test_native_agent import provider


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
    image.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        )
    )
    rules = tmp_path / ".claude" / "rules"
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
            assert {tool.get("name") for tool in responses.requests[0].get("tools", [])} == {name}
            assert (
                await (await session.submit("Answer.", tools=[])).get_result()
            ).final_output == "empty"
            assert not responses.requests[1].get("tools")
            assert (
                await (await session.submit("Answer.", tools=[environment])).get_result()
            ).final_output == "workspace"
            assert {"read_text_file", "view_image"}.issubset(
                {tool.get("name") for tool in responses.requests[2].get("tools", [])}
            )
