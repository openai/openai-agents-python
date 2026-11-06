"""One process owns admission, Sessions, live approvals, and application storage."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from agents import (
    ActionRequested,
    Agent,
    Completed,
    FunctionTool,
    HttpMCP,
    InteractionExpiredError,
    NativeProvider,
    ToolContext,
    TurnStoppedError,
    function_tool,
)
from agents.observation import EventGap
from agents.state import SessionState
from agents.testing import ScriptedResponses, text, tool_call

from . import mcp_peer
from .store import TERMINAL, Store


@dataclass(frozen=True)
class Identity:
    user: Literal["alice", "bob"]


@dataclass(frozen=True)
class RunContext:
    identity: Identity
    receipt: str
    service: Service


class PublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    xml: str = Field(max_length=4096)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approved: bool


class Final(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(max_length=80)
    summary: str = Field(max_length=300)


publish_report = FunctionTool(
    name="publish_report",
    description="Request user approval to publish validated report XML.",
    input_type=PublishInput,
    output_type=ApprovalDecision,
)


@function_tool
async def read_report(ctx: ToolContext[RunContext]) -> str:
    """Read the authenticated user's synthetic report; return a bounded summary."""
    scope = ctx.context
    scope.service.authorize(scope.identity)
    # Full output stays in application storage. No model-selected identity or path.
    rows = [{"label": f"Synthetic row {i}", "value": i} for i in range(10000)]
    raw = json.dumps({"owner": scope.identity.user, "rows": rows}).encode()
    handle = scope.service.store.put(scope.identity.user, scope.receipt, "report", raw)
    scope.service.store.update(
        scope.identity.user,
        scope.receipt,
        card={
            "handle": handle,
            "title": "Synthetic report",
            "row_count": len(rows),
            "preview": rows[:3],
        },
    )
    return json.dumps(
        {"handle": handle, "row_count": len(rows), "summary": "Synthetic values 0–9999"}
    )


@dataclass
class Work:
    identity: Identity
    receipt: str
    deadline: float
    stop: asyncio.Event
    checkpoint: str | None


@dataclass
class Pending:
    work: Work
    request: ActionRequested
    done: asyncio.Event
    failed: bool = False


class Service:
    def __init__(self, directory: Path, *, workers=1, queue_size=2, deadline=60.0, live=False):
        if workers < 1 or queue_size < 1 or deadline <= 0:
            raise ValueError("Positive worker, queue and deadline limits required")
        if live and not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("Live mode requires OPENAI_API_KEY in the server environment")
        self.live = live
        self.store = Store(directory)
        self.queue: asyncio.Queue[Work] = asyncio.Queue(queue_size)
        self.worker_count = workers
        self.deadline = deadline
        self.work: dict[str, Work] = {}
        self.pending: dict[str, Pending] = {}
        self.revoked: set[str] = set()
        self.logins: dict[str, Identity] = {}
        self.tasks: list[asyncio.Task] = []
        self.closing = False
        self.credentials = {
            user: "synthetic-" + secrets.token_urlsafe(24) for user in ("alice", "bob")
        }
        self.mcp_runner = None
        self.mcp_url = ""

    def authorize(self, identity):
        if identity.user in self.revoked:
            raise web.HTTPForbidden(text="Identity revoked")

    def mcp_for(self, identity):
        """Trusted server configuration only; never accept URL/token from a browser/model.

        A declaration captures this credential for its Session. This callback
        checks local revocation before selected tool dispatch, not on every HTTP
        request, discovery or an effect already running on the remote server.
        """
        self.authorize(identity)

        async def approval(context, call):
            return context.context.identity == identity and identity.user not in self.revoked

        return HttpMCP[RunContext](
            name="records",
            url=self.mcp_url,
            bearer_token=self.credentials[identity.user],
            tools=["read_record"],
            approval=approval,
        )

    async def start(self):
        app = web.Application(client_max_size=16384)
        app["service"] = self
        app.router.add_post("/mcp", mcp_peer.serve)
        self.mcp_runner = web.AppRunner(app, access_log=None)
        await self.mcp_runner.setup()
        site = web.TCPSite(self.mcp_runner, "127.0.0.1", 0)
        await site.start()
        port = self.mcp_runner.addresses[0][1]
        self.mcp_url = f"http://127.0.0.1:{port}/mcp"
        self.tasks = [asyncio.create_task(self.worker()) for _ in range(self.worker_count)]

    async def close(self):
        self.closing = True
        for work in self.work.values():
            work.stop.set()
        await self.queue.join()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.mcp_runner is not None:
            await self.mcp_runner.cleanup()
        self.store.close()

    def submit(self, identity, key, prompt, checkpoint=None):
        self.authorize(identity)
        payload = json.dumps({"prompt": prompt, "checkpoint": checkpoint}, sort_keys=True)
        existing = self.store.find(identity.user, key, payload)
        if existing is not None:
            return existing
        if self.closing or self.queue.full():
            raise web.HTTPServiceUnavailable(text="Admission full; retry this same key later")
        if checkpoint is not None:
            self.store.get(identity.user, checkpoint, "checkpoint")
        state = self.store.create(
            identity.user, key, payload, prompt=prompt, mode="live" if self.live else "scripted"
        )
        work = Work(
            identity, state["id"], time.monotonic() + self.deadline, asyncio.Event(), checkpoint
        )
        self.work[work.receipt] = work
        self.queue.put_nowait(work)
        return state

    async def worker(self):
        while True:
            work = await self.queue.get()
            try:
                await self.run(work)
            finally:
                self.work.pop(work.receipt, None)
                self.queue.task_done()

    async def run(self, work):
        owner, receipt = work.identity.user, work.receipt
        outcome = "error"
        try:
            if work.stop.is_set():
                outcome = "cancelled"
                return
            self.authorize(work.identity)
            remaining = work.deadline - time.monotonic()
            if remaining <= 0:
                outcome = "expired"
                return
            self.store.update(owner, receipt, status="running")
            await asyncio.wait_for(self.execute(work), remaining)
            outcome = "completed"
        except asyncio.TimeoutError:
            outcome = "expired"
        except TurnStoppedError:
            outcome = "cancelled"
        except Exception:
            # Do not serialize exception messages, arguments, credentials or chains.
            outcome = "error"
        finally:
            for key, pending in list(self.pending.items()):
                if pending.work is work:
                    del self.pending[key]
            self.store.update(owner, receipt, status=outcome, approval=None)

    async def execute(self, work):
        owner, receipt = work.identity.user, work.receipt
        # One synthetic provider per run avoids a shared concurrent script queue.
        async with AsyncExitStack() as stack:
            if self.live:
                provider = await stack.enter_async_context(NativeProvider())
            else:
                peer = await stack.enter_async_context(
                    ScriptedResponses(
                        [tool_call("read_report", {}, call_id="read")],
                        [
                            tool_call(
                                "publish_report",
                                {"xml": "<presentation><slide>Report</slide></presentation>"},
                                call_id="publish",
                            )
                        ],
                        [
                            text(
                                '{"title":"Report complete",'
                                '"summary":"Reviewed 10000 synthetic rows."}'
                            )
                        ],
                    )
                )
                provider = peer.provider
            agent = Agent[RunContext, Final](
                instructions=(
                    "Read the report. You may read the authenticated MCP record too. "
                    "Request publication with publish_report using XML with a presentation root "
                    "and slide children. Its approved result acknowledges the user decision; "
                    "the application owns publication. Return a short report summary."
                ),
                tools=[
                    read_report,
                    publish_report,
                    self.mcp_for(work.identity),
                ],
                output_type=Final,
                provider=provider,
            )
            state = None
            if work.checkpoint is not None:
                state = SessionState.from_json(
                    self.store.get(owner, work.checkpoint, "checkpoint").decode()
                )
            session = await stack.enter_async_context(
                agent.session(context=RunContext(work.identity, receipt, self), state=state)
            )
            turn = await session.submit(self.store.snapshot(owner, receipt)["prompt"])
            observer = asyncio.create_task(self.observe(work, turn))
            stopper = asyncio.create_task(self.stop_when_requested(work, turn))
            try:
                result = await self.drive(work, turn)
                # Settled checkpoints only. Browser receives an opaque ID, never bytes.
                checkpoint = self.store.put(
                    owner, receipt, "checkpoint", (await session.save_state()).to_json().encode()
                )
                self.store.update(
                    owner, receipt, final=result.final_output.model_dump(), checkpoint=checkpoint
                )
            finally:
                for task in (observer, stopper):
                    task.cancel()
                await asyncio.gather(observer, stopper, return_exceptions=True)
                # Session exit joins actual work before capacity is released.

    async def observe(self, work, turn):
        events = turn.events()
        try:
            async for event in events:
                if isinstance(event, EventGap):
                    self.store.update(work.identity.user, work.receipt, resync_required=True)
        finally:
            await events.aclose()

    async def stop_when_requested(self, work, turn):
        await work.stop.wait()
        await turn.cancel()
        for pending in self.pending.values():
            if pending.work is work:
                pending.done.set()

    async def drive(self, work, turn):
        while True:
            result = await turn.wait_for_request_or_result()
            if isinstance(result, Completed):
                return result
            if work.stop.is_set():
                return await turn.get_result()
            for event in result.requests:
                request = publish_report.match(event)
                if request is None:
                    raise RuntimeError("Unsupported interaction")
                token = secrets.token_urlsafe(24)
                pending = Pending(work, request, asyncio.Event())
                self.pending[token] = pending
                self.store.update(
                    work.identity.user,
                    work.receipt,
                    status="pending",
                    approval={"id": token, "label": "Publish report XML?"},
                )
                await pending.done.wait()
                if pending.failed:
                    raise RuntimeError("Application approval or publication failed")
                if work.stop.is_set():
                    return await turn.get_result()
                self.store.update(work.identity.user, work.receipt, status="running", approval=None)

    async def reply(self, identity, receipt, token, allow):
        self.authorize(identity)
        pending = self.pending.get(token)
        if pending is None or pending.work.identity != identity or pending.work.receipt != receipt:
            raise web.HTTPConflict(text="Foreign, stale or already consumed approval")
        work = pending.work
        if work.stop.is_set() or time.monotonic() >= work.deadline:
            raise web.HTTPConflict(text="Approval expired")
        xml = pending.request.arguments.xml
        if allow:
            try:
                document = ElementTree.fromstring(xml)
            except ElementTree.ParseError:
                raise web.HTTPUnprocessableEntity(text="Invalid report XML") from None
            if document.tag != "presentation" or not document.findall("slide"):
                raise web.HTTPUnprocessableEntity(text="Expected presentation with a slide")
            # Serialize the validated tree without retaining an incompatible declaration.
            # Escape CR so an XML reader does not normalize referenced CR text to LF.
            content = ElementTree.tostring(document, encoding="utf-8").replace(b"\r", b"&#13;")
        # Consume before any await. Native grant acceptance precedes the effect:
        # a retained registry token alone does not prove the invocation is live.
        del self.pending[token]
        try:
            await pending.request.respond(ApprovalDecision(approved=allow))
            self.authorize(identity)
            if work.stop.is_set() or time.monotonic() >= work.deadline:
                raise web.HTTPConflict(text="Approval expired before publication")
            if allow:
                # No await between revalidation and the application effect.
                handle = self.store.put(identity.user, receipt, "artifact", content)
                self.store.update(
                    identity.user,
                    receipt,
                    artifact={
                        "id": handle,
                        "media_type": "application/xml",
                        "filename": "report.xml",
                    },
                )
        except BaseException:
            pending.failed = True
            raise
        finally:
            pending.done.set()


@web.middleware
async def boundary(request, handler):
    if request.host.split(":")[0] not in {"127.0.0.1", "localhost"}:
        raise web.HTTPForbidden(text="Loopback Host required")
    if request.method == "POST":
        if request.headers.get("X-Web-Starter") != "1":
            raise web.HTTPForbidden(text="Same-origin application request required")
        origin = request.headers.get("Origin")
        if origin is not None and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="Foreign origin")
    try:
        return await handler(request)
    except KeyError:
        raise web.HTTPNotFound(text="Not found or expired") from None
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Invalid request or conflicting request key") from None
    except web.HTTPException:
        raise
    except Exception:
        # Return a fixed response; do not log or retain the original exception.
        return web.Response(status=500, text="Application operation failed")


def identity_for(request):
    service = request.app["service"]
    identity = service.logins.get(request.cookies.get("demo_session"))
    if identity is None:
        raise web.HTTPUnauthorized(text="Choose a demo user first")
    service.authorize(identity)
    return identity


async def login(request):
    data = await request.json()
    user = data.get("user")
    if user not in {"alice", "bob"}:
        raise web.HTTPBadRequest(text="Choose alice or bob")
    service = request.app["service"]
    identity = Identity(user)
    service.authorize(identity)
    token = secrets.token_urlsafe(32)
    service.logins[token] = identity
    response = web.json_response({"user": user, "mode": "live" if service.live else "scripted"})
    response.set_cookie("demo_session", token, httponly=True, samesite="Strict")
    return response


async def submit(request):
    identity = identity_for(request)
    data = await request.json()
    key, prompt = data.get("key"), data.get("prompt")
    checkpoint = data.get("checkpoint")
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= 100
        or not isinstance(prompt, str)
        or not 1 <= len(prompt) <= 1000
        or (checkpoint is not None and not isinstance(checkpoint, str))
    ):
        raise web.HTTPBadRequest(text="Expected bounded key, prompt and optional checkpoint ID")
    return web.json_response(request.app["service"].submit(identity, key, prompt, checkpoint))


async def snapshot(request):
    identity = identity_for(request)
    return web.json_response(
        request.app["service"].store.snapshot(identity.user, request.match_info["receipt"])
    )


async def stop(request):
    identity = identity_for(request)
    service = request.app["service"]
    receipt = request.match_info["receipt"]
    state = service.store.snapshot(identity.user, receipt)
    if work := service.work.get(receipt):
        work.stop.set()
        # UI reports "stopping" until the owner has joined cleanup.
        state = service.store.update(identity.user, receipt, status="stopping")
    return web.json_response(state)


async def reply(request):
    identity = identity_for(request)
    service = request.app["service"]
    receipt = request.match_info["receipt"]
    service.store.snapshot(identity.user, receipt)
    body = await request.json()
    if not isinstance(body.get("id"), str) or type(body.get("allow")) is not bool:
        raise web.HTTPBadRequest(text="Expected approval ID and boolean decision")
    try:
        await service.reply(identity, receipt, body["id"], body["allow"])
    except InteractionExpiredError:
        raise web.HTTPConflict(text="Native approval expired") from None
    return web.json_response({"accepted": True})


async def object_response(request):
    identity = identity_for(request)
    service = request.app["service"]
    kind = request.match_info["kind"]
    if kind not in {"report", "artifact", "checkpoint"}:
        raise web.HTTPNotFound()
    content = service.store.get(identity.user, request.match_info["handle"], kind)
    if kind == "checkpoint":
        # Validate access without exporting a credential-bearing native checkpoint.
        return web.json_response({"available": True, "restore": "Supply this ID in a new request"})
    if kind == "report":
        return web.Response(body=content, content_type="application/json")
    return web.Response(
        body=content,
        content_type="application/xml",
        headers={
            "Content-Disposition": 'attachment; filename="report.xml"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def sse(value):
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


async def stream(request):
    identity = identity_for(request)
    service = request.app["service"]
    receipt = request.match_info["receipt"]
    service.store.snapshot(identity.user, receipt)
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "x-vercel-ai-ui-message-stream": "v1",
            "Cache-Control": "no-store",
        }
    )
    await response.prepare(request)
    try:
        await response.write(sse({"type": "start"}))
        revision = -1
        while True:
            service.authorize(identity)
            state = service.store.snapshot(identity.user, receipt)
            if state["revision"] != revision:
                # Only app-selected fields enter the stream; no raw SDK JSON/deltas.
                await response.write(
                    sse({"type": "data-snapshot", "id": "snapshot", "data": state})
                )
                revision = state["revision"]
            if state["status"] in TERMINAL:
                break
            await asyncio.sleep(0.1)
        await response.write(sse({"type": "finish"}))
        await response.write(b"data: [DONE]\n\n")
    except (ConnectionError, asyncio.CancelledError):
        # Disconnect releases this reader. The worker still owns its Session.
        pass
    return response


async def index(request):
    return web.FileResponse(Path(__file__).with_name("index.html"))


async def bundle(request):
    path = Path(__file__).with_name("client.js")
    if not path.is_file():
        raise web.HTTPServiceUnavailable(
            text="Run npm ci && npm run build in examples/codex/web_starter"
        )
    return web.FileResponse(path)


def create_app(directory: Path, **limits):
    service = Service(directory, **limits)
    app = web.Application(middlewares=[boundary], client_max_size=8192)
    app["service"] = service
    app.router.add_get("/", index)
    app.router.add_get("/client.js", bundle)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/receipts", submit)
    app.router.add_get("/api/receipts/{receipt}", snapshot)
    app.router.add_post("/api/receipts/{receipt}/stop", stop)
    app.router.add_post("/api/receipts/{receipt}/reply", reply)
    app.router.add_post("/api/receipts/{receipt}/stream", stream)
    app.router.add_get("/api/objects/{kind}/{handle}", object_response)

    async def lifecycle(app):
        try:
            await service.start()
            yield
        finally:
            await service.close()

    app.cleanup_ctx.append(lifecycle)
    return app
