"""Keep unknown model limits distinct from native operating defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents import Agent, NativeAgents, NativeRole, SessionState
from agents.observation import UsageUpdated
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent_observation import collect, provider
from tests.test_native_child_agents import child_peer, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_usage import _omit_usage, _report_usage

UNKNOWN_MODEL = "fixture-model-without-context-metadata"


async def test_unknown_model_reports_null_in_results_and_usage_events(
    responses: ResponsesFixture,
) -> None:
    responses.before_event = _report_usage
    responses.respond(assistant_output("finished"))
    agent = Agent(model=UNKNOWN_MODEL, instructions="Answer.", provider=provider(responses))
    async with timeout(30), agent.session() as session:
        turn = await session.submit("one")
        result = await turn.get_result()
        assert result.final_output == "finished"
        assert result.usage is not None
        assert result.usage.model_context_window is None
        assert result.usage.total_token_usage.total_tokens == 24
        events = [
            event for event in await collect(turn.events()) if isinstance(event, UsageUpdated)
        ]
        assert events and events[-1].usage == result.usage
        assert all(
            event.usage is None or event.usage.model_context_window is None for event in events
        )
        assert responses.requests[0]["model"] == UNKNOWN_MODEL
        assert len(responses.requests) == 1


async def test_new_usage_clears_old_limit_and_null_survives_restore_and_fork(
    responses: ResponsesFixture,
) -> None:
    for text in ("known", "no usage", "unknown", "restored", "forked"):
        responses.respond(assistant_output(text))
    responses.before_event = _report_usage
    agent = Agent(model="gpt-5.5", instructions="Answer.", provider=provider(responses))
    async with timeout(30):
        async with agent.session() as session:
            first = await session.submit("known model")
            known = await first.get_result()
            assert known.usage is not None
            assert known.usage.model_context_window is not None
            assert known.usage.model_context_window > 0

            await session.update(model=UNKNOWN_MODEL)
            responses.before_event = _omit_usage
            second = await session.submit("unknown model without new usage")
            unchanged = await second.get_result()
            # Changing a model does not rewrite the last observed usage snapshot.
            assert unchanged.usage == known.usage

            responses.before_event = _report_usage
            third = await session.submit("unknown model with new usage")
            unknown = await third.get_result()
            assert unknown.usage is not None
            assert unknown.usage.model_context_window is None
            assert unknown.usage.total_token_usage.total_tokens == 48
            assert unknown.usage.last_token_usage.total_tokens == 24
            assert known.usage.model_context_window is not None
            saved = SessionState.from_json((await session.save_state()).to_json())
            forked = SessionState.from_json((await session.fork()).to_json())

        responses.before_event = _omit_usage
        for state in (saved, forked):
            async with agent.session(state=state) as restored:
                turn = await restored.submit("keep the observed snapshot")
                result = await turn.get_result()
                assert result.usage == unknown.usage
                events = [
                    event
                    for event in await collect(turn.events())
                    if isinstance(event, UsageUpdated)
                ]
                assert events and all(event.usage == unknown.usage for event in events)
        assert len(responses.requests) == 5
        assert [request["model"] for request in responses.requests] == [
            "gpt-5.5",
            *([UNKNOWN_MODEL] * 4),
        ]


async def test_child_metadata_uses_its_own_model_instead_of_the_parent_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with (
        timeout(30),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        root_response = peer.response

        def respond() -> list[dict[str, Any]]:
            path = metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"]
            if path == "/root":
                return root_response()
            answer = assistant_output("child complete")
            answer["item"]["id"] = "done-alpha"
            return [answer]

        async def report_and_hold(event: dict[str, Any]) -> None:
            await _report_usage(event)
            await peer.hold(event)

        monkeypatch.setattr(peer, "response", respond)
        peer.before_event = report_and_hold
        agent = Agent(
            model="gpt-5.5",
            provider=peer.provider(),
            instructions="Delegate.",
            tools=[
                NativeAgents(
                    additional_roles=[
                        NativeRole(
                            name="research",
                            description="Read the record.",
                            instructions="Answer.",
                            model="gpt-daybreak-red-latest",
                        )
                    ]
                )
            ],
        )
        async with agent.session(environment=declaration(executor)) as session:
            try:
                root = await session.submit("Start the research child")
                await peer.entered["alpha"].wait()
                parent = await root.get_result()
                assert parent.usage is not None
                assert parent.usage.model_context_window == 258_400
                [child] = await handles(session, 1)
                peer.release["alpha"].set()
                result = await child.get_result()
                assert result.final_output == "child complete"
                assert result.usage is not None
                assert result.usage.model_context_window == 353_400
                assert result.usage.last_token_usage.total_tokens == 24
                events = [
                    event
                    for event in await collect(child.events())
                    if isinstance(event, UsageUpdated)
                ]
                assert events and events[-1].usage == result.usage
                assert all(
                    event.usage is None or event.usage.model_context_window == 353_400
                    for event in events
                )
                parent_events = [
                    event
                    for event in await collect(root.events())
                    if isinstance(event, UsageUpdated)
                ]
                assert parent_events and parent_events[-1].usage == parent.usage
                assert all(
                    event.usage is None or event.usage.model_context_window == 258_400
                    for event in parent_events
                )
                assert [
                    body["model"]
                    for body in peer.requests
                    if body["model"] == "gpt-daybreak-red-latest"
                ] == ["gpt-daybreak-red-latest"]
                # The settled parent keeps its own known snapshot after child completion.
                assert parent.usage.model_context_window == 258_400
            finally:
                peer.release["alpha"].set()
