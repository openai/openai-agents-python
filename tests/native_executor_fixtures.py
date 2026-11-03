"""TLS and authentication fixtures in front of the real native exec server."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import ClientConnectorError, ClientSession, ClientTimeout, WSMsgType, web
from aiohttp.test_utils import TestServer, unused_port

import agents
from agents.history import RecordedTurn
from tests._asyncio import timeout


def assert_portable_executor_turns(restored: list[RecordedTurn], live: list[RecordedTurn]) -> None:
    """Compare turns that sampled the fixture executor before a portable restore."""
    assert restored and len(restored) == len(live)
    for actual, original in zip(restored, live, strict=True):
        live_receipt = original.get("receipt")
        restored_receipt = actual.get("receipt")
        assert live_receipt is not None and live_receipt["executors"]
        assert restored_receipt is not None and restored_receipt["executors"] is None
        assert actual == {**original, "receipt": {**live_receipt, "executors": None}}


async def _openssl(*arguments: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "openssl", *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, error = await process.communicate()
    assert process.returncode == 0, error.decode()


async def test_certificate(
    root: Path, names: str, *, issuer_name: str = "Native executor fixture CA"
) -> tuple[Path, ssl.SSLContext]:
    ca, key = root / "ca.pem", root / "ca.key"
    cert, server_key, csr = root / "server.pem", root / "server.key", root / "server.csr"
    await _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(ca),
        "-days",
        "1",
        "-subj",
        f"/CN={issuer_name}",
    )
    await _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(server_key),
        "-out",
        str(csr),
        "-subj",
        "/CN=localhost",
    )
    extensions = root / "extensions.cnf"
    extensions.write_text(f"subjectAltName={names}\nextendedKeyUsage=serverAuth\n")
    await _openssl(
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca),
        "-CAkey",
        str(key),
        "-CAcreateserial",
        "-out",
        str(cert),
        "-days",
        "1",
        "-extfile",
        str(extensions),
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, server_key)
    return ca, context


@dataclass
class ExecutorPeer:
    root: Path
    token: str = field(default="fixture-executor-secret", repr=False)
    auth_status: int = 401
    certificate_names: str = "DNS:localhost,IP:127.0.0.1"
    requests: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    transports: list[Any] = field(default_factory=list)
    url: str = ""
    ca: Path | None = None
    deny_terminate: bool = False
    drop_terminate_reply: bool = False
    drop_start_reply_after: Path | None = None
    start_reply_dropped: asyncio.Event = field(default_factory=asyncio.Event)
    synthetic_process_output: str | None = field(default=None, repr=False)
    synthetic_process_starts: list[str] = field(default_factory=list)

    async def wait_disconnected(self) -> None:
        assert self.transports, "Executor transport was never opened"
        async with timeout(5):
            while any(not transport.is_closing() for transport in self.transports):
                await asyncio.sleep(0.001)

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[ExecutorPeer]:
        binary = Path(os.environ["CODEX_EXEC_SERVER_TEST_BINARY"])
        assert binary.is_file()
        self.root.mkdir()
        config = self.root / ".fixture-config"
        config.mkdir()
        ca, context = await test_certificate(config, self.certificate_names)
        self.ca = ca
        port = unused_port()
        address = f"http://127.0.0.1:{port}"
        # This fixture is the remote host and must find its own sandbox helper.
        helper_path = Path(agents.__file__).resolve().parent / "bin"
        environment = {
            **os.environ,
            "CODEX_HOME": str(config),
            "PATH": str(helper_path) + os.pathsep + os.environ.get("PATH", ""),
        }
        with (config / "exec-server.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                str(binary),
                address.replace("http:", "ws:"),
                cwd=self.root,
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                async with (
                    timeout(15) as startup,
                    ClientSession(timeout=ClientTimeout(total=2)) as client,
                ):
                    while True:
                        assert process.returncode is None, (
                            "Native exec server exited during startup"
                        )
                        try:
                            async with client.get(address + "/readyz") as response:
                                assert response.status == 200
                                break
                        except (ClientConnectorError, asyncio.TimeoutError):
                            # aiohttp can translate an expired outer deadline on Python 3.10.
                            startup_expired = (
                                startup.expired() if callable(startup.expired) else startup.expired
                            )
                            if startup_expired:
                                raise
                            await asyncio.sleep(0.02)

                async def proxy(request: web.Request) -> web.WebSocketResponse:
                    self.attempts += 1
                    self.transports.append(request.transport)
                    if request.headers.get("Authorization") != "Bearer " + self.token:
                        if self.auth_status == 403:
                            raise web.HTTPForbidden()
                        raise web.HTTPUnauthorized()
                    downstream = web.WebSocketResponse()
                    await downstream.prepare(request)
                    async with ClientSession() as client:
                        async with client.ws_connect(address.replace("http:", "ws:")) as upstream:
                            methods: dict[str | int, str] = {}
                            synthetic_processes: set[str] = set()

                            async def forward(source: Any, destination: Any, record: bool) -> None:
                                async for message in source:
                                    if message.type == WSMsgType.TEXT:
                                        value = json.loads(message.data)
                                        if record:
                                            self.requests.append(value)
                                            if "id" in value and "method" in value:
                                                methods[value["id"]] = value["method"]
                                            if self.synthetic_process_output is not None:
                                                method = value.get("method")
                                                params = value.get("params", {})
                                                process_id = params.get("processId")
                                                if method == "process/start":
                                                    assert (
                                                        isinstance(process_id, str) and process_id
                                                    )
                                                    assert process_id not in synthetic_processes
                                                    synthetic_processes.add(process_id)
                                                    self.synthetic_process_starts.append(process_id)
                                                    await downstream.send_json(
                                                        {
                                                            "id": value["id"],
                                                            "result": {
                                                                "processId": process_id,
                                                                "sandboxType": "none",
                                                            },
                                                        }
                                                    )
                                                    chunk = base64.b64encode(
                                                        self.synthetic_process_output.encode()
                                                    ).decode()
                                                    for notification, payload in (
                                                        (
                                                            "process/output",
                                                            {
                                                                "processId": process_id,
                                                                "seq": 1,
                                                                "stream": "stdout",
                                                                "chunk": chunk,
                                                            },
                                                        ),
                                                        (
                                                            "process/exited",
                                                            {
                                                                "processId": process_id,
                                                                "seq": 2,
                                                                "exitCode": 0,
                                                            },
                                                        ),
                                                        (
                                                            "process/closed",
                                                            {"processId": process_id, "seq": 3},
                                                        ),
                                                    ):
                                                        await downstream.send_json(
                                                            {
                                                                "method": notification,
                                                                "params": payload,
                                                            }
                                                        )
                                                    # Never forward a synthetic process/start.
                                                    continue
                                                if process_id in synthetic_processes:
                                                    if method == "process/read":
                                                        chunk = base64.b64encode(
                                                            self.synthetic_process_output.encode()
                                                        ).decode()
                                                        chunks = (
                                                            [
                                                                {
                                                                    "seq": 1,
                                                                    "stream": "stdout",
                                                                    "chunk": chunk,
                                                                }
                                                            ]
                                                            if (params.get("afterSeq") or 0) < 1
                                                            else []
                                                        )
                                                        reply = {
                                                            "chunks": chunks,
                                                            "nextSeq": 3,
                                                            "exited": True,
                                                            "exitCode": 0,
                                                            "closed": True,
                                                            "failure": None,
                                                            "sandboxDenied": False,
                                                        }
                                                    elif method == "process/terminate":
                                                        reply = {"running": False}
                                                    else:
                                                        raise AssertionError(
                                                            "Unexpected synthetic process operation"
                                                        )
                                                    await downstream.send_json(
                                                        {"id": value["id"], "result": reply}
                                                    )
                                                    continue
                                            if (
                                                self.drop_terminate_reply
                                                and value.get("method") == "process/terminate"
                                            ):
                                                await downstream.close()
                                                return
                                            if (
                                                self.deny_terminate
                                                and value.get("method") == "process/terminate"
                                            ):
                                                await downstream.send_json(
                                                    {
                                                        "id": value["id"],
                                                        "error": {
                                                            "code": -32000,
                                                            "message": "private-termination-error",
                                                        },
                                                    }
                                                )
                                                continue
                                        elif (
                                            methods.get(value.get("id")) == "process/start"
                                            and self.drop_start_reply_after is not None
                                        ):
                                            async with timeout(5):
                                                while not self.drop_start_reply_after.exists():
                                                    await asyncio.sleep(0.01)
                                            self.drop_start_reply_after = None
                                            self.start_reply_dropped.set()
                                            await downstream.close()
                                            return
                                        await destination.send_str(message.data)
                                    elif message.type == WSMsgType.BINARY:
                                        await destination.send_bytes(message.data)
                                    else:
                                        break

                            tasks = [
                                asyncio.create_task(forward(downstream, upstream, True)),
                                asyncio.create_task(forward(upstream, downstream, False)),
                            ]
                            try:
                                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            finally:
                                for task in tasks:
                                    task.cancel()
                                await asyncio.gather(*tasks, return_exceptions=True)
                    await downstream.close()
                    return downstream

                app = web.Application()
                app.router.add_get("/executor", proxy)
                server = TestServer(app)
                await server.start_server(ssl=context)
                try:
                    self.url = str(server.make_url("/executor")).replace("https:", "wss:")
                    yield self
                finally:
                    await server.close()
            finally:
                if process.returncode is None:
                    process.terminate()
                async with timeout(5):
                    await process.wait()
