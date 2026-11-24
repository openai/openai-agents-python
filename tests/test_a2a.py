"""Loopback wire peers let us drop a reply after commit and control disconnects."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel
from tests.test_responses import get_function_tool_call, get_text_message

pytest.importorskip("a2a")

from a2a.types import Message, Task, a2a_pb2 as a2a

from agents.extensions.a2a import A2AClient, A2AError
from examples.a2a.manager import Receipt, Registry, UnresolvedCall, ask_specialist, find_specialists


def card(url):
    return {
        "name": "synthetic",
        "description": "Synthetic verdicts",
        "version": "1",
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
        "supportedInterfaces": [
            {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
    }


class Receiver:
    """The committed verdict is the business effect. Reads never recompute it."""

    def __init__(self, db: sqlite3.Connection, verdict: str, *, drop=False):
        self.db = db
        self.verdict = verdict
        self.drop = drop
        self.methods: list[str] = []
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS effects ("
            "owner TEXT, endpoint TEXT, message_id TEXT PRIMARY KEY, digest TEXT, "
            "task_id TEXT UNIQUE, context_id TEXT, verdict TEXT)"
        )
        self.app = web.Application()
        self.app.router.add_get("/card", self.discover)
        self.app.router.add_post("/rpc", self.rpc)

    def lookup(self, owner, endpoint, message_id):
        row = self.db.execute(
            "SELECT * FROM effects WHERE owner=? AND endpoint=? AND message_id=?",
            (owner, endpoint, message_id),
        ).fetchone()
        return Receipt(*row) if row else None

    async def discover(self, request):
        assert request.headers["Authorization"] == "Bearer synthetic-card"
        self.methods.append("card")
        response = web.json_response(card(str(request.url.with_path("/rpc"))))
        response.set_cookie("card-only", "synthetic")
        return response

    async def rpc(self, request):
        assert "Cookie" not in request.headers
        assert request.headers["A2A-Version"] == "1.0"
        if request.headers.get("Authorization") != "Bearer synthetic-rpc":
            raise web.HTTPUnauthorized()
        owner = "synthetic-owner"
        body = await request.json()
        assert body["jsonrpc"] == "2.0"
        method = body["method"]
        self.methods.append(method)
        if method == "SendMessage":
            message = body["params"]["message"]
            assert message["role"] == "ROLE_USER"
            question = message["parts"][0]["text"]
            task_id = uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO effects VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        owner,
                        str(request.url),
                        message["messageId"],
                        hashlib.sha256(question.encode()).hexdigest(),
                        task_id,
                        uuid4().hex,
                        self.verdict,
                    ),
                )
            if self.drop:
                request.transport.close()
                return web.Response()
        else:
            assert method == "GetTask"
            task_id = body["params"]["id"]

        context_id, verdict = self.db.execute(
            "SELECT context_id, verdict FROM effects WHERE task_id=? AND owner=? AND endpoint=?",
            (task_id, owner, str(request.url)),
        ).fetchone()
        task = {
            "id": task_id,
            "contextId": context_id,
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "verdict", "parts": [{"text": verdict}]}],
        }
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"task": task} if method == "SendMessage" else task,
            }
        )


def stored(url: str) -> A2AClient:
    return A2AClient(
        rpc_url=url,
        card_json=json.dumps(card(url)).encode(),
        rpc_token_provider=lambda: "synthetic-rpc",
    )


@pytest.mark.asyncio
async def test_direct_message_preserves_standard_fields_without_a_task():
    methods = []
    reply = {
        "messageId": "reply",
        "role": "ROLE_AGENT",
        "parts": [{"text": "yes"}, {"data": {"count": 1}}, {"text": ""}],
        "metadata": {"source": "example"},
        "referenceTaskIds": ["earlier"],
    }

    async def handle(request):
        body = await request.json()
        methods.append(body["method"])
        assert body["params"]["message"]["messageId"] == "outgoing"
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": {"message": reply}})

    app = web.Application()
    app.router.add_post("/rpc", handle)
    async with TestServer(app) as server:
        client = stored(str(server.make_url("/rpc")))
        message = await client.send_message("Ready?", message_id="outgoing")
        assert isinstance(message, Message)
        assert message.message_id == "reply" and not message.context_id
        assert message.parts[0].text == "yes"
        assert message.parts[1].WhichOneof("content") == "data"
        assert message.parts[2].WhichOneof("content") == "text"
        assert message.metadata["source"] == "example"
        assert list(message.reference_task_ids) == ["earlier"]
        with pytest.raises(A2AError, match="request"):
            await client.refresh_task(message)  # type: ignore[arg-type]
        assert methods == ["SendMessage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "id", "contextId"])
async def test_refresh_task_reads_once_and_checks_both_ids(mismatch):
    methods = []

    async def handle(request):
        body = await request.json()
        methods.append(body["method"])
        task = {"id": "one", "contextId": "group", "status": {"state": "TASK_STATE_WORKING"}}
        if body["method"] == "SendMessage":
            result = {"task": task}
        else:
            assert body["params"] == {"id": "one", "historyLength": 0}
            if mismatch:
                task[mismatch] = "private"
            result = task
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})

    app = web.Application()
    app.router.add_post("/rpc", handle)
    async with TestServer(app) as server:
        client = stored(str(server.make_url("/rpc")))
        pending = await client.send_message("Ready?", message_id=uuid4().hex)
        assert isinstance(pending, Task)
        assert methods == ["SendMessage"]
        if mismatch:
            with pytest.raises(A2AError, match="^A2A protocol error$"):
                await client.refresh_task(pending)
        else:
            update = await client.refresh_task(pending)
            assert update is not pending
            assert update.status.state == a2a.TASK_STATE_WORKING
        assert (pending.id, pending.context_id) == ("one", "group")
        assert methods == ["SendMessage", "GetTask"]


def ask(name: str, call_id: str, question: str = "Ready?"):
    return get_function_tool_call(
        "ask_specialist", json.dumps({"name": name, "question": question}), call_id=call_id
    )


async def run_manager(registry, turns):
    agent = Agent(
        name="manager",
        tools=[find_specialists, ask_specialist],
        model=ScriptedModel([*turns, [get_text_message("Manager finished")]]),
    )
    result = await Runner.run(
        agent, "Check readiness", context=registry, run_config=RunConfig(tracing_disabled=True)
    )
    assert result.last_agent is agent
    assert result.final_output == "Manager finished"
    return {
        item["call_id"]: item["output"]
        for item in result.to_input_list()
        if item.get("type") == "function_call_output"
    }


@pytest.mark.asyncio
async def test_manager_filters_500_names_and_calls_two_hosts(tmp_path: Path):
    with (
        closing(sqlite3.connect(tmp_path / "east")) as east_db,
        closing(sqlite3.connect(tmp_path / "west")) as west_db,
        closing(sqlite3.connect(tmp_path / "manager")) as db,
    ):
        east, west = Receiver(east_db, "yes"), Receiver(west_db, "no")
        async with TestServer(east.app) as a, TestServer(west.app) as b:
            names = {f"other-{i}": stored(f"http://127.0.0.1:1/{i}") for i in range(498)}
            names["weather-east"] = stored(str(a.make_url("/rpc")))
            names["weather-west"] = A2AClient(
                card_url=str(b.make_url("/card")),
                card_token_provider=lambda: "synthetic-card",
                rpc_url=str(b.make_url("/rpc")),
                rpc_token_provider=lambda: "synthetic-rpc",
            )

            def lookup(owner, url, message):
                return east.lookup(owner, url, message) or west.lookup(owner, url, message)

            registry = Registry("synthetic-owner", "run", names, db, lookup)
            outputs = await run_manager(
                registry,
                [
                    [
                        get_function_tool_call(
                            "find_specialists", '{"prefix":"weather-"}', call_id="list"
                        )
                    ],
                    [ask("weather-west", "one"), ask("weather-east", "two")],
                ],
            )
            assert "weather-west" in str(outputs["list"]) and "weather-east" in str(outputs["list"])
            assert "other-" not in str(outputs["list"])
            assert outputs["one"] == "no" and outputs["two"] == "yes"
            east_only = replace(registry, specialists={"weather-east": names["weather-east"]})
            assert east_only.reserved_calls() == [("two", "weather-east")]
            assert east.methods == ["SendMessage"]
            assert west.methods == ["card", "SendMessage"]
            assert await run_manager(registry, [[ask("weather-east", "two")]]) == {"two": "yes"}
            assert east.methods == ["SendMessage", "GetTask"]


@pytest.mark.asyncio
async def test_lost_ack_restart_verifies_receipt_and_projects_original_result(tmp_path: Path):
    def registry(db, peer, url):
        return Registry("synthetic-owner", "run", {"east": stored(url)}, db, peer.lookup)

    with (
        closing(sqlite3.connect(tmp_path / "receiver")) as remote,
        closing(sqlite3.connect(tmp_path / "manager")) as db,
    ):
        peer = Receiver(remote, "yes", drop=True)
        async with TestServer(peer.app) as server:
            port = server.port
            url = str(server.make_url("/rpc"))
            with pytest.raises(A2AError) as err:
                await run_manager(
                    registry(db, peer, url), [[ask("east", uuid4().hex, f"Is {uuid4()} ready?")]]
                )
            assert err.value.reason == "transport"
            assert err.value.run_data is not None
            assert err.value.__cause__ is None and err.value.__context__ is None
            assert peer.methods == ["SendMessage"]

    with (
        closing(sqlite3.connect(tmp_path / "receiver")) as remote,
        closing(sqlite3.connect(tmp_path / "manager")) as db,
    ):
        peer = Receiver(remote, "no")
        async with TestServer(peer.app, port=port):
            restored = registry(db, peer, url)
            [(call_id, name)] = restored.reserved_calls()
            assert name == "east"
            with pytest.raises(PermissionError):
                await restored.ask(call_id, "east", "Different?")
            for denied in (
                replace(restored, specialists={}),
                replace(restored, specialists={"east": stored(url + "/other")}),
            ):
                assert denied.reserved_calls() == []
                with pytest.raises(PermissionError):
                    await denied.recover(call_id)
            for other in (replace(restored, owner="foreign"), replace(restored, run_id="other")):
                assert other.reserved_calls() == []
                with pytest.raises(UnresolvedCall):
                    await other.recover(call_id)
            with pytest.raises(UnresolvedCall):
                await restored.recover("missing")
            receipt = Receipt(*remote.execute("SELECT * FROM effects").fetchone())
            for invalid in (
                None,
                replace(receipt, owner="foreign"),
                replace(receipt, digest="wrong"),
            ):
                with pytest.raises(UnresolvedCall):
                    await replace(
                        restored, lookup_receipt=lambda *args, value=invalid: value
                    ).recover(call_id)
            assert peer.methods == []
            assert await restored.recover(call_id) == "yes"
            assert peer.methods == ["GetTask"]
            assert remote.execute("SELECT count(*) FROM effects").fetchone() == (1,)
            assert db.execute("SELECT count(*) FROM calls").fetchone() == (1,)
            with pytest.raises(UnresolvedCall, match="verdict"):
                await replace(
                    restored, lookup_receipt=lambda *_: replace(receipt, verdict="no")
                ).recover(call_id)
            with pytest.raises(A2AError, match="protocol"):
                await stored(url).get_task(task_id=receipt.task_id, context_id="wrong")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["redirect-card", "redirect-rpc", "foreign", "large", "encoded", "rpc-id", "private-error"],
)
async def test_untrusted_responses_never_reroute_or_expose_credentials(fault):
    calls = []

    def rpc_token_provider():
        calls.append("grant")
        return "synthetic-rpc"

    async def handle(request):
        calls.append(request.path)
        if request.path == "/card":
            assert request.headers["Authorization"] == "Bearer synthetic-card"
            if fault == "redirect-card":
                raise web.HTTPTemporaryRedirect("/unapproved")
            if fault == "large":
                return web.Response(body=b" " * 1_048_576 + json.dumps(card(url)).encode())
            if fault == "encoded":
                return web.Response(headers={"Content-Encoding": "gzip"})
            return web.json_response(card(url + "/other" if fault == "foreign" else url))
        if fault == "redirect-rpc":
            raise web.HTTPTemporaryRedirect("/unapproved")
        body = await request.json()
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": "wrong" if fault == "rpc-id" else body["id"],
                "error": {"code": -32603, "message": "synthetic-rpc"},
            }
        )

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with TestServer(app) as server:
        url = str(server.make_url("/rpc"))
        client = A2AClient(
            rpc_url=url,
            card_url=str(server.make_url("/card")),
            card_token_provider=lambda: "synthetic-card",
            rpc_token_provider=rpc_token_provider,
        )
        with pytest.raises(A2AError) as err:
            await client.send_message("Ready?", message_id="request")
        assert err.value.reason == (
            "remote" if fault.startswith("redirect") or fault == "private-error" else "protocol"
        )
        assert "synthetic" not in str(err.value)
        assert err.value.__context__ is None and err.value.__cause__ is None
        assert calls == (
            ["/card", "grant", "/rpc"]
            if fault in ("redirect-rpc", "rpc-id", "private-error")
            else ["/card"]
        )


@pytest.mark.asyncio
async def test_timeout_and_local_cancellation_do_not_replay_or_cancel_remote_work(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    methods = []

    async def hold(request):
        methods.append((await request.json())["method"])
        started.set()
        await release.wait()
        return web.Response()

    app = web.Application()
    app.router.add_post("/rpc", hold)
    async with TestServer(app) as server:
        endpoint = stored(str(server.make_url("/rpc")))
        with closing(sqlite3.connect(tmp_path / "manager")) as db:
            registry = Registry("owner", "run", {"east": endpoint}, db, lambda *_: None)
            first = asyncio.create_task(registry.ask("call", "east", "Ready?"))
            try:
                await asyncio.wait_for(started.wait(), 5)
                with pytest.raises(UnresolvedCall):
                    await registry.ask("call", "east", "Ready?")
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                with pytest.raises(UnresolvedCall):
                    await registry.ask("call", "east", "Ready?")
                assert methods == ["SendMessage"]
                with pytest.raises(A2AError, match="timeout"):
                    await replace(endpoint, timeout=0.05).get_task(
                        task_id="task", context_id="context"
                    )
                assert "CancelTask" not in methods
            finally:
                release.set()
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_invalid_endpoint_and_oversize_request_fail_locally():
    with pytest.raises(ValueError):
        stored("http://service.example/rpc")
    endpoint = stored("https://service.example/rpc")
    with pytest.raises(A2AError, match="request"):
        await endpoint.send_message("é" * 40000, message_id="message")


@pytest.mark.asyncio
async def test_unavailable_grant_fails_without_retaining_supplier_error():
    def deny():
        raise PermissionError("synthetic-private-detail")

    client = replace(stored("http://127.0.0.1:1/rpc"), rpc_token_provider=deny)
    with pytest.raises(A2AError) as err:
        await client.send_message("Ready?", message_id="one")
    assert err.value.reason == "authorization"
    assert err.value.__context__ is None and err.value.__cause__ is None


@pytest.mark.parametrize("field", ["rpc_token_provider", "card_token_provider"])
def test_token_providers_require_callbacks_without_echoing_values(field):
    options = {field: "private-value"}
    with pytest.raises(TypeError, match=field) as exc:
        A2AClient(rpc_url="https://example.org/rpc", card_url="https://example.org/card", **options)
    assert "private-value" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["id", "role", "parts", "content", "missing", "unknown", "both", "error", "correlation"],
)
async def test_invalid_message_or_envelope_fails_without_replay(fault):
    methods = []

    async def handle(request):
        body = await request.json()
        methods.append(body["method"])
        message = {
            "messageId": "" if fault == "id" else "reply",
            "role": "ROLE_USER" if fault == "role" else "ROLE_AGENT",
            "parts": []
            if fault == "parts"
            else [{} if fault == "content" else {"text": "private"}],
        }
        result = {"message": message}
        if fault == "missing":
            result = {}
        elif fault == "unknown":
            result = {"other": message}
        elif fault == "both":
            result["task"] = {
                "id": "1",
                "contextId": "c",
                "status": {"state": "TASK_STATE_WORKING"},
            }
        envelope = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if fault == "error":
            envelope["error"] = {"code": -32603, "message": "private"}
        elif fault == "correlation":
            envelope["id"] = "wrong"
        return web.json_response(envelope)

    app = web.Application()
    app.router.add_post("/rpc", handle)
    async with TestServer(app) as server:
        client = stored(str(server.make_url("/rpc")))
        with pytest.raises(A2AError) as exc:
            await client.send_message("Ready?", message_id="outgoing")
        assert exc.value.reason == "protocol"
        assert "private" not in str(exc.value)
        assert exc.value.__cause__ is None and exc.value.__context__ is None
        assert methods == ["SendMessage"]


@pytest.mark.asyncio
async def test_direct_message_keeps_the_registry_reservation():
    methods = []

    async def handle(request):
        body = await request.json()
        methods.append(body["method"])
        message = {"messageId": "reply", "role": "ROLE_AGENT", "parts": [{"text": "yes"}]}
        return web.json_response(
            {"jsonrpc": "2.0", "id": body["id"], "result": {"message": message}}
        )

    app = web.Application()
    app.router.add_post("/rpc", handle)
    async with TestServer(app) as server:
        with closing(sqlite3.connect(":memory:")) as db:
            peer = stored(str(server.make_url("/rpc")))
            registry = Registry("owner", "run", {"east": peer}, db, lambda *_: None)
            with pytest.raises(UnresolvedCall, match="requires a task"):
                await registry.ask("call", "east", "Ready?")
            with pytest.raises(UnresolvedCall):
                await registry.ask("call", "east", "Ready?")
            assert registry.reserved_calls() == [("call", "east")]
            assert methods == ["SendMessage"]
