"""Public native storage qualification with SQLite and independent processes.

The failure switches wrap real persistent operations; they do not replace the
native engine, provider transport, callback dispatch, or session lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agents import (
    Agent,
    CanonicalStorage,
    LocalStorage,
    NativeAgents,
    NativeGoals,
    NativeProvider,
    SQLiteThreadStore,
    ToolContext,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="SQLiteThreadStore reference adapter requires POSIX locking"
)


class FaultStore(SQLiteThreadStore):
    """Inject one operation failure while retaining SQLite ownership and CAS."""

    def __init__(self, path: Path, mode: str):
        super().__init__(path)
        self.mode = mode
        self.fired = False

    def fail(self, operation: str) -> None:
        if self.mode == operation and not self.fired:
            self.fired = True
            raise OSError("injected storage failure secret-do-not-expose")

    def read(self, thread_id, token):
        self.fail("read-fail")
        return super().read(thread_id, token)

    def commit(self, thread_id, token, expected_revision, write_id, payload):
        items = _responses(json.loads(payload))
        has_call = any(item.get("type") == "function_call" for item in items)
        has_result = any(item.get("type") == "function_call_output" for item in items)
        boundary = "result-fail" if has_result else "intent-fail" if has_call else ""
        self.fail(boundary)
        if self.mode == "stale" and has_call and not self.fired:
            self.fired = True
            super().release(thread_id, token)
            successor = super().acquire(thread_id)
            try:
                # An old native writer still holds token. Its commit must fail.
                return super().commit(thread_id, token, expected_revision, write_id, payload)
            finally:
                super().release(thread_id, successor)
        super().commit(thread_id, token, expected_revision, write_id, payload)
        if self.mode == "ack-confirm-fail" and has_result:
            raise OSError("injected confirmation failure secret-do-not-expose")
        if self.mode == "ack-loss" and has_result and not self.fired:
            self.fired = True
            raise OSError("injected lost acknowledgement secret-do-not-expose")

    def flush(self, thread_id, token):
        self.fail("flush-fail")
        return super().flush(thread_id, token)

    def release(self, thread_id, token):
        self.fail("release-before-fail")
        # A backend can release ownership and still lose its acknowledgement.
        result = super().release(thread_id, token)
        self.fail("release-fail")
        return result


async def _worker() -> None:
    mode, url, root, identity, backend = sys.argv[1:]
    directory = Path(root)
    if mode.startswith("local-"):
        import signal

        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    store = FaultStore(directory / "canonical.sqlite", mode)
    storage = CanonicalStorage(store) if backend == "custom" else LocalStorage(directory / "local")
    effect_started = asyncio.Event()

    @function_tool
    async def record(context: ToolContext[str], key: str) -> str:
        """Append one durable application effect."""
        with (directory / "effects").open("a") as stream:
            stream.write(context.context + ":" + key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if mode == "unknown":
            os._exit(67)
        if mode in {"graceful-close", "graceful-cancel"}:
            effect_started.set()
            await asyncio.Event().wait()
        if mode == "local-result-fail":
            import resource

            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        return context.context + ":" + key

    provider = NativeProvider(base_url=url, api_key="synthetic-" + mode, supports_websockets=False)
    agent = Agent(
        instructions="Record the requested effect once.",
        tools=[record],
        provider=provider,
        model="gpt-5.1-codex",
    )
    turn_error = None
    try:
        async with (
            provider,
            agent.session(context=mode, storage=storage, resume=identity or None) as session,
        ):
            print(json.dumps({"opened": session.id, "pid": os.getpid()}), flush=True)
            if mode == "hold":
                await asyncio.to_thread(sys.stdin.readline)
                return
            try:
                turn = await session.submit(mode + " input")
                if mode in {"graceful-close", "graceful-cancel"}:
                    await effect_started.wait()
                    print(json.dumps({"effect_started": True}), flush=True)
                    await asyncio.to_thread(sys.stdin.readline)
                    if mode == "graceful-cancel":
                        await turn.cancel()
                    return
                result = await turn.get_result()
                print(json.dumps({"output": result.final_output}), flush=True)
                print(json.dumps({"items": [item async for item in session.items()]}), flush=True)
            except Exception as error:
                print(json.dumps({"turn_error": str(error)}), flush=True)
                try:
                    await (await session.submit("must remain failed")).get_result()
                except Exception as later:
                    print(json.dumps({"sticky_error": str(later)}), flush=True)
                else:
                    raise AssertionError("Storage failure was forgotten by the next turn")
                turn_error = error
        if mode == "source":
            # There is no public Session.close API. The context manager and
            # other SDK owners share this internal cleanup helper.
            await session._aclose()
            await session._aclose()
            print(json.dumps({"successful_cleanup_repeated": True}), flush=True)
        if turn_error is not None:
            print(json.dumps({"close_succeeded": True}), flush=True)
            raise turn_error
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
        if mode in {"intent-fail", "local-intent-fail", "release-fail", "release-before-fail"}:
            # Probe an actual SDK cleanup retry, rather than calling __aexit__
            # again on an already exhausted Python context manager.
            for _ in range(2):
                try:
                    await session._aclose()
                except Exception as later:
                    print(json.dumps({"cleanup_retry_error": str(later)}), flush=True)
                else:
                    print(json.dumps({"cleanup_retry_succeeded": True}), flush=True)
        if backend == "custom" and mode in {
            "read-fail",
            "flush-fail",
            "release-fail",
            "release-before-fail",
        }:
            # Probe before this process exits, so OS cleanup cannot hide a
            # missing explicit release in initialization or close.
            probe = SQLiteThreadStore(directory / "canonical.sqlite")
            token = probe.acquire(identity)
            try:
                assert probe.read(identity, token) is not None
            finally:
                probe.release(identity, token)
            print(json.dumps({"released": True}), flush=True)
        raise


async def _start(peer, directory, mode, identity="", backend="custom"):
    root = str(Path(__file__).resolve().parents[1])
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (root, os.environ.get("PYTHONPATH")))),
    }
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "tests.test_custom_storage_durability",
        mode,
        peer.url,
        str(directory),
        identity,
        backend,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _collect(process):
    try:
        async with timeout(45):
            out, err = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    return process.returncode, [json.loads(line) for line in out.splitlines()], err.decode()


def _responses(snapshot):
    return [item["payload"] for item in snapshot["items"] if item.get("type") == "response_item"]


def _snapshot(directory: Path, identity: str):
    store = SQLiteThreadStore(directory / "canonical.sqlite")
    token = store.acquire(identity)
    try:
        value = store.read(identity, token)
        return json.loads(value) if value is not None else None
    finally:
        store.release(identity, token)


def _row(rows, name):
    return next(row[name] for row in rows if name in row)


async def test_custom_store_reopens_history_with_current_bindings(responses, tmp_path):
    responses.respond(function_call("record", {"key": "first"}, "call-first"))
    responses.respond(assistant_output("first finished"))
    code, first, err = await _collect(await _start(responses, tmp_path, "source"))
    assert code == 0, (first, err)
    identity = _row(first, "opened")
    before = _snapshot(tmp_path, identity)
    assert before is not None
    responses.respond(function_call("record", {"key": "second"}, "call-second"))
    responses.respond(assistant_output("second finished"))
    code, second, err = await _collect(await _start(responses, tmp_path, "destination", identity))
    assert code == 0, (second, err)
    assert _row(second, "opened") == identity
    assert _row(first, "pid") != _row(second, "pid")
    assert _row(first, "successful_cleanup_repeated") is True
    assert _row(second, "output") == "second finished"
    assert (tmp_path / "effects").read_text() == "source:first\ndestination:second\n"
    assert len(responses.requests) == 4
    history = responses.requests[2]["input"]
    assert "first finished" in json.dumps(history)
    assert "source:first" in json.dumps(history)
    assert [item["call_id"] for item in history if item.get("type") == "function_call"] == [
        "call-first"
    ]
    assert [item["call_id"] for item in history if item.get("type") == "function_call_output"] == [
        "call-first"
    ]
    assert responses.request_headers[2]["authorization"] == "Bearer synthetic-destination"
    after = _snapshot(tmp_path, identity)
    assert after["revision"] > before["revision"]
    assert after["items"][: len(before["items"])] == before["items"]


@pytest.mark.parametrize("mode,effects", [("intent-fail", 0), ("result-fail", 1), ("stale", 0)])
async def test_failed_commits_stop_effects_and_dependent_model(responses, tmp_path, mode, effects):
    responses.respond(function_call("record", {"key": "once"}, "failed-call"))
    responses.respond(assistant_output("must not publish success"))
    code, rows, err = await _collect(await _start(responses, tmp_path, mode))
    assert code != 0, (rows, err)
    assert _row(rows, "turn_error")
    assert _row(rows, "sticky_error")
    assert not any("close_succeeded" in row for row in rows), rows
    assert _row(rows, "error")
    if mode == "intent-fail":
        retries = [row["cleanup_retry_error"] for row in rows if "cleanup_retry_error" in row]
        assert len(retries) == 2 and all(retries), rows
        assert not any("cleanup_retry_succeeded" in row for row in rows), rows
    assert "secret-do-not-expose" not in json.dumps(rows)
    assert len(responses.requests) == 1
    path = tmp_path / "effects"
    assert (path.read_text().splitlines() if path.exists() else []) == (
        [mode + ":once"] if effects else []
    )
    saved = _snapshot(tmp_path, _row(rows, "opened"))
    if effects:
        assert "failed-call" in json.dumps(saved["items"])
        assert not any(item.get("type") == "function_call_output" for item in _responses(saved))
        count = len(responses.requests)
        for _ in range(2):
            retry_code, retry, retry_err = await _collect(
                await _start(responses, tmp_path, "destination", _row(rows, "opened"))
            )
            assert retry_code != 0 and _row(retry, "error"), (retry, retry_err)
            assert "unknown" in _row(retry, "message").lower()
            assert len(responses.requests) == count
        assert path.read_text() == mode + ":once\n"
        # Application reconciliation uses the authoritative fsynced effect,
        # preserving the original turn and call identity without running a tool.
        responses.replies.clear()
        storage = CanonicalStorage(SQLiteThreadStore(tmp_path / "canonical.sqlite"))
        await storage.reconcile(
            _row(rows, "opened"),
            turn_id=next(
                item["internal_chat_message_metadata_passthrough"]["turn_id"]
                for item in _responses(saved)
                if item.get("type") == "function_call" and item["call_id"] == "failed-call"
            ),
            call_id="failed-call",
            output=mode + ":once",
        )
        responses.respond(function_call("record", {"key": "fresh"}, "fresh-call"))
        responses.respond(assistant_output("reconciled continuation"))
        resumed_code, resumed, resumed_err = await _collect(
            await _start(responses, tmp_path, "destination", _row(rows, "opened"))
        )
        assert resumed_code == 0, (resumed, resumed_err)
        assert _row(resumed, "output") == "reconciled continuation"
        assert len(responses.requests) == count + 2
        assert mode + ":once" in json.dumps(responses.requests[count]["input"])
        assert path.read_text() == mode + ":once\ndestination:fresh\n"


async def test_lost_result_ack_is_reconciled_without_duplicate_effects(responses, tmp_path):
    responses.respond(function_call("record", {"key": "once"}, "lost-ack-call"))
    responses.respond(assistant_output("ack reconciled"))
    code, rows, err = await _collect(await _start(responses, tmp_path, "ack-loss"))
    assert code == 0, (rows, err)
    assert _row(rows, "output") == "ack reconciled"
    assert (tmp_path / "effects").read_text() == "ack-loss:once\n"
    assert len(responses.requests) == 2
    saved = _snapshot(tmp_path, _row(rows, "opened"))
    assert sum(item.get("type") == "function_call" for item in _responses(saved)) == 1
    assert sum(item.get("type") == "function_call_output" for item in _responses(saved)) == 1


async def test_readable_lost_ack_without_confirmation_keeps_session_failed(responses, tmp_path):
    responses.respond(function_call("record", {"key": "once"}, "unconfirmed-call"))
    responses.respond(assistant_output("must not complete"))
    code, rows, err = await _collect(await _start(responses, tmp_path, "ack-confirm-fail"))
    assert code != 0, (rows, err)
    assert _row(rows, "turn_error") and _row(rows, "sticky_error")
    assert not any("close_succeeded" in row for row in rows), rows
    assert len(responses.requests) == 1
    assert "secret-do-not-expose" not in json.dumps(rows)
    assert (tmp_path / "effects").read_text() == "ack-confirm-fail:once\n"
    identity = _row(rows, "opened")
    saved = _snapshot(tmp_path, identity)
    assert sum(item.get("type") == "function_call_output" for item in _responses(saved)) == 1
    # A fresh owner can confirm the saved known receipt; opening never repeats
    # the already recorded callback merely to reconstruct its result.
    responses.replies.clear()
    responses.respond(assistant_output("fresh owner confirmed"))
    code, newer, err = await _collect(await _start(responses, tmp_path, "destination", identity))
    assert code == 0 and _row(newer, "output") == "fresh owner confirmed", (newer, err)
    assert len(responses.requests) == 2
    assert "ack-confirm-fail:once" in json.dumps(responses.requests[-1]["input"])
    assert (tmp_path / "effects").read_text() == "ack-confirm-fail:once\n"


@pytest.mark.parametrize("mode", ["read-fail", "flush-fail", "release-fail", "release-before-fail"])
async def test_lifecycle_failure_surfaces_and_releases_writer(responses, tmp_path, mode):
    responses.respond(assistant_output("retained baseline"))
    code, source, err = await _collect(await _start(responses, tmp_path, "source"))
    assert code == 0, (source, err)
    identity = _row(source, "opened")
    baseline = _snapshot(tmp_path, identity)
    responses.respond(assistant_output("completed before close"))
    code, rows, err = await _collect(await _start(responses, tmp_path, mode, identity))
    assert code != 0 and _row(rows, "error"), (rows, err)
    assert _row(rows, "released") is True, (rows, err)
    assert "secret-do-not-expose" not in json.dumps(rows)
    assert not (tmp_path / "effects").exists()
    if mode == "read-fail":
        assert len(responses.requests) == 1
    else:
        assert len(responses.requests) <= 2
    # Failed initialization and close both relinquish this same thread's writer.
    saved = _snapshot(tmp_path, identity)
    assert saved["items"][: len(baseline["items"])] == baseline["items"]
    responses.respond(assistant_output("successor"))
    code, successor, err = await _collect(
        await _start(responses, tmp_path, "destination", identity)
    )
    assert code == 0 and _row(successor, "opened") == identity, (successor, err)
    assert "retained baseline" in json.dumps(responses.requests[-1]["input"])


@pytest.mark.parametrize("backend", ["custom", "local"])
async def test_competing_process_is_fenced_and_owner_death_releases(responses, tmp_path, backend):
    owner = await _start(responses, tmp_path, "hold", backend=backend)
    try:
        async with timeout(30):
            opened = json.loads(await owner.stdout.readline())
        assert "opened" in opened, opened
        identity = opened["opened"]
        code, rows, err = await _collect(
            await _start(responses, tmp_path, "destination", identity, backend)
        )
        assert code != 0 and _row(rows, "error"), (rows, err)
        assert not responses.requests
    finally:
        if owner.returncode is None:
            owner.kill()
        await _collect(owner)
    responses.respond(assistant_output("new owner"))
    code, rows, err = await _collect(
        await _start(responses, tmp_path, "destination", identity, backend)
    )
    assert code == 0 and _row(rows, "output") == "new owner", (rows, err)
    assert len(responses.requests) == 1


@pytest.mark.parametrize("backend", ["custom", "local"])
async def test_process_death_with_unknown_effect_blocks_every_resume(responses, tmp_path, backend):
    responses.respond(function_call("record", {"key": "unknown"}, "unknown-call"))
    code, rows, err = await _collect(await _start(responses, tmp_path, "unknown", backend=backend))
    assert code == 67, (rows, err)
    assert (tmp_path / "effects").read_text() == "unknown:unknown\n"
    identity = _row(rows, "opened")
    assert len(responses.requests) == 1
    responses.respond(assistant_output("must not resume"))
    for _ in range(2):
        code, rows, err = await _collect(
            await _start(responses, tmp_path, "destination", identity, backend)
        )
        assert code != 0 and "unknown" in _row(rows, "message").lower(), (rows, err)
        assert len(responses.requests) == 1
        assert (tmp_path / "effects").read_text() == "unknown:unknown\n"


@pytest.mark.parametrize("backend", ["custom", "local"])
@pytest.mark.parametrize("ending", ["close", "cancel"])
async def test_graceful_stop_cannot_relabel_an_inflight_effect_as_a_known_result(
    responses, tmp_path, backend, ending
):
    mode = "graceful-" + ending
    responses.respond(function_call("record", {"key": "inflight"}, "inflight-call"))
    responses.respond(assistant_output("must not resume"))
    process = await _start(responses, tmp_path, mode, backend=backend)
    try:
        async with timeout(30):
            opened = json.loads(await process.stdout.readline())
            assert "opened" in opened, opened
            reached = json.loads(await process.stdout.readline())
            assert reached == {"effect_started": True}, reached
        assert (tmp_path / "effects").read_text() == mode + ":inflight\n"
        assert len(responses.requests) == 1
        process.stdin.write(b"stop\n")
        await process.stdin.drain()
        code, rows, err = await _collect(process)
        assert code == 0, (rows, err)
    finally:
        if process.returncode is None:
            process.kill()
            await _collect(process)
    assert len(responses.requests) == 1
    identity = opened["opened"]
    for _ in range(2):
        code, rows, err = await _collect(
            await _start(responses, tmp_path, "destination", identity, backend)
        )
        assert code != 0 and "unknown" in _row(rows, "message").lower(), (rows, err)
        assert len(responses.requests) == 1
        assert (tmp_path / "effects").read_text() == mode + ":inflight\n"
    if backend == "custom":
        saved = _snapshot(tmp_path, identity)
        assert not any(
            item.get("type") == "function_call_output" and item["call_id"] == "inflight-call"
            for item in _responses(saved)
        )
        original_call = next(
            item
            for item in _responses(saved)
            if item.get("type") == "function_call" and item["call_id"] == "inflight-call"
        )
        await CanonicalStorage(SQLiteThreadStore(tmp_path / "canonical.sqlite")).reconcile(
            identity,
            turn_id=original_call["internal_chat_message_metadata_passthrough"]["turn_id"],
            call_id="inflight-call",
            output=mode + ":inflight",
        )
        responses.replies.clear()
        responses.respond(assistant_output("externally confirmed result"))
        code, newer, err = await _collect(
            await _start(responses, tmp_path, "destination", identity, backend)
        )
        assert code == 0 and _row(newer, "output") == "externally confirmed result", (newer, err)
        assert len(responses.requests) == 2
        assert mode + ":inflight" in json.dumps(responses.requests[-1]["input"])
        assert (tmp_path / "effects").read_text() == mode + ":inflight\n"


@pytest.mark.parametrize("backend", ["custom", "local"])
async def test_committed_receipt_survives_process_death_without_replaying(
    responses, tmp_path, backend
):
    reached = asyncio.Event()
    release = asyncio.Event()

    async def hold(event):
        if len(responses.requests) == 2 and event["type"] == "response.created":
            reached.set()
            await release.wait()

    responses.before_event = hold
    responses.respond(function_call("record", {"key": "known"}, "known-call"))
    responses.respond(assistant_output("unpublished final"))
    process = await _start(responses, tmp_path, "source", backend=backend)
    try:
        async with timeout(30):
            opened = json.loads(await process.stdout.readline())
            assert "opened" in opened, opened
            await reached.wait()
        assert (tmp_path / "effects").read_text() == "source:known\n"
        assert "source:known" in json.dumps(responses.requests[1]["input"])
        process.kill()
        await _collect(process)
    finally:
        release.set()
        if process.returncode is None:
            process.kill()
            await _collect(process)
    responses.before_event = None
    responses.respond(assistant_output("new turn"))
    code, rows, err = await _collect(
        await _start(responses, tmp_path, "destination", opened["opened"], backend)
    )
    assert code == 0 and _row(rows, "output") == "new turn", (rows, err)
    assert len(responses.requests) == 3
    history = responses.requests[-1]["input"]
    assert "source:known" in json.dumps(history)
    assert "unpublished final" not in json.dumps(history)
    assert (tmp_path / "effects").read_text() == "source:known\n"


@pytest.mark.parametrize("unsupported", [NativeAgents, NativeGoals], ids=["children", "goals"])
async def test_custom_unsupported_state_rejects_before_acquisition_or_effects(
    responses, tmp_path, unsupported
):
    acquisitions = []
    effects = []

    class ObservedStore(SQLiteThreadStore):
        def acquire(self, thread_id):
            acquisitions.append(thread_id)
            return super().acquire(thread_id)

    @function_tool
    def record() -> str:
        """Record an application effect."""
        effects.append("called")
        return "called"

    backend = ObservedStore(tmp_path / "canonical.sqlite")
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-admission")
    agent = Agent(
        instructions="Admission must precede any execution.",
        model="gpt-5.1-codex",
        tools=[unsupported(), record],
        provider=provider,
    )
    async with provider:
        with pytest.raises(ValueError, match="CanonicalStorage preview does not support"):
            async with agent.session(storage=CanonicalStorage(backend)):
                pytest.fail("Unsupported custom storage combination opened")
    assert acquisitions == []
    assert effects == []
    assert responses.requests == []


async def test_local_code_mode_rejects_before_opening_profile_or_running_work(responses, tmp_path):
    effects = []

    @function_tool
    def record() -> str:
        """Record an application effect."""
        effects.append("called")
        return "called"

    profile = tmp_path / "must-not-open"
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-code-admission")
    agent = Agent(
        instructions="Reject unsupported persistent code mode before execution.",
        model="gpt-5.1-codex",
        tools=[record],
        provider=provider,
        programmatic_tool_calling=True,
    )
    async with provider:
        with pytest.raises(ValueError, match="LocalStorage"):
            async with agent.session(storage=LocalStorage(profile)):
                pytest.fail("Unsupported local code mode opened")
    assert not profile.exists()
    assert effects == []
    assert responses.requests == []


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux prlimit and SIGXFSZ")
@pytest.mark.parametrize("mode,effects", [("local-intent-fail", 0), ("local-result-fail", 1)])
async def test_builtin_local_write_failure_gates_native_work(responses, tmp_path, mode, effects):
    import resource

    if not hasattr(resource, "prlimit"):
        pytest.skip("OS has no prlimit")
    process = None

    async def fail_before_call(event):
        if event["type"] == "response.created" and len(responses.requests) == 1:
            assert process is not None
            resource.prlimit(process.pid, resource.RLIMIT_FSIZE, (0, 0))

    if not effects:
        responses.before_event = fail_before_call
    responses.respond(function_call("record", {"key": "once"}, "local-failed-call"))
    responses.respond(assistant_output("must not complete"))
    process = await _start(responses, tmp_path, mode, backend="local")
    code, rows, err = await _collect(process)
    responses.before_event = None
    assert code != 0, (rows, err)
    assert _row(rows, "turn_error") and _row(rows, "sticky_error")
    assert not any("close_succeeded" in row for row in rows), rows
    assert _row(rows, "error")
    if mode == "local-intent-fail":
        retries = [row["cleanup_retry_error"] for row in rows if "cleanup_retry_error" in row]
        assert len(retries) == 2 and all(retries), rows
        assert not any("cleanup_retry_succeeded" in row for row in rows), rows
    assert len(responses.requests) == 1
    path = tmp_path / "effects"
    assert (path.read_text().splitlines() if path.exists() else []) == (
        [mode + ":once"] if effects else []
    )
    identity = _row(rows, "opened")
    responses.replies.clear()
    responses.respond(assistant_output("successor"))
    successor_code, successor, successor_err = await _collect(
        await _start(responses, tmp_path, "destination", identity, "local")
    )
    if effects:
        assert successor_code != 0 and "unknown" in _row(successor, "message").lower(), (
            successor,
            successor_err,
        )
        assert len(responses.requests) == 1
        assert path.read_text() == mode + ":once\n"
    else:
        assert successor_code == 0 and _row(successor, "output") == "successor", (
            successor,
            successor_err,
        )
        assert len(responses.requests) == 2


if __name__ == "__main__":
    asyncio.run(_worker())
