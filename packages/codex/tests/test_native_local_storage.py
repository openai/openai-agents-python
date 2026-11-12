"""Native persistence and receipt controls in real independent application processes."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agents import (
    Agent,
    FunctionTool,
    LocalRecoveryRequired,
    LocalStorage,
    NativeError,
    NativeProvider,
    ReconciledCall,
    ToolContext,
    UncertainCall,
    function_tool,
)
from agents._history import project_history
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs, next_request, provider


async def _manual_receipt_failure_worker(url: str, directory: Path) -> None:
    import resource
    import signal

    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Read the record.",
        tools=[lookup],
        provider=NativeProvider(base_url=url, api_key="synthetic-manual-write-failure"),
    )
    try:
        async with agent.session(storage=LocalStorage(directory / "profile")) as session:
            await session.submit("Begin")
            request = await next_request(session.interactions(), lookup)
            limits = resource.getrlimit(resource.RLIMIT_FSIZE)
            previous_signal = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            try:
                # Fail real native append/flush operations without changing host state.
                resource.setrlimit(resource.RLIMIT_FSIZE, (0, limits[1]))
                try:
                    await request.respond("must not reach dependent model work")
                except Exception:
                    print(json.dumps({"reply_failed": True}), flush=True)
                else:
                    print(json.dumps({"reply_failed": False}), flush=True)
            finally:
                resource.setrlimit(resource.RLIMIT_FSIZE, limits)
                signal.signal(signal.SIGXFSZ, previous_signal)
    except Exception:
        print(json.dumps({"close_failed": True}), flush=True)


async def _worker() -> None:
    mode, url, root, thread_id = sys.argv[1:]
    directory = Path(root)
    if mode == "manual-receipt-failure":
        await _manual_receipt_failure_worker(url, directory)
        return

    @function_tool
    async def record(context: ToolContext[str], key: str) -> str:
        """Record one application effect."""
        if mode == "unknown-before-effect":
            os._exit(67)
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


@pytest.mark.parametrize("custom_input", ["null", "42", "{}", "", '  "quoted"\n'])
def test_unknown_custom_call_preserves_exact_input(custom_input: str) -> None:
    history = [
        {"type": "turn_context", "payload": {"turn_id": "original-turn"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "id": "original-item",
                "call_id": "missing-custom-result",
                "name": "apply_patch",
                "input": custom_input,
            },
        },
    ]
    uncertain: list[UncertainCall] = []
    _, items = project_history(history, "original-thread", uncertain_calls=uncertain)
    (call,) = uncertain
    assert call.arguments == custom_input
    (item,) = items
    assert item["type"] == "function_call"
    assert item["arguments"] == custom_input

    # The native event can replace the projected item and use the call ID
    # as its item ID. Recovery still needs the original custom tool input.
    native_arguments = {"normalized": True}
    history.append(
        {
            "type": "event_msg",
            "payload": {
                "type": "item_started",
                "turn_id": "original-turn",
                "item": {
                    "type": "DynamicToolCall",
                    "id": "missing-custom-result",
                    "tool": "apply_patch",
                    "status": "in_progress",
                    "arguments": native_arguments,
                },
            },
        }
    )
    uncertain.clear()
    _, items = project_history(history, "original-thread", uncertain_calls=uncertain)
    (call,) = uncertain
    assert call.arguments == custom_input
    (item,) = items
    assert item["type"] == "function_call"
    assert item["arguments"] == native_arguments


@pytest.mark.parametrize(
    ("outcome", "native_start"), [("applied", False), ("applied", True), ("not_applied", True)]
)
async def test_reconciled_local_call_can_reopen_without_replay_or_id_reuse(
    responses, tmp_path, outcome, native_start
):
    responses.respond(function_call("record", {"key": "unknown-effect"}, "missing-receipt"))
    mode = "unknown" if outcome == "applied" else "unknown-before-effect"
    crashed = await _start(responses, tmp_path, mode)
    code, rows, err = await _collect(crashed)
    assert code == 67 and rows and "opened" in rows[0], (rows, err)
    thread_id = rows[0]["opened"]
    storage = LocalStorage(tmp_path / "profile")
    if native_start:
        # Keep the model call and add a native start without a matching completion.
        (rollout,) = (tmp_path / "profile").rglob(f"*{thread_id}.jsonl")
        history = [json.loads(line) for line in rollout.read_text().splitlines()]
        context = next(row for row in reversed(history) if row["type"] == "turn_context")
        with rollout.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "timestamp": history[-1]["timestamp"],
                        "ordinal": history[-1]["ordinal"] + 1,
                        "type": "event_msg",
                        "payload": {
                            "type": "item_started",
                            "thread_id": thread_id,
                            "turn_id": context["payload"]["turn_id"],
                            "started_at_ms": 0,
                            "item": {
                                "type": "DynamicToolCall",
                                "id": "missing-receipt",
                                "tool": "record",
                                "status": "in_progress",
                                "arguments": {"key": "unknown-effect"},
                            },
                        },
                    }
                )
                + "\n"
            )
    invoked = []

    @function_tool
    async def record(key: str) -> str:
        """Record one application effect."""
        invoked.append(key)
        return key

    provider = NativeProvider(base_url=responses.url, api_key="synthetic-reconciled-worker")
    agent = Agent(instructions="Use record for requested work.", tools=[record], provider=provider)
    async with timeout(40), provider:
        with pytest.raises(LocalRecoveryRequired) as missing:
            async with agent.session(storage=storage, resume=thread_id):
                pytest.fail("Reopen accepted an unknown outcome")
        (call,) = missing.value.calls
        assert (call.thread_id, call.call_id, call.name) == (thread_id, "missing-receipt", "record")
        assert call.arguments == {"key": "unknown-effect"}
        if native_start:
            (invocation,) = [
                item for item in missing.value.items if item["type"] == "function_call"
            ]
            assert invocation["arguments"] == {"key": "unknown-effect"}
        effects = tmp_path / "effects"
        if outcome == "applied":
            assert effects.read_text() == "unknown:unknown-effect\n"
        else:
            assert not effects.exists()
        assert len(responses.requests) == 1

        with pytest.raises(ValueError, match="Reconciliation requires a confirmed outcome"):
            call.reconcile(outcome="unknown", result="Outcome is still unknown")
        with pytest.raises(ValueError, match="a text or ShellOutput result"):
            call.reconcile(outcome="applied", result={"confirmed": True})
        confirmed = call.reconcile(outcome=outcome, result="Confirmed application record")
        from_journal = ReconciledCall(
            thread_id=thread_id,
            turn_id=call.turn_id,
            call_id=call.call_id,
            outcome=outcome,
            result="Confirmed application record",
        )
        assert confirmed == from_journal
        # Repeating the same application receipt after a reconnect must be
        # safe. The same native call ID must stay fenced across later opens.
        for receipts in ([confirmed], [], [from_journal]):
            async with agent.session(
                storage=storage, resume=thread_id, reconciled_calls=receipts
            ) as session:
                recorded = [
                    item
                    async for item in session.items()
                    if item["type"] == "function_call_output" and item["call_id"] == call.call_id
                ]
                assert len(recorded) == 1
                assert recorded[0]["turn_id"] == call.turn_id
                assert recorded[0]["reconciliation"] == outcome
        assert len(responses.requests) == 1
        responses.respond(function_call("record", {"key": "duplicate"}, call.call_id))
        async with agent.session(storage=storage, resume=thread_id) as session:
            reuse = await session.submit("Check the existing effect")
            with pytest.raises(NativeError, match="Cannot reuse an original call identity"):
                await reuse.get_result()
        assert invoked == []
        assert "Confirmed application record" in json.dumps(responses.requests[1]["input"])


async def test_local_cannot_export_portable_state(responses, tmp_path):
    process = await _start(responses, tmp_path, "export")
    code, rows, err = await _collect(process)
    assert code == 0, (rows, err)
    message = rows[1]["rejected"]
    assert "cannot export portable SessionState" in message, rows
    assert "Session.id" in message and "same storage backend" in message, rows
    assert not responses.requests


async def test_local_resume_requires_profile_before_provider_acquisition():
    agent = Agent(instructions="No operations without a profile.")
    with pytest.raises(ValueError, match="resume requires"):
        async with agent.session(resume="not-a-portable-thread"):
            pytest.fail("unscoped resume opened")


async def test_local_resume_opening_rejection_order(responses, tmp_path):
    storage = LocalStorage(tmp_path / "profile")
    missing = tmp_path / "missing-instructions.md"
    malformed = [object()]
    agent = Agent(instructions="No operations before validation.", provider=provider(responses))
    cases = [
        (None, "", malformed, ValueError, "resume requires the original storage backend"),
        (storage, "", malformed, TypeError, "resume must be a nonempty native Session ID"),
        (
            storage,
            "synthetic-existing-session",
            malformed,
            TypeError,
            "reconciled_calls must contain ReconciledCall records",
        ),
        (storage, "synthetic-existing-session", [], FileNotFoundError, "missing-instructions.md"),
    ]
    for backend, identity, receipts, error, message in cases:
        with pytest.raises(error, match=message):
            async with agent.session(
                storage=backend,
                resume=identity,
                reconciled_calls=receipts,
                instruction_files=[missing],
            ):
                pytest.fail("Invalid local resume opened")
        assert responses.requests == []
        assert list(tmp_path.iterdir()) == []


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
        assert status.status == "not_ready" and "persistent storage" in status.reasons[0].lower()
        with pytest.raises(ValueError, match="Persistent storage"):
            await session.suspend()
        await request.respond("known receipt")
        assert (await turn.get_result()).final_output == "still-live"
        assert "known receipt" in json.dumps(responses.requests[-1]["input"])


async def test_local_manual_reply_survives_reopen_without_repeating_the_effect(responses, tmp_path):
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "native-key"}, "durable-manual"))
    responses.respond(assistant_output("original complete"))
    responses.respond(assistant_output("resumed complete"))
    agent = Agent(instructions="Read the record.", tools=[lookup], provider=provider(responses))
    storage = LocalStorage(tmp_path / "profile")
    effects: list[str] = []
    async with timeout(20):
        async with agent.session(storage=storage) as original:
            identity = original.id
            turn = await original.submit("Begin")
            request = await next_request(original.interactions(), lookup)
            effects.append(request.arguments.key)
            await request.respond("durable manual receipt")
            assert (await turn.get_result()).final_output == "original complete"
        assert len(responses.requests) == 2
        async with agent.session(storage=storage, resume=identity) as reopened:
            assert reopened.id == identity
            assert (
                await (await reopened.submit("Continue without another lookup")).get_result()
            ).final_output == "resumed complete"
            assert not await reopened.pending_interactions()
        assert len(responses.requests) == 3
        results = [
            item
            for item in responses.requests[-1]["input"]
            if item.get("type") == "function_call_output"
        ]
        assert len(results) == 1 and results[0]["call_id"] == "durable-manual"
        assert "durable manual receipt" in json.dumps(results)
        assert effects == ["native-key"]


async def test_local_manual_missing_reply_blocks_reopen_before_model(responses, tmp_path):
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "native-key"}, "missing-manual"))
    responses.respond(assistant_output("must never start"))
    agent = Agent(instructions="Read the record.", tools=[lookup], provider=provider(responses))
    storage = LocalStorage(tmp_path / "profile")
    async with timeout(20):
        async with agent.session(storage=storage) as original:
            identity = original.id
            await original.submit("Begin")
            request = await next_request(original.interactions(), lookup)
            assert request.arguments.key == "native-key"
            # The application may have performed its effect without replying.
        request_count = len(responses.requests)
        with pytest.raises(LocalRecoveryRequired):
            async with agent.session(storage=storage, resume=identity):
                pytest.fail("A missing manual receipt reopened without recovery")
        assert len(responses.requests) == request_count


@pytest.mark.skipif(sys.platform == "win32", reason="Uses POSIX per-process file limits")
async def test_local_manual_receipt_write_failure_stops_dependent_model(responses, tmp_path):
    responses.respond(function_call("lookup", {"key": "native-key"}, "manual-write-failure"))
    responses.respond(assistant_output("must never start"))
    process = await _start(responses, tmp_path, "manual-receipt-failure")
    code, rows, err = await _collect(process)
    assert code == 0, (rows, err)
    assert rows == [{"reply_failed": True}, {"close_failed": True}]
    assert len(responses.requests) == 1


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
