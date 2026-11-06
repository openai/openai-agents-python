"""Forward the real native helper, gating one state reply and import shutdown receipt.

Only the transport response is delayed or failed. The native helper parses,
installs and closes its actual session, so no Python implementation supplies VM state.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import struct
import sys


def parent_death_signal() -> None:
    # The Linux-only fault test must not orphan its nested real helper if Rust
    # kills the forwarding process while disposing of the host connection.
    parent = os.getppid()
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    if os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGTERM)


def read_parent_frame() -> bytes | None:
    prefix = sys.stdin.buffer.read(4)
    if not prefix:
        return None
    if len(prefix) != 4:
        raise EOFError("Incomplete parent frame")
    size = struct.unpack("<I", prefix)[0]
    data = bytearray()
    while len(data) < size:
        chunk = sys.stdin.buffer.read(size - len(data))
        if not chunk:
            raise EOFError("Incomplete parent payload")
        data.extend(chunk)
    return prefix + data


def encoded(message: dict) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode()
    return struct.pack("<I", len(payload)) + payload


async def main() -> None:
    helper, address, port, mode, selected_snapshot = sys.argv[1:]
    selected_snapshot = json.loads(selected_snapshot)
    target_sessions: set[str] = set()
    control_reader, control_writer = await asyncio.open_connection(address, int(port))
    process = await asyncio.create_subprocess_exec(
        helper,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        preexec_fn=parent_death_signal,
    )
    assert process.stdin is not None and process.stdout is not None
    release_shutdown = asyncio.Event()
    release_export = asyncio.Event()
    release_terminate = asyncio.Event()
    if mode == "export":
        release_shutdown.set()
    imports: dict[int, str] = {}
    exports: dict[int, str] = {}
    held_imports: dict[str, bytes] = {}
    deliveries: set[asyncio.Task] = set()
    first_import = True
    first_export = True

    async def event(kind: str, **fields) -> None:
        control_writer.write((json.dumps({"event": kind, **fields}) + "\n").encode())
        await control_writer.drain()

    def send_parent(frame: bytes) -> None:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()

    async def commands() -> None:
        while line := await control_reader.readline():
            command = json.loads(line)["command"]
            if command == "release_shutdown":
                release_shutdown.set()
            elif command == "release_export":
                release_export.set()
            elif command == "release_terminate":
                release_terminate.set()
                await event("terminate_released")
            elif command == "release_import":
                for session, frame in list(held_imports.items()):
                    held_imports.pop(session)
                    send_parent(frame)
                    await event(
                        "import_forwarded", session=session, target=session in target_sessions
                    )

    async def to_helper() -> None:
        while (frame := await asyncio.to_thread(read_parent_frame)) is not None:
            message = json.loads(frame[4:])
            request = message.get("request", {})
            if (
                mode == "terminate"
                and request.get("method") == "session/terminate"
                and not release_terminate.is_set()
            ):
                send_parent(
                    encoded(
                        {
                            "type": "operation/response",
                            "id": message["id"],
                            "result": {
                                "status": "error",
                                "message": "private-native-termination-error",
                            },
                        }
                    )
                )
                await event("terminate_rejected", cell=request["cellId"])
                continue
            if request.get("method") == "session/import":
                imports[message["id"]] = request["sessionId"]
                if mode != "export" and request.get("state") == selected_snapshot:
                    target_sessions.add(request["sessionId"])
            elif request.get("method") == "session/export":
                exports[message["id"]] = request["sessionId"]
            process.stdin.write(frame)
            await process.stdin.drain()
        process.stdin.close()

    async def deliver_shutdown(frame: bytes, session: str) -> None:
        await event("shutdown_applied", session=session, target=session in target_sessions)
        if mode == "export" or session in target_sessions:
            await release_shutdown.wait()
        send_parent(frame)
        if prior := held_imports.pop(session, None):
            # A late response to a canceled import must not close a successor.
            send_parent(prior)
        await event("shutdown_forwarded", session=session, target=session in target_sessions)

    async def deliver_export(frame: bytes, session: str) -> None:
        await release_export.wait()
        send_parent(frame)
        await event("export_forwarded", session=session)

    async def to_parent() -> None:
        nonlocal first_import, first_export
        while True:
            try:
                prefix = await process.stdout.readexactly(4)
            except asyncio.IncompleteReadError as error:
                assert not error.partial
                return
            data = await process.stdout.readexactly(struct.unpack("<I", prefix)[0])
            frame = prefix + data
            message = json.loads(data)
            result = message.get("result", {})
            value = result.get("value", {})
            request_id = message.get("id")
            if request_id in exports and value.get("type") == "session/exported":
                session = exports.pop(request_id)
                await event("export_observed", session=session)
                if mode == "export" and first_export:
                    first_export = False
                    task = asyncio.create_task(deliver_export(frame, session))
                    deliveries.add(task)
                    task.add_done_callback(deliveries.discard)
                    continue
            if request_id in imports and value.get("type") == "session/imported":
                session = imports.pop(request_id)
                target = session in target_sessions
                await event("import_applied", session=session, target=target)
                if first_import and (mode == "export" or target):
                    first_import = False
                    if mode in {"cancel", "cancel-before-ack"}:
                        held_imports[session] = frame
                        continue
                    assert mode == "fail"
                    message["result"] = {
                        "status": "error",
                        "message": "synthetic-code-import-failure",
                    }
                    frame = encoded(message)
            if value.get("type") == "session/closed":
                task = asyncio.create_task(deliver_shutdown(frame, value["sessionId"]))
                deliveries.add(task)
                task.add_done_callback(deliveries.discard)
                continue
            send_parent(frame)

    await event("helper_started", pid=process.pid)
    command_task = asyncio.create_task(commands())
    input_task = asyncio.create_task(to_helper())
    output_task = asyncio.create_task(to_parent())
    try:
        await input_task
        await output_task
        if deliveries:
            await asyncio.gather(*deliveries)
        await process.wait()
    finally:
        command_task.cancel()
        if process.returncode is None:
            process.terminate()
            await process.wait()
        control_writer.close()
        await control_writer.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
