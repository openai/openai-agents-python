"""An ordinary manual checkpoint resumes through its original canonical writer."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from agents import Agent, CustomStorage, SQLiteSessionStore
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_custom_storage_manual import lookup_tool, provider_for
from tests.test_native_manual_tools import next_request


async def run_worker(mode: str, url: str, directory: Path) -> None:
    backend = "different.sqlite" if mode == "wrong-store" else "conversation.sqlite"
    storage = CustomStorage(SQLiteSessionStore(directory / backend))
    state_path = directory / "checkpoint.json"
    lookup = lookup_tool()
    async with timeout(15), provider_for(url) as provider:
        agent = Agent(
            instructions="Read exactly once.",
            model="gpt-5.5",
            provider=provider,
            tools=[lookup],
        )
        if mode == "capture":
            async with agent.session(storage=storage) as session:
                await session.submit("read")
                request = await next_request(session.interactions(), lookup)
                assert request.call_id == "manual-durable"
                state_path.write_text((await session.suspend()).to_json())
                print(json.dumps({"id": session.id}), flush=True)
            return

        state = SessionState.from_json(state_path.read_text())
        if mode in {"continue", "crash", "hold", "wrong-store"}:
            async with agent.session(state=state, storage=storage) as session:
                assert session.id == state.source_session_id
                request = await next_request(session.interactions(), lookup)
                assert request.call_id == "manual-durable"
                if mode == "crash":
                    os._exit(73)
                if mode == "hold":
                    print(json.dumps({"holding": session.id}), flush=True)
                    await asyncio.to_thread(sys.stdin.readline)
                    return
                await request.respond("durable-answer")
                turn = session.restored_turn
                assert turn is not None
                print(
                    json.dumps({"output": (await turn.get_result()).final_output}),
                    flush=True,
                )
            return

        assert mode == "inspect"
        async with agent.session(resume=state.source_session_id, storage=storage) as session:
            history = [item async for item in session.items()]
            results = [
                item["call_id"] for item in history if item["type"] == "function_call_output"
            ]
            print(
                json.dumps({"id": session.id, "results": results, "items": history}),
                flush=True,
            )


async def start_process(mode: str, url: str, directory: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        *(("-S",) if sys.flags.no_site else ()),
        "-c",
        "import asyncio,runpy,sys; from pathlib import Path; "
        "worker = runpy.run_path(sys.argv[1])['run_worker']; "
        "asyncio.run(worker(sys.argv[2], sys.argv[3], Path(sys.argv[4])))",
        __file__,
        mode,
        url,
        str(directory),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def collect_process(
    process: asyncio.subprocess.Process, *, expected_exit: int | None = 0
) -> dict[str, Any]:
    try:
        stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if expected_exit is None:
        assert process.returncode != 0, stdout.decode() + stderr.decode()
        return {"error": stderr.decode()}
    assert process.returncode == expected_exit, stdout.decode() + stderr.decode()
    return json.loads(stdout) if stdout else {}


async def run_process(
    mode: str, url: str, directory: Path, *, expected_exit: int | None = 0
) -> dict[str, Any]:
    return await collect_process(
        await start_process(mode, url, directory), expected_exit=expected_exit
    )


@pytest.mark.skipif(os.name != "posix", reason="SQLiteSessionStore requires POSIX locking")
async def test_manual_checkpoint_resumes_into_same_store_across_processes(responses, tmp_path):
    async with timeout(30):
        responses.respond(function_call("lookup", {"key": "A"}, "manual-durable"))
        responses.respond(assistant_output("done"))
        captured = await run_process("capture", responses.url, tmp_path)
        assert len(responses.requests) == 1

        continued = await run_process("continue", responses.url, tmp_path)
        assert continued == {"output": "done"}
        assert len(responses.requests) == 2
        outputs = [
            item
            for item in responses.requests[1]["input"]
            if item["type"] == "function_call_output"
        ]
        assert [(item["call_id"], item["output"]) for item in outputs] == [
            ("manual-durable", "durable-answer")
        ]

        reopened = await run_process("inspect", responses.url, tmp_path)
        assert reopened["id"] == captured["id"]
        assert reopened["results"] == ["manual-durable"]
        history = json.dumps(reopened["items"])
        assert "durable-answer" in history and "done" in history
        assert len(responses.requests) == 2


@pytest.mark.skipif(os.name != "posix", reason="SQLiteSessionStore requires POSIX locking")
async def test_unconsumed_checkpoint_survives_crash_and_excludes_another_writer(
    responses, tmp_path
):
    async with timeout(45):
        responses.respond(function_call("lookup", {"key": "A"}, "manual-durable"))
        responses.respond(assistant_output("done"))
        captured = await run_process("capture", responses.url, tmp_path)
        await run_process("crash", responses.url, tmp_path, expected_exit=73)
        assert len(responses.requests) == 1

        holder = await start_process("hold", responses.url, tmp_path)
        try:
            assert holder.stdout is not None
            assert json.loads(await holder.stdout.readline()) == {"holding": captured["id"]}
            competing = await run_process("continue", responses.url, tmp_path, expected_exit=None)
            assert "CustomStorage" in competing["error"] or "storage" in competing["error"]
            assert len(responses.requests) == 1
        finally:
            if holder.returncode is None:
                holder.kill()
            await holder.wait()

        assert await run_process("continue", responses.url, tmp_path) == {"output": "done"}
        stale = await run_process("continue", responses.url, tmp_path, expected_exit=None)
        assert "checkpoint no longer matches the original pending call" in stale["error"]
        assert len(responses.requests) == 2


@pytest.mark.skipif(os.name != "posix", reason="SQLiteSessionStore requires POSIX locking")
async def test_wrong_store_and_tampered_checkpoint_fail_before_sampling(responses, tmp_path):
    async with timeout(35):
        responses.respond(function_call("lookup", {"key": "A"}, "manual-durable"))
        responses.respond(assistant_output("done"))
        await run_process("capture", responses.url, tmp_path)
        state_path = tmp_path / "checkpoint.json"
        original = state_path.read_text()
        rejected = await run_process("wrong-store", responses.url, tmp_path, expected_exit=None)
        assert "not found" in rejected["error"].lower()
        assert len(responses.requests) == 1

        modified = json.loads(original)
        modified["native_continuation"]["turn_usage"]["cached_input_tokens"] += 1
        state_path.write_text(json.dumps(modified))
        refused = await run_process("continue", responses.url, tmp_path, expected_exit=None)
        assert "checkpoint no longer matches the original pending call" in refused["error"]
        assert len(responses.requests) == 1

        state_path.write_text(original)
        assert await run_process("continue", responses.url, tmp_path) == {"output": "done"}
        assert len(responses.requests) == 2
