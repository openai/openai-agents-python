"""Run the supervised worker example in separate processes against synthetic provider peers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sys
from argparse import Namespace
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, Environment, LocalStorage, NativeProvider, Session
from examples.supervised_worker import __main__ as worker
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_mcp_tools import Peer, mcp_call

pytestmark = [
    pytest.mark.skipif(
        sys.platform not in ("linux", "darwin"), reason="Uses a native Unix executor"
    ),
    (
        pytest.mark.requires_native_linux_sandbox
        if sys.platform == "linux"
        else pytest.mark.requires_native_macos_sandbox
    ),
]
ARTIFACT_RESULT = '{"artifact":"summary.txt","check":"artifact checked"}'
EFFECT_RESULT = "The application recorded the handoff once."


@asynccontextmanager
async def second_provider() -> AsyncIterator[ResponsesFixture]:
    fixture = ResponsesFixture()
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    async with TestServer(app) as server:
        fixture.url = str(server.make_url("/v1"))
        yield fixture


@asynccontextmanager
async def process(
    command: str, directory: Path, url: str, *options: str
) -> AsyncIterator[asyncio.subprocess.Process]:
    repository = Path(__file__).resolve().parents[1]
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("OPENAI_", "AZURE_OPENAI_", "WORKER_"))
    }
    env.update(
        PYTHONPATH=os.pathsep.join(filter(None, (str(repository), env.get("PYTHONPATH")))),
        OPENAI_API_KEY="synthetic-supervised-worker",
        OPENAI_BASE_URL=url,
    )
    env.pop("WORKER_MODEL", None)
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "examples.supervised_worker",
        command,
        str(directory),
        *options,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        yield child
    finally:
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGKILL)
        await child.communicate()


async def result(child: asyncio.subprocess.Process) -> dict[str, Any]:
    out, err = await child.communicate()
    assert child.returncode == 0, (out.decode(), err.decode())
    return json.loads(out)


async def invoke(command: str, directory: Path, url: str, *options: str) -> dict[str, Any]:
    async with process(command, directory, url, *options) as child:
        return await result(child)


async def wait_for_request(ready: asyncio.Event, child: asyncio.subprocess.Process) -> None:
    request = asyncio.create_task(ready.wait())
    exited = asyncio.create_task(child.wait())
    try:
        await asyncio.wait((request, exited), return_when=asyncio.FIRST_COMPLETED)
        if not ready.is_set():
            out, err = await child.communicate()
            pytest.fail(f"Worker exited before its model request: {out.decode()} {err.decode()}")
    finally:
        request.cancel()
        exited.cancel()
        await asyncio.gather(request, exited, return_exceptions=True)


async def wait_for_file(path: Path, owner: asyncio.subprocess.Process) -> None:
    while not path.is_file():
        if owner.returncode is not None:
            _, err = await owner.communicate()
            pytest.fail(err.decode())
        await asyncio.sleep(0.01)


async def test_readme_shell_runs_both_workers_and_publishes(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    readme = (repository / "examples/supervised_worker/README.md").read_text()
    script = readme.split("```sh\n", 1)[1].split("```", 1)[0]
    # macOS mktemp can use its per-user directory instead of TMPDIR.
    # Read the path from the shell without changing the README commands or their exit status.
    script += (
        '\nreadme_status=$?\nprintf "%s" "$job_dir" > "$README_JOB_PATH"\nexit "$readme_status"\n'
    )
    fixture = ResponsesFixture()

    async def respond(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        history = json.dumps(body.get("input", []))
        if '"call_id": "readme-check"' in history:
            reply = assistant_output(ARTIFACT_RESULT)
        elif '"call_id": "readme-write"' in history:
            reply = function_call("exec_command", {"cmd": worker.VERIFY_COMMAND}, "readme-check")
        elif '"call_id": "readme-brief"' in history:
            expected = re.search(r"\b(alpha|bravo): verified", history)
            assert expected is not None
            reply = function_call(
                "exec_command",
                {"cmd": f"printf '{expected[1]}: verified\\n' > summary.txt"},
                "readme-write",
            )
        else:
            reply = function_call("brief", {}, "readme-brief")
        fixture.respond(reply)
        return await fixture.handle(request)

    app = web.Application()
    app.router.add_post("/v1/responses", respond)
    home, jobs = tmp_path / "home", tmp_path / "jobs"
    home.mkdir()
    jobs.mkdir()
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("OPENAI_", "AZURE_OPENAI_", "WORKER_")) and name != "PYTHONSAFEPATH"
    }
    env.update(
        HOME=str(home),
        TMPDIR=str(jobs),
        PATH=str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
        PYTHONPATH=os.pathsep.join(filter(None, (str(repository), env.get("PYTHONPATH")))),
        OPENAI_API_KEY="synthetic-supervised-worker",
        README_JOB_PATH=str(tmp_path / "job-path.txt"),
    )
    async with timeout(100), TestServer(app) as server:
        env["OPENAI_BASE_URL"] = str(server.make_url("/v1"))
        child = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            script,
            cwd=repository,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await child.communicate()
            assert child.returncode == 0, (out.decode(), err.decode())
        finally:
            if child.returncode is None:
                os.killpg(child.pid, signal.SIGKILL)
            await child.communicate()

    directory_text = (tmp_path / "job-path.txt").read_text()
    assert directory_text
    directory = Path(directory_text)
    assert directory.is_absolute() and directory.name.startswith("tmp.")
    assert (directory / "alpha" / "identity.json").is_file()
    assert (directory / "bravo" / "identity.json").is_file()
    try:
        for name in ("alpha", "bravo"):
            journal = directory / name
            text = f"{name}: verified\n"
            assert (journal / "workspace" / worker.ARTIFACT).read_text() == text
            accepted = worker.read_record(journal, "accepted")
            assert accepted is not None
            assert accepted["text"] == text
            assert accepted["sha256"] == hashlib.sha256(text.encode()).hexdigest()
            if name == "alpha":
                assert worker.read_record(journal, "published") == accepted
            else:
                assert worker.read_record(journal, "published") is None
        assert len(fixture.requests) == 8
    finally:
        shutil.rmtree(directory)


async def test_two_workers_revise_reconnect_recover_and_publish_once(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    alpha = tmp_path / "alpha"
    bravo = tmp_path / "work space +" / "bravo"
    method = tmp_path / "method.txt"
    method.write_text("Use the exact text supplied by the application.\n")
    method_options = ("--instruction-file", str(method))
    denied_peer = Peer(names=("lookup", "excluded"), annotations=None)
    read_peer = Peer(names=("lookup", "excluded"))
    alpha_ready, bravo_ready, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold_first(
        event: dict[str, Any], fixture: ResponsesFixture, ready: asyncio.Event
    ) -> None:
        if event["type"] == "response.created" and len(fixture.requests) == 1:
            ready.set()
            await release.wait()

    async with timeout(100), AsyncExitStack() as scope:
        other = await scope.enter_async_context(second_provider())
        alpha_mcp = await scope.enter_async_context(denied_peer.serve())
        bravo_mcp = await scope.enter_async_context(read_peer.serve())

        async def hold_alpha(event: dict[str, Any]) -> None:
            await hold_first(event, responses, alpha_ready)

        async def hold_bravo(event: dict[str, Any]) -> None:
            await hold_first(event, other, bravo_ready)

        responses.before_event = hold_alpha
        other.before_event = hold_bravo
        scope.callback(release.set)

        responses.respond(function_call("brief", {}, "same-brief"))
        responses.respond(mcp_call("lookup", "mcp__job_notes", "denied-lookup"))
        responses.respond(
            function_call(
                "exec_command",
                {"cmd": "printf 'alpha: verified\\n' > summary.txt; printf 'alpha: verified\\n'"},
                "wrote-file-but-faked-check",
            )
        )
        responses.respond(assistant_output(ARTIFACT_RESULT))
        responses.respond(assistant_output('{"artifact":"unexpected","check":"artifact checked"}'))
        responses.respond(
            function_call("exec_command", {"cmd": "cat -- summary.txt"}, "real-check")
        )
        responses.respond(assistant_output(ARTIFACT_RESULT))

        outside = alpha / "workspace" / "outside-bravo"
        other.respond(function_call("brief", {}, "same-brief"))
        other.respond(mcp_call("lookup", "mcp__job_notes", "read-lookup"))
        other.respond(
            function_call(
                "exec_command", {"cmd": f"printf outside > {shlex.quote(str(outside))}"}, "outside"
            )
        )
        other.respond(
            function_call(
                "exec_command", {"cmd": "printf 'bravo: verified\\n' > summary.txt"}, "write"
            )
        )
        other.respond(
            function_call(
                "exec_command",
                {"cmd": "cat -- summary.txt", "workdir": str(bravo.resolve() / "workspace")},
                "real-check",
            )
        )
        other.respond(assistant_output(ARTIFACT_RESULT))

        alpha_child = await scope.enter_async_context(
            process("work", alpha, responses.url, "--mcp-url", alpha_mcp, *method_options)
        )
        bravo_child = await scope.enter_async_context(
            process("work", bravo, other.url, "--mcp-url", bravo_mcp)
        )
        await asyncio.gather(
            wait_for_request(alpha_ready, alpha_child), wait_for_request(bravo_ready, bravo_child)
        )
        connected = await invoke("status", alpha, responses.url)
        assert connected["accepted"] is None
        assert connected["identity"] is not None
        assert alpha_child.pid != bravo_child.pid
        assert alpha_child.returncode is bravo_child.returncode is None
        # The status client has exited. Both independently owned model turns remain active.
        release.set()
        left, right = await asyncio.gather(result(alpha_child), result(bravo_child))

        assert left["session_id"] == connected["identity"]["session_id"] != right["session_id"]
        assert left["status"] == right["status"] == "accepted"
        assert left["rejections"] == [
            "The file or successful check is missing.",
            "The output did not match the required result schema.",
        ]
        assert right["rejections"] == []
        assert left["denied_approvals"] == 1 and denied_peer.calls == []
        assert right["denied_approvals"] == 0 and len(read_peer.calls) == 1
        assert not outside.exists()
        assert len(left["follow_up_ms"]) == 2 and not right["follow_up_ms"]

        for directory, row, fixture in [(alpha, left, responses), (bravo, right, other)]:
            artifact = (directory / "workspace" / "summary.txt").read_bytes()
            assert artifact == f"{directory.name}: verified\n".encode()
            assert row["sha256"] == hashlib.sha256(artifact).hexdigest()
            assert row["command_id"] in row["execution_ids"]
            assert row["startup_ms"] > 0 and row["first_tool_result_ms"] > 0
            assert row["completion_ms"] > 0 and all(ms > 0 for ms in row["follow_up_ms"])
            first, second = row["steering"]
            assert first["turn_id"] == second["turn_id"]
            assert first["input_id"] != second["input_id"]
            history = json.dumps(fixture.requests[1]["input"])
            assert "Use the exact text supplied by the brief tool." in history
            assert "verify it with this exact exec_command: cat -- summary.txt" in history
            assert tool_output(fixture.requests[1], "same-brief") == artifact.decode()
            advertised = json.dumps(fixture.requests[0])
            assert '"lookup"' in advertised
            assert '"excluded"' not in advertised and '"record_effect"' not in advertised
        assert left["steering"][0]["turn_id"] != right["steering"][0]["turn_id"]
        assert left["turn_receipt"]["user_config_applied"] is False
        assert left["turn_receipt"]["instruction_files"] == [
            {
                "path": str(method.resolve()),
                "source": "application_file",
                "sha256": hashlib.sha256(method.read_bytes()).hexdigest(),
            }
        ]
        assert "Use the exact text supplied by the application." in json.dumps(
            responses.requests[0]
        )

        reopened_client = await invoke("status", alpha, responses.url)
        assert reopened_client["identity"] == connected["identity"]
        assert reopened_client["accepted"]["sha256"] == left["sha256"]
        assert reopened_client["published"] is None

        responses.respond(assistant_output(EFFECT_RESULT))
        unverified = await invoke(
            "effect", alpha, responses.url, "--mcp-url", alpha_mcp, *method_options
        )
        assert unverified == {
            "status": "rejected",
            "stage": "turn",
            "reason": "The application journal did not confirm this turn's handoff.",
        }
        assert not (alpha / "effect.json").exists()

        other.respond(function_call("record_effect", {}, "bravo-handoff"))
        other.respond(assistant_output("This text is not the application result."))
        confirmed = await invoke("effect", bravo, other.url, "--mcp-url", bravo_mcp)
        assert confirmed == {
            "status": "completed",
            "session_id": right["session_id"],
            "result": EFFECT_RESULT,
        }
        prior_effect = (bravo / "effect.json").read_bytes()
        other.respond(assistant_output(EFFECT_RESULT))
        assert await invoke("effect", bravo, other.url, "--mcp-url", bravo_mcp) == unverified
        assert (bravo / "effect.json").read_bytes() == prior_effect

        responses.respond(function_call("record_effect", {}, "one-handoff"))
        async with process(
            "effect",
            alpha,
            responses.url,
            "--mcp-url",
            alpha_mcp,
            "--pause-after-effect",
            *method_options,
        ) as child:
            path = alpha / "effect.json"
            await wait_for_file(path, child)
            effect = path.read_bytes()
            assert json.loads(effect)["session_id"] == left["session_id"]
            os.killpg(child.pid, signal.SIGKILL)
            assert await child.wait() == -signal.SIGKILL

        request_count = len(responses.requests)
        refusal = await invoke(
            "effect", alpha, responses.url, "--mcp-url", alpha_mcp, *method_options
        )
        assert refusal == {"status": "recovery_required", "stage": "startup"}
        assert len(responses.requests) == request_count

        responses.respond(assistant_output("This text is not the application result."))
        recovered = await invoke(
            "recover", alpha, responses.url, "--mcp-url", alpha_mcp, *method_options
        )
        assert recovered == {
            "status": "completed",
            "session_id": left["session_id"],
            "result": EFFECT_RESULT,
        }
        assert tool_output(responses.requests[-1], "one-handoff") == EFFECT_RESULT
        assert not responses.requests[-1].get("tools")
        assert path.read_bytes() == effect
        assert len(responses.requests) == request_count + 1
        assert (await invoke("publish", alpha, responses.url))["status"] == "published"
        assert (await invoke("publish", alpha, responses.url))["status"] == "already_published"
        published = await invoke("status", alpha, responses.url)
        assert published["accepted"] == published["published"] == reopened_client["accepted"]


async def test_worker_joins_interrupt_and_result_timeout(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    async with timeout(45):
        responses.respond(function_call("brief", {}, "interrupt-brief"))
        interrupted = await invoke("interrupt", tmp_path / "interrupt", responses.url)
        assert interrupted == {"status": "interrupted", "stage": "turn"}
        assert len(responses.requests) == 1
        assert not (tmp_path / "interrupt" / "accepted.json").exists()

        began, release = asyncio.Event(), asyncio.Event()

        async def stall(event: dict[str, Any]) -> None:
            if event["type"] == "response.created":
                began.set()
                await release.wait()

        responses.before_event = stall
        responses.respond(assistant_output(ARTIFACT_RESULT))
        async with process(
            "work", tmp_path / "deadline", responses.url, "--turn-timeout", "3"
        ) as child:
            try:
                await wait_for_request(began, child)
                assert await result(child) == {"status": "timed_out", "stage": "turn"}
            finally:
                release.set()
        assert not (tmp_path / "deadline" / "accepted.json").exists()


@pytest.mark.parametrize("command", ("effect", "recover"))
async def test_handoff_deadline_includes_submission(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    directory = tmp_path / "deadline"
    async with timeout(45):
        # Seed completed history: interrupting an unfinished callback would require
        # reconciliation before this test can reach the handoff deadline.
        directory.mkdir(mode=0o700)
        workspace = worker.Worker(directory, pause_after_effect=False).workspace
        workspace.mkdir(mode=0o700)
        async with NativeProvider(
            api_key="synthetic-supervised-worker", base_url=responses.url, supports_websockets=False
        ) as provider:
            async with Agent(instructions="Record a handoff.", provider=provider).session(
                storage=LocalStorage(directory / "history"),
                environment=Environment.local(
                    cwd=str(workspace),
                    tools=("exec_command",),
                    writable_roots=(str(workspace),),
                    capability_directories=(),
                ),
            ) as session:
                responses.respond(assistant_output("Worker ready."))
                await (await session.submit("Initialize the worker.")).get_result()
                worker.record_once(directory, "identity", {"session_id": session.id})
        if command == "recover":
            responses.respond(function_call("record_effect", {}, "handoff"))
            responses.respond(assistant_output(EFFECT_RESULT))
            assert (await invoke("effect", directory, responses.url))["status"] == "completed"

        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-supervised-worker")
        monkeypatch.setenv("OPENAI_BASE_URL", responses.url)
        monkeypatch.delenv("WORKER_MODEL", raising=False)
        original_submit = Session.submit

        async def submit(session: Session[Any, Any], *args: Any, **kwargs: Any) -> Any:
            turn = await original_submit(session, *args, **kwargs)
            # Simulate an acknowledgement that consumes the worker's turn budget.
            deadline = worker.monotonic() + 10
            monkeypatch.setattr(worker, "monotonic", lambda: deadline)
            return turn

        monkeypatch.setattr(Session, "submit", submit)
        responses.respond(assistant_output("The model reports a handoff."))
        outcome = await worker.run_worker(
            Namespace(
                command=command,
                directory=directory,
                instruction_file=[],
                mcp_url=None,
                pause_after_effect=False,
                startup_timeout=25,
                turn_timeout=10,
            )
        )
        assert outcome == {"status": "timed_out", "stage": "turn"}
