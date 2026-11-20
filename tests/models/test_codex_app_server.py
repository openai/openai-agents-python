import asyncio
import sys

import pytest

from agents.models._codex_app_server import CodexAppServer


@pytest.mark.asyncio
async def test_stock_jsonl_multiplexing_and_redacted_error():
    # A real subprocess preserves pipe framing, asynchronous events, and process cleanup.
    script = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "notify":
        continue
    if message["method"] == "bad":
        result = {"id": message["id"], "error": {"message": "secret provider payload"}}
        print(json.dumps(result), flush=True)
    else:
        print(json.dumps({"method": "progress", "params": {"ok": True}}), flush=True)
        print(json.dumps({"id": message["id"], "result": {"ok": True}}), flush=True)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    server = CodexAppServer(process)
    try:
        await server.notify("notify")
        assert await server.request("good", {}) == {"ok": True}
        assert await anext(server.events()) == {"method": "progress", "params": {"ok": True}}
        with pytest.raises(RuntimeError, match="request failed") as caught:
            await server.request("bad", {})
        assert "secret" not in str(caught.value)
        assert caught.value.__context__ is None
    finally:
        await server.close()
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_cancellation_during_pending_request_reaps_process():
    script = "import sys; sys.stdin.readline(); sys.stdin.read()"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    server = CodexAppServer(process)
    task = asyncio.create_task(server.request("wait", {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await server.close()
    assert process.returncode is not None
    assert not server._pending


@pytest.mark.asyncio
async def test_malformed_protocol_does_not_retain_payload():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        'print("secret malformed json", flush=True)',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    server = CodexAppServer(process)
    try:
        with pytest.raises(RuntimeError, match="Invalid Codex") as caught:
            await anext(server.events())
        assert caught.value.__context__ is None
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["once", "always", "protocol"])
async def test_initialization_retries_only_child_exit_and_reaps_failed_attempt(
    tmp_path,
    monkeypatch,
    failure,
):
    import os

    from agents.models._codex_app_server import _CodexProcessExitedError

    monkeypatch.setattr("agents.models._codex_app_server._INITIALIZE_RETRY_DELAYS", (0, 0, 0))

    script = tmp_path / "codex-app-server.py"
    script.write_text(
        r"""
import json, os, pathlib, sys
attempt_file = pathlib.Path(os.environ["ATTEMPT_FILE"])
try:
    attempt_file.open("x").close()
    first = True
except FileExistsError:
    first = False
for line in sys.stdin:
    message = json.loads(line)
    mode = os.environ["FAILURE_MODE"]
    if mode == "protocol":
        print("malformed", flush=True)
        continue
    if message.get("method") == "initialize" and (mode == "always" or first):
        sys.exit(1)
    if "id" in message:
        print(json.dumps({"id": message["id"], "result": {"ok": True}}), flush=True)
"""
    )
    create_subprocess_exec = asyncio.create_subprocess_exec

    async def launch_script(*args, **kwargs):
        # Run the same real protocol child through Python on every host.
        if args[0] == str(script):
            args = (sys.executable, "-u", *args)
        return await create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch_script)
    opened = []
    original = CodexAppServer.open

    async def open_server(*args):
        server = await original(*args)
        opened.append(server)
        return server

    monkeypatch.setattr(CodexAppServer, "open", open_server)
    environment = {
        **os.environ,
        "ATTEMPT_FILE": str(tmp_path / "attempt"),
        "FAILURE_MODE": failure,
    }
    if failure == "once":
        server = await CodexAppServer.open_initialized(str(script), str(tmp_path), [], environment)
        assert len(opened) == 2
        assert opened[0]._process.returncode is not None
        assert await server.request("thread/start", {}) == {"ok": True}
        await server.close()
    else:
        error = _CodexProcessExitedError if failure == "always" else RuntimeError
        with pytest.raises(error):
            await CodexAppServer.open_initialized(str(script), str(tmp_path), [], environment)
        assert len(opened) == (4 if failure == "always" else 1)
    assert all(server._process.returncode is not None for server in opened)


@pytest.mark.asyncio
async def test_initialization_cancellation_closes_attempt_without_retry(monkeypatch):
    entered = asyncio.Event()
    closed = []

    class PendingServer:
        async def request(self, method, params):
            assert method == "initialize"
            entered.set()
            await asyncio.Future()

        async def close(self):
            closed.append(self)

    opened = []

    async def open_server(*args):
        server = PendingServer()
        opened.append(server)
        return server

    monkeypatch.setattr(CodexAppServer, "open", open_server)
    task = asyncio.create_task(CodexAppServer.open_initialized("codex", ".", [], None))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert opened == closed
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_cancellation_during_initialize_backoff_never_opens_another_attempt(monkeypatch):
    from agents.models._codex_app_server import _CodexProcessExitedError

    closed = asyncio.Event()
    opened = []

    class ExitedServer:
        async def request(self, method, params):
            raise _CodexProcessExitedError("startup exit")

        async def close(self):
            closed.set()

    async def open_server(*args):
        server = ExitedServer()
        opened.append(server)
        return server

    monkeypatch.setattr(CodexAppServer, "open", open_server)
    monkeypatch.setattr("agents.models._codex_app_server._INITIALIZE_RETRY_DELAYS", (60,))
    task = asyncio.create_task(CodexAppServer.open_initialized("codex", ".", [], None))
    await closed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(opened) == 1
