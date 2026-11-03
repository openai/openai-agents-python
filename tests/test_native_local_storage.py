"""Native persistence and receipt controls in real independent application processes."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agents import Agent, FunctionTool, LocalStorage, NativeProvider, ToolContext, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs, next_request, provider


async def _worker() -> None:
    mode, url, root, thread_id = sys.argv[1:]
    directory = Path(root)

    @function_tool
    async def record(context: ToolContext[str], key: str) -> str:
        """Record one application effect."""
        with (directory / "effects").open("a") as stream:
            stream.write(context.context + ":" + key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if mode == "unknown":
            # Crash after the external effect, before returning a native receipt.
            os._exit(67)
        return context.context + ":" + key

    provider = NativeProvider(base_url=url, api_key="synthetic-" + mode)
    agent = Agent(
        instructions="Use record for requested work.",
        tools=[] if mode == "deselected" else [record],
        provider=provider,
        model="gpt-5.1-codex",
    )
    try:
        async with (
            provider,
            agent.session(
                context=mode,
                storage=LocalStorage(directory / "profile"),
                resume=thread_id or None,
            ) as session,
        ):
            print(json.dumps({"opened": session.id, "pid": os.getpid()}), flush=True)
            if mode == "hold":
                await asyncio.to_thread(sys.stdin.readline)
            elif mode == "export":
                try:
                    await session.save_state()
                except Exception as error:
                    print(json.dumps({"rejected": str(error)}), flush=True)
                    return
                raise AssertionError("Native local storage exported as a portable checkpoint")
            else:
                turn = await session.submit(mode + "-input")
                result = await turn.get_result()
                print(json.dumps({"output": result.final_output, "id": session.id}), flush=True)
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
        raise


async def _start(responses, tmp_path, mode, identity=""):
    # Safe-path interpreters omit cwd. Add only the repository's test package;
    # the SDK must still come from the active editable or installed environment.
    root = str(Path(__file__).resolve().parents[1])
    pythonpath = os.pathsep.join(filter(None, (root, os.environ.get("PYTHONPATH"))))
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "tests.test_native_local_storage",
        mode,
        responses.url,
        str(tmp_path),
        identity,
        env={**os.environ, "PYTHONPATH": pythonpath},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _collect(process):
    try:
        async with timeout(60):
            out, err = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    return process.returncode, [json.loads(line) for line in out.splitlines()], err.decode()


async def test_local_reopen_keeps_history_and_binds_only_current_callbacks(responses, tmp_path):
    responses.respond(function_call("record", {"key": "first-effect"}, "record-1"))
    responses.respond(assistant_output("source-finished"))
    first = await _start(responses, tmp_path, "source")
    code, rows, err = await _collect(first)
    assert code == 0, (rows, err)
    identity = rows[0]["opened"]
    assert rows[1] == {"output": "source-finished", "id": identity}
    assert (tmp_path / "effects").read_text() == "source:first-effect\n"

    responses.respond(function_call("record", {"key": "fresh-effect"}, "record-2"))
    responses.respond(assistant_output("destination-finished"))
    second = await _start(responses, tmp_path, "destination", identity)
    code, newer, err = await _collect(second)
    assert code == 0, (newer, err)
    assert newer[0]["pid"] != rows[0]["pid"] and newer[0]["opened"] == identity
    assert newer[1] == {"output": "destination-finished", "id": identity}
    assert (tmp_path / "effects").read_text() == "source:first-effect\ndestination:fresh-effect\n"
    assert len(responses.requests) == 4
    resumed = responses.requests[2]["input"]
    calls = [item for item in resumed if item.get("type") == "function_call"]
    results = [item for item in resumed if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in calls] == ["record-1"]
    assert [item["call_id"] for item in results] == ["record-1"]
    assert "source:first-effect" in json.dumps(results)
    assert "source-finished" in json.dumps(resumed)
    assert "destination:fresh-effect" in json.dumps(responses.requests[3]["input"])
    assert responses.request_headers[2]["authorization"] == "Bearer synthetic-destination"


async def test_local_single_writer_fence_releases_for_successor(responses, tmp_path):
    owner = await _start(responses, tmp_path, "hold")
    try:
        async with timeout(45):
            row = json.loads(await owner.stdout.readline())
        assert "opened" in row, row
        identity = row["opened"]
        other = await _start(responses, tmp_path, "destination", identity)
        code, rows, err = await _collect(other)
        assert code != 0 and rows and "error" in rows[0], (rows, err)
        assert not responses.requests
    finally:
        owner.stdin.write(b"close\n")
        await owner.stdin.drain()
        code, _, err = await _collect(owner)
        assert code == 0, err
    responses.respond(assistant_output("successor"))
    next_owner = await _start(responses, tmp_path, "destination", identity)
    code, rows, err = await _collect(next_owner)
    assert code == 0, (rows, err)
    assert rows[0]["opened"] == identity
    assert rows[1]["output"] == "successor"


async def test_unknown_local_receipt_never_replays_or_starts_model(responses, tmp_path):
    responses.respond(function_call("record", {"key": "unknown-effect"}, "missing-receipt"))
    original = await _start(responses, tmp_path, "unknown")
    code, rows, err = await _collect(original)
    assert code == 67 and rows and "opened" in rows[0], (rows, err)
    original_id = rows[0]["opened"]
    assert (tmp_path / "effects").read_text() == "unknown:unknown-effect\n"
    request_count = len(responses.requests)
    # Even with a valid new model response available, reopen must reject first.
    responses.respond(assistant_output("must-never-start"))
    reopened = await _start(responses, tmp_path, "destination", original_id)
    code, rows, err = await _collect(reopened)
    assert code != 0 and rows and "error" in rows[0], (rows, err)
    assert "unknown tool outcome" in rows[0]["message"]
    assert len(responses.requests) == request_count
    assert (tmp_path / "effects").read_text() == "unknown:unknown-effect\n"
    # Failure must release its writer and must not relabel an unknown effect.
    retried = await _start(responses, tmp_path, "destination", original_id)
    code, rows, err = await _collect(retried)
    assert code != 0 and rows and "unknown tool outcome" in rows[0]["message"], (rows, err)
    assert len(responses.requests) == request_count


async def test_local_cannot_export_portable_state(responses, tmp_path):
    process = await _start(responses, tmp_path, "export")
    code, rows, err = await _collect(process)
    assert code == 0, (rows, err)
    assert "local" in rows[1]["rejected"].lower(), rows
    assert not responses.requests


async def test_local_resume_requires_profile_before_provider_acquisition():
    agent = Agent(instructions="No operations without a profile.")
    with pytest.raises(ValueError, match="resume requires"):
        async with agent.session(resume="not-a-portable-thread"):
            pytest.fail("unscoped resume opened")


async def test_local_known_receipt_before_crash_survives_next_root(responses, tmp_path):
    receipt_reached_model = asyncio.Event()
    release = asyncio.Event()

    async def hold_second_request(event):
        if len(responses.requests) == 2 and event["type"] == "response.created":
            receipt_reached_model.set()
            await release.wait()

    responses.before_event = hold_second_request
    responses.respond(function_call("record", {"key": "received"}, "known-receipt"))
    responses.respond(assistant_output("unpublished-old-final"))
    original = await _start(responses, tmp_path, "received")
    try:
        async with timeout(45):
            first = json.loads(await original.stdout.readline())
            assert "opened" in first, first
            await receipt_reached_model.wait()
        assert (tmp_path / "effects").read_text() == "received:received\n"
        assert "received:received" in json.dumps(responses.requests[1]["input"])
        original.kill()
        await _collect(original)
    finally:
        release.set()
        if original.returncode is None:
            original.kill()
            await _collect(original)
    responses.before_event = None
    responses.respond(assistant_output("new-root"))
    reopened = await _start(responses, tmp_path, "destination", first["opened"])
    code, rows, err = await _collect(reopened)
    assert code == 0, (rows, err)
    assert rows[0]["opened"] == first["opened"] and rows[1]["output"] == "new-root"
    assert (tmp_path / "effects").read_text() == "received:received\n"
    history = responses.requests[-1]["input"]
    assert "received:received" in json.dumps(history)
    assert "unpublished-old-final" not in json.dumps(history)


async def test_local_saved_tools_never_authorize_destination_effects(responses, tmp_path):
    responses.respond(function_call("record", {"key": "once"}, "old-allowed"))
    responses.respond(assistant_output("old-done"))
    source = await _start(responses, tmp_path, "source")
    code, rows, err = await _collect(source)
    assert code == 0, (rows, err)
    identity = rows[0]["opened"]
    # A provider tries the old tool even though the destination did not select it.
    responses.respond(function_call("record", {"key": "forbidden"}, "old-unauthorized"))
    responses.respond(assistant_output("new-done"))
    destination = await _start(responses, tmp_path, "deselected", identity)
    code, rows, err = await _collect(destination)
    assert code == 0, (rows, err)
    assert (tmp_path / "effects").read_text() == "source:once\n"
    assert len(responses.requests) == 4
    assert "forbidden" in json.dumps(responses.requests[3]["input"])
    tools = json.dumps(responses.requests[2]["tools"])
    assert '"name": "record"' not in tools


async def test_local_manual_export_rejects_before_sealing_or_spending_grants(responses, tmp_path):
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "native-key"}, "local-manual"))
    responses.respond(assistant_output("still-live"))
    agent = Agent(instructions="Read the record.", tools=[lookup], provider=provider(responses))
    async with timeout(20), agent.session(storage=LocalStorage(tmp_path / "profile")) as session:
        turn = await session.submit("Begin")
        request = await next_request(session.interactions(), lookup)
        status = await session.checkpoint_status(kind="manual_wait")
        assert status.status == "not_ready" and "local" in status.reasons[0].lower()
        with pytest.raises(ValueError, match="Native local storage"):
            await session.suspend()
        await request.respond("known receipt")
        assert (await turn.get_result()).final_output == "still-live"
        assert "known receipt" in json.dumps(responses.requests[-1]["input"])


async def test_portable_state_cannot_select_a_native_directory(responses, tmp_path):
    agent = Agent(instructions="Settled input.", provider=provider(responses))
    async with agent.session() as original:
        saved = await original.save_state()
    directory = tmp_path / "must-not-be-created"
    with pytest.raises(ValueError, match="Portable state"):
        async with agent.session(storage=LocalStorage(directory), state=saved):
            pytest.fail("mixed storage admitted")
    assert not directory.exists()
    assert not responses.requests


if __name__ == "__main__":
    asyncio.run(_worker())
