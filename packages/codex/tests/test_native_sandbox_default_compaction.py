"""Sandbox defaults use live Responses model controls and preserve explicit clears."""

from contextlib import asynccontextmanager

import pytest

from agents import Agent, Environment, ModelSettings
from agents.sandbox import Manifest, sandbox_environment
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_sandbox_environment import Client, Options


@pytest.mark.parametrize(
    ("defaults", "settings", "expected"),
    [
        (True, {}, [{"type": "compaction", "compact_threshold": 240000}]),
        (False, {}, None),
        (True, {"context_management": []}, []),
        (True, {"context_management": None}, None),
        (True, {"provider_compaction": None}, None),
        (
            True,
            {"provider_compaction": {"type": "static", "threshold": 1024}},
            [{"type": "compaction", "compact_threshold": 1024}],
        ),
    ],
)
async def test_current_sandbox_capability_controls(
    tmp_path, monkeypatch, responses, defaults, settings: ModelSettings, expected
):
    native = provider(responses)
    client = Client([])
    try:
        async with ExecutorPeer(tmp_path / "sandbox").serve() as peer:
            trust(peer, tmp_path, monkeypatch)

            @asynccontextmanager
            async def connect(_sandbox):
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(peer.root),
                    write_policy="legacy",
                )
                await peer.wait_disconnected()

            source = sandbox_environment(
                client,
                manifest=Manifest(root=str(peer.root)),
                options=Options(),
                connect=connect,
                **({} if defaults else {"capabilities": []}),
            )
            responses.respond(assistant_output("done"))
            agent = Agent(
                instructions="Answer.",
                model="fixture-sandbox-default-compaction",
                provider=native,
                model_settings=settings,
            )
            async with agent.session(environment=source) as session:
                assert (await (await session.submit("Begin.")).get_result()).final_output == "done"
                assert (
                    "sdk_default_provider_compaction" not in (await session.save_state()).to_json()
                )
    finally:
        await native.aclose()

    assert len(responses.requests) == 1
    assert responses.requests[0].get("context_management") == expected
