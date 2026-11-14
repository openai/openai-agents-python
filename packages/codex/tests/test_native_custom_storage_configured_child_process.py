"""Process death has been retaining or consuming the exact configured-child intent."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents import CustomStorage, PendingRequests, SQLiteSessionStore
from agents.state import SessionState

from tests import test_native_configured_agent_tool as configured
from tests._asyncio import timeout
from tests.test_native_custom_storage_configured_child import capture, snapshot
from tests.test_native_custom_storage_same_backend import collect_process


async def worker(mode: str, urls: list[str], directory: Path):
    selected = iter(urls)

    @asynccontextmanager
    async def remote_peer():
        yield SimpleNamespace(url=next(selected), respond=lambda *args: None)

    class CrashStore(SQLiteSessionStore):
        claimed = False

        def commit(self, session_id, token, revision, write_id, payload):
            old = json.loads(self.read(session_id, token) or "{}")
            new = json.loads(payload)
            claim = (
                bool((old.get("manual_checkpoint") or {}).get("configured_child"))
                and new.get("manual_checkpoint") is None
            )
            if claim and mode == "before":
                os._exit(73)
            super().commit(session_id, token, revision, write_id, payload)
            self.claimed = self.claimed or claim

        def flush(self, session_id, token):
            super().flush(session_id, token)
            if self.claimed and mode == "after_flush":
                os._exit(74)

    configured.peer = remote_peer
    async with timeout(15), configured.fixture() as values:
        parent, _, context, _, _, effects, _, _ = values
        parent = replace(parent, tools=[parent.tools[0]])
        state = SessionState.from_json((directory / "checkpoint.json").read_text())
        storage = CustomStorage(CrashStore(directory / "canonical.sqlite"))
        try:
            async with parent.session(
                context=context, storage=storage, state=state
            ) as session:
                assert session.restored_turn is not None
                pending = await session.restored_turn.wait_for_request_or_result()
                assert (
                    isinstance(pending, PendingRequests) and len(pending.requests) == 1
                )
                if mode == "after_admission":
                    os._exit(75)
                assert mode == "continue"
                await session.respond(pending.requests[0].reply(True))
                result = await session.restored_turn.get_result()
                assert result.final_output == "parent complete"
                assert effects == ["child-effect"]
                print(json.dumps({"output": result.final_output}), flush=True)
        except RuntimeError:
            if mode != "refuse":
                raise
            assert not effects
            print(json.dumps({"refused": True}), flush=True)
        else:
            assert mode != "refuse", "Consumed intent must not open a stale child frame"


async def start_process(mode, urls, directory):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        *(("-S",) if sys.flags.no_site else ()),
        "-c",
        "import asyncio,json,runpy,sys; from pathlib import Path; "
        "worker=runpy.run_path(sys.argv[1])['worker']; "
        "asyncio.run(worker(sys.argv[2],json.loads(sys.argv[3]),Path(sys.argv[4])))",
        __file__,
        mode,
        json.dumps(urls),
        str(directory),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def run_process(mode, urls, directory, expected_exit=0):
    return await collect_process(
        await start_process(mode, urls, directory), expected_exit=expected_exit
    )


@pytest.mark.skipif(
    os.name != "posix", reason="SQLiteSessionStore requires POSIX locking"
)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase,exit_code", [("before", 73), ("after_flush", 74), ("after_admission", 75)]
)
async def test_configured_child_process_death_at_claim_boundary(
    tmp_path, phase, exit_code
):
    async with timeout(45), configured.fixture() as values:
        parent, _, context, root, child, effects, _, _ = values
        parent = replace(parent, tools=[parent.tools[0]])
        root.replies[0] = [root.replies[0][0]]
        path = tmp_path / "canonical.sqlite"
        backend = SQLiteSessionStore(path)
        state = await capture(parent, context, CustomStorage(backend))
        (tmp_path / "checkpoint.json").write_text(state.to_json())
        urls = [root.url, child.url]
        sealed = snapshot(backend, state.source_session_id)["manual_checkpoint"]
        assert sealed["configured_child"] and not effects
        await run_process(phase, urls, tmp_path, expected_exit=exit_code)
        assert len(root.requests) == len(child.requests) == 1
        fresh = SQLiteSessionStore(path)
        marker = snapshot(fresh, state.source_session_id).get("manual_checkpoint")
        if phase == "before":
            assert marker == sealed
            assert await run_process("continue", urls, tmp_path) == {
                "output": "parent complete"
            }
            assert len(root.requests) == len(child.requests) == 2
        else:
            assert marker is None
            assert await run_process("refuse", urls, tmp_path) == {"refused": True}
            assert len(root.requests) == len(child.requests) == 1
        assert (
            not effects
        )  # Child effects, if accepted, have been running only in the worker.


@pytest.mark.skipif(
    os.name != "posix", reason="SQLiteSessionStore requires POSIX locking"
)
@pytest.mark.asyncio
async def test_configured_child_process_death_after_accepted_child_effect(tmp_path):
    async with timeout(45), configured.fixture() as values:
        parent, _, context, root, child, effects, _, _ = values
        parent = replace(parent, tools=[parent.tools[0]])
        root.replies[0] = [root.replies[0][0]]
        path = tmp_path / "canonical.sqlite"
        backend = SQLiteSessionStore(path)
        state = await capture(parent, context, CustomStorage(backend))
        (tmp_path / "checkpoint.json").write_text(state.to_json())
        entered, release = asyncio.Event(), asyncio.Event()

        async def before(event):
            if len(child.requests) == 2 and event["type"] == "response.created":
                entered.set()
                await release.wait()

        child.before_event = before
        urls = [root.url, child.url]
        process = await start_process("continue", urls, tmp_path)
        try:
            await entered.wait()
            outputs = [
                item
                for item in child.requests[1]["input"]
                if item["type"] == "function_call_output"
            ]
            assert len(outputs) == 1
            assert outputs[0]["call_id"] == "child-approval-call"
            assert "child effect output" in json.dumps(outputs[0])
            assert len(root.requests) == 1
            process.kill()
            await collect_process(process, expected_exit=-9)
        finally:
            release.set()
            if process.returncode is None:
                process.kill()
                await process.wait()
        fresh = SQLiteSessionStore(path)
        assert snapshot(fresh, state.source_session_id).get("manual_checkpoint") is None
        assert await run_process("refuse", urls, tmp_path) == {"refused": True}
        assert len(root.requests) == 1 and len(child.requests) == 2
        assert not effects
