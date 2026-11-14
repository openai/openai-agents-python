"""Sandbox defaults use live Responses model controls and preserve explicit clears."""

import json
from contextlib import asynccontextmanager

import pytest

from agents import Agent, Environment, ModelSettings, SessionState
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
                saved = await session.save_state()
                assert "sdk_default_provider_compaction" not in saved.to_json()
                snapshots = [
                    row["payload"]["thread_settings"]
                    for row in saved.history
                    if row.get("type") == "event_msg"
                    and row.get("payload", {}).get("type") == "thread_settings_applied"
                ]
                assert snapshots
                assert all(snapshot.get("request_controls") != {} for snapshot in snapshots)
                await session.compact(summary="The initial answer has been completed.")
                responses.respond(assistant_output("continued"))
                assert (
                    await (await session.submit("Continue.")).get_result()
                ).final_output == "continued"
                assert (
                    "sdk_default_provider_compaction" not in (await session.save_state()).to_json()
                )
    finally:
        await native.aclose()

    assert len(responses.requests) == 2
    assert all(request.get("context_management") == expected for request in responses.requests)


@pytest.mark.parametrize("with_environment", [False, True], ids=["absent", "false"])
async def test_portable_compaction_default_requires_current_authority(
    tmp_path, monkeypatch, responses, with_environment
):
    native = provider(responses)
    try:
        async with ExecutorPeer(tmp_path / "portable").serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            environment = (
                Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(peer.root),
                    write_policy="legacy",
                )
                if with_environment
                else None
            )
            agent = Agent(
                instructions="Answer.",
                model="fixture-sandbox-default-compaction",
                provider=native,
            )
            responses.respond(assistant_output("before"))
            async with agent.session(environment=environment) as session:
                await (await session.submit("Begin.")).get_result()
                saved = json.loads((await session.save_state()).to_json())
            settings = [
                row["payload"]["thread_settings"]
                for row in saved["history"]
                if row.get("payload", {}).get("type") == "thread_settings_applied"
            ]
            assert settings
            for snapshot in settings:
                snapshot.setdefault("request_controls", {})["sdk_default_provider_compaction"] = (
                    True
                )
            responses.respond(assistant_output("after"))
            async with agent.session(
                state=SessionState.from_json(json.dumps(saved)), environment=environment
            ) as restored:
                assert (
                    await (await restored.submit("Continue.")).get_result()
                ).final_output == "after"
    finally:
        await native.aclose()
    assert len(responses.requests) == 2
    assert all(request.get("context_management") is None for request in responses.requests)
