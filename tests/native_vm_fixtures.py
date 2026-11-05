"""Native child-family and transparent helper-fault fixtures for VM transfer."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeAgents, NativeProvider, ToolContext, function_tool
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import collaboration, handles, trust
from tests.test_native_code_mode import code_call
from tests.test_native_executor_tools import declaration
from tests.test_review_ii_code_cell_interrupt import CellGraph


@asynccontextmanager
async def vm_family(tmp_path: Path, monkeypatch):
    effects: list[tuple[str, str]] = []

    @function_tool
    def mark(context: ToolContext[str], value: str) -> str:
        """Record one synthetic effect under the present application context."""
        effects.append((context.context, value))
        return context.context + ":" + value

    peer = CellGraph()
    peer.programs = {
        "/root": deque(
            [
                [
                    collaboration(
                        "spawn_agent",
                        {
                            "task_name": "alpha",
                            "message": "Store the one requested value.",
                            "agent_type": "default",
                            "fork_turns": "none",
                        },
                        "spawn-alpha",
                    )
                ],
                [assistant_output("source root complete")],
            ]
        ),
        "/root/alpha": deque(
            [
                [code_call("store('kept',await tools.mark({value:'seed'}));", "seed-child")],
                [assistant_output("source child complete")],
            ]
        ),
    }
    peer.by_thread = {path: [] for path in peer.programs}
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.url = str(server.make_url("/v1"))
        source = Agent(
            instructions="Use only the requested synthetic family values.",
            model="gpt-6.1-sol",
            provider=NativeProvider(base_url=peer.url, api_key="source-vm-key"),
            tools=[mark, NativeAgents(max_children=1)],
            programmatic_tool_calling=True,
        )
        async with source.session(context="source", environment=declaration(executor)) as session:
            assert (
                await (await session.submit("Store in alpha")).get_result()
            ).final_output == "source root complete"
            [child] = await handles(session, 1)
            try:
                assert (await child.get_result()).final_output == "source child complete"
            except BaseException:
                print(
                    "vm_family source child wait failed: "
                    + json.dumps(
                        {
                            "child_id": child.child_id,
                            "turn_id": child.turn_id,
                            "effects": effects,
                            "request_counts": {
                                path: len(requests) for path, requests in peer.by_thread.items()
                            },
                            "remaining_replies": {
                                path: len(program) for path, program in peer.programs.items()
                            },
                        }
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                raise
            saved = await session.save_state()
        await source.provider.aclose()
        assert effects == [("source", "seed")]
        current = Agent(
            instructions=source.instructions,
            model=source.model,
            provider=NativeProvider(base_url=peer.url, api_key="destination-vm-key"),
            tools=source.tools,
            programmatic_tool_calling=True,
        )
        try:
            yield peer, executor, current, saved, effects
        finally:
            await current.provider.aclose()


def followup(peer: CellGraph, *, call: str, result: str, value: str | None = None) -> None:
    peer.programs["/root"].extend(
        [
            [
                collaboration(
                    "followup_task",
                    {"target": "/root/alpha", "message": "Read the retained value."},
                    "followup-" + call,
                )
            ],
            [assistant_output("root " + result)],
        ]
    )
    code = (
        "text(JSON.stringify([load('kept')"
        + (",await tools.mark({value:" + json.dumps(value) + "})" if value is not None else "")
        + "]));"
    )
    peer.programs["/root/alpha"].extend(
        [
            [code_call(code, call)],
            [assistant_output("child " + result)],
        ]
    )


class StateTransferGate:
    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.history: list[dict[str, Any]] = []
        self.writers: set[asyncio.StreamWriter] = set()
        self.exits: list[asyncio.Future[None]] = []
        self.pidfds: list[int] = []

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        try:
            while line := await reader.readline():
                message = json.loads(line)
                if message["event"] == "helper_started":
                    descriptor = os.pidfd_open(message["pid"])
                    future = asyncio.get_running_loop().create_future()

                    def exited(fd=descriptor, done=future):
                        asyncio.get_running_loop().remove_reader(fd)
                        if not done.done():
                            done.set_result(None)

                    asyncio.get_running_loop().add_reader(descriptor, exited)
                    self.pidfds.append(descriptor)
                    self.exits.append(future)
                self.history.append(message)
                self.events.put_nowait(message)
        finally:
            self.writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                # Release commands may race control-socket closure after helper exit.
                if not self.exits or not all(
                    future.done() and not future.cancelled() for future in self.exits
                ):
                    raise

    async def wait(self, kind: str, *, target: bool | None = None) -> dict[str, Any]:
        while True:
            event = await self.events.get()
            if event["event"] == kind and (target is None or event.get("target") is target):
                return event

    async def release(self, operation: str) -> None:
        for writer in tuple(self.writers):
            if writer.is_closing():
                continue
            try:
                writer.write((json.dumps({"command": "release_" + operation}) + "\n").encode())
                await writer.drain()
            except ConnectionError:
                pass

    async def release_shutdown(self) -> None:
        await self.release("shutdown")

    async def release_export(self) -> None:
        await self.release("export")

    async def wait_for_helper_exit(self) -> None:
        assert self.exits, "The real native helper never started"
        await asyncio.gather(*self.exits)


@asynccontextmanager
async def state_transfer_gate(
    tmp_path: Path, monkeypatch, mode: str, *, import_snapshot: dict[str, Any] | None = None
):
    assert (mode in {"export", "terminate"}) == (import_snapshot is None)
    gate = StateTransferGate()
    server = await asyncio.start_server(gate.accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    import agents._runtime.session as session_module

    bundled = Path(session_module.__file__).parents[1] / "bin"
    helper = bundled / "codex-code-mode-host"
    # Select the test helper before Session captures client and thread runtime.
    # Keep every other bundled executable unchanged through this private mirror.
    package = tmp_path / "gated-native-helpers"
    helpers = package / "bin"
    helpers.mkdir(parents=True)
    for executable in bundled.iterdir():
        if executable.name != "codex-code-mode-host":
            (helpers / executable.name).symlink_to(executable)
    forwarder = Path(__file__).parent / "fixtures" / "code_mode_import_gate.py"
    launcher = helpers / "codex-code-mode-host"
    launcher.write_text(
        "#!"
        + sys.executable
        + "\nimport runpy,sys\n"
        + "sys.argv="
        + repr(
            [str(forwarder), str(helper), "127.0.0.1", str(port), mode, json.dumps(import_snapshot)]
        )
        + "\n"
        + "runpy.run_path("
        + repr(str(forwarder))
        + ",run_name='__main__')\n"
    )
    launcher.chmod(0o755)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(session_module, "__file__", str(package / "_runtime" / "session.py"))
            yield gate
    finally:
        await gate.release("terminate")
        await gate.release_export()
        await gate.release_shutdown()
        server.close()
        await server.wait_closed()
        for writer in tuple(gate.writers):
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                if not gate.exits or not all(
                    future.done() and not future.cancelled() for future in gate.exits
                ):
                    raise
        for descriptor in gate.pidfds:
            asyncio.get_running_loop().remove_reader(descriptor)
            os.close(descriptor)
