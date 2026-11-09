"""Workspace image viewing through the public Python native session option."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from agents import Agent, Environment
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider


async def test_project_guidance_requires_an_executor(responses) -> None:
    with pytest.raises(ValueError, match="requires an Environment"):
        async with Agent(instructions="Answer.", provider=provider(responses)).session(
            project_guidance=True
        ):
            pass
    assert responses.requests == []


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
        completed = await (await session.submit("Read the image.")).wait()
        assert completed.output == "image read"
    request = json.dumps(responses.requests[1])
    assert "data:image/png;base64," in request
    assert "SYNTHETIC_WORKSPACE_GUIDANCE" in request
    assert not any(
        tool.get("name") == "exec_command" for tool in responses.requests[0].get("tools", [])
    )
