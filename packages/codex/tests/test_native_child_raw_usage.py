"""Exercise invocation-scoped raw usage across a real native collaboration."""

import json
from pathlib import Path
from typing import Any

import pytest

from agents import RawProviderUsage
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import agent_for, child_peer, collaboration, metadata, trust
from tests.test_native_executor_tools import declaration


@pytest.mark.asyncio
@pytest.mark.requires_native_linux_sandbox
async def test_native_original_provider_usage_includes_an_admitted_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_executor_binary: Path
) -> None:
    monkeypatch.setenv("CODEX_EXEC_SERVER_TEST_BINARY", str(native_executor_binary))
    private_marker = "synthetic-child-usage-extension"
    expected: list[RawProviderUsage] = []
    child_responses: set[str] = set()
    waited = False

    async with (
        timeout(30),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        original_response = peer.response

        def respond() -> list[dict[str, Any]]:
            nonlocal waited
            request = peer.requests[-1]
            actor = metadata(request, peer.request_headers[-1])["agent_name"]
            if actor != "/root":
                child_responses.add(f"resp_{len(peer.requests)}")
                return [assistant_output("child finished")]
            if peer.root_started and not waited:
                waited = True
                return [
                    collaboration(
                        "wait_agent",
                        {"timeout_ms": 10_000},
                        "raw-usage-wait",
                    )
                ]
            return original_response()

        async def report_usage(event: dict[str, Any]) -> None:
            if event["type"] != "response.completed":
                return
            response = event["response"]
            if response["id"] in child_responses:
                usage = response["usage"]
                usage["provider_extension"] = {"opaque": private_marker}
                expected.append(
                    RawProviderUsage(
                        response_id=response["id"], upstream_request_id=None, usage=usage
                    )
                )

        monkeypatch.setattr(peer, "response", respond)
        peer.before_event = report_usage
        trust(executor, tmp_path, monkeypatch)
        result = await agent_for(peer, executor).run(
            "Delegate and wait for the child.",
            environment=declaration(executor),
            return_raw_usage=True,
            return_history=True,
        )
    assert result.final_output == "root complete"
    assert waited and len(expected) == 1
    assert result.raw_usage is not None
    assert len(result.raw_usage) == 4
    assert expected[0] in result.raw_usage[1:-1]
    assert result.history is not None and private_marker not in json.dumps(result.history)
