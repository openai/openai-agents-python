"""Explicit host trust for native project guidance, without ambient model selection."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from agents import Agent, Environment
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider


@pytest.mark.parametrize("trusted", [False, True], ids=["untrusted", "trusted"])
async def test_project_guidance_projects_only_explicit_host_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, responses, trusted: bool
) -> None:
    home = tmp_path / "host-config"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    async with ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certificates"))
        (home / "config.toml").write_text(
            'model = "ambient-model-canary"\nmodel_provider = "ambient-provider-canary"\n'
            '[model_providers.ambient-provider-canary]\nname = "ambient-provider-canary"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
            f"[projects.{json.dumps(str(peer.root))}]\n"
            f"trust_level = {json.dumps('trusted' if trusted else 'untrusted')}\n"
        )
        image = peer.root / "pixel.png"
        image.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
            )
        )
        rules = peer.root / ".agents" / "rules"
        rules.mkdir(parents=True)
        marker = "EXPLICIT_HOST_TRUST_GUIDANCE"
        (rules / "general.md").write_text(marker)
        responses.replies.extend(
            [
                [assistant_output("baseline")],
                [function_call("view_image", {"path": str(image)}, "image-1")],
                [assistant_output("image read")],
            ]
        )
        agent = Agent(instructions="Answer.", provider=provider(responses))
        environment = Environment(
            url=peer.url,
            cwd=str(peer.root),
            headers={"Authorization": "Bearer " + peer.token},
        )
        async with agent.session(environment=environment) as baseline:
            assert (
                await (await baseline.submit("Answer.")).get_result()
            ).final_output == "baseline"
        baseline_model = responses.requests[0]["model"]
        assert baseline_model != "ambient-model-canary"
        assert marker not in json.dumps(responses.requests[0])
        async with agent.session(environment=environment, project_guidance=True) as session:
            assert (
                await (await session.submit("Read the image.")).get_result()
            ).final_output == "image read"
        assert len(responses.requests) == 3
        assert all(request["model"] == baseline_model for request in responses.requests)
        model_input = json.dumps(responses.requests[2])
        assert "data:image/png;base64," in model_input
        assert (marker in model_input) is trusted
        assert environment.tools == ()
        assert all(
            tool.get("name") != "exec_command"
            for request in responses.requests
            for tool in request.get("tools", [])
        )
        assert not any(request.get("method") == "process/start" for request in peer.requests)
