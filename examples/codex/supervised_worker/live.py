"""Supervise a worker-owned Turn through a private Unix socket."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import socket
import stat
import struct
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from agents import (
    ActionRequested,
    ApprovalRequest,
    FormRequest,
    Interaction,
    InteractionExpiredError,
    NativeError,
    Session,
    StaleTurnError,
    Turn,
    TurnNeedsAttention,
    TurnStoppedError,
    UrlRequest,
)
from agents.observation import (
    EventGap,
    ExecutionItemCompleted,
    OutputTextDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    UsageUpdated,
)

_OBSERVATIONS = (
    EventGap,
    ExecutionItemCompleted,
    OutputTextDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    UsageUpdated,
)
_REQUEST_LIMIT = 64 * 1024
# A bounded SDK observation holds up to 64 Ki characters. JSON escapes can expand them.
_EVENT_LINE_LIMIT = 1024 * 1024


async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(json.dumps(message, allow_nan=False).encode() + b"\n")
    await writer.drain()


def _peer_uid(sock: socket.socket) -> int:
    if sys.platform == "linux":
        credentials = struct.Struct("iII")
        _, uid, _ = credentials.unpack(
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, credentials.size)
        )
        return uid
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
        getpeereid = ctypes.CDLL(None, use_errno=True).getpeereid
        if getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
            return uid.value
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    raise OSError("Local supervision requires Linux or macOS")


class _LiveTurn:
    """Keep reply authority and result ownership inside the worker process."""

    def __init__(self, session: Session[Any, Any], turn: Turn[str]) -> None:
        self.session = session
        self.turn = turn
        self.questions: dict[str, ActionRequested[Any, Any]] = {}
        self.outcome: dict[str, Any] | None = None
        self.changed = asyncio.Event()
        self.finished = asyncio.Event()

    def _notify(self) -> None:
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()

    def identity(self) -> dict[str, Any]:
        return {
            "session_id": self.turn.session_id,
            "submission_id": self.turn.submission_id,
            "turn_id": self.turn.turn_id,
        }

    async def observe_requests(self, requests: AsyncIterator[Interaction]) -> None:
        async for request in requests:
            try:
                if isinstance(request, ApprovalRequest):
                    await request.respond("deny")
                elif isinstance(request, (FormRequest, UrlRequest)):
                    await request.decline()
                elif (
                    isinstance(request, ActionRequested)
                    and request.submission_id == self.turn.submission_id
                ):
                    self.questions[uuid4().hex] = request
                    self._notify()
            except InteractionExpiredError:
                continue

    async def collect(self, requests: AsyncIterator[Interaction], timeout: float) -> None:
        async def result() -> dict[str, Any]:
            try:
                completed = await self.turn.get_result()
            except (NativeError, TurnStoppedError):
                attention = await self.turn.wait_for_attention()
                assert isinstance(attention, TurnNeedsAttention)
                return {"status": attention.reason}
            return {"status": "completed", "output": completed.final_output}

        observer = asyncio.create_task(self.observe_requests(requests))
        completion = asyncio.create_task(result())
        try:
            # The native result owns settlement, including when cleanup stops the observer.
            done, _ = await asyncio.wait((completion,), timeout=timeout)
            if not done:
                # Cancellation can join an existing stop or find native cleanup underway.
                with suppress(RuntimeError):
                    await self.turn.cancel()
            self.outcome = await completion
        except Exception:
            self.outcome = {"status": "failed"}
        finally:
            if self.outcome is None:
                self.outcome = {"status": "failed"}
            self.questions.clear()
            self._notify()
            self.finished.set()
            observer.cancel()
            completion.cancel()
            await asyncio.gather(observer, completion, return_exceptions=True)

    def _pending(self, requests: tuple[Interaction, ...]) -> list[dict[str, Any]]:
        # Snapshots contain fresh wrappers. Compare call identity and keep the original grant.
        live_calls = {
            (request.submission_id, request.turn_id, request.call_id)
            for request in requests
            if isinstance(request, ActionRequested)
        }
        return [
            {
                "question_id": key,
                "turn_id": question.turn_id,
                "type": question.type,
                "name": question.name,
                "arguments": question.arguments.model_dump(mode="json"),
            }
            for key, question in self.questions.items()
            if (question.submission_id, question.turn_id, question.call_id) in live_calls
        ]

    async def wait(self) -> dict[str, Any]:
        while self.outcome is None:
            changed = self.changed
            if self.questions:
                try:
                    requests = await self.session.pending_interactions()
                except RuntimeError:
                    # The owner is still collecting the outcome of a closed Session.
                    break
                if questions := self._pending(requests):
                    return {"status": "attention", "questions": questions}
            await changed.wait()
        await self.finished.wait()
        assert self.outcome is not None
        return self.outcome

    async def dispatch(self, request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        action = request.get("action")
        if action == "status":
            response = {"status": "active"} if self.outcome is None else self.outcome
        elif action == "events":
            after = request.get("after", 0)
            details = request.get("execution_details", False)
            if type(after) is not int or after < 0 or type(details) is not bool:
                raise ValueError("Invalid event cursor")
            async with aclosing(
                self.turn.events(after=after, include_execution_details=details)
            ) as events:
                async for event in events:
                    if isinstance(event, _OBSERVATIONS):
                        await _send(writer, asdict(event))
            return
        elif action == "wait":
            response = await self.wait()
        elif action in ("steer", "answer", "cancel"):
            target = request.get("turn_id")
            if not isinstance(target, str) or not target:
                raise ValueError("A native turn ID is required")
            if action == "steer":
                text = request.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("Steering requires text")
                if self.outcome is not None:
                    raise StaleTurnError("The turn has settled")
                receipt = await self.turn.steer_exact(text, expected_turn_id=target)
                response = {"status": "steered", **asdict(receipt)}
            elif action == "cancel":
                if target != self.turn.turn_id:
                    raise StaleTurnError("The turn has changed")
                await self.turn.cancel()
                await self.finished.wait()
                response = {"status": "cancel_joined"}
            else:
                key = request.get("question_id")
                if not isinstance(key, str):
                    raise ValueError("A question ID is required")
                question = self.questions.get(key)
                if question is None or question.turn_id != target:
                    raise InteractionExpiredError("The question has changed")
                answer = request.get("answer")
                if not isinstance(answer, str):
                    raise ValueError("Expected a JSON string answer")
                try:
                    await question.respond(answer)
                except InteractionExpiredError:
                    self.questions.pop(key, None)
                    raise
                self.questions.pop(key, None)
                response = {"status": "answered", "question_id": key}
        else:
            raise ValueError("Unknown supervisor action")
        await _send(writer, {**self.identity(), **response})

    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), 5)
                if not line:
                    return
                if not line.endswith(b"\n"):
                    raise ValueError("Expected a complete request line")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Expected a request object")
                if request.get("action") in ("events", "wait"):
                    # Leaving a passive client must release its reader even on an idle turn.
                    operation = asyncio.create_task(self.dispatch(request, writer))
                    disconnect = asyncio.create_task(reader.read(1))
                    try:
                        await asyncio.wait(
                            (operation, disconnect), return_when=asyncio.FIRST_COMPLETED
                        )
                        if operation.done():
                            operation.result()
                    finally:
                        operation.cancel()
                        disconnect.cancel()
                        await asyncio.gather(operation, disconnect, return_exceptions=True)
                else:
                    # An admitted control call can settle after its client leaves.
                    await self.dispatch(request, writer)
            except StaleTurnError:
                await _send(writer, {"error": "stale_turn"})
            except InteractionExpiredError:
                await _send(writer, {"error": "expired_question"})
            except (TypeError, ValueError, RecursionError, asyncio.TimeoutError):
                await _send(writer, {"error": "invalid_request"})
            except (ConnectionError, asyncio.CancelledError):
                raise
            except Exception:
                await _send(writer, {"error": "operation_failed"})
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                with suppress(ConnectionError, asyncio.TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), 2)
            finally:
                writer.transport.abort()


async def serve_turn(
    session: Session[Any, Any], prompt: str, *, timeout: float, stop: asyncio.Event
) -> None:
    """Submit one Turn and serve its local control endpoint until the owner stops it."""
    # Acquire the iterator while the Session is open, before native work can fail.
    async with aclosing(session.interactions()) as requests:
        live = _LiveTurn(session, await session.submit(prompt, output_type=str))
        clients: dict[asyncio.Task[None], asyncio.StreamWriter] = {}

        def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if not server.is_serving():
                writer.close()
                return
            client = asyncio.create_task(live.connection(reader, writer))
            clients[client] = writer
            client.add_done_callback(clients.pop)

        # Keep the private directory outside the workspace and the socket path short.
        with TemporaryDirectory(prefix="agents-worker-", dir="/tmp") as directory:
            endpoint = Path(directory) / "control.sock"
            server = await asyncio.start_unix_server(
                connect, path=endpoint, limit=_REQUEST_LIMIT, start_serving=False
            )
            collector = asyncio.create_task(live.collect(requests, timeout))
            try:
                endpoint.chmod(0o600)
                await server.start_serving()
                print(json.dumps({"socket": str(endpoint), **live.identity()}), flush=True)
                await stop.wait()
            finally:
                server.close()
                for client, writer in clients.items():
                    writer.transport.abort()
                    client.cancel()
                collector.cancel()
                await asyncio.gather(collector, *clients, return_exceptions=True)
                await server.wait_closed()


async def _attach(endpoint: Path, request: bytes, *, stream: bool) -> int:
    # Connect only to a private endpoint announced by the worker.
    uid = os.geteuid()
    parent = endpoint.parent.lstat()
    info = endpoint.lstat()
    if (
        parent.st_uid != uid
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_mode & 0o077
        or info.st_uid != uid
        or not stat.S_ISSOCK(info.st_mode)
        or info.st_mode & 0o077
    ):
        raise ValueError("Use the private socket path announced by your worker.")
    reader, writer = await asyncio.open_unix_connection(endpoint, limit=_EVENT_LINE_LIMIT)
    try:
        if _peer_uid(writer.get_extra_info("socket")) != uid:
            raise PermissionError("The socket peer has a different Unix user.")
        writer.write(request)
        await writer.drain()
        if not stream:
            data = await reader.read()
            result = json.loads(data)
            sys.stdout.buffer.write(data)
            return int("error" in result)
        async for line in reader:
            result = json.loads(line)
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            if "error" in result:
                return 1
        return 0
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    actions.add_parser("wait")
    events = actions.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--execution-details", action="store_true")
    actions.add_parser("steer", help="Read the correction text from standard input.").add_argument(
        "turn_id"
    )
    actions.add_parser("cancel").add_argument("turn_id")
    answer = actions.add_parser("answer", help="Read a JSON answer from standard input.")
    answer.add_argument("turn_id")
    answer.add_argument("question_id")
    args = vars(parser.parse_args())
    endpoint = args.pop("socket")
    if args["action"] == "events" and args["after"] < 0:
        parser.error("The event cursor must be nonnegative.")
    if args["action"] in ("steer", "answer"):
        value = sys.stdin.buffer.read(_REQUEST_LIMIT + 1)
        if len(value) > _REQUEST_LIMIT:
            parser.error("The supervisor input is too large.")
        try:
            text = value.decode()
            if args["action"] == "steer":
                args["text"] = text
            else:
                args["answer"] = json.loads(text)
        except (ValueError, RecursionError):
            parser.error("Pass UTF-8 text for steering or a JSON answer.")
    try:
        request = json.dumps(args, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    except (ValueError, RecursionError):
        parser.error("Pass a valid UTF-8 JSON request.")
    if len(request) > _REQUEST_LIMIT:
        parser.error("The supervisor request is too large.")
    try:
        raise SystemExit(asyncio.run(_attach(endpoint, request, stream=args["action"] == "events")))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Could not attach to the worker ({type(error).__name__}).\n")
    except KeyboardInterrupt:
        parser.exit(130)


if __name__ == "__main__":
    main()
