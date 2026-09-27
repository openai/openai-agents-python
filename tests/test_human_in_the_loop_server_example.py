from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

import pytest

from agents import Agent, RunResult, UserError
from agents.decorators import tool
from agents.testing import ScriptedModel
from examples.agent_patterns.human_in_the_loop_server import ApprovalServer, PendingApproval

from .test_responses import get_function_tool_call, get_text_message


def make_server(
    calls: list[str],
    *,
    started: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
    fail: bool = False,
    repeat: bool = False,
    multiple: bool = False,
) -> ApprovalServer:
    @tool(needs_approval=True, failure_error_function=None)
    async def send_report(destination: str) -> str:
        calls.append(destination)
        if started is not None:
            started.set()
        if release is not None:
            await release.wait()
        if fail:
            raise RuntimeError("Synthetic tool failure")
        return "sent"

    steps = [[get_function_tool_call("send_report", '{"destination":"original"}', "call-1")]]
    if multiple:
        steps[0].append(get_function_tool_call("send_report", '{"destination":"second"}', "call-2"))
    if repeat:
        steps.append([get_function_tool_call("send_report", '{"destination":"second"}', "call-2")])
    steps.append([get_text_message("done")])
    return ApprovalServer(Agent(name="Reports", tools=[send_report], model=ScriptedModel(steps)))


async def pending(server: ApprovalServer) -> PendingApproval:
    response = await server.start("owner", "Send a report")
    assert isinstance(response, PendingApproval)
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_owner_decision_uses_server_snapshot(approved: bool) -> None:
    calls: list[str] = []
    server = make_server(calls)
    request = await pending(server)
    client_view = asdict(request)
    assert set(client_view) == {"request_id", "prompts"}
    assert set(client_view["prompts"][0]) == {"decision_id", "tool_name", "arguments"}
    client_view["prompts"][0]["arguments"] = '{"destination":"attacker"}'
    response = await server.decide(
        "owner", request.request_id, {request.prompts[0].decision_id: approved}
    )
    assert isinstance(response, RunResult)
    assert response.final_output == "done"
    assert calls == (["original"] if approved else [])
    with pytest.raises(ValueError, match="unavailable"):
        await server.decide("owner", request.request_id, {request.prompts[0].decision_id: True})


@pytest.mark.asyncio
async def test_foreign_owner_and_unknown_request_do_not_consume_pending_run() -> None:
    calls: list[str] = []
    server = make_server(calls)
    request = await pending(server)
    decisions = {request.prompts[0].decision_id: True}
    for user_id, request_id in [("other", request.request_id), ("owner", "unknown")]:
        with pytest.raises(ValueError, match="unavailable"):
            await server.decide(user_id, request_id, decisions)
    assert calls == []
    await server.decide("owner", request.request_id, decisions)
    assert calls == ["original"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "foreign", "extra", "non_boolean", "snapshot"])
async def test_invalid_batch_cannot_grant_approval(invalid: str) -> None:
    calls: list[str] = []
    server = make_server(calls)
    request = await pending(server)
    decision_id = request.prompts[0].decision_id
    batches: dict[str, dict[str, Any]] = {
        "missing": {},
        "foreign": {"unknown": True},
        "extra": {decision_id: True, "unknown": True},
        "non_boolean": {decision_id: "true"},
        "snapshot": {decision_id: True, "context": {"approvals": {"send_report": True}}},
    }
    with pytest.raises(ValueError, match="boolean decision"):
        await server.decide("owner", request.request_id, batches[invalid])
    assert calls == []
    await server.decide("owner", request.request_id, {decision_id: False})
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_consumption_prevents_in_flight_and_later_replay(outcome: str) -> None:
    calls: list[str] = []
    started, release = asyncio.Event(), asyncio.Event()
    server = make_server(calls, started=started, release=release, fail=outcome == "failure")
    request = await pending(server)
    decisions = {request.prompts[0].decision_id: True}
    first = asyncio.create_task(server.decide("owner", request.request_id, decisions))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(ValueError, match="unavailable"):
            await server.decide("owner", request.request_id, decisions)
        if outcome == "cancel":
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            release.set()
            if outcome == "failure":
                with pytest.raises(UserError, match="Synthetic tool failure"):
                    await first
            else:
                response = await first
                assert isinstance(response, RunResult)
                assert response.final_output == "done"
        with pytest.raises(ValueError, match="unavailable"):
            await server.decide("owner", request.request_id, decisions)
        assert calls == ["original"]
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_new_interruption_gets_new_owner_bound_decision_ids() -> None:
    calls: list[str] = []
    server = make_server(calls, repeat=True)
    first = await pending(server)
    second = await server.decide("owner", first.request_id, {first.prompts[0].decision_id: True})
    assert isinstance(second, PendingApproval)
    assert second.request_id != first.request_id
    assert second.prompts[0].decision_id != first.prompts[0].decision_id
    with pytest.raises(ValueError, match="boolean decision"):
        await server.decide("owner", second.request_id, {first.prompts[0].decision_id: True})
    response = await server.decide(
        "owner", second.request_id, {second.prompts[0].decision_id: False}
    )
    assert isinstance(response, RunResult)
    assert calls == ["original"]


@pytest.mark.asyncio
async def test_complete_batch_maps_each_decision_to_its_stored_call() -> None:
    calls: list[str] = []
    server = make_server(calls, multiple=True)
    request = await pending(server)
    assert len(request.prompts) == 2
    first, second = request.prompts
    with pytest.raises(ValueError, match="boolean decision"):
        await server.decide("owner", request.request_id, {first.decision_id: True})
    assert calls == []
    response = await server.decide(
        "owner", request.request_id, {second.decision_id: False, first.decision_id: True}
    )
    assert isinstance(response, RunResult)
    assert calls == ["original"]
