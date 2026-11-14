"""Verify remote cancellation through the native SDK and a controlled TLS peer."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from agents import Agent, Environment, NativeError, NativeProvider
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)

CALL_ID = "selected-terminal"


def command(yield_time_ms: int) -> dict[str, Any]:
    return function_call(
        "exec_command",
        {
            "cmd": "controlled-fixture-command",
            "tty": True,
            "login": False,
            "yield_time_ms": yield_time_ms,
        },
        CALL_ID,
    )


def selected_environment(peer: ExecutorPeer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert peer.ca is not None
    monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
    return Environment(
        url=peer.url,
        cwd=str(peer.root),
        headers={"Authorization": "Bearer " + peer.token},
        tools=("exec_command",),
        # Windows does not support strict file writes. The peer simulates
        # commands so this test can still exercise the TLS lifecycle there.
        write_policy="legacy" if sys.platform == "win32" else "strict",
    )


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-native-agent-key")


def terminate_requests(peer: ExecutorPeer):
    return [request for request in peer.requests if request.get("method") == "process/terminate"]


def matching_stop(peer: ExecutorPeer, data: dict[str, Any]) -> str | None:
    if data.get("result") != {"running": False}:
        return None
    return next(
        (
            request["params"]["processId"]
            for request in terminate_requests(peer)
            if request["id"] == data["id"]
            and request["params"]["processId"] in peer.synthetic_process_starts
        ),
        None,
    )


async def send_peer_exit(
    send_json, socket: web.WebSocketResponse, process_id: str, last_output: dict[str, int]
) -> None:
    seq = last_output[process_id] + 1
    await send_json(
        socket,
        {
            "method": "process/exited",
            "params": {
                "processId": process_id,
                "seq": seq,
                "exitCode": 137,
                "sandboxDenied": False,
            },
        },
    )
    await send_json(
        socket,
        {"method": "process/closed", "params": {"processId": process_id, "seq": seq + 1}},
    )


async def terminal_inventory(session) -> list[dict[str, Any]]:
    return json.loads(await session._native.call("list_background_terminals"))


async def assert_pending(cancel: asyncio.Task[None], gate: str) -> None:
    completed, _ = await asyncio.wait({cancel}, timeout=0.15)
    assert not completed, f"Turn.cancel() completed while {gate} was held"


@pytest.mark.parametrize("registered", [False, True], ids=["first-start", "registered"])
@pytest.mark.parametrize("ack_first", [True, False], ids=["ack-first", "exit-first"])
async def test_remote_cancel_waits_for_stop_and_peer_exit(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registered: bool,
    ack_first: bool,
) -> None:
    start_received = asyncio.Event()
    release_start = asyncio.Event()
    stop_received = asyncio.Event()
    release_stop = asyncio.Event()
    stop_sent = asyncio.Event()
    socket: web.WebSocketResponse | None = None
    last_output: dict[str, int] = {}
    send_json = web.WebSocketResponse.send_json

    async def hold_start(_process_id: str) -> None:
        start_received.set()
        await release_start.wait()

    async def intercept_stop_reply(
        selected_socket: web.WebSocketResponse, data: Any, *args: Any, **kwargs: Any
    ) -> None:
        nonlocal socket
        if data.get("method") == "process/output":
            params = data["params"]
            last_output[params["processId"]] = params["seq"]
        if matching_stop(peer, data) is not None:
            socket = selected_socket
            stop_received.set()
            await release_stop.wait()
            await send_json(selected_socket, data, *args, **kwargs)
            stop_sent.set()
            return
        await send_json(selected_socket, data, *args, **kwargs)

    monkeypatch.setattr(web.WebSocketResponse, "send_json", intercept_stop_reply)
    if registered:
        release_start.set()

    async with (
        timeout(20),
        ExecutorPeer(
            tmp_path / "remote",
            synthetic_process_output="remote-ready",
            synthetic_process_stays_running=True,
            synthetic_exit_on_terminate=False,
            before_synthetic_process_reply=hold_start,
        ).serve() as peer,
        provider(responses) as native,
        Agent(instructions="Call the controlled tool.", provider=native).session(
            environment=selected_environment(peer, tmp_path, monkeypatch)
        ) as session,
    ):
        responses.respond(command(30_000))
        turn = await session.submit("Start the controlled terminal.")
        async with timeout(5):
            await start_received.wait()
            if registered:
                for _ in range(400):
                    inventory = await terminal_inventory(session)
                    if any(entry["item_id"] == CALL_ID for entry in inventory):
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("The command never registered in Core")
        assert len(responses.requests) == 1
        cancel = asyncio.create_task(turn.cancel())

        async def send_exit() -> None:
            assert socket is not None
            [process_id] = peer.synthetic_process_starts
            await send_peer_exit(send_json, socket, process_id, last_output)

        exit_sent = False
        try:
            if not registered:
                await assert_pending(cancel, "the start reply")
                assert not stop_received.is_set()
                release_start.set()
            async with timeout(3):
                await stop_received.wait()
            [stop] = terminate_requests(peer)
            [process_id] = peer.synthetic_process_starts
            assert stop["params"]["processId"] == process_id
            if ack_first:
                release_stop.set()
                async with timeout(3):
                    await stop_sent.wait()
                await assert_pending(cancel, "peer exit")
                await send_exit()
                exit_sent = True
            else:
                await send_exit()
                exit_sent = True
                await assert_pending(cancel, "the stop reply")
                release_stop.set()
            async with timeout(3):
                await asyncio.shield(cancel)
            assert len(responses.requests) == 1

            responses.respond(assistant_output("successor finished"))
            successor = await session.submit("Continue in this session.")
            assert (await successor.get_result()).final_output == "successor finished"
            assert len(responses.requests) == 2
            assert terminate_requests(peer) == [stop]
            [transport] = peer.transports
            assert not transport.is_closing()
        finally:
            release_start.set()
            release_stop.set()
            if socket is not None and not exit_sent and not socket.closed:
                await send_exit()
            if not cancel.done():
                async with timeout(6):
                    await asyncio.gather(cancel, return_exceptions=True)
        assert len(peer.synthetic_process_starts) == 1
    assert len(terminate_requests(peer)) == 1


async def test_remote_cancel_raises_when_peer_acknowledges_stop_without_exiting(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses.respond(command(30_000))
    async with (
        timeout(20),
        ExecutorPeer(
            tmp_path / "remote",
            synthetic_process_output="remote-ready",
            synthetic_process_stays_running=True,
            synthetic_exit_on_terminate=False,
        ).serve() as peer,
        provider(responses) as native,
    ):
        with pytest.raises(NativeError) as scope_error:
            async with Agent(instructions="Call the controlled tool.", provider=native).session(
                environment=selected_environment(peer, tmp_path, monkeypatch)
            ) as session:
                turn = await session.submit("Start the controlled terminal.")
                async with timeout(5):
                    for _ in range(400):
                        inventory = await terminal_inventory(session)
                        if any(entry["item_id"] == CALL_ID for entry in inventory):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        pytest.fail("The command never registered in Core")
                # The synthetic peer acknowledges terminate and keeps process/read
                # running. Only a real exit notification can confirm cancellation.
                with pytest.raises(
                    NativeError, match="Session cancellation failed during owned cleanup"
                ) as error:
                    async with timeout(10):
                        await turn.cancel()
                assert isinstance(error.value.__cause__, RuntimeError)
                assert (
                    str(error.value.__cause__) == "Interrupted local command cleanup did not finish"
                )
                # Let session exit join cleanup with the observed cancel error as primary.
                raise error.value
        assert scope_error.value is error.value
        [stop] = terminate_requests(peer)
        [process_id] = peer.synthetic_process_starts
        assert stop["params"]["processId"] == process_id
        assert len(responses.requests) == 1


async def test_remote_close_retires_transport_when_start_reply_never_arrives(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_received = asyncio.Event()
    release_start = asyncio.Event()

    async def hold_start(_process_id: str) -> None:
        start_received.set()
        await release_start.wait()

    responses.respond(command(30_000))
    async with (
        ExecutorPeer(
            tmp_path / "remote",
            synthetic_process_output="remote-ready",
            synthetic_process_stays_running=True,
            before_synthetic_process_reply=hold_start,
        ).serve() as peer,
        provider(responses) as native,
    ):
        try:
            with pytest.raises(NativeError) as error:
                async with timeout(20):
                    async with Agent(
                        instructions="Call the controlled tool.", provider=native
                    ).session(
                        environment=selected_environment(peer, tmp_path, monkeypatch)
                    ) as session:
                        turn = await session.submit("Start the controlled terminal.")
                        async with timeout(5):
                            await start_received.wait()
                        with pytest.raises(NativeError) as cancelled:
                            await turn.cancel()
                        assert cancelled.value.__cause__ is not None
                        assert "cleanup did not finish" in str(cancelled.value.__cause__)
                        assert terminate_requests(peer) == []
                        raise cancelled.value
            assert error.value is cancelled.value
            [transport] = peer.transports
            assert transport.is_closing()
        finally:
            release_start.set()


async def test_outer_close_retires_named_profile_transport_with_held_start(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_received = asyncio.Event()
    release_start = asyncio.Event()

    async def hold_start(_process_id: str) -> None:
        start_received.set()
        await release_start.wait()

    responses.respond(function_call("transfer_to_destination", {}, "handoff"))
    responses.respond(command(30_000))
    async with (
        ExecutorPeer(
            tmp_path / "destination",
            synthetic_process_output="remote-ready",
            synthetic_process_stays_running=True,
            before_synthetic_process_reply=hold_start,
        ).serve() as peer,
        provider(responses) as native,
    ):
        destination = Agent(
            name="Destination",
            instructions="Call the controlled tool.",
            provider=native,
            environment=selected_environment(peer, tmp_path, monkeypatch),
        )
        root = Agent(
            name="Root",
            instructions="Transfer to Destination.",
            provider=native,
            handoffs=[destination],
        )
        try:
            async with timeout(40):
                with pytest.raises(
                    RuntimeError, match="Interrupted local command cleanup did not finish"
                ):
                    async with root.session() as session:
                        await session.submit("Transfer and start the controlled terminal.")
                        async with timeout(5):
                            await start_received.wait()
                        assert await session.get_active_agent() is destination
                [start] = [
                    request for request in peer.requests if request.get("method") == "process/start"
                ]
                assert start["params"]["cwd"] == peer.root.as_uri()
                assert terminate_requests(peer) == []
                await peer.wait_disconnected()
        finally:
            release_start.set()


async def test_delivered_terminal_survives_cancellation_and_successor(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    followup_waiting = asyncio.Event()
    release_followup = asyncio.Event()

    last_output: dict[str, int] = {}
    send_json = web.WebSocketResponse.send_json

    async def finish_terminal_on_stop(
        socket: web.WebSocketResponse, data: Any, *args: Any, **kwargs: Any
    ) -> None:
        if data.get("method") == "process/output":
            params = data["params"]
            last_output[params["processId"]] = params["seq"]
        await send_json(socket, data, *args, **kwargs)
        if (process_id := matching_stop(peer, data)) is not None:
            await send_peer_exit(send_json, socket, process_id, last_output)

    monkeypatch.setattr(web.WebSocketResponse, "send_json", finish_terminal_on_stop)

    async def pause_followup(_event: dict[str, Any]) -> None:
        if len(responses.requests) == 2:
            followup_waiting.set()
            await release_followup.wait()

    responses.respond(command(1000))
    responses.respond(assistant_output("interrupted followup"))
    responses.before_event = pause_followup
    async with (
        timeout(20),
        ExecutorPeer(
            tmp_path / "remote",
            synthetic_process_output="remote-ready",
            synthetic_process_stays_running=True,
            synthetic_exit_on_terminate=False,
        ).serve() as peer,
        provider(responses) as native,
        Agent(instructions="Call the controlled tool.", provider=native).session(
            environment=selected_environment(peer, tmp_path, monkeypatch)
        ) as session,
    ):
        turn = await session.submit("Start the controlled terminal.")
        try:
            async with timeout(5):
                await followup_waiting.wait()
            delivered = tool_output(responses.requests[1], CALL_ID)
            assert "Process running with session ID" in delivered
            before = await terminal_inventory(session)
            [terminal] = before
            assert terminal["item_id"] == CALL_ID
            assert str(terminal["process_id"]) in delivered
            async with timeout(3):
                await turn.cancel()
            assert await terminal_inventory(session) == before
            assert terminate_requests(peer) == []
        finally:
            release_followup.set()
            responses.before_event = None

        responses.respond(assistant_output("successor finished"))
        successor = await session.submit("Continue with the terminal still open.")
        assert (await successor.get_result()).final_output == "successor finished"
        assert await terminal_inventory(session) == before
        assert terminate_requests(peer) == []
        [transport] = peer.transports
        assert not transport.is_closing()
    [process_id] = peer.synthetic_process_starts
    [stop] = terminate_requests(peer)
    assert stop["params"]["processId"] == process_id
