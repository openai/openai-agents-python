"""Public native storage qualification with SQLite and independent processes.

The failure switches wrap real persistent operations; they do not replace the
native engine, provider transport, callback dispatch, or session lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from agents import (
    Agent,
    CustomStorage,
    Environment,
    HostedShell,
    LocalRecoveryRequired,
    LocalStorage,
    NativeAgents,
    NativeGoals,
    NativeProvider,
    SessionStoreCleanup,
    ShellCall,
    ShellCommandOutput,
    ShellExit,
    ShellOutput,
    SQLiteSessionStore,
    ToolContext,
    ToolOutput,
    ToolOutputImage,
    ToolOutputText,
    function_tool,
    get_cleanup,
    shell_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="SQLiteSessionStore reference adapter requires POSIX locking"
)


class FaultStore(SQLiteSessionStore):
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
        has_call = any(item.get("type") in {"function_call", "shell_call"} for item in items)
        has_result = any(
            item.get("type") in {"function_call_output", "shell_call_output"} for item in items
        )
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
        if self.mode == "intent-crash" and has_call and not has_result:
            # Die after durable native intent, before the callback can journal it.
            os._exit(68)
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
    storage = CustomStorage(store) if backend == "custom" else LocalStorage(directory / "local")
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
        if isinstance(error, LocalRecoveryRequired):
            print(
                json.dumps(
                    {
                        "recovery_calls": [
                            {
                                "thread_id": call.thread_id,
                                "turn_id": call.turn_id,
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": call.arguments,
                            }
                            for call in error.calls
                        ]
                    }
                ),
                flush=True,
            )
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
            probe = SQLiteSessionStore(directory / "canonical.sqlite")
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
    store = SQLiteSessionStore(directory / "canonical.sqlite")
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
    assert _row(rows, "close_succeeded") is True
    assert _row(rows, "error")
    if mode == "intent-fail":
        assert sum("cleanup_retry_succeeded" in row for row in rows) == 2, rows
        assert not any("cleanup_retry_error" in row for row in rows), rows
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
        storage = CustomStorage(SQLiteSessionStore(tmp_path / "canonical.sqlite"))
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


@pytest.mark.parametrize("interrupted", [False, True])
async def test_application_shell_commits_one_typed_receipt_to_custom_storage(
    responses, tmp_path, interrupted
):
    from tests.test_native_application_shell import shell_call
    from tests.test_native_manual_tools import provider

    calls: list[str] = []
    output = ShellOutput(
        output=[
            ShellCommandOutput(
                stdout="ready and more", stderr="details", outcome=ShellExit(exit_code=0)
            )
        ]
    )

    class ShellStore(FaultStore):
        pending_shell_calls: list[list[str]]

        def commit(self, thread_id, token, expected_revision, write_id, payload):
            super().commit(thread_id, token, expected_revision, write_id, payload)
            self.pending_shell_calls = json.loads(payload).get("pending_shell_calls", [])

    def execute(_context: ToolContext[None], call: ShellCall) -> ShellOutput:
        ((_, saved_call),) = store.pending_shell_calls
        assert saved_call == call.call_id
        calls.append(call.call_id)
        return output

    responses.respond(shell_call())
    if not interrupted:
        responses.respond(assistant_output("done"))
    path = tmp_path / "canonical.sqlite"
    store = ShellStore(path, "result-fail" if interrupted else "normal")
    async with timeout(30), provider(responses) as native_provider:
        agent = Agent(
            instructions="Use the configured Shell tool.",
            model="gpt-5",
            provider=native_provider,
            tools=[shell_tool(execute)],
        )
        async with agent.session(storage=CustomStorage(store)) as session:
            identity = session.id
            turn = await session.submit("Run the test command.")
            if interrupted:
                with pytest.raises(RuntimeError):
                    await turn.get_result()
            else:
                assert (await turn.get_result()).final_output == "done"
                (history_receipt,) = [
                    item async for item in session.items() if item["type"] == "shell_call_output"
                ]
                assert history_receipt["id"] == "shell-local:output"
                assert history_receipt["call_id"] == "shell-local"

        if interrupted:
            assert len(responses.requests) == 1
            saved = _snapshot(tmp_path, identity)
            assert not any(item["type"] == "shell_call_output" for item in _responses(saved))
            (original,) = [item for item in _responses(saved) if item["type"] == "shell_call"]
            turn_id = original["internal_chat_message_metadata_passthrough"]["turn_id"]
            recovery = CustomStorage(SQLiteSessionStore(path))
            with pytest.raises(LocalRecoveryRequired) as blocked:
                async with agent.session(storage=recovery, resume=identity):
                    pytest.fail("The unresolved application Shell call allowed reopening")
            (uncertain,) = blocked.value.calls
            assert (uncertain.thread_id, uncertain.turn_id, uncertain.call_id, uncertain.name) == (
                identity,
                turn_id,
                "shell-local",
                "shell",
            )
            assert len(responses.requests) == 1
            with pytest.raises(
                RuntimeError, match="Shell reconciliation requires a valid ShellOutput"
            ):
                await recovery.reconcile(
                    identity, turn_id=turn_id, call_id="shell-local", output="ready"
                )
            await recovery.reconcile(
                identity, turn_id=turn_id, call_id="shell-local", output=output
            )
            with pytest.raises(
                RuntimeError, match="requires an unresolved original turn/call identity"
            ):
                await recovery.reconcile(
                    identity, turn_id=turn_id, call_id="shell-local", output=output
                )

            responses.respond(assistant_output("done"))
            async with agent.session(storage=recovery, resume=identity) as resumed:
                assert (
                    await (await resumed.submit("Continue.")).get_result()
                ).final_output == "done"

    assert calls == ["shell-local"]
    saved = _snapshot(tmp_path, identity)
    assert saved is not None
    assert "pending_shell_calls" not in saved
    items = _responses(saved)
    calls_saved = [item for item in items if item["type"] == "shell_call"]
    assert [item["call_id"] for item in calls_saved] == calls
    (receipt,) = [item for item in items if item["type"] == "shell_call_output"]
    assert receipt["call_id"] == "shell-local"
    assert receipt["max_output_length"] == 5
    assert receipt["output"] == [
        {"stdout": "ready", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}
    ]
    (sent,) = [
        item for item in responses.requests[1]["input"] if item["type"] == "shell_call_output"
    ]
    assert sent["call_id"] == receipt["call_id"] and sent["output"] == receipt["output"]


async def test_local_storage_reconciles_interrupted_application_shell(responses, tmp_path):
    from tests.test_native_application_shell import shell_call
    from tests.test_native_manual_tools import provider

    entered = asyncio.Event()
    calls: list[str] = []

    async def execute(_context: ToolContext[None], call: ShellCall) -> ShellOutput:
        calls.append(call.call_id)
        entered.set()
        await asyncio.Future()
        raise AssertionError("The interrupted executor must not return")

    responses.respond(shell_call())
    storage = LocalStorage(tmp_path / "local")
    async with timeout(30), provider(responses) as native_provider:
        agent = Agent(
            instructions="Use the application Shell tool.",
            model="gpt-5",
            provider=native_provider,
            tools=[shell_tool(execute)],
        )
        async with agent.session(storage=storage) as session:
            identity = session.id
            turn = await session.submit("Run the test command.")
            await entered.wait()
            await turn.cancel()

        with pytest.raises(LocalRecoveryRequired) as blocked:
            async with agent.session(storage=storage, resume=identity):
                pytest.fail("The unresolved application Shell call allowed reopening")
        (uncertain,) = blocked.value.calls
        assert (uncertain.thread_id, uncertain.call_id, uncertain.name) == (
            identity,
            "shell-local",
            "shell",
        )
        assert len(responses.requests) == 1

        untyped = uncertain.reconcile(outcome="applied", result="verified")
        with pytest.raises(RuntimeError, match="requires a verified ShellOutput"):
            async with agent.session(storage=storage, resume=identity, reconciled_calls=[untyped]):
                pytest.fail("A text result settled the application Shell call")
        verified = uncertain.reconcile(
            outcome="applied",
            result=ShellOutput(
                output=[
                    ShellCommandOutput(
                        stdout="ready and more", stderr="details", outcome=ShellExit(exit_code=0)
                    )
                ]
            ),
        )
        async with agent.session(
            storage=storage, resume=identity, reconciled_calls=[verified]
        ) as confirmed:
            (confirmed_receipt,) = [
                item async for item in confirmed.items() if item["type"] == "shell_call_output"
            ]
            assert confirmed_receipt["id"]
        conflicting = uncertain.reconcile(
            outcome="applied",
            result=ShellOutput(
                output=[
                    ShellCommandOutput(stdout="wrong", stderr="", outcome=ShellExit(exit_code=0))
                ]
            ),
        )
        with pytest.raises(RuntimeError, match="Reconciliation conflicts with a retained result"):
            async with agent.session(
                storage=storage, resume=identity, reconciled_calls=[conflicting]
            ):
                pytest.fail("A second result replaced the confirmed application Shell receipt")
        responses.respond(assistant_output("done"))
        async with agent.session(
            storage=storage, resume=identity, reconciled_calls=[verified]
        ) as resumed:
            (history_receipt,) = [
                item async for item in resumed.items() if item["type"] == "shell_call_output"
            ]
            assert history_receipt["id"] == confirmed_receipt["id"]
            assert history_receipt["turn_id"] == uncertain.turn_id
            assert (await (await resumed.submit("Continue.")).get_result()).final_output == "done"

    assert calls == ["shell-local"]
    assert len(responses.requests) == 2
    (receipt,) = [
        item for item in responses.requests[1]["input"] if item["type"] == "shell_call_output"
    ]
    assert receipt["call_id"] == "shell-local" and receipt["max_output_length"] == 5
    assert receipt["output"] == [
        {"stdout": "ready", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}
    ]


async def test_local_storage_reopens_after_markerless_hosted_shell(responses, tmp_path):
    from tests.test_native_application_shell import shell_call
    from tests.test_native_manual_tools import provider

    responses.respond(shell_call(), assistant_output("Provider finished."))
    storage = LocalStorage(tmp_path / "local")
    async with timeout(30), provider(responses) as native_provider:
        agent = Agent(
            instructions="Use the provider's Shell tool.",
            model="gpt-5.5",
            provider=native_provider,
            tools=[HostedShell(environment={"type": "container_auto"})],
        )
        async with agent.session(storage=storage) as session:
            identity = session.id
            assert (await (await session.submit("Answer.")).get_result()).final_output == (
                "Provider finished."
            )
        async with agent.session(storage=storage, resume=identity):
            pass
    assert len(responses.requests) == 1


@pytest.mark.parametrize(
    "environment",
    [
        None,
        {"type": "container_auto"},
        {"type": "container_reference", "container_id": "cntr-shell"},
    ],
)
async def test_canonical_reconciliation_rejects_provider_shell_calls(
    responses, tmp_path, environment
):
    from tests.test_native_application_shell import shell_call
    from tests.test_native_manual_tools import provider

    def execute(_context: ToolContext[None], _call: ShellCall) -> ShellOutput:
        pytest.fail("A provider Shell call reached the application executor")

    call = shell_call() if environment is None else shell_call(environment=environment)
    responses.respond(call, assistant_output("Provider finished."))
    storage = CustomStorage(SQLiteSessionStore(tmp_path / "canonical.sqlite"))
    async with timeout(30), provider(responses) as native_provider:
        agent = Agent(
            instructions="Use the configured Shell tool.",
            model="gpt-5.5" if environment is None else "gpt-5",
            provider=native_provider,
            tools=(
                [HostedShell(environment={"type": "container_auto"})]
                if environment is None
                else [shell_tool(execute)]
            ),
        )
        async with agent.session(storage=storage) as session:
            identity = session.id
            assert (await (await session.submit("Answer.")).get_result()).final_output == (
                "Provider finished."
            )

    before = _snapshot(tmp_path, identity)
    assert "pending_shell_calls" not in before
    (original,) = [item for item in _responses(before) if item["type"] == "shell_call"]
    if environment is None:
        assert "container_id" not in original and "environment" not in original
    with pytest.raises(
        RuntimeError, match="requires an unresolved original application Shell call"
    ):
        await storage.reconcile(
            identity,
            turn_id=original["internal_chat_message_metadata_passthrough"]["turn_id"],
            call_id="shell-local",
            output=ShellOutput(
                output=[
                    ShellCommandOutput(stdout="verified", stderr="", outcome=ShellExit(exit_code=0))
                ]
            ),
        )
    assert _snapshot(tmp_path, identity) == before
    assert len(responses.requests) == 1


async def test_readable_lost_ack_without_confirmation_keeps_session_failed(responses, tmp_path):
    responses.respond(function_call("record", {"key": "once"}, "unconfirmed-call"))
    responses.respond(assistant_output("must not complete"))
    code, rows, err = await _collect(await _start(responses, tmp_path, "ack-confirm-fail"))
    assert code != 0, (rows, err)
    assert _row(rows, "turn_error") and _row(rows, "sticky_error")
    assert _row(rows, "close_succeeded") is True
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
    if mode in {"release-fail", "release-before-fail"}:
        assert sum("cleanup_retry_succeeded" in row for row in rows) == 2, rows
        assert not any("cleanup_retry_error" in row for row in rows), rows
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


async def test_custom_store_inspects_durable_intent_before_callback_journal(responses, tmp_path):
    responses.respond(function_call("record", {"key": "undispatched"}, "intent-only-call"))
    code, rows, err = await _collect(await _start(responses, tmp_path, "intent-crash"))
    assert code == 68, (rows, err)
    identity = _row(rows, "opened")
    assert not (tmp_path / "effects").exists()
    assert len(responses.requests) == 1

    for _ in range(2):
        code, rows, err = await _collect(await _start(responses, tmp_path, "hold", identity))
        assert code != 0, (rows, err)
        assert _row(rows, "error") == "LocalRecoveryRequired"
        calls = _row(rows, "recovery_calls")
        assert len(calls) == 1
        call = calls[0]
        assert call["thread_id"] == identity
        assert call["call_id"] == "intent-only-call"
        assert call["name"] == "record"
        assert call["arguments"] == {"key": "undispatched"}
        assert call["turn_id"]
        assert len(responses.requests) == 1
        assert not (tmp_path / "effects").exists()

    await CustomStorage(SQLiteSessionStore(tmp_path / "canonical.sqlite")).reconcile(
        call["thread_id"],
        turn_id=call["turn_id"],
        call_id=call["call_id"],
        output="Application verified that this effect was not dispatched.",
    )
    responses.respond(assistant_output("continued without replay"))
    code, rows, err = await _collect(await _start(responses, tmp_path, "destination", identity))
    assert code == 0, (rows, err)
    assert _row(rows, "output") == "continued without replay"
    assert len(responses.requests) == 2
    assert "Application verified" in json.dumps(responses.requests[-1]["input"])
    assert not (tmp_path / "effects").exists()


@pytest.mark.parametrize("receipt_fails", [False, True])
async def test_reconciliation_retains_release_without_repeating_receipt(
    responses, tmp_path, receipt_fails
):
    responses.respond(function_call("record", {"key": "undispatched"}, "cleanup-call"))
    code, rows, err = await _collect(await _start(responses, tmp_path, "intent-crash"))
    assert code == 68, (rows, err)
    identity = _row(rows, "opened")
    code, rows, err = await _collect(await _start(responses, tmp_path, "hold", identity))
    assert code != 0, (rows, err)
    [call] = _row(rows, "recovery_calls")

    class HeldReleaseStore(SQLiteSessionStore):
        blocked = True
        writes = 0
        releases = 0

        def commit(self, *args):
            self.writes += 1
            if receipt_fails:
                raise OSError("private-backend-receipt")
            return super().commit(*args)

        def release(self, *args):
            self.releases += 1
            if self.blocked:
                raise OSError("private-backend-release")
            return super().release(*args)

    backend = HeldReleaseStore(tmp_path / "canonical.sqlite")
    storage = CustomStorage(backend)
    with pytest.raises(RuntimeError) as caught:
        await storage.reconcile(
            identity, turn_id=call["turn_id"], call_id=call["call_id"], output="Verified no effect."
        )
    assert "private-backend" not in str(caught.value)
    if receipt_fails:
        assert "Reconciliation failed:" in str(caught.value)
        assert "writer cleanup failed:" in str(caught.value)
    cleanup = get_cleanup(caught.value)
    assert isinstance(cleanup, SessionStoreCleanup)
    writes = backend.writes
    with pytest.raises(RuntimeError) as repeated:
        await cleanup.aclose()
    assert get_cleanup(repeated.value) is cleanup
    competitor = SQLiteSessionStore(tmp_path / "canonical.sqlite")
    with pytest.raises(BlockingIOError):
        competitor.acquire(identity)
    backend.blocked = False
    await asyncio.gather(cleanup.aclose(), cleanup.aclose())
    await cleanup.aclose()
    assert backend.writes == writes
    assert backend.releases == 3
    token = competitor.acquire(identity)
    competitor.release(identity, token)
    if receipt_fails:
        await CustomStorage(competitor).reconcile(
            identity, turn_id=call["turn_id"], call_id=call["call_id"], output="Verified no effect."
        )
    responses.respond(assistant_output("continued without replay"))
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-cleanup", supports_websockets=False
    )
    async with (
        provider,
        Agent(instructions="Continue.", provider=provider).session(
            storage=CustomStorage(competitor), resume=identity
        ) as session,
    ):
        assert session.capabilities.get("native_output_manual_restore").status == "unsupported"
        result = await (await session.submit("Continue.")).get_result()
        assert result.final_output == "continued without replay"
    assert not (tmp_path / "effects").exists()


async def test_cancelled_reconciliation_and_cleanup_keep_the_original_writer(responses, tmp_path):
    responses.respond(function_call("record", {"key": "undispatched"}, "cancelled-cleanup"))
    code, rows, err = await _collect(await _start(responses, tmp_path, "intent-crash"))
    assert code == 68, (rows, err)
    identity = _row(rows, "opened")
    code, rows, err = await _collect(await _start(responses, tmp_path, "hold", identity))
    assert code != 0, (rows, err)
    [call] = _row(rows, "recovery_calls")
    entered = threading.Event()
    proceed = threading.Event()
    release_entered = threading.Event()
    release_proceed = threading.Event()

    class HeldStore(SQLiteSessionStore):
        blocked = True
        writes = 0
        releases = 0

        def acquire(self, session_id):
            token = super().acquire(session_id)
            entered.set()
            assert proceed.wait(15)
            return token

        def commit(self, *args):
            self.writes += 1
            return super().commit(*args)

        def release(self, *args):
            self.releases += 1
            if self.blocked:
                raise OSError("private-release-unavailable")
            release_entered.set()
            assert release_proceed.wait(15)
            return super().release(*args)

    backend = HeldStore(tmp_path / "canonical.sqlite")
    task = asyncio.create_task(
        CustomStorage(backend).reconcile(
            identity, turn_id=call["turn_id"], call_id=call["call_id"], output="Verified no effect."
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 15)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        proceed.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    cleanup = get_cleanup(caught.value)
    assert isinstance(cleanup, SessionStoreCleanup)
    assert backend.writes == 1
    backend.blocked = False
    closing = asyncio.create_task(cleanup.aclose())
    try:
        assert await asyncio.to_thread(release_entered.wait, 15)
        closing.cancel()
        another = asyncio.create_task(cleanup.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        assert not another.done()
    finally:
        release_proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await another
    await cleanup.aclose()
    assert backend.writes == 1
    assert backend.releases == 2
    competitor = SQLiteSessionStore(tmp_path / "canonical.sqlite")
    token = competitor.acquire(identity)
    competitor.release(identity, token)


async def test_custom_recovery_retains_facts_and_public_provider_cleanup(responses, tmp_path):
    responses.respond(function_call("record", {"key": "undispatched"}, "cleanup-intent"))
    code, rows, err = await _collect(await _start(responses, tmp_path, "intent-crash"))
    assert code == 68, (rows, err)
    identity = _row(rows, "opened")

    class HeldReleaseStore(SQLiteSessionStore):
        blocked = True

        def release(self, session_id: str, token: str) -> None:
            if self.blocked:
                raise OSError("fixture canonical release unavailable")
            super().release(session_id, token)

    store = HeldReleaseStore(tmp_path / "canonical.sqlite")
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-recovery", supports_websockets=False
    )
    agent = Agent(
        instructions="Record the requested effect once.", provider=provider, model="gpt-5.1-codex"
    )
    try:
        with pytest.raises(LocalRecoveryRequired) as caught:
            async with agent.session(storage=CustomStorage(store), resume=identity):
                pytest.fail("An unresolved intent must not publish a Session")
        call = caught.value.calls[0]
        assert call.thread_id == identity
        assert call.call_id == "cleanup-intent"
        assert call.arguments == {"key": "undispatched"}
        competitor = SQLiteSessionStore(tmp_path / "canonical.sqlite")
        with pytest.raises(BlockingIOError):
            competitor.acquire(identity)
    finally:
        store.blocked = False
        try:
            await provider.aclose()
        except RuntimeError:
            # Canonical failures remain observable even after physical retirement.
            pass
    token = competitor.acquire(identity)
    competitor.release(identity, token)
    assert caught.value.calls[0] == call
    assert len(responses.requests) == 1
    assert not (tmp_path / "effects").exists()


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
        await CustomStorage(SQLiteSessionStore(tmp_path / "canonical.sqlite")).reconcile(
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


@pytest.mark.parametrize(
    "unsupported,approval",
    [(NativeAgents, False), (NativeGoals, False), (None, True), (None, lambda *args: True)],
    ids=["children", "goals", "approval", "approval-callback"],
)
async def test_custom_unsupported_state_rejects_before_acquisition_or_effects(
    responses, tmp_path, unsupported, approval
):
    acquisitions = []
    effects = []

    class ObservedStore(SQLiteSessionStore):
        def acquire(self, thread_id):
            acquisitions.append(thread_id)
            return super().acquire(thread_id)

    @function_tool(needs_approval=approval)
    def record() -> str:
        """Record an application effect."""
        effects.append("called")
        return "called"

    backend = ObservedStore(tmp_path / "canonical.sqlite")
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-admission")
    agent = Agent(
        instructions="Admission must precede any execution.",
        model="gpt-5.1-codex",
        tools=[record] if unsupported is None else [unsupported(), record],
        provider=provider,
    )
    async with provider:
        with pytest.raises(ValueError, match="CustomStorage preview does not support"):
            async with agent.session(storage=CustomStorage(backend)):
                pytest.fail("Unsupported custom storage combination opened")
    assert acquisitions == []
    assert effects == []
    assert responses.requests == []


@pytest.mark.parametrize("name", ["project_glob", "project_grep"])
async def test_custom_storage_rejects_search_environment_before_acquisition(
    responses, tmp_path, name
):
    """CustomStorage does not support Environment, including search-only grants."""

    class UnopenedStore(SQLiteSessionStore):
        def acquire(self, thread_id):
            pytest.fail("An unsupported search environment acquired the store")

    backend = UnopenedStore(tmp_path / "canonical.sqlite")
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-admission")
    agent = Agent(instructions="Search the project.", provider=provider)
    async with provider:
        with pytest.raises(
            ValueError, match="CustomStorage preview does not support.*environments"
        ):
            async with agent.session(
                storage=CustomStorage(backend),
                environment=Environment.local(cwd=tmp_path, tools=[name]),
            ):
                pytest.fail("CustomStorage accepted a search environment")
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


@pytest.mark.parametrize("initial_failure", ["read", "commit"])
async def test_failed_initialization_retries_release_before_another_owner_opens(
    responses, tmp_path, initial_failure
):
    class FailedInitializationStore(SQLiteSessionStore):
        identity = None
        release_attempts = 0

        def acquire(self, thread_id):
            self.identity = thread_id
            return super().acquire(thread_id)

        def read(self, thread_id, token):
            if initial_failure == "read":
                raise OSError("injected initial read failure")
            return super().read(thread_id, token)

        def commit(self, thread_id, token, expected_revision, write_id, payload):
            raise OSError("injected initial commit failure")

        def release(self, thread_id, token):
            self.release_attempts += 1
            if self.release_attempts == 1:
                raise OSError("injected release failure before relinquishing ownership")
            return super().release(thread_id, token)

    store = FailedInitializationStore(tmp_path / "canonical.sqlite")
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-initialization")
    agent = Agent(instructions="Opening must fail before work starts.", provider=provider)
    async with timeout(15), provider:
        with pytest.raises(RuntimeError, match="Canonical storage failed"):
            async with agent.session(storage=CustomStorage(store)):
                pytest.fail("Failed canonical initialization unexpectedly succeeded")
        assert store.identity is not None
        # Keep the failed backend alive in this process: process death and
        # destruction of its lock descriptors cannot rescue the successor.
        successor = SQLiteSessionStore(tmp_path / "canonical.sqlite")
        token = successor.acquire(store.identity)
        try:
            assert successor.read(store.identity, token) is None
        finally:
            successor.release(store.identity, token)
        assert store.release_attempts >= 2
    assert responses.requests == []
    assert not (tmp_path / "effects").exists()


@pytest.mark.parametrize("kind", ["error", "image", "mixed"])
async def test_typed_reconciliation_preserves_normal_callback_receipt_and_model_input(
    responses, tmp_path, kind
):
    image = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    output = {
        "error": ToolOutput.error("externally confirmed failure"),
        "image": ToolOutput.image(image),
        "mixed": ToolOutput([ToolOutputText("verified image"), ToolOutputImage(image)]),
    }[kind]
    normal_effects = []

    @function_tool
    async def record(key: str) -> ToolOutput:
        """Return the authoritative typed application result."""
        normal_effects.append(key)
        return output

    normal = tmp_path / "ordinary"
    normal.mkdir()
    responses.respond(function_call("record", {"key": "once"}, "normal-call"))
    responses.respond(assistant_output("ordinary callback finished"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-typed-result")
    agent = Agent(
        instructions="Record the result once.",
        model="gpt-5.1-codex",
        tools=[record],
        provider=provider,
    )
    async with (
        timeout(30),
        provider,
        agent.session(
            storage=CustomStorage(SQLiteSessionStore(normal / "canonical.sqlite"))
        ) as session,
    ):
        assert (await (await session.submit("record once")).get_result()).final_output == (
            "ordinary callback finished"
        )
        normal_id = session.id
    assert normal_effects == ["once"]
    [normal_receipt] = [
        item
        for item in _responses(_snapshot(normal, normal_id))
        if item.get("type") == "function_call_output" and item["call_id"] == "normal-call"
    ]
    [normal_model_receipt] = [
        item
        for item in responses.requests[1]["input"]
        if item.get("type") == "function_call_output" and item["call_id"] == "normal-call"
    ]

    uncertain = tmp_path / "uncertain"
    uncertain.mkdir()
    responses.respond(function_call("record", {"key": "once"}, "uncertain-call"))
    code, rows, err = await _collect(await _start(responses, uncertain, "unknown"))
    assert code == 67, (rows, err)
    identity = _row(rows, "opened")
    assert _row(rows, "pid") != os.getpid()
    before = len(responses.requests)
    assert (uncertain / "effects").read_text() == "unknown:once\n"
    saved = _snapshot(uncertain, identity)
    [call] = [
        item
        for item in _responses(saved)
        if item.get("type") == "function_call" and item["call_id"] == "uncertain-call"
    ]
    assert not any(item.get("type") == "function_call_output" for item in _responses(saved))
    turn_id = call["internal_chat_message_metadata_passthrough"]["turn_id"]
    await CustomStorage(SQLiteSessionStore(uncertain / "canonical.sqlite")).reconcile(
        identity, turn_id=turn_id, call_id="uncertain-call", output=output
    )
    [reconciled] = [
        item
        for item in _responses(_snapshot(uncertain, identity))
        if item.get("type") == "function_call_output" and item["call_id"] == "uncertain-call"
    ]
    assert reconciled["output"] == normal_receipt["output"]
    assert reconciled["internal_chat_message_metadata_passthrough"]["turn_id"] == turn_id
    assert len(responses.requests) == before
    responses.respond(assistant_output("reconciled typed result"))
    code, resumed, err = await _collect(await _start(responses, uncertain, "destination", identity))
    assert code == 0 and _row(resumed, "output") == "reconciled typed result", (resumed, err)
    assert _row(resumed, "pid") != _row(rows, "pid")
    assert len(responses.requests) == before + 1
    [resumed_model_receipt] = [
        item
        for item in responses.requests[-1]["input"]
        if item.get("type") == "function_call_output" and item["call_id"] == "uncertain-call"
    ]
    assert resumed_model_receipt["output"] == normal_model_receipt["output"]
    assert (uncertain / "effects").read_text() == "unknown:once\n"
    assert normal_effects == ["once"]


if __name__ == "__main__":
    asyncio.run(_worker())


@pytest.mark.parametrize("fault", ["release-lock", "shutdown-flush"])
async def test_session_cleanup_retries_sqlite_release_without_replaying_flush(
    responses, tmp_path, fault
):
    import sqlite3

    class CloseFailureStore(SQLiteSessionStore):
        releases = 0
        flushes = 0
        armed = False

        def flush(self, session_id, token):
            self.flushes += 1
            if self.armed and fault == "shutdown-flush":
                self.armed = False
                raise OSError("injected shutdown flush failure")
            return super().flush(session_id, token)

        def release(self, session_id, token):
            self.releases += 1
            if self.releases != 1 or fault != "release-lock":
                return super().release(session_id, token)
            with sqlite3.connect(self.path) as blocker:
                blocker.execute("BEGIN EXCLUSIVE")
                try:
                    return super().release(session_id, token)
                finally:
                    blocker.rollback()

    store = CloseFailureStore(tmp_path / "canonical.sqlite")
    responses.respond(assistant_output("saved"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-release-retry")
    async with provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=CustomStorage(store)) as session:
            assert (await (await session.submit("Save.")).get_result()).final_output == "saved"
            store.armed = True
            with pytest.raises(RuntimeError, match="Canonical storage failed") as caught:
                await session.aclose()
            cleanup = get_cleanup(caught.value)
            assert cleanup is not None
            flushes = store.flushes
            await cleanup.aclose()
            await cleanup.aclose()
            await session.aclose()
            assert store.flushes == flushes
            assert store.releases == (2 if fault == "release-lock" else 1)
            token = store.acquire(session.id)
            try:
                assert store.read(session.id, token) is not None
            finally:
                store.release(session.id, token)
    # Leaving the provider scope proves engine and provider cleanup completed.
    assert len(responses.requests) == 1
