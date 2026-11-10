"""Qualify the default Windows command path on an installed x64 wheel."""

import asyncio
import csv
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import replace
from multiprocessing.connection import Client
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def serve(address):
    import _winapi
    import ctypes
    from ctypes import wintypes
    from multiprocessing.connection import PipeConnection

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", wintypes.LPVOID),
            ("inherit", wintypes.BOOL),
        ]

    # Only the current logon can exchange data. Owner Rights suppresses implicit WRITE_DAC.
    identity = subprocess.check_output(["whoami.exe", "/logonid"], encoding="oem")
    (logon_sid,) = re.findall(r"\bS-1-5-5-[0-9]+-[0-9]+\b", identity)
    sddl = f"D:P(A;;GRGW;;;{logon_sid})(A;;RC;;;OW)"
    descriptor = wintypes.LPVOID()
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        ctypes.c_wchar_p(sddl), 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        reject_remote_clients = 0x8  # PIPE_REJECT_REMOTE_CLIENTS
        handle = _winapi.CreateNamedPipe(
            address,
            _winapi.PIPE_ACCESS_DUPLEX
            | _winapi.FILE_FLAG_OVERLAPPED
            | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE,
            _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | reject_remote_clients,
            1,
            512,
            512,
            0,
            ctypes.addressof(attrs),
        )
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)
    with PipeConnection(handle) as peer:
        connected = _winapi.ConnectNamedPipe(handle, overlapped=True)
        print("ready", flush=True)
        _, error = connected.GetOverlappedResult(True)
        assert error == 0, error
        peer.send_bytes(hashlib.sha256(peer.recv_bytes(32)).digest())
        assert peer.poll(5) and peer.recv_bytes(1) == b"\x01"


def exchange(nonce):
    address = rf"\\.\pipe\agents-{uuid4().hex}"
    peer = subprocess.Popen(
        [sys.executable, "-I", __file__, "serve", address],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    try:
        assert peer.stdout and peer.stdout.readline().strip() == b"ready"
        with Client(address, family="AF_PIPE") as channel:
            challenge = bytes.fromhex(nonce)
            channel.send_bytes(challenge)
            assert channel.poll(5) and channel.recv_bytes(32) == hashlib.sha256(challenge).digest()
            channel.send_bytes(b"\x01")
        assert peer.wait(timeout=5) == 0
        print(json.dumps({"named_pipe": nonce, "client": os.getpid(), "server": peer.pid}))
    finally:
        try:
            _, stderr = peer.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            print("pipe server remained active after the client finished", flush=True)
            peer.kill()
            _, stderr = peer.communicate()
        if stderr:
            print(
                f"pipe server stderr (exit {peer.returncode}):",
                stderr.decode(errors="replace"),
                flush=True,
            )


def restricted_job(nonce):
    import ctypes
    from ctypes.wintypes import HANDLE

    token = HANDLE()
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    if not security.OpenProcessToken(HANDLE(-1), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        assert security.IsTokenRestricted(token), "expected a restricted process token"
    finally:
        ctypes.windll.kernel32.CloseHandle(token)
    print("restricted process token", flush=True)
    workspace = Path(__file__).parent
    (workspace / "ordinary").write_text(nonce)
    for path in (workspace / ".git" / "protected", workspace.parent / "protected"):
        try:
            path.write_text(nonce)
        except PermissionError as exc:
            assert exc.errno == errno.EACCES, exc
        else:
            raise AssertionError(f"unexpected write to {path}")
    print("protected writes denied", flush=True)
    exchange(nonce)


async def public_command(url, token, workspace, command, requests):
    import pytest
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from tests._asyncio import timeout
    from tests.native_fixtures import ResponsesFixture, assistant_output, function_call, tool_output

    from agents import (
        Agent,
        ApprovalRequest,
        Environment,
        NativeProvider,
        NetworkPolicy,
        PendingRequests,
        _native,
    )

    binary = Path(_native.__file__).resolve()
    assert binary.is_relative_to(Path(sys.prefix).resolve()), "expected an installed wheel"
    next_event = _native.NativeThread._next_session_event
    approvals = []

    async def observe_event(thread):
        raw = await next_event(thread)
        message = (json.loads(raw) or {}).get("msg", {})
        if message.get("type") == "exec_approval_request":
            approvals.append(message["call_id"])
        if message.get("type") in {
            "exec_approval_request",
            "apply_patch_approval_request",
            "elicitation_request",
            "request_user_input",
            "request_permissions",
            "error",
        }:
            fields = (
                "type",
                "kind",
                "call_id",
                "reason",
                "message",
                "codex_error_info",
                "command",
                "cwd",
                "additional_permissions",
                "permissions",
            )
            print(
                "native event",
                json.dumps({k: message[k] for k in fields if k in message}),
                flush=True,
            )
        return raw

    replies = ResponsesFixture()
    app = web.Application()
    app.router.add_post("/v1/responses", replies.handle)
    async with TestServer(app) as server:
        provider = NativeProvider(base_url=str(server.make_url("/v1")), api_key="fixture")
        try:
            selected = Environment(
                url=url,
                write_policy="legacy",
                cwd=str(workspace),
                tools=["exec_command", "write_stdin"],
                headers={"Authorization": "Bearer " + token},
            )
            agent = Agent(instructions="Use the job.", provider=provider)
            for policy in (
                NetworkPolicy(),
                NetworkPolicy(allowed_domains=["fixture.invalid"]),
                NetworkPolicy(allow_local_binding=True),
                NetworkPolicy(allow_all_unix_sockets=True),
            ):
                with pytest.raises(
                    RuntimeError, match="Linux or macOS executor with managed proxy launch"
                ):
                    async with agent.session(environment=replace(selected, network=policy)):
                        pytest.fail("Windows restricted-token executor admitted a network policy")
            assert not any(r.get("method") == "process/start" for r in requests)
            # Observe the native request as well as the public Turn's pending approval.
            with patch.object(_native.NativeThread, "_next_session_event", observe_event):
                async with agent.session(environment=selected) as session:
                    output_chunks = []
                    process_id = None
                    name = "exec_command"
                    arguments = {"cmd": command, "yield_time_ms": 30000}
                    # A running reply has been leaving the job active in this Session.
                    # Polling has been awaiting exit without starting another process.
                    async with timeout(60):
                        while True:
                            call = uuid4().hex
                            replies.respond(function_call(name, arguments, call))
                            replies.respond(assistant_output("complete"))
                            result = await (
                                await session.submit("Run or wait for the same job command.")
                            ).get_result()
                            assert result.final_output == "complete"
                            chunk = tool_output(replies.requests[-1], call)
                            print(chunk, flush=True)
                            output_chunks.append(chunk)
                            running = re.search(
                                r"^Process running with session ID ([0-9]+)$", chunk, re.MULTILINE
                            )
                            if running is None:
                                assert re.search(
                                    r"^Process exited with code 0$", chunk, re.MULTILINE
                                ), chunk
                                break
                            yielded_id = int(running[1])
                            assert process_id is None or process_id == yielded_id, chunk
                            process_id = yielded_id
                            name = "write_stdin"
                            arguments = {
                                "session_id": process_id,
                                "chars": "",
                                "yield_time_ms": 5000,
                            }
                    output = "\n".join(output_chunks)
                    blocked = uuid4().hex
                    replies.respond(
                        function_call(
                            "exec_command",
                            {"cmd": "Remove-Item -LiteralPath ordinary -Force -WhatIf"},
                            blocked,
                        )
                    )
                    replies.respond(assistant_output("approval denied"))
                    turn = await session.submit("Check the command approval.")
                    pending = await turn.wait_for_request_or_result()
                    assert isinstance(pending, PendingRequests), pending
                    (question,) = pending.requests
                    assert isinstance(question, ApprovalRequest), question
                    assert question.submission_id == turn.submission_id
                    assert question.turn_id == turn.turn_id
                    assert approvals == [blocked], approvals
                    assert len([r for r in requests if r.get("method") == "process/start"]) == 1
                    await question.respond("deny")
                    assert (await turn.get_result()).final_output == "approval denied"
            (start,) = [r["params"] for r in requests if r.get("method") == "process/start"]
            sandbox = start["sandbox"]
            assert sandbox["windowsSandboxLevel"] == "restricted-token", sandbox
            assert sandbox["permissions"]["type"] == "managed", sandbox
            assert sandbox["permissions"]["network"] == "restricted", sandbox
            assert not start.get("enforceManagedNetwork") and start.get("networkProxy") is None
            return output
        finally:
            starts = []
            for request in requests:
                if request.get("method") == "process/start":
                    params = request["params"]
                    starts.append({k: params.get(k) for k in ("argv", "cwd", "sandbox")})
            print("process/start requests", json.dumps(starts), flush=True)
            await provider.aclose()


def powershell_command(*args):
    return "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in args)


async def baseline(fixture, root):
    from tests.native_executor_fixtures import ExecutorPeer

    workspace = root / "job"
    workspace.mkdir()
    # TemporaryDirectory's protected ACL has no explicit grant for the executor account.
    identity = subprocess.check_output(["whoami.exe", "/user", "/fo", "csv", "/nh"], encoding="oem")
    ((_, user_sid),) = csv.reader(identity.splitlines())
    subprocess.run(["icacls.exe", workspace, "/grant", f"*{user_sid}:(OI)(CI)M"], check=True)
    (workspace / ".git").mkdir()
    protected = (workspace / ".git" / "protected", root / "protected")
    for path in protected:
        path.write_text("control")
    script = workspace / "windows_ipc.py"
    shutil.copyfile(__file__, script)
    python = sys._base_executable
    control = uuid4().hex
    try:
        positive = subprocess.run(
            [python, "-I", str(script), "exchange", control],
            capture_output=True,
            text=True,
            timeout=10,
        )
        host_ok = positive.returncode == 0 and f'"named_pipe": "{control}"' in positive.stdout
        print("host pipe", positive.stdout, positive.stderr, flush=True)
    except subprocess.TimeoutExpired:
        host_ok = False
        print("host pipe timed out", flush=True)
    nonce = uuid4().hex
    command = powershell_command(python, "-I", script, "job", nonce)
    os.environ["CODEX_EXEC_SERVER_TEST_BINARY"] = str(fixture)
    peer = ExecutorPeer(root / "executor")
    try:
        async with peer.serve():
            os.environ["SSL_CERT_FILE"] = str(peer.ca)
            output = await public_command(peer.url, peer.token, workspace, command, peer.requests)
        assert f'"named_pipe": "{nonce}"' in output, "missing restricted job reply"
        assert "restricted process token" in output and "protected writes denied" in output, output
        assert (workspace / "ordinary").read_text() == nonce
        assert all(path.read_text() == "control" for path in protected)
        assert host_ok, "host pipe positive control failed"
    except BaseException:
        log = peer.root / ".fixture-config" / "exec-server.log"
        if log.exists():
            print(log.read_text(errors="replace")[-4000:], file=sys.stderr)
        raise


if __name__ == "__main__":
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("requires native Windows x64")
    if struct.calcsize("P") != 8 or not __debug__:
        raise SystemExit("requires 64-bit Python with assertions enabled")
    if sys.argv[1] == "serve":
        serve(sys.argv[2])
    elif sys.argv[1] == "exchange":
        exchange(sys.argv[2])
    elif sys.argv[1] == "job":
        restricted_job(sys.argv[2])
    else:
        with tempfile.TemporaryDirectory(prefix="win-ipc-") as directory:
            root = Path(directory).resolve(strict=True)
            asyncio.run(
                asyncio.wait_for(baseline(Path(sys.argv[1]).resolve(strict=True), root), 90)
            )
