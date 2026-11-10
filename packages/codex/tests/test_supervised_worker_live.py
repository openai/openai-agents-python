"""Supervise the example across processes with the native runtime and a synthetic model."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket as unix_socket
import stat
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from agents import Agent, HttpMCP, NativeProvider, Session, Turn
from examples.codex.supervised_worker import live
from examples.codex.supervised_worker.__main__ import ask_supervisor
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_mcp_tools import mcp_call
from tests.test_native_scoped_policies import AskingPeer
from tests.test_supervised_worker_example import process

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="Requires a Unix socket"
)


@asynccontextmanager
async def supervisor(socket: Path, *arguments: str):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "examples.codex.supervised_worker.live",
        str(socket),
        *arguments,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3])},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield child
    finally:
        if child.returncode is None:
            child.kill()
        await child.communicate()


async def command(
    socket: Path, *arguments: str, data: bytes = b"", exit_code: int = 0
) -> dict[str, Any]:
    async with supervisor(socket, *arguments) as client:
        stdout, stderr = await client.communicate(data)
        assert client.returncode == exit_code, (stdout, stderr)
        return json.loads(stdout)


async def stop_owner(owner: asyncio.subprocess.Process, socket: Path) -> None:
    owner.send_signal(signal.SIGTERM)
    stdout, stderr = await owner.communicate()
    assert owner.returncode == 0, (stdout, stderr)
    assert json.loads(stdout)["status"] == "closed"
    assert not socket.parent.exists()


@asynccontextmanager
async def serving(
    session: Session[Any, Any],
    prompt: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    turn_timeout: float = 15,
) -> AsyncIterator[Path]:
    ready = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(
        live,
        "print",
        lambda message, **_: ready.set_result(Path(json.loads(message)["socket"])),
        raising=False,
    )
    stop = asyncio.Event()
    owner = asyncio.create_task(live.serve_turn(session, prompt, timeout=turn_timeout, stop=stop))
    try:
        yield await ready
    finally:
        stop.set()
        await owner


def observe_wait_dispatch(
    monkeypatch: pytest.MonkeyPatch, after: asyncio.Event, entered: asyncio.Event
) -> None:
    dispatch = live._LiveTurn.dispatch

    async def observe(self, request, writer):
        if request["action"] == "wait" and after.is_set():
            entered.set()
        await dispatch(self, request, writer)

    monkeypatch.setattr(live._LiveTurn, "dispatch", observe)


async def test_supervisor_sends_nothing_to_a_different_peer_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = asyncio.get_running_loop().create_future()

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read()
        writer.close()
        await writer.wait_closed()
        received.set_result(data)

    monkeypatch.setattr(live, "_peer_uid", lambda _sock: os.geteuid() + 1)
    with TemporaryDirectory(prefix="agents-peer-", dir="/tmp") as directory:
        socket = Path(directory) / "control.sock"
        server = await asyncio.start_unix_server(receive, socket)
        async with server, timeout(5):
            socket.chmod(0o600)
            with pytest.raises(PermissionError, match="different Unix user"):
                await live._attach(
                    socket, b'{"action":"steer","text":"Synthetic correction"}\n', stream=False
                )
            assert await received == b""


async def test_supervisor_reconnects_to_the_same_native_turn(
    tmp_path: Path, responses: ResponsesFixture
) -> None:
    answer = "青" * 12_000
    responses.respond(
        function_call("ask_supervisor", {"question": "Which color?"}, "synthetic-question")
    )
    responses.respond(assistant_output("Finished the task."))
    async with (
        timeout(30),
        process(
            "serve", tmp_path / "alpha", responses.url, "--prompt", "Ask me for a color."
        ) as owner,
    ):
        assert owner.stdout is not None
        ready = json.loads(await owner.stdout.readline())
        socket = Path(ready["socket"])
        assert stat.S_IMODE(socket.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(socket.stat().st_mode) == 0o600
        alias = socket.parent / "alias.sock"
        alias.symlink_to(socket)
        async with supervisor(alias, "status") as refused:
            stdout, stderr = await refused.communicate()
            assert refused.returncode == 1
            assert stdout == b""
            assert b"Could not attach" in stderr

        async with supervisor(socket, "events") as watching:
            assert watching.stdout is not None
            started = json.loads(await watching.stdout.readline())
            assert started["type"] == "turn_started"
            assert started["session_id"] == ready["session_id"]
            assert started["submission_id"] == ready["submission_id"]
            turn_id = started["turn_id"]
            # This client exits while the native question is still unanswered.
        assert (await command(socket, "status"))["turn_id"] == turn_id
        assert await command(socket, "events", "--after", "1000000000", exit_code=1) == {
            "error": "invalid_request"
        }

        attention = await command(socket, "wait")
        assert attention["status"] == "attention"
        assert len(responses.requests) == 1
        [question] = attention["questions"]
        assert question["turn_id"] == turn_id
        assert question["arguments"] == {"question": "Which color?"}
        question_id = question["question_id"]
        assert (await command(socket, "wait"))["questions"] == [question]

        assert await command(
            socket, "steer", "stale-turn", data=b"Ignore this stale instruction", exit_code=1
        ) == {"error": "stale_turn"}
        async with supervisor(socket, "steer", turn_id) as oversized:
            stdout, stderr = await oversized.communicate(b"x" * (64 * 1024 - 32))
            assert oversized.returncode == 2
            assert stdout == b""
            assert b"The supervisor request is too large" in stderr
        receipt = await command(socket, "steer", turn_id, data=b"Use the chosen color.")
        assert receipt["status"] == "steered"
        assert receipt["turn_id"] == turn_id
        assert receipt["submission_id"] == started["submission_id"]
        assert receipt["input_id"]

        assert await command(
            socket, "answer", "stale-turn", question_id, data=b'"red"', exit_code=1
        ) == {"error": "expired_question"}
        accepted = await command(
            socket,
            "answer",
            turn_id,
            question_id,
            data=json.dumps(answer, ensure_ascii=False).encode(),
        )
        assert accepted["status"] == "answered"
        assert await command(
            socket, "answer", turn_id, question_id, data=b'"red"', exit_code=1
        ) == {"error": "expired_question"}

        completed = await command(socket, "wait")
        assert completed["status"] == "completed"
        assert completed["turn_id"] == turn_id
        assert completed["output"] == "Finished the task."
        async with supervisor(socket, "events", "--after", str(started["sequence"])) as replay:
            stdout, stderr = await replay.communicate()
            assert replay.returncode == 0, (stdout, stderr)
        events = [json.loads(line) for line in stdout.splitlines()]
        assert all(event["sequence"] > started["sequence"] for event in events)
        assert any(
            event["type"] == "turn_finished"
            and event["status"] == "completed"
            and event["turn_id"] == turn_id
            for event in events
        )
        assert await command(
            socket, "steer", turn_id, data=b"Ignore this late instruction", exit_code=1
        ) == {"error": "stale_turn"}
        await stop_owner(owner, socket)

    assert len(responses.requests) == 2
    assert tool_output(responses.requests[1], "synthetic-question") == answer
    inputs = json.dumps(responses.requests[1]["input"])
    assert "Use the chosen color." in inputs
    assert "Ignore this" not in inputs


async def test_supervisor_cancels_the_observed_turn(
    tmp_path: Path, responses: ResponsesFixture
) -> None:
    responses.respond(
        function_call("ask_supervisor", {"question": "Continue?"}, "synthetic-cancel")
    )
    async with (
        timeout(30),
        process(
            "serve", tmp_path / "alpha", responses.url, "--prompt", "Ask before proceeding."
        ) as owner,
    ):
        assert owner.stdout is not None
        socket = Path(json.loads(await owner.stdout.readline())["socket"])
        attention = await command(socket, "wait")
        assert attention["status"] == "attention"
        turn_id = attention["turn_id"]

        assert await command(socket, "cancel", "stale-turn", exit_code=1) == {"error": "stale_turn"}
        assert (await command(socket, "status"))["status"] == "active"
        assert (await command(socket, "cancel", turn_id))["status"] == "cancel_joined"
        assert (await command(socket, "wait"))["status"] == "interrupted"
        reader, writer = await asyncio.open_unix_connection(socket)
        try:
            await stop_owner(owner, socket)
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
    assert len(responses.requests) == 1


async def test_owner_deadline_joins_native_interruption(
    tmp_path: Path, responses: ResponsesFixture
) -> None:
    responses.respond(
        function_call("ask_supervisor", {"question": "Continue?"}, "synthetic-deadline")
    )
    async with (
        timeout(30),
        process(
            "serve",
            tmp_path / "alpha",
            responses.url,
            "--prompt",
            "Ask before proceeding.",
            "--turn-timeout",
            "2",
        ) as owner,
    ):
        assert owner.stdout is not None
        socket = Path(json.loads(await owner.stdout.readline())["socket"])
        assert (await command(socket, "wait"))["status"] == "attention"
        async with supervisor(socket, "events") as events:
            stdout, stderr = await events.communicate()
            assert events.returncode == 0, (stdout, stderr)
        assert (await command(socket, "wait"))["status"] == "interrupted"
        await stop_owner(owner, socket)
    assert len(responses.requests) == 1


async def test_supervisor_reports_native_connection_loss(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses.respond(function_call("ask_supervisor", {"question": "Continue?"}, "synthetic-eof"))
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic", supports_websockets=False
    )
    agent = Agent(provider=provider, instructions="Ask before proceeding.", tools=[ask_supervisor])
    async with timeout(30), provider, agent.session() as session:
        thread = session._native
        disconnect, retiring, release, entered, attempted = (asyncio.Event() for _ in range(5))

        class DisconnectedThread:
            saw_question = False

            def __getattr__(self, name: str) -> Any:
                return getattr(thread, name)

            async def _next_session_event(self) -> str:
                if self.saw_question:
                    await disconnect.wait()
                    return "null"
                raw = await thread._next_session_event()
                self.saw_question = bool(json.loads(raw)["msg"].get("_sdk_manual"))
                return raw

            async def close(self) -> None:
                retiring.set()
                await release.wait()
                await thread.close()

        cancel = Turn.cancel

        async def observe_cancel(turn):
            try:
                await cancel(turn)
            finally:
                attempted.set()

        monkeypatch.setattr(session, "_native", DisconnectedThread())
        monkeypatch.setattr(Turn, "cancel", observe_cancel)
        observe_wait_dispatch(monkeypatch, retiring, entered)
        async with serving(
            session, "Ask before proceeding.", monkeypatch, turn_timeout=3
        ) as socket:
            attention = await command(socket, "wait")
            assert attention["status"] == "attention"
            disconnect.set()
            await retiring.wait()
            with pytest.raises(RuntimeError, match="cannot continue"):
                await session.pending_interactions()
            wait = asyncio.create_task(command(socket, "wait"))
            try:
                await entered.wait()
                await attempted.wait()
                assert (await command(socket, "status"))["status"] == "active"
            finally:
                release.set()
                outcome = await wait
            assert outcome["status"] == "connection_lost"
            assert await command(
                socket, "steer", attention["turn_id"], data=b"Late correction.", exit_code=1
            ) == {"error": "stale_turn"}


async def test_supervisor_reports_connection_loss_during_owner_startup(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses.respond(assistant_output("Finished."))
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic", supports_websockets=False
    )
    agent = Agent(provider=provider, instructions="Answer briefly.")
    async with timeout(30), provider, agent.session() as session:
        thread = session._native
        disconnect = asyncio.Event()

        class DisconnectedThread:
            def __getattr__(self, name: str) -> Any:
                return getattr(thread, name)

            async def _next_session_event(self) -> str:
                await disconnect.wait()
                return "null"

        submit = session.submit

        async def submit_and_disconnect(prompt: str, **kwargs: Any):
            turn = await submit(prompt, **kwargs)
            disconnect.set()
            await session._reader
            return turn

        monkeypatch.setattr(session, "_native", DisconnectedThread())
        monkeypatch.setattr(session, "submit", submit_and_disconnect)
        async with serving(session, "Finish the task.", monkeypatch) as socket:
            assert (await command(socket, "wait"))["status"] == "connection_lost"


async def test_supervisor_wait_hides_question_during_cancel_across_deadline(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses.respond(function_call("ask_supervisor", {"question": "Continue?"}, "synthetic-race"))
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic", supports_websockets=False
    )
    agent = Agent(provider=provider, instructions="Ask before proceeding.", tools=[ask_supervisor])
    async with timeout(30), provider, agent.session() as session:
        thread = session._native
        parked, release, checked = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cancel_started, deadline_reached = asyncio.Event(), asyncio.Event()

        class ParkedInterrupt:
            def __getattr__(self, name: str) -> Any:
                return getattr(thread, name)

            async def _interrupt_session(self, *args: Any, **kwargs: Any) -> Any:
                parked.set()
                await release.wait()
                return await thread._interrupt_session(*args, **kwargs)

        monkeypatch.setattr(session, "_native", ParkedInterrupt())
        native_cancel = Turn.cancel

        async def track_cancel(turn):
            if cancel_started.is_set():
                deadline_reached.set()
            cancel_started.set()
            await native_cancel(turn)

        monkeypatch.setattr(Turn, "cancel", track_cancel)
        observe_wait_dispatch(monkeypatch, parked, checked)
        async with serving(
            session, "Ask before proceeding.", monkeypatch, turn_timeout=3
        ) as socket:
            [question] = (await command(socket, "wait"))["questions"]
            cancel = asyncio.create_task(command(socket, "cancel", question["turn_id"]))
            try:
                await parked.wait()
                assert not await session.pending_interactions()
                wait = asyncio.create_task(command(socket, "wait"))
                try:
                    await checked.wait()
                    await deadline_reached.wait()
                finally:
                    release.set()
                    outcome = await wait
            finally:
                release.set()
                joined = await cancel
            assert joined["status"] == "cancel_joined"
            assert outcome["status"] == "interrupted"


async def test_supervisor_waits_for_native_close_while_worker_declines_mcp_form(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = AskingPeer()
    peer.names = ("lookup",)
    responses.respond(mcp_call("lookup", "mcp__job_notes", "synthetic-form-eof"))
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic", supports_websockets=False
    )
    async with timeout(30), peer.serve() as url, provider:
        agent = Agent(
            provider=provider,
            instructions="Read the job notes.",
            tools=[HttpMCP(name="job_notes", url=url, tools=["lookup"])],
        )
        async with agent.session() as session:
            thread = session._native
            declining, disconnect, retiring, release = (asyncio.Event() for _ in range(4))

            class ParkedDecline:
                saw_form = False

                def __getattr__(self, name: str) -> Any:
                    return getattr(thread, name)

                async def _next_session_event(self) -> str:
                    if self.saw_form:
                        await disconnect.wait()
                        return "null"
                    raw = await thread._next_session_event()
                    self.saw_form = json.loads(raw)["msg"]["type"] == "elicitation_request"
                    return raw

                async def call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
                    if operation == "resolve_elicitation":
                        declining.set()
                        await asyncio.Future()
                    return await thread.call(operation, *args, **kwargs)

                async def close(self) -> None:
                    retiring.set()
                    await release.wait()
                    await thread.close()

            monkeypatch.setattr(session, "_native", ParkedDecline())
            async with serving(session, "Read the job notes.", monkeypatch) as socket:
                await declining.wait()
                disconnect.set()
                try:
                    await retiring.wait()
                    assert (await command(socket, "status"))["status"] == "active"
                finally:
                    release.set()
                    peer.answered.set()
                assert (await command(socket, "wait"))["status"] == "connection_lost"


async def test_worker_declines_mcp_form_and_supervisor_reads_execution_details(
    tmp_path: Path, responses: ResponsesFixture
) -> None:
    peer = AskingPeer()
    peer.names = ("lookup",)
    lookup = mcp_call("lookup", "mcp__job_notes", "synthetic-form")
    key = "🌈" * 50_000
    lookup["item"]["arguments"] = json.dumps({"key": key}, ensure_ascii=False)
    responses.respond(lookup)
    responses.respond(assistant_output("Lookup complete."))
    async with (
        timeout(30),
        peer.serve() as url,
        process(
            "serve",
            tmp_path / "alpha",
            responses.url,
            "--prompt",
            "Read the job notes.",
            "--mcp-url",
            url,
        ) as owner,
    ):
        assert owner.stdout is not None
        socket = Path(json.loads(await owner.stdout.readline())["socket"])
        settled = await command(socket, "wait")
        assert settled["status"] == "completed"
        assert settled["output"] == "Lookup complete."
        assert [answer["result"]["action"] for answer in peer.answers] == ["decline"]

        async with supervisor(socket, "events") as summary:
            stdout, stderr = await summary.communicate()
            assert summary.returncode == 0, (stdout, stderr)
        summary_events = [json.loads(line) for line in stdout.splitlines()]
        summary_item = next(
            event["item"]
            for event in summary_events
            if event["type"] == "execution_item_completed" and event["item"]["type"] == "mcp_call"
        )
        assert "arguments" not in summary_item
        assert "output" not in summary_item
        async with supervisor(socket, "events", "--execution-details") as replay:
            stdout, stderr = await replay.communicate()
            assert replay.returncode == 0, (stdout, stderr)
        detail = next(
            event["item"]
            for line in stdout.splitlines()
            if (event := json.loads(line))["type"] == "execution_item_completed"
            and event["item"]["id"] == summary_item["id"]
        )
        assert detail["name"] == "lookup"
        assert detail["arguments"] == {"key": key}
        assert max(map(len, stdout.splitlines())) > 64 * 1024

        # Read one byte and leave the large event backed up in the worker's transport.
        with unix_socket.socket(unix_socket.AF_UNIX) as stalled:
            stalled.setblocking(False)
            loop = asyncio.get_running_loop()
            await loop.sock_connect(stalled, str(socket))
            await loop.sock_sendall(stalled, b'{"action":"events","execution_details":true}\n')
            assert await loop.sock_recv(stalled, 1) == b"{"
            async with timeout(5):
                await stop_owner(owner, socket)
