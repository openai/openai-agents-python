"""Real private Core runs; all observations and failure owners remain public-compatible."""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any
from uuid import UUID

import pytest

from agents import Agent, Session
from agents.observation import EventGap, ModelRequestDispatching, ModelResponseLinked
from tests._asyncio import timeout
from tests.test_buffered_responses import json_peer, json_response, provider


class OrdinaryAgent(Agent[Any, str]):
    """Keep the oracle outside exact-Agent completion admission."""


def record_runs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    archives: list[list[dict[str, Any] | None]] | None = None,
) -> list[dict[str, Any]]:
    native = importlib.import_module("agents._native")
    original = native._prepare_run_conversation
    recorded: list[dict[str, Any]] = []

    class Owner:
        def __init__(self, actual: Any) -> None:
            self.actual = actual

        async def run(self) -> Any:
            result = await self.actual.run()
            recorded.append(json.loads(result[1]))
            if archives is not None:
                archives.append(json.loads(result[2].json()))
            return result

        def cancel(self) -> None:
            self.actual.cancel()

    monkeypatch.setattr(
        native, "_prepare_run_conversation", lambda *a, **k: Owner(original(*a, **k))
    )
    return recorded


@pytest.mark.parametrize("characters", [8, 512 * 1024])
async def test_disposable_completion_preserves_request_and_detached_views(
    characters: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = record_runs(monkeypatch)
    prompt = "PRIVATE_INPUT " + "x" * characters
    async with timeout(30), json_peer() as peer, provider(peer) as owner:
        private = Agent(
            model="gpt-5.2", instructions="Use the current instruction.", provider=owner
        )
        ordinary = OrdinaryAgent(
            model="gpt-5.2", instructions="Use the current instruction.", provider=owner
        )
        peer.json_replies.append(json_response("answer", "private-response", "private-item"))
        result = await private.run(prompt)
        peer.json_replies.append(json_response("answer", "ordinary-response", "ordinary-item"))
        expected = await ordinary.run(prompt)
        assert result.final_output == expected.final_output == "answer"
        assert result.thread_usage == expected.thread_usage
        assert len(recorded) == 1
        assert recorded[0]["private_disposable"] is True
        assert recorded[0]["status"] == "completed"
        assert all(recorded[0]["retired"].values())
        assert len(peer.requests) == 2
        for request in peer.requests:
            assert request["model"] == "gpt-5.2"
            assert any(
                item.get("role") == "user"
                and any(part.get("text") == prompt for part in item.get("content", []))
                for item in request["input"]
            )
            assert "Use the current instruction." in json.dumps(request)
    # First materialization follows Provider close, including a >64KiB original.
    rows = [row async for row in result.turn.events()]
    baseline = [row async for row in expected.turn.events()]
    assert [type(row) for row in rows] == [type(row) for row in baseline]
    dispatches = [row for row in rows if isinstance(row, ModelRequestDispatching)]
    links = [row for row in rows if isinstance(row, ModelResponseLinked)]
    assert len(dispatches) == len(links) == 1
    assert dispatches[0].request_id == links[0].request_id
    assert links[0].thread_id == result.turn.session_id
    assert links[0].turn_id == result.turn.turn_id
    UUID(links[0].response_observation_id)
    assert [row async for row in result.turn.events()] == rows


async def test_disposable_run_leaves_public_storage_and_parent_configuration_unchanged() -> None:
    async with timeout(30), json_peer() as peer, provider(peer) as owner:
        agent = Agent(model="gpt-5.2", instructions="Keep independent history.", provider=owner)
        peer.json_replies.append(json_response("private", "private", "private-item"))
        assert (await agent.run("PRIVATE_ONLY_INPUT")).final_output == "private"
        peer.json_replies.append(json_response("public", "public", "public-item"))
        async with agent.session() as session:
            turn = await session.submit("PUBLIC_ONLY_INPUT")
            assert (await turn.get_result()).final_output == "public"
            snapshot = await session.history_snapshot()
            assert "PUBLIC_ONLY_INPUT" in json.dumps(snapshot.items)
            assert "PRIVATE_ONLY_INPUT" not in json.dumps(snapshot.items)
            assert [item async for item in session.items()]
            state = await session.save_state()
        peer.json_replies.append(json_response("restored", "restored", "restored-item"))
        assert (await agent.run("RESTORED_INPUT", state=state)).final_output == "restored"
        assert "PUBLIC_ONLY_INPUT" in json.dumps(peer.requests[-1])
        assert "PRIVATE_ONLY_INPUT" not in json.dumps(peer.requests[-1])
        assert len(peer.requests) == 3


async def test_disposable_archive_overflow_keeps_live_model_history_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archives: list[list[dict[str, Any] | None]] = []
    recorded = record_runs(monkeypatch, archives=archives)
    actual_wait = Session._wait
    captured: list[bool] = []

    async def inspect(session: Session[Any, Any], *args: Any, **kwargs: Any) -> Any:
        result = await actual_wait(session, *args, **kwargs)
        # This existing model-context getter needs no original-history journal.
        context = await session._native._read_model_context()
        captured.append("HANDOFF_ORIGINAL_INPUT" in context)
        return result

    monkeypatch.setattr(Session, "_wait", inspect)
    output = "z" * (96 * 1024)
    async with timeout(30), json_peer() as peer, provider(peer) as owner:
        peer.json_replies.append(json_response(output, "overflow", "overflow-item"))
        agent = Agent(model="gpt-5.2", instructions="Keep the original request.", provider=owner)
        result = await agent.run("HANDOFF_ORIGINAL_INPUT " + "x" * (512 * 1024))
        assert result.final_output == output
        assert captured == [True]
        assert len(recorded) == 1 and recorded[0]["private_disposable"] is True
        assert recorded[0]["status"] == "live"
        assert len(peer.requests) == 1
        assert result.turn._session._closed and result.turn._session._resources_retired
        with pytest.raises(RuntimeError, match="closed|closing"):
            await result.turn._session.save_state()
    # Capture precedes adoption into the same bounded public buffer as ordinary Sessions.
    assert len(archives) == 1
    metadata = [
        event
        for event in archives[0]
        if event is not None and event["msg"]["type"] == "_sdk_request_metadata"
    ]
    dispatches = [
        event
        for event in metadata
        if event["msg"]["observation"]["type"] == "model_request_dispatching"
    ]
    links = [
        event
        for event in metadata
        if event["msg"]["observation"]["type"] == "model_response_linked"
    ]
    assert len(dispatches) == len(links) == 1
    linked = links[0]["msg"]["observation"]
    assert linked["request_id"] == dispatches[0]["msg"]["observation"]["request_id"]
    assert linked["thread_id"] == result.turn.session_id
    assert linked["turn_id"] == links[0]["id"] == result.turn.turn_id
    assert links[0]["msg"]["submission_id"] == result.turn.submission_id
    UUID(linked["response_observation_id"])
    assert any(
        event is not None
        and event["msg"]["type"] == "agent_message_content_delta"
        and len(event["msg"]["delta"]) > 65_536
        for event in archives[0]
    )
    rows = [row async for row in result.turn.events()]
    assert any(isinstance(row, EventGap) for row in rows)
    retained_links = [row for row in rows if isinstance(row, ModelResponseLinked)]
    assert len(retained_links) <= 1
    for link in retained_links:
        assert link.response_observation_id == linked["response_observation_id"]


async def test_disposable_live_handoff_joins_double_cancellation_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = importlib.import_module("agents._native")
    original = native._prepare_run_conversation
    captured, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed: dict[str, Any] = {}
    actual_adopt = Session._finish_run_opening
    adopted: list[Session[Any, Any]] = []

    class HeldOwner:
        def __init__(self, actual: Any) -> None:
            self.actual = actual

        async def run(self) -> Any:
            result = await self.actual.run()
            observed["scope"], info, _, observed["failures"] = result
            observed["info"] = json.loads(info)
            captured.set()
            await release.wait()
            return result

        def cancel(self) -> None:
            self.actual.cancel()
            stopped.set()

    async def adopt(session: Session[Any, Any], *args: Any, **kwargs: Any) -> None:
        adopted.append(session)
        await actual_adopt(session, *args, **kwargs)

    monkeypatch.setattr(
        native, "_prepare_run_conversation", lambda *a, **k: HeldOwner(original(*a, **k))
    )
    monkeypatch.setattr(Session, "_finish_run_opening", adopt)
    async with timeout(30), json_peer() as peer, provider(peer) as owner:
        peer.json_replies.append(json_response("z" * (96 * 1024), "held", "held-item"))
        agent = Agent(model="gpt-5.2", instructions="Keep cancellation ownership.", provider=owner)
        task = asyncio.create_task(agent.run("one private request"))
        try:
            await captured.wait()
            assert observed["info"]["private_disposable"] is True
            assert observed["info"]["status"] == "live" and not observed["failures"]
            assert observed["scope"] is not None
            with pytest.raises(RuntimeError, match=r"thread .* not found|persistence is disabled"):
                await observed["scope"][1].read_thread_history()
            task.cancel("original private cancellation")
            await stopped.wait()
            task.cancel("later private cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == ("original private cancellation",)
            assert len(adopted) == 1 and adopted[0]._closed and adopted[0]._resources_retired
            assert adopted[0]._reader is not None and adopted[0]._reader.done()
            assert len(peer.requests) == 1
            monkeypatch.setattr(native, "_prepare_run_conversation", original)
            peer.json_replies.append(json_response("recovered", "recovery", "recovery-item"))
            assert (await agent.run("independent recovery input")).final_output == "recovered"
            assert len(peer.requests) == 2
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            observed.clear()


@pytest.mark.parametrize("characters", [8, 512 * 1024])
async def test_explicit_websocket_selects_disposable_completion_without_http_policy(
    characters: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.native_fixtures import assistant_output
    from tests.test_native_provider_websockets import peer as websocket_peer

    recorded = record_runs(monkeypatch)
    prompt = "EXPLICIT_WS_INPUT " + "x" * characters
    async with timeout(30), websocket_peer() as server:
        owner = server.provider()
        assert owner.supports_websockets is True
        try:
            server.respond(assistant_output("private websocket answer"))
            result = await Agent(
                model="gpt-5.2", instructions="Keep the WebSocket choice.", provider=owner
            ).run(prompt)
            assert result.final_output == "private websocket answer"
            assert len(recorded) == 1 and recorded[0]["private_disposable"] is True
            assert recorded[0]["status"] == "completed"
            assert len(server.requests) == 1
            assert any(
                item.get("role") == "user"
                and any(part.get("text") == prompt for part in item.get("content", []))
                for item in server.requests[0]["input"]
            )
        finally:
            await owner.aclose()
    dispatches = [
        row async for row in result.turn.events() if isinstance(row, ModelRequestDispatching)
    ]
    links = [row async for row in result.turn.events() if isinstance(row, ModelResponseLinked)]
    assert len(dispatches) == len(links) == 1
    assert dispatches[0].transport == "websocket"
    assert dispatches[0].request_id == links[0].request_id
    UUID(links[0].response_observation_id)
