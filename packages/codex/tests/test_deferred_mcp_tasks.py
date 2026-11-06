"""Exercise the optional MCP Tasks client against a separate loopback MCP server."""

from __future__ import annotations

import asyncio
import base64
import gc
import hashlib
import json
import logging
import socket
import sqlite3
import threading
import time
import traceback
import urllib.request
import weakref
from collections import Counter
from contextlib import asynccontextmanager, closing
from contextvars import Context, ContextVar
from dataclasses import astuple, replace
from importlib.metadata import version
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from agents import Agent, FunctionTool, Turn
from agents.experimental.deferred import DeferredTools, JobBinding, JobClaim
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_deferred_jobs import Journal, declared
from tests.test_native_manual_tools import next_request, provider
from tests.test_sdk_session_tls import tls_files as tls_files

# Only the explicit MCP test suite needs this optional client and peer.
pytest.importorskip("mcp")
if version("mcp") != "2.0.0" or version("mcp-types") != "2.0.0":
    pytest.skip("Requires the qualified MCP 2.0.0 packages", allow_module_level=True)
import httpx2
import uvicorn
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolResult,
    ElicitResult,
    ListToolsResult,
    Notification,
    RequestParams,
    TextContent,
    Tool,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from agents.experimental.mcp_tasks import MCPInputReply, MCPTasksProvider

EXT = "io.modelcontextprotocol/tasks"
TASK = "private job /é/WIRE"
TOKEN = "synthetic-mcp-private-token"
READ_TOKEN = "synthetic-read-only-token"
PROMPT = "synthetic-private-approval-prompt"
HEADER = "=?base64?" + base64.b64encode(TASK.encode()).decode() + "?="
STAMP = "2026-10-27T00:00:00Z"


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pages: int


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pages: int
    is_error: bool


class PeerParams(RequestParams):
    task_id: str


class PeerUpdate(PeerParams):
    input_responses: dict[str, ElicitResult]


def _forms(keys, prompt, field):
    return {
        key: {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": prompt,
                "requestedSchema": {
                    "type": "object",
                    "properties": {"color": field},
                    "required": ["color"],
                },
            },
        }
        for key in keys
    }


class Peer:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.counts: Counter[str] = Counter()
        self.headers: list[tuple[str, str | None, bool]] = []
        self.creates = 0
        self.authorizations = {"Bearer " + TOKEN}
        self.writes: list[dict[str, Any]] = []
        self.mode = "deferred"
        self.status = "completed"
        self.interval = 0
        self.prompt = PROMPT
        self.input_key = "accept-1"
        self.extra_input_keys: tuple[str, ...] = ()
        self.form_field: dict[str, Any] = {"type": "string", "enum": ["blue"]}
        self.satisfied: set[str] = set()
        self.drop = ""
        self.redirect: str | None = None
        self.server = Server(
            "local-fixture", on_call_tool=self.start, on_list_tools=self.list_tools
        )
        self.server.extensions[EXT] = {}
        self.server.add_request_handler("tasks/get", PeerParams, self.get)
        self.server.add_request_handler("tasks/update", PeerUpdate, self.update)
        self.server.add_request_handler("tasks/cancel", PeerParams, self.cancel)
        self.app = self.server.streamable_http_app()
        self.app.add_middleware(BaseHTTPMiddleware, dispatch=self.audit)

    async def audit(self, request, call_next):
        payload = await request.json() if request.method == "POST" else {}
        method = payload.get("method", request.method)
        params = payload.get("params") or {}
        self.counts[method] += 1
        modern = request.headers.get("mcp-protocol-version") == "2026-07-28"
        self.headers.append((method, request.headers.get("mcp-name"), modern))
        if (
            request.headers.get("authorization") not in self.authorizations
            or self.mode == "revoked"
        ):
            return JSONResponse({"error": "denied"}, status_code=403)
        if self.mode == "legacy" and method == "server/discover":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {"code": -32601, "message": "unsupported"},
                }
            )
        if method in ("tools/call", "tasks/get", "tasks/update", "tasks/cancel"):
            assert modern and request.headers["mcp-method"] == method
            assert params["_meta"]["io.modelcontextprotocol/clientCapabilities"]["extensions"] == {
                EXT: {}
            }
            assert request.headers["mcp-name"] == ("render" if method == "tools/call" else HEADER)
        if method == "tools/call" and self.redirect:
            return RedirectResponse(self.redirect, status_code=307)
        reply = await call_next(request)
        if method == self.drop:
            return JSONResponse({"error": "synthetic lost reply"}, status_code=503)
        return reply

    async def list_tools(self, ctx, params):
        remote = Tool(
            name="render",
            input_schema={"type": "object", "properties": {"pages": {"type": "integer"}}},
        )
        if self.mode == "external-schema":
            remote.output_schema = {"$ref": "https://not-contacted.invalid/schema"}
        return ListToolsResult(tools=[remote])

    def result(self):
        return CallToolResult(
            content=[TextContent(type="text", text=(TASK if self.mode == "echo" else "summary"))],
            structured_content={"pages": 3},
            is_error=self.mode in {"immediate-error", "error"},
        )

    async def start(self, ctx, params):
        assert params.name == "render" and params.arguments == {"pages": 3}
        self.creates += 1
        (self.directory / "report.txt").write_text("page\n" * params.arguments["pages"])
        if self.mode.startswith("immediate"):
            return self.result()
        if self.mode == "core-input":
            return {"resultType": "input_required", "requestState": "private-state"}
        return {"resultType": "task", **self.task("working")}

    def task(self, status):
        return {
            "taskId": TASK,
            "status": status,
            "createdAt": STAMP,
            "lastUpdatedAt": STAMP,
            "ttlMs": 60000,
            "pollIntervalMs": self.interval,
        }

    async def get(self, ctx, params):
        assert params.task_id == TASK
        if self.mode == "expired":
            raise MCPError(-32602, TASK + TOKEN)
        if self.mode == "notice":
            await ctx.session.send_notification(
                Notification[dict[str, Any], str](
                    method="notifications/tasks",
                    params={
                        **self.task("completed"),
                        "result": self.result().model_dump(mode="json", by_alias=True),
                    },
                ),
                related_request_id=ctx.request_id,
            )
            self.counts["notifications/tasks"] += 1
        task = self.task(self.status)
        if self.status == "completed":
            task["result"] = self.result().model_dump(mode="json", by_alias=True, exclude_none=True)
        if self.status == "failed":
            task["error"] = {"code": -32603, "message": TASK + TOKEN}
        if self.status == "input_required":
            task["inputRequests"] = self.forms()
        return {"resultType": "complete", **task}

    def forms(self):
        return _forms((self.input_key, *self.extra_input_keys), self.prompt, self.form_field)

    def provider(self, **options):
        def admit(claim, task_id, requests_json):
            return (
                claim.binding.tenant == "tenant"
                and claim.binding.tool_name == "render"
                and task_id == TASK
                and (requests_json is None or json.loads(requests_json) == self.forms())
            )

        return MCPTasksProvider(admit_pending=admit, **options)

    async def update(self, ctx, params):
        assert params.task_id == TASK
        keys = params.input_responses.keys()
        if keys - {self.input_key, *self.extra_input_keys} or keys & self.satisfied:
            raise MCPError(-32602, "Request is no longer outstanding")
        self.satisfied.update(keys)
        self.writes.append(
            {
                k: v.model_dump(exclude_none=True, by_alias=True)
                for k, v in params.input_responses.items()
            }
        )
        return {}

    async def cancel(self, ctx, params):
        assert params.task_id == TASK
        return {}


@asynccontextmanager
async def serving(peer, certs: dict[str, Path] | None = None):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                peer.app,
                log_level="critical",
                ws="none",
                timeout_graceful_shutdown=2,
                log_config=None,
                ssl_certfile=str(certs["server-A.pem"]) if certs else None,
                ssl_keyfile=str(certs["server-A.key"]) if certs else None,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with timeout(5):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.005)
            scheme = "https" if certs else "http"
            yield f"{scheme}://127.0.0.1:{port}/mcp"
        finally:
            server.should_exit = True
            await task


def starting() -> JobClaim:
    return JobClaim(
        JobBinding(
            "tenant",
            "run",
            "render/v1",
            "session",
            "turn",
            "original",
            "render",
            hashlib.sha256(b'{"pages":3}').hexdigest(),
            "checkpoint-digest",
        ),
        "private-business-key",
        1,
        "start",
    )


async def authorize(claim, operation):
    if claim.binding.tenant != "tenant":
        raise PermissionError(TOKEN)
    return TOKEN


class Reservations:
    """Count dispatch attempts and use one SQLite transaction per selected batch."""

    def __init__(self, db):
        self.db = db
        self.attempts = 0
        db.execute("CREATE TABLE spent (scope TEXT, key TEXT, PRIMARY KEY (scope, key))")

    async def __call__(self, claim, reply):
        self.attempts += 1
        scope = json.dumps([astuple(claim.binding), claim.key, claim.update.receipt, claim.action])
        keys = (None,) if reply is None else tuple(reply.responses)
        try:
            with self.db:
                self.db.executemany(
                    "INSERT INTO spent VALUES (?, ?)", [(scope, json.dumps(key)) for key in keys]
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def keys(self):
        return {json.loads(key) for (key,) in self.db.execute("SELECT key FROM spent")}


class ControlJournal(Journal):
    """Issue control claims for active jobs. Only the reservation callback spends keys."""

    async def claim(self, binding, operation):
        if operation == "advance":
            return await super().claim(binding, operation)
        if self.rows[binding].update is None:
            raise PermissionError
        claim = await super().claim(binding, "advance")
        if claim.action != "observe":
            raise PermissionError
        return replace(claim, action=operation)


@pytest.fixture
def reservations(tmp_path):
    with closing(sqlite3.connect(tmp_path / "reservations.sqlite")) as db:
        yield Reservations(db)


@pytest.fixture
def clean_egress(monkeypatch):
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "ssl_cert_file",
        "ssl_cert_dir",
    ):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


def test_stalled_caller_leaves_released_worker_available():
    from _agents_codex.experimental._mcp_tasks_wire import _Workers

    workers = _Workers()

    async def claim_released_slot():
        caller = asyncio.create_task(workers.acquire())
        done, _ = await asyncio.wait({caller}, timeout=0)
        assert caller in done
        caller.result()

    with closing(asyncio.new_event_loop()) as parked:
        for _ in range(workers.available):
            parked.run_until_complete(workers.acquire())
        waiting = parked.create_task(workers.acquire())
        parked.call_soon(parked.stop)
        parked.run_forever()
        try:
            assert not waiting.done()
            workers.release()
            asyncio.run(claim_released_slot())
        finally:
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                parked.run_until_complete(waiting)


def test_private_resolution_preserves_application_debug(clean_egress, monkeypatch, caplog):
    hostname = "private-mcp.invalid"
    lookup = socket.getaddrinfo
    private_lookups = []
    direct = []

    def resolve(host, *args):
        if host in (hostname, hostname.encode()):
            private_lookups.append(host)
            raise socket.gaierror("unavailable")
        return lookup(host, *args)

    class Capture(logging.Handler):
        def emit(self, record):
            direct.append(record.getMessage())

    async def run():
        owner = asyncio.get_running_loop()
        grants = []

        async def credential(claim, operation):
            grants.append(asyncio.get_running_loop())
            return TOKEN

        p = MCPTasksProvider(
            endpoint=f"https://{hostname}/mcp", tool="render", authorize=credential
        )
        result, addresses = await asyncio.gather(
            p.start(starting(), Args(pages=3)), owner.getaddrinfo("localhost", 443)
        )
        assert result.status == "unknown" and addresses
        assert owner.get_debug() and grants == [owner, owner]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    caplog.set_level(logging.DEBUG, logger="asyncio")
    logger, handler = logging.getLogger("asyncio"), Capture()
    logger.addHandler(handler)
    try:
        asyncio.run(run(), debug=True)
    finally:
        logger.removeHandler(handler)
    assert len(private_lookups) == 1
    for messages in (direct, [record.getMessage() for record in caplog.records]):
        assert any("localhost" in message for message in messages)
        assert all(hostname not in message for message in messages)


@pytest.mark.parametrize("source", ["credential", "peer-result"])
async def test_public_provider_keeps_private_task_results_out_of_asyncio_logs(
    tmp_path, caplog, source
):
    token = "kV7q"
    peer_type = "pR9x"

    class ResultPeer(Peer):
        def result(self):
            result = super().result()
            if source == "peer-result":
                result.result_type = peer_type
            return result

    peer = ResultPeer(tmp_path)
    peer.mode = "immediate"
    peer.authorizations = {"Bearer " + token}
    owner = asyncio.get_running_loop()
    settings = []

    async def credential(claim, operation):
        loop = asyncio.get_running_loop()
        settings.append((loop, loop.get_debug(), loop.slow_callback_duration))
        return token

    async def public_control():
        return "public-log-value"

    async with serving(peer) as endpoint:
        provider = MCPTasksProvider(endpoint=endpoint, tool="render", authorize=credential)
        debug, duration = owner.get_debug(), owner.slow_callback_duration
        try:
            owner.set_debug(True)
            owner.slow_callback_duration = 0
            with caplog.at_level(logging.WARNING, logger="asyncio"):
                update = await provider.start(starting(), Args(pages=3))
                await asyncio.create_task(public_control())
            assert owner.get_debug() and owner.slow_callback_duration == 0
        finally:
            owner.slow_callback_duration = duration
            owner.set_debug(debug)

    assert peer.creates == 1
    assert update.status == ("unknown" if source == "peer-result" else "completed")
    assert len(settings) > 1 and all(entry == (owner, True, 0) for entry in settings)
    messages = [r.getMessage() for r in caplog.records if r.name == "asyncio"]
    assert any("result='public-log-value'" in message for message in messages)
    protected = peer_type if source == "peer-result" else token
    assert all(protected not in message for message in messages)


async def test_host_proxy_denies_https_without_direct_fallback(tmp_path, monkeypatch, clean_egress):
    connects = []

    async def deny(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        connects.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    proxy = await asyncio.start_server(deny, "127.0.0.1", 0)
    peer = Peer(tmp_path)
    async with proxy, serving(peer) as loopback:
        port = proxy.sockets[0].getsockname()[1]
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{port}")
        # The synthetic remote name must never reach DNS, even if the client bypasses the proxy.
        real_dns = socket.getaddrinfo
        direct = []

        def local_dns(host, *args, **kwargs):
            if host in ("synthetic-egress.invalid", b"synthetic-egress.invalid"):
                direct.append(host)
                host = "127.0.0.1"
            return real_dns(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", local_dns)
        remote = loopback.replace("http://127.0.0.1", "https://synthetic-egress.invalid")
        provider = MCPTasksProvider(endpoint=remote, tool="render", authorize=authorize)
        assert (await provider.start(starting(), Args(pages=3))).status == "unknown"
        authority = remote.split("/")[2].encode()
        assert [head.split(b"\r\n")[0] for head in connects] == [
            b"CONNECT " + authority + b" HTTP/1.1"
        ]
        assert (
            b"\r\nauthorization:" not in connects[0].lower() and TOKEN.encode() not in connects[0]
        )
        assert not direct and not peer.counts

        # The permitted cleartext loopback peer neither uses the proxy nor loads TLS settings.
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "synthetic-missing-ca"))
        peer.mode = "immediate"
        local = MCPTasksProvider(endpoint=loopback, tool="render", authorize=authorize)
        assert (await local.start(starting(), Args(pages=3))).status == "completed"
        assert len(connects) == 1 and peer.creates == 1


async def test_https_loads_current_host_ca_before_sending(
    tmp_path, monkeypatch, clean_egress, tls_files
):
    peer = Peer(tmp_path)
    peer.mode = "immediate"
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    async with serving(peer, tls_files) as endpoint:
        provider = MCPTasksProvider(endpoint=endpoint, tool="render", authorize=authorize)
        monkeypatch.setenv("SSL_CERT_FILE", str(tls_files["ca-B.pem"]))
        assert (await provider.start(starting(), Args(pages=3))).status == "unknown"
        assert not peer.counts

        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "synthetic-missing-private-ca"))
        with pytest.raises(PermissionError, match="MCP HTTPS transport configuration") as caught:
            await provider.start(starting(), Args(pages=3))
        assert "synthetic-missing-private-ca" not in repr(caught.value.args)
        assert caught.value.__cause__ is caught.value.__context__ is None
        assert not peer.counts

        monkeypatch.setenv("SSL_CERT_FILE", str(tls_files["ca-A.pem"]))
        assert (await provider.start(starting(), Args(pages=3))).status == "completed"
        assert peer.counts == {"server/discover": 1, "tools/list": 1, "tools/call": 1}


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://[private-endpoint-canary]/mcp",
        "https://host:private-endpoint-canary/mcp",
        "https://host/mcp?private-endpoint-canary",
        "https://host/mcp#private-endpoint-canary",
        "http://localhost/mcp?",
        "http://localhost/mcp#",
        "http://example.org/mcp",
        "http://localhost/private-endpoint-canary\n",
        "http://local\thost/private-endpoint-canary",
        " http://localhost/private-endpoint-canary",
    ],
)
def test_endpoint_rejection_has_no_raw_exception(endpoint):
    from _agents_codex.experimental import mcp_tasks as runtime_mcp_tasks

    with pytest.raises(ValueError) as caught:
        MCPTasksProvider(endpoint=endpoint, tool="render", authorize=authorize)
    assert caught.value.args == ("Use a fixed HTTPS endpoint or a loopback HTTP endpoint",)
    assert "private-endpoint-canary" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is caught.value.__context__ is None
    sdk_frames = [
        (frame, line)
        for frame, line in traceback.walk_tb(caught.value.__traceback__)
        if frame.f_globals is vars(runtime_mcp_tasks)
    ]
    assert sdk_frames
    captured = traceback.StackSummary.extract(sdk_frames, capture_locals=True)
    assert "private-endpoint-canary" not in "".join(captured.format())


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("immediate", "completed"),
        ("immediate-error", "completed"),
        ("core-input", "unknown"),
        ("lost", "unknown"),
    ],
)
async def test_single_create_never_drives_a_core_input_round_or_retries(tmp_path, mode, expected):
    peer = Peer(tmp_path)
    peer.mode = mode
    peer.drop = "tools/call" if mode == "lost" else ""
    async with serving(peer) as endpoint:
        p = MCPTasksProvider(endpoint=endpoint, tool="render", authorize=authorize)
        result = await p.start(starting(), Args(pages=3))
    assert result.status == expected
    if expected == "completed":
        assert result.value == {
            "content": [],
            "isError": mode == "immediate-error",
        }
    assert peer.creates == peer.counts["tools/call"] == 1
    assert peer.counts["tasks/get"] == peer.counts["tasks/result"] == 0
    assert (tmp_path / "report.txt").read_text() == "page\n" * 3


@pytest.mark.parametrize("mode", ["immediate", "deferred"])
async def test_reflected_request_url_is_private(tmp_path, monkeypatch, mode):
    class EchoPeer(Peer):
        async def audit(self, request, call_next):
            self.request_url = str(request.url)
            return await super().audit(request, call_next)

        def result(self):
            return CallToolResult(content=[TextContent(type="text", text=self.request_url)])

    peer = EchoPeer(tmp_path)
    peer.mode = mode
    app = peer.server.streamable_http_app(
        transport_security=TransportSecuritySettings(allowed_hosts=["localhost"])
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=peer.audit)
    transport = httpx2.ASGITransport(app=app)
    monkeypatch.setattr(httpx2.AsyncClient, "_transport_for_url", lambda *_: transport)
    async with app.router.lifespan_context(app):
        provider = peer.provider(
            endpoint="http://LoCaLhOsT:80/mcp", tool="render", authorize=authorize
        )
        claim = starting()
        update = await provider.start(claim, Args(pages=3))
        if mode == "deferred":
            assert update.status == "pending"
            update = await provider.observe(replace(claim, action="observe", update=update))
    assert peer.request_url == "http://localhost/mcp"
    assert peer.counts["tools/call"] == 1
    assert peer.counts["tasks/get"] == int(mode == "deferred")
    assert update.status == "completed" and update.value == {"content": [], "isError": False}


@pytest.mark.parametrize("mode", ["no-capability", "legacy", "missing-tool", "external-schema"])
async def test_unavailable_profile_denied_before_create(tmp_path, mode):
    peer = Peer(tmp_path)
    peer.mode = mode
    if mode == "no-capability":
        peer.server.extensions.clear()
    async with serving(peer) as endpoint:
        p = MCPTasksProvider(
            endpoint=endpoint,
            tool="another" if mode == "missing-tool" else "render",
            authorize=authorize,
        )
        with pytest.raises(Exception, match="outside the selected Tasks profile") as exc:
            await p.start(starting(), Args(pages=3))
        assert exc.value.__context__ is exc.value.__cause__ is None
    assert peer.counts["tools/call"] == peer.creates == 0


@pytest.mark.parametrize(
    "shape", ["same-page", "identical", "split-pages", "filtered-header", "cursor-loop"]
)
async def test_ambiguous_listing_never_creates_or_fetches(tmp_path, monkeypatch, shape):
    attempts = []

    def forbid_fetch(request, *args, **kwargs):
        attempts.append(request.full_url)
        raise OSError("Unexpected schema fetch")

    monkeypatch.setattr(urllib.request, "urlopen", forbid_fetch)

    class ListingPeer(Peer):
        async def list_tools(self, ctx, params):
            first = (await super().list_tools(ctx, params)).tools[0]
            second = first.model_copy(deep=True)
            if shape != "identical":
                second.output_schema = {"$ref": "https://schema.invalid/never-fetch"}
            if shape == "filtered-header":
                second.input_schema["properties"]["pages"]["x-mcp-header"] = "invalid token"
            if shape in ("split-pages", "cursor-loop"):
                if not params.cursor:
                    return ListToolsResult(tools=[first], next_cursor="next")
                if shape == "cursor-loop":
                    return ListToolsResult(tools=[], next_cursor="next")
                return ListToolsResult(tools=[second])
            return ListToolsResult(tools=[first, second])

    peer = ListingPeer(tmp_path)
    peer.mode = "immediate"
    async with serving(peer) as endpoint:
        p = MCPTasksProvider(endpoint=endpoint, tool="render", authorize=authorize)
        with pytest.raises(PermissionError):
            await p.start(starting(), Args(pages=3))
    assert peer.counts["tools/list"] == (2 if shape in ("split-pages", "cursor-loop") else 1)
    assert peer.creates == peer.counts["tools/call"] == 0
    assert attempts == []


@pytest.mark.parametrize("pages", [2, 32])
async def test_finite_tool_listing_creates_once(tmp_path, pages):
    class ListingPeer(Peer):
        async def list_tools(self, ctx, params):
            page = int(params.cursor or 1)
            listing = await super().list_tools(ctx, params)
            return ListToolsResult(
                tools=listing.tools if page == 1 else [],
                next_cursor=str(page + 1) if page < pages else None,
            )

    peer = ListingPeer(tmp_path)
    peer.mode = "immediate"
    async with serving(peer) as endpoint:
        p = MCPTasksProvider(endpoint=endpoint, tool="render", authorize=authorize)
        assert (await p.start(starting(), Args(pages=3))).status == "completed"
    assert peer.counts["tools/list"] == pages
    assert peer.creates == peer.counts["tools/call"] == 1


async def test_unique_tool_list_cursors_stop_before_creation(tmp_path):
    class ListingPeer(Peer):
        async def list_tools(self, ctx, params):
            page = int(params.cursor or 1)
            # Stop the old implementation safely after one excess request.
            if page > 32:
                raise MCPError(-32603, "Tool listing exceeded the fixture budget")
            listing = await super().list_tools(ctx, params)
            return ListToolsResult(
                tools=listing.tools if page == 1 else [], next_cursor=str(page + 1)
            )

    peer = ListingPeer(tmp_path)
    async with serving(peer) as endpoint:
        p = MCPTasksProvider(endpoint=endpoint, tool="render", authorize=authorize)
        with pytest.raises(PermissionError, match="outside the selected Tasks profile"):
            await p.start(starting(), Args(pages=3))
    assert peer.counts["tools/list"] == 32
    assert peer.creates == peer.counts["tools/call"] == 0


@pytest.mark.parametrize("header", [None, "present", "duplicate-nested"])
async def test_only_admitted_selected_tool_can_dispatch(tmp_path, header):
    class ListingPeer(Peer):
        async def list_tools(self, ctx, params):
            tool = (await super().list_tools(ctx, params)).tools[0]
            if not params.cursor:
                tool.name = "other"
                tool.output_schema = {"$ref": "https://schema.invalid/unused"}
                return ListToolsResult(tools=[tool], next_cursor="next")
            if header:
                tool.input_schema["properties"]["pages"]["x-mcp-header"] = "route"
            if header == "duplicate-nested":
                tool.input_schema["properties"]["nested"] = {
                    "type": "object",
                    "properties": {"other": {"type": "string", "x-mcp-header": "ROUTE"}},
                }
            return ListToolsResult(tools=[tool])

    peer = ListingPeer(tmp_path)
    peer.mode = "immediate"
    async with serving(peer) as endpoint:
        p = MCPTasksProvider(endpoint=endpoint, tool="render", authorize=authorize)
        if header:
            with pytest.raises(PermissionError):
                await p.start(starting(), Args(pages=3))
        else:
            assert (await p.start(starting(), Args(pages=3))).status == "completed"
    assert peer.counts["tools/list"] == 2
    assert peer.creates == peer.counts["tools/call"] == int(not header)


@pytest.mark.parametrize("pattern", ["^a+!$", "^(a+)+$"], ids=["safe-control", "nested-pattern"])
async def test_output_schema_never_blocks_loop_or_controls_result(tmp_path, pattern):
    text = "a" * 26 + "!"

    class PatternPeer(Peer):
        async def list_tools(self, ctx, params):
            listing = await super().list_tools(ctx, params)
            listing.tools[0].output_schema = {
                "type": "object",
                "properties": {"value": {"type": "string", "pattern": pattern}},
            }
            return listing

        def result(self):
            return CallToolResult(content=[], structured_content={"value": text})

    peer = PatternPeer(tmp_path)
    peer.mode = "immediate"
    ticks = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async with serving(peer) as endpoint:
        p = MCPTasksProvider(
            endpoint=endpoint, tool="render", authorize=authorize, timeout_seconds=1
        )
        pulse = asyncio.create_task(heartbeat())
        try:
            update = await p.start(starting(), Args(pages=3))
            ticks.append(time.monotonic())
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
    assert update.status == "completed" and update.value == {"content": [], "isError": False}
    assert peer.creates == 1 and len(ticks) >= 2
    assert max(b - a for a, b in pairwise(ticks)) < 0.5


@pytest.mark.parametrize("mode", ["immediate", "deferred"])
@pytest.mark.parametrize("label", ["public", "unapproved"])
async def test_admit_structured_result_for_current_job(tmp_path, mode, label):
    class ArrayPeer(Peer):
        async def list_tools(self, ctx, params):
            listing = await super().list_tools(ctx, params)
            listing.tools[0].output_schema = {"type": "array", "items": {"type": "string"}}
            return listing

        def result(self):
            return CallToolResult(
                content=[TextContent(type="text", text=TOKEN)], structured_content=[label]
            )

    peer = ArrayPeer(tmp_path)
    peer.mode = mode
    claim = starting()
    approved = ["public"]

    def admit(current, result):
        assert current.binding == claim.binding and current.key == claim.key
        if result.structured_content != ["public"]:
            raise ValueError("Unapproved label")
        return approved

    async with serving(peer) as endpoint:
        p = peer.provider(endpoint=endpoint, tool="render", authorize=authorize, admit_result=admit)
        update = await p.start(claim, Args(pages=3))
        if mode == "deferred":
            assert update.status == "pending"
            receipt = update.receipt
            update = await p.observe(replace(claim, action="observe", update=update))
            assert update.receipt == receipt
    approved.append("changed after admission")
    if label == "public":
        assert update.status == "completed"
        assert update.value == {"content": [], "structuredContent": ["public"], "isError": False}
    else:
        assert update.status == "failed" and update.value is None
    assert peer.creates == 1 and peer.counts["tasks/get"] == int(mode == "deferred")


@pytest.mark.parametrize("invalid", ["protocol-object", "nonfinite-number", "nonstring-key"])
async def test_invalid_host_result_fails_before_journaling(tmp_path, invalid):
    peer = Peer(tmp_path)
    async with serving(peer) as endpoint:

        def admit(claim, raw):
            if invalid == "protocol-object":
                return raw
            if invalid == "nonfinite-number":
                return {"score": float("nan")}
            return {1: "public"}

        provider = peer.provider(
            endpoint=endpoint, tool="render", authorize=authorize, admit_result=admit
        )
        claim = starting()
        pending = await provider.start(claim, Args(pages=3))
        failed = await provider.observe(replace(claim, action="observe", update=pending))
    assert failed.status == "failed" and failed.value is None
    assert failed.receipt == pending.receipt and failed.receipt


async def test_receipt_poll_deadline_binding_and_terminal_outcomes(tmp_path, caplog, monkeypatch):
    import _agents_codex.experimental.mcp_tasks as adapter

    clock = [time.time()]
    monkeypatch.setattr(adapter, "time", SimpleNamespace(time=lambda: clock[0]))
    peer = Peer(tmp_path)
    peer.interval = 60000
    for name in ("mcp.client.streamable_http", "httpx2", "httpcore2.http11", "client"):
        caplog.set_level(logging.DEBUG, logger=name)
    async with serving(peer) as endpoint:
        p = peer.provider(endpoint=endpoint, tool="render", authorize=authorize)
        claim = starting()
        update = await p.start(claim, Args(pages=3))
        read = replace(claim, action="observe", update=update)
        assert update.status == "pending" and update.value["poll_at"] == clock[0] + 60
        assert await p.observe(read) is update
        assert peer.counts["tasks/get"] == 0
        with pytest.raises(PermissionError):
            await p.observe(replace(read, binding=replace(read.binding, tenant="other")))
        with pytest.raises(PermissionError):
            await p.observe(replace(read, key="another job"))
        assert peer.counts["tasks/get"] == 0
        # The private journal stores a UTC deadline. Exercise a wake after that deadline.
        clock[0] = update.value["poll_at"]
        for mode, status, expected in [
            ("deferred", "working", "pending"),
            ("error", "completed", "completed"),
            ("deferred", "failed", "failed"),
            ("expired", "working", "unknown"),
            ("echo", "completed", "completed"),
        ]:
            peer.mode, peer.status = mode, status
            result = await p.observe(read)
            assert result.status == expected
            if expected == "completed":
                assert result.value == {"content": [], "isError": mode == "error"}
        assert peer.counts["tools/call"] == 1
        assert all(name == HEADER for method, name, _ in peer.headers if method == "tasks/get")
        assert TASK not in repr(read) + repr(update) + repr(p)
    # A control emitted by the same logger outside this private exchange reaches the real sink.
    logging.getLogger("mcp.client.streamable_http").debug("public logging control")
    assert "public logging control" in caplog.text
    for private in (TASK, HEADER, TOKEN):
        assert private not in caplog.text
        assert private not in repr([r.__dict__ for r in caplog.records])


async def test_scoped_input_approval_acknowledgment_and_cooperative_cancellation(
    tmp_path, reservations
):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    async with serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=authorize,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        claim = starting()
        seed = await p.start(claim, Args(pages=3))
        pending = await p.observe(replace(claim, action="observe", update=seed))
        assert pending.status == "input_required" and PROMPT in str(pending.value["requests"])
        respond = replace(claim, action="respond", update=pending)
        approval = MCPInputReply(
            challenge=pending.value["challenge"],
            responses={"accept-1": ElicitResult(action="accept", content={"color": "blue"})},
        )
        with pytest.raises(PermissionError):
            await p.respond(respond, replace(approval, challenge="wrong"))
        with pytest.raises(Exception, match="outside the selected Tasks profile"):
            await p.respond(
                respond, replace(approval, responses={"wrong": ElicitResult(action="decline")})
            )
        with pytest.raises(Exception, match="outside the selected Tasks profile"):
            await p.respond(
                respond,
                replace(
                    approval,
                    responses={"accept-1": ElicitResult(action="accept", content={"color": "red"})},
                ),
            )
        peer.prompt = "a different private request"
        peer.input_key = "accept-2"
        changed = await p.respond(respond, approval)
        assert (
            changed.status == "input_required" and changed.value["challenge"] != approval.challenge
        )
        assert peer.writes == []
        ack = await p.respond(
            replace(respond, update=changed),
            MCPInputReply(
                challenge=changed.value["challenge"],
                responses={"accept-2": approval.responses["accept-1"]},
            ),
        )
        assert ack.status == "pending" and len(peer.writes) == 1
        assert peer.writes[0]["accept-2"]["content"] == {"color": "blue"}
        peer.status = "working"
        cancel = await p.cancel(replace(claim, action="cancel", update=ack))
        assert cancel.status == "pending" and peer.counts["tasks/cancel"] == 1
        observe = replace(claim, action="observe", update=cancel)
        assert (await p.observe(observe)).status == "pending"
        peer.status = "completed"
        assert (await p.observe(observe)).status == "completed"
        peer.status = "cancelled"
        assert (await p.observe(observe)).status == "cancelled"
        assert peer.creates == 1


@pytest.mark.parametrize(
    ("protected_request", "key", "field", "value", "token", "allowed"),
    [
        *(
            (at, "selected", {"type": "string"}, READ_TOKEN, READ_TOKEN, False)
            for at in ("entry", "server/discover", "tasks/get", "tasks/update")
        ),
        ("entry", "key-" + READ_TOKEN, {"type": "string"}, "blue", READ_TOKEN, False),
        ("entry", "selected", {"type": "integer"}, 7841309265, "7841309265", False),
        ("server/discover", "selected", {"type": "number"}, 1e-7, "1e-07", False),
        ("tasks/get", "selected", {"type": "boolean"}, False, "false", False),
        (
            "tasks/update",
            "selected",
            {"type": "array", "items": {"type": "string"}},
            ["blue", "left\nright"],
            r"left\nright",
            False,
        ),
        ("entry", "selected", {"type": "string"}, 'left"right', 'left"right', False),
        ("entry", "{endpoint}/selected", {"type": "integer"}, 42, "inputResponses", True),
        ("entry", "selected", {"type": "number"}, 1e-7, "1e-7", True),
    ],
    ids=[
        "entry",
        "discovery",
        "confirmation",
        "update",
        "selected-key",
        "integer",
        "wire-exponent",
        "boolean",
        "escaped-list-member",
        "decoded-string",
        "clean-partial-form",
        "different-exponent-spelling",
    ],
)
async def test_frozen_form_checks_each_issued_credential(
    tmp_path, reservations, protected_request, key, field, value, token, allowed
):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    peer.extra_input_keys = ("unselected-" + token,)
    peer.form_field = field
    peer.authorizations.add("Bearer " + token)
    credentials = iter(
        token if request == protected_request else TOKEN
        for request in ("entry", "server/discover", "tasks/get", "tasks/update")
    )

    async def credential(claim, operation):
        return next(credentials) if operation == "respond" else TOKEN

    async with serving(peer) as endpoint:
        key = key.format(endpoint=endpoint)
        peer.input_key = key
        backend = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=credential,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        claim = await _observed_form(backend)
        approval = MCPInputReply(
            challenge=claim.update.value["challenge"],
            responses={key: ElicitResult(action="accept", content={"color": value})},
        )
        if allowed:
            assert (await backend.respond(claim, approval)).status == "pending"
            assert peer.writes == [{key: {"action": "accept", "content": {"color": value}}}]
        else:
            with pytest.raises(PermissionError):
                await backend.respond(claim, approval)
            assert not peer.writes
    assert list(credentials) == []
    assert peer.counts["tasks/get"] == 2
    assert peer.counts["tasks/update"] == reservations.attempts == int(allowed)


async def test_revoked_credentials_and_lost_mutation_replies(tmp_path, reservations):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    denied = set()
    grants = []

    async def credential(claim, operation):
        grants.append(operation)
        if operation in denied:
            raise ValueError(TOKEN + TASK)
        return await authorize(claim, operation)

    async with serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=credential,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        start = starting()
        accepted = await p.start(start, Args(pages=3))
        read = replace(start, action="observe", update=accepted)
        challenge = await p.observe(read)
        answer = MCPInputReply(
            challenge=challenge.value["challenge"],
            responses={"accept-1": ElicitResult(action="decline")},
        )
        input_claim = replace(start, action="respond", update=challenge)
        cancel_claim = replace(start, action="cancel", update=accepted)
        before = peer.counts.copy()
        denied.update(("observe", "respond", "cancel"))
        for op in (p.observe(read), p.respond(input_claim, answer), p.cancel(cancel_claim)):
            with pytest.raises(PermissionError) as exc:
                await op
            assert exc.value.__context__ is exc.value.__cause__ is None
            assert TOKEN not in repr(exc.value.args) and TASK not in str(exc.value)
        assert peer.counts == before
        denied.clear()
        peer.mode = "revoked"
        with pytest.raises(PermissionError):
            await p.observe(read)
        assert peer.counts["tasks/get"] == before["tasks/get"]
        peer.mode = "deferred"
        for method, op in [
            ("tasks/update", p.respond(input_claim, answer)),
            ("tasks/cancel", p.cancel(cancel_claim)),
        ]:
            peer.drop = method
            result = await op
            assert result.status == "unknown" and result.receipt == accepted.receipt
            assert peer.counts[method] == 1
            with pytest.raises(PermissionError):
                if method == "tasks/update":
                    await p.respond(replace(input_claim, generation=2), answer)
                else:
                    await p.cancel(replace(cancel_claim, generation=2))
            assert peer.counts[method] == 1
        assert reservations.keys() == {"accept-1", None}
        assert peer.creates == 1


async def test_early_wake_uses_current_truth_and_bounded_fast_path(tmp_path):
    peer = Peer(tmp_path)
    peer.status = "working"
    peer.mode = "notice"
    peer.interval = 40
    async with serving(peer) as endpoint:
        p = peer.provider(endpoint=endpoint, tool="render", authorize=authorize)
        claim = starting()
        accepted = await p.start(claim, Args(pages=3))
        # The bounded post-create read must wait for the server's earliest poll. A forged
        # completed notification in the stream cannot publish a result from this working task.
        assert (await p.observe(replace(claim, update=accepted))).status == "pending"
        assert time.time() >= accepted.value["poll_at"] and peer.counts["tasks/get"] == 1
        assert peer.counts["notifications/tasks"] == 1
        assert peer.creates == 1


async def test_authorization_is_rechecked_between_discovery_and_create(tmp_path):
    peer = Peer(tmp_path)

    async def revoke_after_listing(claim, operation):
        if peer.counts["tools/list"]:
            raise PermissionError(TOKEN)
        return TOKEN

    async with serving(peer) as endpoint:
        p = MCPTasksProvider(
            endpoint=endpoint, tool="render", authorize=revoke_after_listing, timeout_seconds=1
        )
        with pytest.raises(PermissionError) as caught:
            await p.start(starting(), Args(pages=3))
    assert peer.counts["tools/list"] == 1 and peer.counts["tools/call"] == peer.creates == 0
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert TOKEN not in str(caught.value)


async def test_frozen_arguments_and_remote_redirect_cannot_replace_the_binding(tmp_path):
    peer = Peer(tmp_path)
    target = Peer(tmp_path)
    async with serving(target) as untrusted, serving(peer) as endpoint:
        p = MCPTasksProvider(endpoint=endpoint, tool="render", authorize=authorize)
        with pytest.raises(PermissionError):
            await p.start(starting(), Args(pages=4))
        assert peer.counts == {}
        peer.redirect = untrusted
        result = await p.start(starting(), Args(pages=3))
        assert result.status == "unknown" and peer.counts["tools/call"] == 1
        assert not target.counts and peer.creates == target.creates == 0


async def test_approved_answer_is_frozen_across_reauthorization(tmp_path, reservations):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    waiting, resume = asyncio.Event(), asyncio.Event()

    async def credential(claim, action):
        if action == "respond":
            waiting.set()
            await resume.wait()
        return TOKEN

    async with timeout(8), serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=credential,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        claim = starting()
        seed = await p.start(claim, Args(pages=3))
        state = await p.observe(replace(claim, action="observe", update=seed))
        chosen = ElicitResult(action="accept", content={"color": "blue"})
        answer = MCPInputReply(challenge=state.value["challenge"], responses={"accept-1": chosen})
        sending = asyncio.create_task(
            p.respond(replace(claim, action="respond", update=state), answer)
        )
        try:
            await waiting.wait()
            chosen.content["color"] = "red"
        finally:
            resume.set()
        assert (await sending).status == "pending"
        assert peer.writes[0]["accept-1"]["content"] == {"color": "blue"}


async def _observed_form(p):
    claim = starting()
    seed = await p.start(claim, Args(pages=3))
    state = await p.observe(replace(claim, action="observe", update=seed))
    assert state.status == "input_required"
    return replace(claim, action="respond", update=state)


async def test_public_unsent_form_remains_available(tmp_path, responses, reservations, monkeypatch):
    from _agents_codex.experimental import mcp_tasks

    journal = ControlJournal()
    clock = time.time()
    monkeypatch.setattr(mcp_tasks, "time", SimpleNamespace(time=lambda: clock))
    peer = Peer(tmp_path)
    peer.status = "input_required"

    async def reserve(claim, reply):
        await journal.authorize(claim, claim.action)
        assert claim.update == journal.rows[claim.binding].update
        assert reply.challenge == claim.update.value["challenge"]
        assert reply.responses["accept-2"].action == "decline"
        return await reservations(claim, reply)

    async with timeout(12), serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=authorize,
            allow_input_replies=True,
            reserve_mutation=reserve,
        )
        jobs, tool = declared(journal, p, tenant="tenant", require_reconciliation=False)
        agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
        responses.respond(function_call("render", {"pages": 3}, "form-approval"))
        async with agent.session() as opening:
            await opening.submit("Render.")
            request = await next_request(opening.interactions(), tool)
            async with jobs.start(opening, request) as (restored, outcome):
                assert outcome == "pending"
                current = await next_request(restored.interactions(), tool)
                (row,) = journal.rows.values()

                def approval():
                    return MCPInputReply(
                        challenge=row.update.value["challenge"],
                        responses={peer.input_key: ElicitResult(action="decline")},
                    )

                async def respond(reply):
                    return await jobs.advance(
                        restored, current, operation="respond", response=reply
                    )

                assert await jobs.advance(restored, current) == "input_required"
                stale = approval()
                peer.input_key, peer.interval = "accept-2", 60_000
                assert await jobs.advance(restored, current) == "input_required"
                fresh = approval()
                assert fresh.challenge != stale.challenge
                counts = peer.counts.copy()
                with pytest.raises(PermissionError):
                    await respond(stale)
                assert await jobs.advance(restored, current) == "input_required"
                assert peer.counts == counts and reservations.attempts == 0

                peer.interval, peer.drop = 0, "tasks/get"
                assert await respond(fresh) == "unknown"
                previous_owner = row.generation
                assert peer.counts["tasks/get"] == counts["tasks/get"] + 1
                assert peer.counts["tasks/update"] == reservations.attempts == 0
                peer.drop = ""
                clock += 61
                assert await jobs.advance(restored, current) == "input_required"
                assert approval().challenge == fresh.challenge
                assert await respond(fresh) == "pending" and row.generation > previous_owner
    assert reservations.keys() == {"accept-2"} and reservations.attempts == 1
    assert peer.counts["tasks/update"] == 1 and list(peer.writes[0]) == ["accept-2"]


@pytest.mark.parametrize("action", ["respond", "cancel"])
async def test_mutating_controls_require_current_authority_and_storage(
    tmp_path, reservations, action, caplog
):
    caplog.set_level(logging.DEBUG, logger="mcp.client.streamable_http")
    peer = Peer(tmp_path)
    peer.status = "input_required"
    deny_after_get = None

    async def credential(claim, operation):
        if deny_after_get is not None and peer.counts["tasks/get"] > deny_after_get:
            raise PermissionError(PROMPT)
        return await authorize(claim, operation)

    async def deny(claim, reply):
        raise RuntimeError(PROMPT)

    async def binding_only(claim, reply):
        await authorize(claim, claim.action)

    async with serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint, tool="render", authorize=credential, allow_input_replies=True
        )
        claim = replace(await _observed_form(p), action=action)
        claim = replace(
            claim, update=replace(claim.update, value={**claim.update.value, "poll_at": 2**40})
        )
        reply = MCPInputReply(
            challenge=claim.update.value["challenge"],
            responses={peer.input_key: ElicitResult(action="decline")},
        )

        async def attempt(provider):
            if action == "respond":
                return await provider.respond(claim, reply)
            return await provider.cancel(claim)

        before = peer.counts.copy()
        with pytest.raises(PermissionError, match="host reservation"):
            await attempt(p)
        assert peer.counts == before
        deny_after_get = peer.counts["tasks/get"]
        with pytest.raises(PermissionError):
            await attempt(replace(p, reserve_mutation=reservations))
        assert peer.counts["tasks/get"] == deny_after_get + 1 and reservations.attempts == 0
        deny_after_get = None
        for callback in (deny, binding_only):
            with pytest.raises(PermissionError) as caught:
                await attempt(replace(p, reserve_mutation=callback))
            assert caught.value.__cause__ is caught.value.__context__ is None
            assert PROMPT not in "".join(traceback.format_exception(caught.value))
        assert all(PROMPT not in repr(record.__dict__) for record in caplog.records)
        assert peer.counts["tasks/update"] == peer.counts["tasks/cancel"] == 0
        if action == "cancel":
            peer.drop = "tasks/get"
            assert (await attempt(replace(p, reserve_mutation=reservations))).status == "unknown"
            peer.drop, peer.status = "", "cancelled"
            assert (await attempt(replace(p, reserve_mutation=reservations))).status == "cancelled"
            assert peer.counts["tasks/cancel"] == reservations.attempts == 0


async def test_cancelling_form_joins_host_reservation(tmp_path):
    import anyio

    peer = Peer(tmp_path)
    peer.status = "input_required"
    owner = asyncio.get_running_loop()
    entered, cleaning, release, exited = (asyncio.Event() for _ in range(4))

    async def reserve(claim, reply):
        assert asyncio.get_running_loop() is owner
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            with anyio.CancelScope(shield=True):
                await release.wait()
            exited.set()
        return True

    async with timeout(8), serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=authorize,
            allow_input_replies=True,
            reserve_mutation=reserve,
        )
        claim = await _observed_form(p)
        reply = MCPInputReply(
            challenge=claim.update.value["challenge"],
            responses={peer.input_key: ElicitResult(action="decline")},
        )
        sending = asyncio.create_task(p.respond(claim, reply))
        try:
            await entered.wait()
            sending.cancel()
            await cleaning.wait()
            assert not sending.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
        assert exited.is_set() and peer.counts["tasks/update"] == 0


@pytest.mark.parametrize(
    ("action", "callback", "interrupt"),
    [
        ("respond", "reservation", "cancel"),
        ("cancel", "reservation", "cancel"),
        ("respond", "reservation", "deadline"),
        ("start", "credential", "cancel"),
        ("observe", "credential", "cancel"),
        ("respond", "credential", "cancel"),
        ("cancel", "credential", "cancel"),
        ("start", "request", "cancel"),
        ("cancel", "mutation credential", "cancel"),
        ("start", "credential", "deadline"),
    ],
)
async def test_provider_joins_callback_rollback(tmp_path, action, callback, interrupt):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    lock = asyncio.Lock()
    entered, rolling_back, release = (asyncio.Event() for _ in range(3))
    owner = asyncio.get_running_loop()
    reservations = []

    async def hold():
        assert asyncio.get_running_loop() is owner
        await lock.acquire()
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            if callback != "mutation credential" and interrupt != "deadline":
                raise
        finally:
            rolling_back.set()
            await release.wait()
            lock.release()

    async def credential(claim, operation):
        if (
            callback == "credential"
            or (callback == "request" and peer.counts["tools/list"])
            or (callback == "mutation credential" and peer.counts["tasks/get"] > prior_gets)
        ):
            await hold()
        return await authorize(claim, operation)

    async def reserve(claim, reply):
        reservations.append(claim.action)
        if callback == "reservation":
            await hold()
        return True

    async with timeout(8), serving(peer) as endpoint:
        provider = peer.provider(
            endpoint=endpoint, tool="render", authorize=authorize, allow_input_replies=True
        )
        claim = starting()
        if action != "start":
            claim = replace(await _observed_form(provider), action=action)
        provider = replace(
            provider,
            authorize=credential,
            reserve_mutation=reserve,
            timeout_seconds=0.5 if interrupt == "deadline" else 5,
        )
        if action == "start":
            work = provider.start(claim, Args(pages=3))
        elif action == "observe":
            work = provider.observe(claim)
        elif action == "respond":
            reply = MCPInputReply(
                challenge=claim.update.value["challenge"],
                responses={peer.input_key: ElicitResult(action="decline")},
            )
            work = provider.respond(claim, reply)
        else:
            work = provider.cancel(claim)
        before = peer.counts.copy()
        prior_gets = before["tasks/get"]
        interruptions = []

        async def caller():
            try:
                return await work
            except asyncio.CancelledError as error:
                # Observe the public exception before it crosses another task boundary.
                interruptions.append(error.args)
                raise

        sending = asyncio.create_task(caller())
        try:
            await entered.wait()
            if interrupt == "cancel":
                sending.cancel("first")
            await asyncio.wait_for(rolling_back.wait(), 2)
            sending.cancel("later")
            done, _ = await asyncio.wait({sending}, timeout=0.05)
        finally:
            release.set()
            if not rolling_back.is_set():
                sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)
        if interrupt == "deadline" and callback == "credential":
            with pytest.raises(asyncio.TimeoutError):
                await sending
        elif interrupt == "deadline":
            result = await sending
            assert result.status == "unknown" and result.receipt == claim.update.receipt
        else:
            with pytest.raises(asyncio.CancelledError):
                await sending
            assert interruptions == [("first",)]
        assert not done and not lock.locked()
        if callback == "credential":
            assert peer.counts == before
        assert peer.creates == before["tools/call"]
        assert reservations == ([action] if callback == "reservation" else [])
        assert peer.counts["tasks/update"] == peer.counts["tasks/cancel"] == 0


async def test_initial_authorization_context_reaches_requests_and_admission(tmp_path):
    peer = Peer(tmp_path)
    peer.mode = "immediate"
    access = ContextVar("access", default="unchecked")
    seen = []
    owner = asyncio.get_running_loop()

    async def credential(claim, operation):
        seen.append((asyncio.get_running_loop(), access.get()))
        access.set("verified" if len(seen) == 1 else "request")
        return TOKEN

    def admit(claim, result):
        return access.get()

    async with serving(peer) as endpoint:
        provider = MCPTasksProvider(
            endpoint=endpoint, tool="render", authorize=credential, admit_result=admit
        )
        result = await provider.start(starting(), Args(pages=3))
    assert len(seen) > 1 and seen[0] == (owner, "unchecked")
    assert all(entry == (owner, "verified") for entry in seen[1:])
    assert result.value["structuredContent"] == access.get() == "verified"


async def test_overlapping_approvals_reserve_each_key_once(tmp_path, reservations):
    peer = Peer(tmp_path)
    peer.status, peer.extra_input_keys = "input_required", ("shared", "another")
    async with serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=authorize,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        claim = await _observed_form(p)

        def approval(*keys):
            return MCPInputReply(
                challenge=claim.update.value["challenge"],
                responses={key: ElicitResult(action="decline") for key in keys},
            )

        groups = [(peer.input_key, "shared"), ("another", "shared")]
        results = await asyncio.gather(
            *(p.respond(claim, approval(*keys)) for keys in groups), return_exceptions=True
        )
        assert sum(isinstance(result, PermissionError) for result in results) == 1
        assert sum(getattr(result, "status", None) == "pending" for result in results) == 1
        assert peer.counts["tasks/update"] == 1
        sent = set(peer.writes[0])
        assert reservations.keys() == sent and "shared" in sent
        later = replace(claim, generation=claim.generation + 1)
        with pytest.raises(PermissionError):
            await p.respond(later, approval("shared"))
        (unused,) = {peer.input_key, "another"} - sent
        assert (await p.respond(later, approval(unused))).status == "pending"
        assert reservations.keys() == {peer.input_key, "shared", "another"}
        assert peer.counts["tasks/update"] == len(peer.writes) == 2


async def test_unqualified_same_key_replacement_receives_no_answer(tmp_path, reservations):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    async with serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint, tool="render", authorize=authorize, reserve_mutation=reservations
        )
        claim = await _observed_form(p)
        approval = MCPInputReply(
            challenge=claim.update.value["challenge"],
            responses={"accept-1": ElicitResult(action="accept", content={"color": "blue"})},
        )
        # This malicious target reuses the approved key for a different form after the get.
        peer.prompt = "Replacement under the same key"
        with pytest.raises(PermissionError, match="not enabled"):
            await p.respond(claim, approval)
    assert peer.counts["tasks/get"] == 1 and peer.counts["tasks/update"] == 0
    assert not peer.writes and reservations.attempts == 0


async def test_qualified_key_rotation_during_update_authorization(tmp_path, reservations):
    peer = Peer(tmp_path)
    peer.status = "input_required"

    async def rotate_after_confirming_get(claim, operation):
        if operation == "respond" and peer.counts["tasks/get"] == 2:
            peer.input_key = "accept-2"
            peer.prompt = "A new request"
        return TOKEN

    async with serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=rotate_after_confirming_get,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        claim = await _observed_form(p)
        chosen = ElicitResult(action="accept", content={"color": "blue"})
        approval = MCPInputReply(
            challenge=claim.update.value["challenge"], responses={"accept-1": chosen}
        )
        stale = await p.respond(claim, approval)
        assert stale.status == "unknown" and stale.receipt == claim.update.receipt
        assert peer.counts["tasks/update"] == 1 and not peer.writes

        current = await p.observe(replace(claim, action="observe", update=stale))
        assert current.value["challenge"] != approval.challenge
        assert list(current.value["requests"]) == ["accept-2"]
        renewed = MCPInputReply(
            challenge=current.value["challenge"], responses={"accept-2": chosen}
        )
        claim = replace(claim, update=current)
        assert (await p.respond(claim, renewed)).status == "pending"
        # A stale read can still list a satisfied key. Its reservation survives the new owner.
        with pytest.raises(PermissionError):
            await p.respond(replace(claim, generation=claim.generation + 1), renewed)
    assert peer.counts["tasks/update"] == 2
    assert len(peer.writes) == 1 and peer.writes[0]["accept-2"]["content"] == {"color": "blue"}


@pytest.mark.parametrize(
    "constraint", [{"pattern": "^(a+)+$"}, {"$ref": "https://schema.invalid/input"}]
)
async def test_peer_form_constraints_are_refused_before_update(tmp_path, constraint, reservations):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    peer.form_field = {"type": "string", **constraint}
    async with serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=authorize,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        claim = await _observed_form(p)
        answer = MCPInputReply(
            challenge=claim.update.value["challenge"],
            responses={
                "accept-1": ElicitResult(action="accept", content={"color": "a" * 26 + "!"})
            },
        )
        with pytest.raises(PermissionError):
            await p.respond(claim, answer)
    assert peer.counts["tasks/update"] == reservations.attempts == 0 and peer.writes == []


async def test_qualified_integer_form_accepts_only_integral_numbers(tmp_path, reservations):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    peer.form_field = {"type": "integer"}
    async with serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=authorize,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        claim = await _observed_form(p)
        for value in (True, 1.5, 1.0):
            answer = MCPInputReply(
                challenge=claim.update.value["challenge"],
                responses={"accept-1": ElicitResult(action="accept", content={"color": value})},
            )
            if type(value) is float and value == 1.0:
                assert (await p.respond(claim, answer)).status == "pending"
            else:
                with pytest.raises(PermissionError):
                    await p.respond(claim, answer)
    assert peer.counts["tasks/update"] == 1
    assert type(peer.writes[0]["accept-1"]["content"]["color"]) is float


@pytest.mark.parametrize("action", ["decline", "accept"])
async def test_public_advance_preserves_approval_during_journal_wait(
    tmp_path, responses, action, reservations
):
    waiting, resume = asyncio.Event(), asyncio.Event()

    class GatedJournal(ControlJournal):
        async def authorize(self, claim, operation):
            if operation == "respond":
                waiting.set()
                await resume.wait()
            await super().authorize(claim, operation)

    peer = Peer(tmp_path)
    peer.status = "input_required"
    peer.form_field = {"type": "array", "items": {"type": "string", "enum": ["blue", "red"]}}
    journal = GatedJournal()
    async with timeout(12), serving(peer) as endpoint:
        p = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=authorize,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        jobs, tool = declared(journal, p, tenant="tenant", require_reconciliation=False)
        agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
        responses.respond(function_call("render", {"pages": 3}, "journal-wait"))
        async with agent.session() as opening:
            await opening.submit("Render.")
            request = await next_request(opening.interactions(), tool)
            async with jobs.start(opening, request) as (restored, outcome):
                assert outcome == "pending"
                current = await next_request(restored.interactions(), tool)
                assert await jobs.advance(restored, current) == "input_required"
                (row,) = journal.rows.values()
                source = ElicitResult(
                    action=action, content={"color": ["blue"]} if action == "accept" else None
                )
                original = {"accept-1": source}
                answer = MCPInputReply(challenge=row.update.value["challenge"], responses=original)
                sending = asyncio.create_task(
                    jobs.advance(restored, current, operation="respond", response=answer)
                )
                try:
                    await waiting.wait()
                    original.clear()
                    if action == "decline":
                        source.action = "accept"
                        source.content = {"color": ["red"]}
                    else:
                        source.content["color"][0] = "red"
                        answer.responses["accept-1"].content["color"].append("red")
                finally:
                    resume.set()
                assert await sending == "pending"
    sent = peer.writes[0]["accept-1"]
    assert peer.counts["tasks/update"] == len(peer.writes) == 1
    assert sent["action"] == action
    assert sent.get("content") == ({"color": ["blue"]} if action == "accept" else None)


async def _restart_host(
    model_endpoint: str, endpoint: str, directory: Path, read_token: str = TOKEN
) -> str:
    # Reuse a real application-owned conditional SQLite journal across fresh processes.
    from agents import NativeProvider
    from examples.external_job.journal import Journal
    from examples.external_job.store import ThumbnailStore

    journal = Journal(ThumbnailStore(directory, "tenant"), "private-business-key")
    with journal.owner():

        async def credential(claim, operation):
            await journal.authorize(claim, operation)
            return read_token if operation == "observe" else TOKEN

        def admit(claim, result):
            expected = {"pages": 3}
            if (
                claim.key != journal.key
                or claim.binding.provider_id != "fixed-report/v1"
                or result.structured_content != expected
            ):
                raise ValueError("Unexpected report")
            return expected

        def admit_pending(claim, task_id, requests_json):
            return (
                claim.key == journal.key
                and claim.binding.provider_id == "fixed-report/v1"
                and task_id == TASK
                and (
                    requests_json is None
                    or json.loads(requests_json)
                    == _forms(("accept-1",), PROMPT, {"type": "string", "enum": ["blue"]})
                )
            )

        backend = MCPTasksProvider(
            endpoint=endpoint,
            tool="render",
            authorize=credential,
            admit_result=admit,
            admit_pending=admit_pending,
        )
        jobs = DeferredTools(journal=journal, tenant="tenant", run_id=journal.order)
        tool = FunctionTool(
            name="render",
            description="Render the selected report.",
            input_type=Args,
            output_type=Output,
        )
        jobs.register(
            tool,
            provider_id="fixed-report/v1",
            provider=backend,
            require_reconciliation=False,
            project=lambda v: Output(pages=v["structuredContent"]["pages"], is_error=v["isError"]),
        )
        model = NativeProvider(base_url=model_endpoint, api_key="synthetic-native-fixture")
        agent = Agent(instructions="Use the selected report tool.", tools=[tool], provider=model)
        saved, finished = journal.load()
        assert not finished
        try:
            if saved is None:
                async with agent.session() as session:
                    await session.submit("Render the report.")
                    request = await next_request(session.interactions(), tool)
                    async with jobs.start(session, request) as (_, outcome):
                        assert isinstance(outcome, str)
                        return outcome
            state = SessionState.from_json(saved)
            async with agent.session(state=state) as restored:
                request = await next_request(restored.interactions(), tool)
                assert (
                    request.call_id == "original-mcp"
                    and request.turn_id == state.native_continuation.turn_id
                )
                outcome = await jobs.advance(restored, request)
                if isinstance(outcome, Turn):
                    assert outcome.turn_id == request.turn_id
                    assert (await outcome.get_result()).final_output == "Report received."
                    journal.finish((await restored.save_state()).to_json())
                    return "completed"
                return outcome
        finally:
            await model.aclose()


@pytest.mark.parametrize(
    "field", ["task-id", "request-key", "prompt", "earlier-prompt", "earlier-key", "approved-form"]
)
async def test_reflected_credentials_never_reach_job_journal(tmp_path, responses, field):
    class EchoPeer(Peer):
        async def audit(self, request, call_next):
            method = request.headers.get("mcp-method")
            if method == (
                "tools/call"
                if field in {"task-id", "earlier-prompt", "earlier-key"}
                else "tasks/get"
            ):
                self.reflection = "echo-" + request.headers["authorization"].removeprefix("Bearer ")
                if field in {"request-key", "earlier-key"}:
                    self.input_key = self.reflection
                elif field in {"prompt", "earlier-prompt"}:
                    self.prompt = self.reflection
            return await super().audit(request, call_next)

        def task(self, status):
            task = super().task(status)
            if field == "task-id":
                task["taskId"] = self.reflection
            return task

    peer = EchoPeer(tmp_path)
    peer.status = "input_required"
    peer.authorizations.add("Bearer " + READ_TOKEN)
    responses.respond(function_call("render", {"pages": 3}, "original-mcp"))
    directory = tmp_path / "application"
    async with serving(peer) as endpoint:
        outcome = await _restart_host(responses.url, endpoint, directory, READ_TOKEN)
        if field != "task-id":
            assert outcome == "pending"
            outcome = await _restart_host(responses.url, endpoint, directory, READ_TOKEN)
        with sqlite3.connect(directory / "orders.sqlite3") as db:
            (recorded,) = db.execute("SELECT receipt_update FROM orders").fetchone()
        assert TOKEN not in recorded and READ_TOKEN not in recorded
        assert outcome == ("input_required" if field == "approved-form" else "unknown")
        if field == "approved-form":
            assert (
                json.loads(recorded)["value"]["requests"]["accept-1"]["params"]["message"] == PROMPT
            )
        receipt = json.loads(recorded)["receipt"]
        if field == "task-id":
            assert receipt is None
            assert await _restart_host(responses.url, endpoint, directory, READ_TOKEN) == "unknown"
        else:
            assert json.loads(receipt)[1] == TASK
    assert peer.creates == 1


async def test_replaced_form_cannot_return_a_reflected_credential(tmp_path, reservations):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    async with serving(peer) as endpoint:
        backend = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=authorize,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        claim = await _observed_form(backend)
        approval = MCPInputReply(
            challenge=claim.update.value["challenge"],
            responses={peer.input_key: ElicitResult(action="decline")},
        )
        peer.prompt = TOKEN
        update = await backend.respond(claim, approval)
    assert update.status == "unknown" and update.receipt == claim.update.receipt
    assert TOKEN not in json.dumps(update.value)
    assert reservations.attempts == 0 and not peer.writes


async def test_new_task_id_cannot_persist_a_bearer_from_an_earlier_job(tmp_path):
    class EchoPeer(Peer):
        async def audit(self, request, call_next):
            if request.headers.get("mcp-method") == "tools/call" and self.creates == 0:
                self.previous = request.headers["authorization"].removeprefix("Bearer ")
            return await super().audit(request, call_next)

        def task(self, status):
            task = super().task(status)
            if self.creates == 2:
                task["taskId"] = self.previous
            return task

    peer = EchoPeer(tmp_path)
    peer.authorizations.add("Bearer " + READ_TOKEN)
    claim = starting()

    async def rotated(claim, operation):
        return READ_TOKEN

    async with serving(peer) as endpoint:
        first = peer.provider(endpoint=endpoint, tool="render", authorize=authorize)
        assert (await first.start(claim, Args(pages=3))).status == "pending"
        second = peer.provider(endpoint=endpoint, tool="render", authorize=rotated)
        other = replace(claim, binding=replace(claim.binding, call_id="another"), key="another-job")
        update = await second.start(other, Args(pages=3))
    assert peer.previous == TOKEN and peer.creates == 2
    assert (update.status, update.receipt, update.value) == ("unknown", None, None)


@pytest.mark.parametrize("decision", ["missing", "truthy", "error", "approved"])
async def test_pending_admission_on_caller_loop(tmp_path, decision):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    claim = starting()
    owner = asyncio.get_running_loop()
    observed = []
    expected = _forms(("accept-1",), PROMPT, {"type": "string", "enum": ["blue"]})

    def admit(current, task_id, requests):
        observed.append((current, task_id, requests, asyncio.get_running_loop()))
        if decision == "error":
            raise ValueError("private host refusal")
        if decision == "truthy":
            return 1
        return (
            current.binding == claim.binding
            and task_id == TASK
            and (requests is None or json.loads(requests) == expected)
        )

    async with serving(peer) as endpoint:
        options = {"endpoint": endpoint, "tool": "render", "authorize": authorize}
        candidate = MCPTasksProvider(
            **options, admit_pending=None if decision == "missing" else admit
        )
        created = await candidate.start(claim, Args(pages=3))
        if decision == "approved":
            assert created.status == "pending"
        else:
            assert (created.status, created.receipt, created.value) == ("unknown", None, None)
        # A different job supplies a previously accepted receipt to the new provider.
        other = replace(
            claim, binding=replace(claim.binding, call_id="existing"), key="existing-job"
        )
        seed = await peer.provider(**options).start(other, Args(pages=3))
        read = replace(other, action="observe", update=seed)
        observed_form = await candidate.observe(read)
        assert observed_form.status == "unknown" and observed_form.receipt == seed.receipt
        if decision == "approved":
            read_own = replace(claim, action="observe", update=created)
            result = await candidate.observe(read_own)
            assert result.status == "input_required" and result.value["requests"] == expected
    if decision == "missing":
        assert observed == []
    else:
        assert observed[0] == (claim, TASK, None, owner)
        assert observed[1][:2] == (read, TASK)
        assert observed[1][2] == json.dumps(expected, sort_keys=True, separators=(",", ":"))
        assert observed[1][3] is owner


@pytest.mark.parametrize(
    ("action", "approve", "bearer"),
    [
        ("observe", False, "eyJ-review-only"),
        ("observe", True, "eyJ-review-only"),
        ("respond", True, "eyJ-review-only"),
        ("observe", True, "WIRE"),
    ],
)
async def test_pending_admission_rejects_current_bearer(
    tmp_path, reservations, action, approve, bearer
):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    peer.authorizations.update({"Bearer " + bearer, "Bearer " + READ_TOKEN})
    seen = []

    def admit(claim, task_id, requests):
        seen.append(requests)
        return approve

    async with serving(peer) as endpoint:
        backend = peer.provider(endpoint=endpoint, tool="render", authorize=authorize)
        claim = await _observed_form(backend)
        reply = MCPInputReply(
            challenge=claim.update.value["challenge"],
            responses={peer.input_key: ElicitResult(action="decline")},
        )
        peer.prompt = "îyJ-review-only"
        assert bearer not in peer.prompt and (bearer in json.dumps(peer.prompt) or bearer in TASK)
        issued = iter((bearer, TOKEN, READ_TOKEN))

        async def rotate(*_):
            return next(issued)

        backend = replace(
            backend,
            authorize=rotate,
            admit_pending=admit,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        if action == "observe":
            result = await backend.observe(replace(claim, action="observe"))
        else:
            result = await backend.respond(claim, reply)
        assert list(issued) == []
        assert seen == []
        assert (result.status, result.receipt) == ("unknown", claim.update.receipt)
        assert bearer not in json.dumps(result.value)
        assert (
            reservations.attempts == peer.counts["tasks/update"] == peer.counts["tasks/cancel"] == 0
        )


@pytest.mark.parametrize(
    ("action", "failure"),
    [
        ("observe", "binding"),
        ("respond", "binding"),
        ("cancel", "binding"),
        ("cancel", "reservation"),
        ("observe", "authorization"),
        ("respond", "authorization"),
        ("cancel", "authorization"),
        ("observe", "task_get"),
        ("respond", "mutation"),
        ("cancel", "mutation"),
    ],
)
async def test_denials_clear_private_journal_locals(tmp_path, reservations, action, failure):
    peer = Peer(tmp_path)
    peer.status = "input_required"
    answer = "synthetic-distinct-private-answer"
    peer.form_field = {"type": "string", "enum": [answer]}
    async with serving(peer) as endpoint:
        backend = peer.provider(endpoint=endpoint, tool="render", authorize=authorize)
        form = await _observed_form(backend)
        claim = replace(form, action=action)
        reply = MCPInputReply(
            challenge=claim.update.value["challenge"],
            responses={peer.input_key: ElicitResult(action="accept", content={"color": answer})},
        )
        binding = ContextVar("request-credential")
        reset = binding.set("initial")
        authorizations = []

        async def credential(current, operation):
            authorizations.append(operation)
            if len(authorizations) == {"authorization": 2, "task_get": 3, "mutation": 4}.get(
                failure
            ):
                raise PermissionError
            binding.set(TOKEN)
            return TOKEN

        backend = replace(
            backend,
            authorize=credential,
            allow_input_replies=True,
            reserve_mutation=None if failure == "reservation" else reservations,
        )
        if failure == "binding":
            claim = replace(claim, binding=replace(claim.binding, call_id="other-call"))
        with pytest.raises(PermissionError) as error:
            if action == "respond":
                await backend.respond(claim, reply)
            elif action == "cancel":
                await backend.cancel(claim)
            else:
                await backend.observe(claim)
        assert error.value.args == (
            {
                "binding": "MCP receipt does not belong to this prepared operation",
                "reservation": "MCP mutations require a host reservation",
            }.get(failure, "MCP request authorization denied"),
        )
        assert binding.get() == TOKEN
        binding.reset(reset)
        assert binding.get(None) is None
        assert not any(
            isinstance(value, Context) and binding in value
            for frame, _ in _sdk_traceback_frames(error.value)
            for value in frame.f_locals.values()
        )
        captured = _sdk_traceback_locals(error.value)
        assert not any(
            text in captured
            for text in (endpoint, TASK, PROMPT, answer, repr(claim.update.receipt))
        )
        assert (
            reservations.attempts == peer.counts["tasks/update"] == peer.counts["tasks/cancel"] == 0
        )


@pytest.mark.parametrize("action", ["respond", "cancel"])
async def test_control_uses_fresh_task_before_poll(tmp_path, reservations, monkeypatch, action):
    from _agents_codex.experimental import mcp_tasks

    monkeypatch.setattr(mcp_tasks, "time", SimpleNamespace(time=lambda: 1000.0))
    peer = Peer(tmp_path)
    peer.status = "input_required"
    async with serving(peer) as endpoint:
        backend = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=authorize,
            allow_input_replies=True,
            reserve_mutation=reservations,
        )
        claim = starting()
        seed = await backend.start(claim, Args(pages=3))
        peer.interval = 60000
        form = await backend.observe(replace(claim, action="observe", update=seed))
        assert form.value["poll_at"] == 1060
        claim = replace(claim, update=form, action=action)
        counts = peer.counts.copy()
        assert await backend.observe(replace(claim, action="observe")) is form
        assert peer.counts == counts
        reply = MCPInputReply(
            challenge=form.value["challenge"],
            responses={peer.input_key: ElicitResult(action="decline")},
        )
        if action == "respond":
            peer.input_key = "new-form"
            fresh = await backend.respond(claim, reply)
            assert fresh.status == "input_required" and fresh.value["challenge"] != reply.challenge
            assert reservations.attempts == peer.counts["tasks/update"] == 0
            reply = MCPInputReply(
                challenge=fresh.value["challenge"],
                responses={peer.input_key: ElicitResult(action="decline")},
            )
            result = await backend.respond(replace(claim, update=fresh), reply)
        else:
            result = await backend.cancel(claim)
        assert result.status == "pending" and result.receipt == form.receipt
        assert peer.counts["tasks/get"] - counts["tasks/get"] == (2 if action == "respond" else 1)
        assert reservations.attempts == 1
        assert peer.counts["tasks/update"] == (action == "respond")
        assert peer.counts["tasks/cancel"] == (action == "cancel")


@pytest.mark.parametrize("reflected", ["entry", "discovery", "list", "create", "unrelated"])
async def test_start_checks_each_issued_credential(tmp_path, reflected):
    class Credential(str):
        __hash__ = None

    labels = ("entry", "discovery", "list", "create")
    tokens = {label: "synthetic-access-" + label for label in labels}
    issued = iter(tokens.values())

    class RotatingPeer(Peer):
        def task(self, status):
            task = super().task(status)
            task["taskId"] = tokens.get(reflected, TASK)
            return task

    peer = RotatingPeer(tmp_path)
    peer.authorizations = {"Bearer " + token for token in tokens.values()}

    async def credential(claim, operation):
        return Credential(next(issued))

    async with serving(peer) as endpoint:
        backend = MCPTasksProvider(
            endpoint=endpoint,
            tool="render",
            authorize=credential,
            admit_pending=lambda *_: True,
        )
        update = await backend.start(starting(), Args(pages=3))
    assert list(issued) == [] and peer.creates == 1
    if reflected == "unrelated":
        assert update.status == "pending" and json.loads(update.receipt)[1] == TASK
    else:
        assert update.status == "unknown" and update.receipt is None and update.value is None


class _Credential(str):
    pass


async def _assert_credential_released(error, binding, reference):
    # Done callbacks may still be queued after a joined task finishes.
    await asyncio.sleep(0)
    gc.collect()
    for frame, _ in _sdk_traceback_frames(error):
        for value in frame.f_locals.values():
            if isinstance(value, Context):
                assert binding not in value
            if isinstance(value, asyncio.Task) and hasattr(value, "get_context"):
                context = value.get_context()
                assert context is None or not isinstance(context.get(binding), _Credential)
    assert reference() is None


@pytest.mark.parametrize("failure", ["initial", "discovery"])
async def test_retained_exchange_error_releases_credential_and_endpoint(failure):
    endpoint = "http://127.0.0.1:1/synthetic-private-route"
    binding = ContextVar("request-binding")
    reset = binding.set(None)
    references = []

    async def authorize_request(*_):
        if not references:
            credential = _Credential(TOKEN)
            references.append(weakref.ref(credential))
            binding.set(credential)
            if failure == "discovery":
                return credential
        raise PermissionError

    backend = MCPTasksProvider(endpoint=endpoint, tool="render", authorize=authorize_request)
    with pytest.raises(PermissionError) as error:
        await backend.start(starting(), Args(pages=3))
    message = (
        "MCP operation is not currently authorized"
        if failure == "initial"
        else "MCP request authorization denied"
    )
    assert error.value.args == (message,)
    assert error.value.__context__ is error.value.__cause__ is None
    assert binding.get() is references[0]()
    binding.reset(reset)
    assert binding.get(None) is None
    captured = _sdk_traceback_locals(error.value)
    assert endpoint not in captured and TOKEN not in captured
    await _assert_credential_released(error.value, binding, references[0])


async def test_retained_exchange_error_releases_credential_during_dns_cleanup(
    clean_egress, monkeypatch
):
    hostname = "synthetic-private-route.invalid"
    binding = ContextVar("request-binding")
    reset = binding.set(None)
    references = []
    release, finished = threading.Event(), threading.Event()
    loop, caller = asyncio.get_running_loop(), asyncio.current_task()

    def lookup(host, *args):
        assert host in (hostname, hostname.encode())
        loop.call_soon_threadsafe(caller.cancel, "original")
        release.wait(5)
        finished.set()
        raise socket.gaierror("unavailable")

    async def authorize_request(*_):
        if not references:
            credential = _Credential(TOKEN)
            references.append(weakref.ref(credential))
            binding.set(credential)
        assert binding.get() is references[0]()
        return TOKEN

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    backend = MCPTasksProvider(
        endpoint=f"https://{hostname}/mcp", tool="render", authorize=authorize_request
    )
    try:
        with pytest.raises(asyncio.CancelledError) as error:
            await backend.start(starting(), Args(pages=3))
        assert error.value.args == ("original",)
        assert binding.get() is references[0]()
        binding.reset(reset)
        assert hostname not in _sdk_traceback_locals(error.value)
        await _assert_credential_released(error.value, binding, references[0])
        assert not finished.is_set()
    finally:
        release.set()


@pytest.mark.parametrize("interrupt", ["timeout", "cancel"])
async def test_initial_authorization_cleanup_keeps_bearer_out_of_traceback(interrupt):
    marker = "synthetic-cleanup-bearer"
    binding = ContextVar("cleanup-binding", default="")
    entered = asyncio.Event()
    references = []

    async def credential(claim, action):
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            credential = _Credential(marker)
            references.append(weakref.ref(credential))
            binding.set(credential)
            return credential

    backend = MCPTasksProvider(
        endpoint="http://127.0.0.1:1",
        tool="render",
        authorize=credential,
        timeout_seconds=0.05 if interrupt == "timeout" else 5,
    )
    expected = asyncio.TimeoutError if interrupt == "timeout" else asyncio.CancelledError

    async def start():
        reset = binding.set("caller-binding")
        try:
            await backend.start(starting(), Args(pages=3))
        except expected as exc:
            assert binding.get() is references[0]()
            binding.reset(reset)
            assert binding.get() == ""
            assert not any(
                binding in value if isinstance(value, Context) else value is binding
                for frame, _ in _sdk_traceback_frames(exc)
                for value in frame.f_locals.values()
            )
            if interrupt == "cancel":
                assert exc.args == ("original",)
            assert marker not in _sdk_traceback_locals(exc)
            await _assert_credential_released(exc, binding, references[0])
            raise

    sending = asyncio.create_task(start())
    await entered.wait()
    if interrupt == "cancel":
        sending.cancel("original")
    with pytest.raises(expected) as error:
        await sending
    assert marker not in _sdk_traceback_locals(error.value)


def _sdk_traceback_frames(error):
    return (
        (frame, line)
        for frame, line in traceback.walk_tb(error.__traceback__)
        if frame.f_globals.get("__name__", "").startswith(("agents.", "_agents_codex."))
    )


def _sdk_traceback_locals(error):
    frames = list(_sdk_traceback_frames(error))
    assert frames, "No SDK traceback frames were inspected."
    return "".join(traceback.StackSummary.extract(frames, capture_locals=True).format())


@pytest.mark.parametrize("failure", ["stale", "denial", "cancel"])
async def test_failed_form_approval_keeps_answers_out_of_traceback(tmp_path, failure):
    marker = "synthetic-private-answer"
    peer = Peer(tmp_path)
    peer.status = "input_required"
    peer.form_field = {"type": "string", "enum": [marker]}
    entered = asyncio.Event()

    async def reserve(claim, response):
        entered.set()
        if failure == "cancel":
            await asyncio.Future()
        return False

    async with serving(peer) as endpoint:
        backend = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=authorize,
            allow_input_replies=True,
            reserve_mutation=reserve,
        )
        claim = await _observed_form(backend)
        answer = MCPInputReply(
            challenge=claim.update.value["challenge"],
            responses={peer.input_key: ElicitResult(action="accept", content={"color": marker})},
        )
        if failure == "stale":
            peer.input_key = "replacement"
            update = await backend.observe(replace(claim, action="observe"))
            claim = replace(claim, update=update)
        sending = asyncio.create_task(backend.respond(claim, answer))
        if failure == "cancel":
            await asyncio.wait_for(entered.wait(), 3)
            sending.cancel("original")
        with pytest.raises(
            asyncio.CancelledError if failure == "cancel" else PermissionError
        ) as error:
            await sending
        captured = _sdk_traceback_locals(error.value)
        assert not any(
            text in captured for text in (TASK, PROMPT, marker, repr(claim.update.receipt))
        )
        assert entered.is_set() == (failure != "stale")
        assert peer.counts["tasks/update"] == 0 and not peer.writes


@pytest.mark.parametrize("finish", ["success", "denial", "cancel"])
async def test_start_releases_issued_credentials(tmp_path, finish):
    class Credential(str):
        pass

    peer = Peer(tmp_path)
    entered = asyncio.Event()
    references = []

    async def credential(claim, operation):
        if peer.counts["tools/list"]:
            if finish == "denial":
                raise PermissionError
            if finish == "cancel":
                entered.set()
                await asyncio.Future()
        value = Credential(TOKEN)
        references.append(weakref.ref(value))
        return value

    async with timeout(8), serving(peer) as endpoint:
        backend = peer.provider(endpoint=endpoint, tool="render", authorize=credential)
        sending = asyncio.create_task(backend.start(starting(), Args(pages=3)))
        if finish == "success":
            assert (await sending).status == "pending"
        elif finish == "denial":
            with pytest.raises(PermissionError) as failure:
                await sending
        else:
            await entered.wait()
            sending.cancel()
            with pytest.raises(asyncio.CancelledError) as failure:
                await sending
        # Keep the provider and finished task alive, including its exception traceback.
        if finish != "success":
            frames = [
                (frame, line)
                for frame, line in traceback.walk_tb(failure.value.__traceback__)
                if frame.f_globals.get("__name__", "").startswith(("agents.", "_agents_codex."))
            ]
            assert frames, "No SDK traceback frames were inspected."
            summary = traceback.StackSummary.extract(frames, capture_locals=True)
            assert TOKEN not in "".join(summary.format())
        gc.collect()
        assert references and all(ref() is None for ref in references)


@pytest.mark.parametrize("trust_variable", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
async def test_bad_egress_configuration_does_not_consume_a_deferred_order(
    tmp_path, responses, monkeypatch, clean_egress, tls_files, trust_variable
):
    pytest.importorskip("fcntl")
    peer = Peer(tmp_path)
    directory = tmp_path / "application"
    responses.respond(function_call("render", {"pages": 3}, "original-mcp"))
    responses.respond(assistant_output("Report received."))
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tls_files["ca-A.pem"]))
    async with serving(peer, tls_files) as endpoint:
        monkeypatch.setenv(trust_variable, str(tmp_path / "synthetic-missing-private-ca"))
        with pytest.raises(PermissionError, match="MCP HTTPS transport configuration") as caught:
            await _restart_host(responses.url, endpoint, directory)
        assert caught.value.__cause__ is caught.value.__context__ is None
        assert "synthetic-missing-private-ca" not in repr(caught.value.args)
        assert not responses.requests and not peer.counts

        monkeypatch.setenv("SSL_CERT_FILE", str(tls_files["ca-A.pem"]))
        assert await _restart_host(responses.url, endpoint, directory) == "pending"
        assert await _restart_host(responses.url, endpoint, directory) == "completed"
        assert peer.creates == 1 and len(responses.requests) == 2


@pytest.mark.parametrize("lost", [False, True], ids=["completed-is-error", "lost-create"])
async def test_preview_manual_tool_whole_process_restart(tmp_path, responses, lost):
    import os
    import sqlite3
    import sys

    pytest.importorskip("fcntl")

    class EchoCreatePeer(Peer):
        async def audit(self, request, call_next):
            method = request.headers.get("mcp-method")
            if method == "tools/call":
                self.create_bearer = request.headers["authorization"]
            elif method == "tasks/get":
                assert request.headers["authorization"] == "Bearer " + READ_TOKEN
            return await super().audit(request, call_next)

        def result(self):
            result = super().result()
            result.content = [TextContent(type="text", text=self.create_bearer)]
            return result

    peer = EchoCreatePeer(tmp_path)
    peer.authorizations.add("Bearer " + READ_TOKEN)
    peer.mode = "error"
    peer.drop = "tools/call" if lost else ""
    responses.respond(function_call("render", {"pages": 3}, "original-mcp"))
    responses.respond(assistant_output("Report received."))
    directory = tmp_path / "application"
    public_log = ""
    async with timeout(35), serving(peer) as endpoint:
        for expected in ("unknown", "unknown") if lost else ("pending", "completed"):
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.test_deferred_mcp_tasks",
                responses.url,
                endpoint,
                str(directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=Path(__file__).resolve().parents[1],
                env={
                    **{k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"},
                    "PYTHONPATH": os.pathsep.join(sys.path),
                },
            )
            try:
                stdout, stderr = await proc.communicate()
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            public_log += stdout.decode() + stderr.decode()
            assert proc.returncode == 0, stderr.decode()
            assert stdout.decode().strip() == expected
    assert peer.creates == peer.counts["tools/call"] == 1
    assert peer.counts["tasks/get"] == (0 if lost else 1)
    assert peer.counts["tasks/result"] == 0
    assert len(responses.requests) == (1 if lost else 2)
    if not lost:
        assert json.loads(tool_output(responses.requests[1], "original-mcp")) == {
            "pages": 3,
            "is_error": True,
        }
        assert (
            sum(i.get("type") == "function_call_output" for i in responses.requests[1]["input"])
            == 1
        )
    with sqlite3.connect(directory / "orders.sqlite3") as db:
        checkpoint, finished, update = db.execute(
            "SELECT checkpoint, finished, receipt_update FROM orders"
        ).fetchone()
    assert (
        SessionState.from_json(checkpoint).native_continuation.pending_manual[0].call_id
        == "original-mcp"
    )
    assert peer.create_bearer == "Bearer " + TOKEN
    assert TOKEN not in (update or "") and READ_TOKEN not in (update or "")
    exported = public_log + json.dumps(responses.requests) + checkpoint + (finished or "")
    for private in (TASK, TOKEN, READ_TOKEN, HEADER, "private-business-key", PROMPT):
        assert private not in exported


if __name__ == "__main__":
    import sys

    print(asyncio.run(_restart_host(sys.argv[1], sys.argv[2], Path(sys.argv[3]), READ_TOKEN)))
