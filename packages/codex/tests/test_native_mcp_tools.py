"""Native HTTP MCP admission, calls, replacement, and current restore authority."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    Allow,
    HttpMCP,
    MCPCallContext,
    NativeError,
    SessionBusyError,
    StopTurn,
    ToolContext,
    TurnStoppedError,
    function_tool,
)
from agents.observation import ToolStarted
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_agent_observation import collect


@dataclass
class Peer:
    names: tuple[str, ...] = ("search", "excluded")
    label: str = "native-receipt"
    requests: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    auth: list[str | None] = field(default_factory=list)
    call_entered: asyncio.Event = field(default_factory=asyncio.Event)
    release_call: asyncio.Event | None = None
    ready_entered: asyncio.Event = field(default_factory=asyncio.Event)
    release_ready: asyncio.Event | None = None
    denied: bool = False
    annotations: dict[str, bool] | None = field(default_factory=lambda: {"readOnlyHint": True})
    is_error: bool = False
    call_result: dict[str, Any] | None = None

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        self.auth.append(request.headers.get("Authorization"))
        if self.denied:
            return web.Response(status=401)
        if "id" not in body:
            return web.Response(status=202)
        method = body["method"]
        if method == "initialize":
            self.ready_entered.set()
            if self.release_ready is not None:
                await self.release_ready.wait()
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": "Read one synthetic record",
                        **(
                            {"annotations": self.annotations}
                            if self.annotations is not None
                            else {}
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {"key": {"type": "string"}},
                            "required": ["key"],
                            "additionalProperties": False,
                        },
                    }
                    for name in self.names
                ]
            }
        elif method == "tools/call":
            self.calls.append(body["params"])
            self.call_entered.set()
            if self.release_call is not None:
                await self.release_call.wait()
            result = (
                self.call_result
                if self.call_result is not None
                else {
                    "content": [{"type": "text", "text": self.label}],
                    "structuredContent": {"receipt": self.label},
                    "isError": self.is_error,
                }
            )
        else:
            result = {}
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})

    @asynccontextmanager
    async def serve(self):
        app = web.Application()
        app.router.add_post("/mcp", self.handle)
        server = TestServer(app)
        try:
            await server.start_server(shutdown_timeout=1)
            yield str(server.make_url("/mcp"))
        finally:
            await server.close()


async def test_mcp_peer_shutdown_cancels_incomplete_request(monkeypatch):
    peer = Peer()
    entered = asyncio.Event()
    finished = asyncio.Event()
    original = peer.handle

    async def track_request(request):
        entered.set()
        try:
            return await original(request)
        finally:
            finished.set()

    monkeypatch.setattr(peer, "handle", track_request)
    async with timeout(10):
        context = peer.serve()
        url = urlsplit(await context.__aenter__())
        writer = None
        closing = None
        try:
            _, writer = await asyncio.open_connection(url.hostname, url.port)
            writer.write(
                b"POST /mcp HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"
            )
            await writer.drain()
            await entered.wait()
            closing = asyncio.create_task(context.__aexit__(None, None, None))
            await asyncio.wait_for(asyncio.shield(closing), 5)
            assert finished.is_set()
        finally:
            if writer is not None:
                writer.close()
                await asyncio.gather(writer.wait_closed(), return_exceptions=True)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            else:
                await context.__aexit__(None, None, None)


def mcp_call(name: str, namespace: str, call_id: str):
    event = function_call(name, {"key": "43"}, call_id)
    event["item"]["namespace"] = namespace
    return event


def declarations(request):
    tools = list(request.get("tools", []))
    for item in request["input"]:
        if item.get("type") == "additional_tools":
            tools.extend(item["tools"])
    return {
        (group["name"], function_tool["name"]): function_tool
        for group in tools
        if group["type"] == "namespace"
        for function_tool in group["tools"]
    } | {
        ("functions", function_tool["name"]): function_tool
        for function_tool in tools
        if function_tool["type"] == "function"
    }


@dataclass
class Context:
    secret: str = "python-context-private"
    calls: list[str] = field(default_factory=list)


async def test_native_mixed_wire_identity_schema_history_and_python_slots(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("find-record", "find_record", "查找"))
    context = Context()

    @function_tool(name="mcp__docsfind_record", callback_timeout=2)
    async def local(ctx: ToolContext[Context], key: str) -> str:
        ctx.context.calls.append(ctx.call_id)
        return "python-receipt:" + key

    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(name="docs", url=url, tools=["find-record", "查找"])
        native_ref = HttpMCP(name="docs", url=url, tools=list(peer.names)).tool("find-record")
        responses.respond(function_call("mcp__docsfind_record", {"key": "43"}, "python-call"))
        responses.respond(mcp_call(native_ref.name, native_ref.namespace or "", "native-call"))
        responses.respond(mcp_call("__", "mcp__docs", "unicode-call"))
        responses.respond(mcp_call("excluded", "mcp__docs", "excluded-call"))
        responses.respond(assistant_output("complete"))
        agent = Agent(
            instructions="Use selected tools.",
            tools=[descriptor, local],
            provider=provider(responses),
            model="gpt-5.5",
        )
        async with agent.session(context=context) as session:
            handle = await session.submit("read")
            result = await (handle).get_result()
            assert result.final_output == "complete"
            events = await collect(handle.events())
            assert [(e.family, e.call_id) for e in events if isinstance(e, ToolStarted)] == [
                ("function", "python-call"),
                ("mcp", "native-call"),
                ("mcp", "unicode-call"),
            ]
            assert context.calls == ["python-call"]
            assert [(c["name"], c["_meta"]["callId"]) for c in peer.calls] == [
                ("find-record", "native-call"),
                ("查找", "unicode-call"),
            ]
            assert all(c["arguments"] == {"key": "43"} for c in peer.calls)
            declared = declarations(responses.requests[0])
            assert set(declared) == {
                ("functions", "mcp__docsfind_record"),
                ("mcp__docs", native_ref.name),
                ("mcp__docs", "__"),
            }, json.dumps(responses.requests[0])
            assert declared["mcp__docs", native_ref.name]["parameters"]["properties"] == {
                "key": {"type": "string"}
            }
            items = [item async for item in session.items()]
            names = {i["call_id"]: i["name"] for i in items if i["type"] == "function_call"}
            assert names["python-call"] in {
                "mcp__docsfind_record",
                "functions.mcp__docsfind_record",
            }
            assert names["native-call"] == f"mcp__docs.{native_ref.name}"
            outputs = {
                i["call_id"]: i["output"] for i in items if i["type"] == "function_call_output"
            }
            assert peer.label in str(outputs["native-call"])
            assert "python-receipt:43" in str(outputs["python-call"])
            assert len(responses.requests) == 5
            saved = await session.save_state()
            assert saved.agent_options == {}
            encoded = saved.to_json()
            for private in (context.secret, "synthetic-native-agent-key", repr(local._bind)):
                assert private not in encoded


@pytest.mark.parametrize("credential_source", ["environment", "literal"])
async def test_selection_pins_endpoint_and_checkpoint_uses_current_authority(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    credential_source: str,
) -> None:
    old, new = Peer(label="old-receipt"), Peer(label="new-receipt")
    monkeypatch.setenv("T06_OLD_TOKEN", "synthetic-old-mcp-secret")
    monkeypatch.setenv("T06_NEW_TOKEN", "synthetic-new-mcp-secret")
    async with timeout(30), old.serve() as old_url, new.serve() as new_url:
        if credential_source == "literal":
            original = HttpMCP(
                name="docs-api",
                url=old_url,
                tools=["search"],
                bearer_token="synthetic-old-mcp-secret",
            )
            current = replace(original, url=new_url, bearer_token="synthetic-new-mcp-secret")
        else:
            original = HttpMCP(
                name="docs-api",
                url=old_url,
                tools=["search"],
                bearer_token=os.environ["T06_OLD_TOKEN"],
            )
            current = replace(original, url=new_url, bearer_token=os.environ["T06_NEW_TOKEN"])
        agent = Agent(
            instructions="Read selected records.",
            tools=[original],
            provider=provider(responses),
            model="gpt-5.5",
        )
        async with agent.session() as session:
            responses.respond(mcp_call("search", "mcp__docs_api", "old"))
            responses.respond(assistant_output("old"))
            first = await session.submit("old")
            completed = await (first).get_result()
            state = await session.save_state()
            history = [item async for item in session.items()]
            assert completed.final_output == "old"
            assert state.agent_options == {}
            for forbidden in (old_url, "T06_OLD_TOKEN", "synthetic-old-mcp-secret"):
                assert forbidden not in state.to_json()
            responses.respond(mcp_call("search", "mcp__docs_api", "removed"))
            responses.respond(assistant_output("removed"))
            assert (
                await (await session.submit("removed", tools=[])).get_result()
            ).final_output == "removed"
            assert len(old.calls) == 1
            assert not any(ns.startswith("mcp__") for ns, _ in declarations(responses.requests[2]))
            with pytest.raises(ValueError, match="captured"):
                await session.submit("select", tools=[current])
            assert new.requests == []
            responses.respond(mcp_call("search", "mcp__docs_api", "replaced"))
            responses.respond(assistant_output("replaced"))
            assert (
                await (await session.submit("replaced")).get_result()
            ).final_output == "replaced"
            assert len(old.calls) == 2
            with pytest.raises(ValueError, match="captured"):
                await session.submit("select", tools=[replace(current, tools=["excluded"])])
        assert set(old.auth) == {"Bearer synthetic-old-mcp-secret"}
        assert not new.auth
        old_count = len(old.requests)
        # Even supplied host launch config is not restored or interpreted.
        poisoned = state.model_copy(
            update={"agent_options": {"config": {"mcp_servers": {"stale": {"url": old_url}}}}}
        )
        restored_agent = Agent(
            instructions=agent.instructions,
            tools=[current],
            provider=provider(responses),
            model="gpt-5.5",
        )
        async with restored_agent.session(state=poisoned) as restored:
            assert restored.id == session.id
            assert [item async for item in restored.items()] == history
            responses.respond(mcp_call("search", "mcp__docs_api", "restored"))
            responses.respond(assistant_output("restored"))
            assert (
                await (await restored.submit("restore")).get_result()
            ).final_output == "restored"
            assert (await restored.save_state()).agent_options == {}
        assert len(old.requests) == old_count
        assert [call["_meta"]["callId"] for call in new.calls] == ["restored"]
        assert set(new.auth) == {"Bearer synthetic-new-mcp-secret"}
        async with Agent(
            instructions=agent.instructions, provider=provider(responses), model="gpt-5.5"
        ).session(state=state) as without_mcp:
            assert [item async for item in without_mcp.items()] == history
            responses.respond(assistant_output("no server"))
            assert (
                await (await without_mcp.submit("continue")).get_result()
            ).final_output == "no server"
            assert not any(ns.startswith("mcp__") for ns, _ in declarations(responses.requests[-1]))
        assert len(old.requests) == old_count


@pytest.mark.parametrize("failure", ["before", "after_mcp", "python"])
async def test_replacement_failure_preserves_or_fences_native_ownership(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    peer = Peer()
    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(name="docs", url=url, tools=["search"])
        async with Agent(
            instructions="Read.", tools=[descriptor], provider=provider(responses)
        ).session() as session:
            native = session._native

            class Failure:
                def __getattr__(self, name: str) -> Any:
                    return getattr(native, name)

                async def _replace_mcp_servers(self, servers: str, policies: str):
                    if failure == "before":
                        raise ValueError("synthetic pre-publication rejection")
                    receipt = await native._replace_mcp_servers(servers, policies)
                    if failure == "after_mcp":
                        raise OSError("synthetic lost native acknowledgement")
                    return receipt

                async def _replace_tools(self, *args: Any, **kwargs: Any):
                    raise RuntimeError("synthetic Python publication rejection")

            monkeypatch.setattr(session, "_native", Failure())
            with pytest.raises((ValueError, OSError, RuntimeError), match="synthetic"):
                issued = await session.submit("select", tools=[descriptor])
                await issued.get_result()
            assert session._mcp_tools == ()
            monkeypatch.setattr(session, "_native", native)
            if failure == "before":
                responses.respond(mcp_call("search", "mcp__docs", "retained"))
                responses.respond(assistant_output("retained"))
                assert (
                    await (await session.submit("read")).get_result()
                ).final_output == "retained"
                assert len(peer.calls) == 1
            else:
                with pytest.raises(RuntimeError, match="unconfirmed"):
                    await session.submit("must not run")
                with pytest.raises(RuntimeError, match="unconfirmed"):
                    await session.save_state()
                assert not peer.calls


async def test_replacement_cancellation_close_and_save_share_the_owned_mutation(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    peer = Peer()
    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(name="docs", url=url, tools=["search"])
        async with Agent(
            instructions="Read.", tools=[descriptor], provider=provider(responses)
        ).session() as session:
            native = session._native

            class Held:
                def __getattr__(self, name: str) -> Any:
                    return getattr(native, name)

                async def _replace_mcp_servers(self, servers: str, policies: str):
                    receipt = await native._replace_mcp_servers(servers, policies)
                    entered.set()
                    try:
                        await release.wait()
                    finally:
                        # The publisher retains its receipt until released,
                        # even if close requests cancellation during the hold.
                        await release.wait()
                    return receipt

                async def close(self):
                    await native.close()
                    closed.set()

            monkeypatch.setattr(session, "_native", Held())
            submitted = await session.submit("select", tools=[descriptor])
            await entered.wait()
            try:
                with pytest.raises(RuntimeError, match="mutation|active|idle|busy"):
                    await session.save_state()
                with pytest.raises(RuntimeError, match="mutation|active|idle|busy"):
                    await session.submit("blocked")
                with pytest.raises(RuntimeError, match="active|mutation"):
                    await session.submit("select", tools=[descriptor])
                pending = asyncio.create_task(submitted.cancel())
                await asyncio.sleep(0)
                closing = asyncio.create_task(session._aclose())
                await asyncio.sleep(0)
                assert not pending.done() and not closing.done()
                assert not session._resources_retired
            finally:
                release.set()
            await pending
            await closing
            assert closed.is_set()
            with pytest.raises(TurnStoppedError):
                await submitted.get_result()
            assert not responses.requests


@pytest.mark.parametrize("ending", ["cancel", "close", "timeout"])
async def test_native_call_cancellation_and_timeout_keep_core_as_owner(
    responses: ResponsesFixture,
    ending: str,
) -> None:
    peer = Peer(release_call=asyncio.Event())
    responses.respond(mcp_call("search", "mcp__docs", "slow"))
    responses.respond(assistant_output("after timeout"))
    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(
            name="docs",
            url=url,
            tools=["search"],
            execution_timeout=0.05 if ending == "timeout" else 20,
        )
        async with Agent(
            instructions="Read.", tools=[descriptor], provider=provider(responses)
        ).session() as session:
            handle = await session.submit("read")
            await peer.call_entered.wait()
            try:
                with pytest.raises(SessionBusyError):
                    await session.submit("select", tools=[])
                with pytest.raises(RuntimeError, match="idle Session"):
                    await session.save_state()
                if ending == "timeout":
                    assert (await (handle).get_result()).final_output == "after timeout"
                    assert "MCP tool invocation failed" in str(responses.requests[-1]["input"])
                else:
                    await (handle.cancel() if ending == "cancel" else session._aclose())
                    with pytest.raises(TurnStoppedError):
                        await (handle).get_result()
                    assert len(responses.requests) == 1
                assert len(peer.calls) == 1
            finally:
                peer.release_call.set()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "Native MCP server 'docs': selected tools unavailable: 'search'"),
        ("denied", "Native MCP server 'docs': connection not ready"),
        ("timeout", "Native MCP server 'docs': connection not ready"),
    ],
)
async def test_open_failure_waits_for_native_settlement_and_cleans_acquired_owners(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    from agents import _native

    homes = []
    original_home = _native.NativeProviderLease.home

    def capture_home(lease):
        value = original_home(lease)
        homes.append(value.name)
        return value

    monkeypatch.setattr(_native.NativeProviderLease, "home", capture_home)
    peer = Peer(
        names=("other",) if case == "missing" else ("search",),
        denied=case == "denied",
        release_ready=asyncio.Event() if case == "timeout" else None,
    )
    from pathlib import Path

    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(
            name="docs",
            url=url,
            tools=["search"],
            startup_timeout=0.05 if case == "timeout" else 20,
        )
        try:
            with pytest.raises(NativeError) as caught:
                async with Agent(
                    instructions="Read.", tools=[descriptor], provider=provider(responses)
                ).session() as session:
                    assert not peer.requests
                    selected = await session.submit("Select")
                    await selected.get_result()
            assert str(caught.value) == message
            assert homes and not any(Path(home).exists() for home in homes)
            assert not responses.requests and not peer.calls
        finally:
            if peer.release_ready is not None:
                peer.release_ready.set()


@pytest.mark.parametrize(
    "aliases, selections",
    [
        (("docs-api", "docs.api"), (("read",), ("write",))),
        (("docs", "mcp--docs"), (("read",), ("write",))),
        (("a", "ab"), (("bc",), ("c",))),
        (("docs", "docs"), (("read",), ("write",))),
    ],
)
async def test_whole_initial_mcp_ceiling_rejects_collisions_before_io(
    monkeypatch: pytest.MonkeyPatch,
    aliases: tuple[str, str],
    selections: tuple[tuple[str, ...], ...],
) -> None:
    from agents import NativeProvider

    def no_runtime(*args, **kwargs):
        pytest.fail("Name collision reached runtime acquisition")

    monkeypatch.setattr(NativeProvider, "_acquire_lease", no_runtime)
    tools = [
        HttpMCP(name=name, url="https://example.invalid/mcp", tools=selected)
        for name, selected in zip(aliases, selections, strict=True)
    ]
    with pytest.raises(ValueError, match="collid|collision"):
        async with Agent(instructions="Read.", tools=tools).session():
            pytest.fail("Invalid ceiling opened")


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "codex_apps"},
        {"name": "bad space"},
        {"name": ""},
        {"tools": "read"},
        {"tools": ["read", "read"]},
        {"tools": ["\ud800"]},
        {"tools": [""]},
        {"tools": ["\x00"]},
        {"url": "file:///tmp/mcp"},
        {"url": "https://secret@example.invalid/mcp"},
        {"url": "https://example.invalid/mcp#fragment"},
        {"startup_timeout": 0},
        {"startup_timeout": float("nan")},
        {"execution_timeout": float("inf")},
        {"execution_timeout": True},
    ],
)
def test_invalid_mcp_declarations_fail_before_runtime(changes):
    with pytest.raises((ValueError, TypeError)):
        HttpMCP(
            **{"name": "docs", "url": "https://example.invalid/mcp", "tools": ["read"], **changes}
        )


def test_mcp_declaration_snapshots_selection_and_hides_connection_details() -> None:
    selected = ["find-record"]
    declaration = HttpMCP(
        name="docs",
        url="https://private.invalid/mcp",
        tools=selected,
        bearer_token="synthetic-private-token",
    )
    selected.append("write")
    assert declaration.tools == ("find-record",)
    assert isinstance(declaration.tools, tuple)
    assert "private.invalid" not in repr(declaration)
    assert "synthetic-private-token" not in repr(declaration)


async def test_idempotent_native_prefix_and_punctuation_projection(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("look.up😀",))
    responses.respond(mcp_call("look_up_", "mcp__docs", "projected"))
    responses.respond(assistant_output("done"))
    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(name="mcp--docs", url=url, tools=["look.up😀"])
        async with Agent(
            instructions="Read.", tools=[descriptor], provider=provider(responses), model="gpt-5.5"
        ).session() as session:
            assert (await (await session.submit("read")).get_result()).final_output == "done"
            assert ("mcp__docs", "look_up_") in declarations(responses.requests[0])
            assert peer.calls[0]["name"] == "look.up😀"


async def test_cancelled_selection_joins_publication_before_releasing_native_owners(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from agents import _native

    homes = []
    original_home = _native.NativeProviderLease.home

    def capture_home(lease):
        value = original_home(lease)
        homes.append(value.name)
        return value

    monkeypatch.setattr(_native.NativeProviderLease, "home", capture_home)
    peer = Peer(release_ready=asyncio.Event())
    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(name="docs", url=url, tools=["search"])

        async def use_one():
            async with Agent(
                instructions="Read.", tools=[descriptor], provider=provider(responses)
            ).session() as session:
                assert not peer.requests
                turn = await session.submit("Select")
                await turn.get_result()
                pytest.fail("Cancelled selection completed")

        opening = asyncio.create_task(use_one())
        await peer.ready_entered.wait()
        try:
            opening.cancel("first")
            await asyncio.sleep(0)
            opening.cancel("later")
            await asyncio.sleep(0)
            assert not opening.done()
        finally:
            peer.release_ready.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await opening
        assert "first" in cancellation_messages(caught.value)
        assert homes and not any(Path(home).exists() for home in homes)
        assert not responses.requests and not peer.calls


async def test_python_only_selection_uses_bound_generation_without_republication(
    responses: ResponsesFixture,
) -> None:
    @function_tool
    async def read() -> str:
        return "old"

    @function_tool(name="read", callback_timeout=1)
    async def current() -> str:
        return "current"

    async with Agent(
        instructions="Read.", tools=[current], provider=provider(responses)
    ).session() as session:
        native = session._native
        publications = []

        class PythonOnly:
            def __getattr__(self, name):
                return getattr(native, name)

            def _replace_tools(self, generation, specs):
                publications.append((generation, specs))
                return native._replace_tools(generation, specs)

            def _replace_mcp_servers(self, _config, _policies):
                pytest.fail("Python-only replacement opened an MCP publication")

        session._native = PythonOnly()
        with pytest.raises(ValueError, match="captured"):
            await session.submit("foreign", tools=[read])
        responses.respond(function_call("read", {}, "python-only"))
        responses.respond(assistant_output("current"))
        assert (
            await (await session.submit("read", tools=[current])).get_result()
        ).final_output == "current"
        assert not publications
        items = [item async for item in session.items()]
        assert any(item.get("output") == "current" for item in items)


def cancellation_messages(error: BaseException) -> set[str]:
    # Python 3.10 may wrap a cancelled task; retain the original in its chain.
    pending = [error]
    seen: set[int] = set()
    messages = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, asyncio.CancelledError):
            messages.add(str(current))
        pending.extend(
            linked for linked in (current.__cause__, current.__context__) if linked is not None
        )
    return messages


@pytest.mark.parametrize(
    "initial_model", [None, "gpt-5.5"], ids=["native-default", "explicit-model"]
)
async def test_fresh_and_restored_native_default_model_with_current_mcp_selection(
    responses: ResponsesFixture, initial_model: str | None, tmp_path
) -> None:
    peer = Peer()
    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(name="docs", url=url, tools=["search"])
        initial = Agent(
            instructions="Read selected records.",
            tools=[descriptor],
            provider=provider(responses),
            model=initial_model,
        )
        responses.respond(mcp_call("search", "mcp__docs", "fresh-default"))
        responses.respond(assistant_output("fresh"))
        async with initial.session() as session:
            result = await (await session.submit("fresh")).get_result()
            assert result.final_output == "fresh"
            state = await session.save_state()
            history = [item async for item in session.items()]
        restored = Agent(
            instructions=initial.instructions, tools=[descriptor], provider=provider(responses)
        )
        responses.respond(mcp_call("search", "mcp__docs", "restored-default"))
        responses.respond(assistant_output("restored"))
        try:
            async with restored.session(state=state) as session:
                assert session.id == state.source_session_id
                assert [item async for item in session.items()] == history
                result = await (await session.submit("restore with current server")).get_result()
                assert result.final_output == "restored"
                items = [item async for item in session.items()]
                assert any(
                    item["type"] == "function_call"
                    and item["call_id"] == "restored-default"
                    and item["name"] == "mcp__docs.search"
                    for item in items
                )
                assert any(
                    item["type"] == "function_call_output"
                    and item["call_id"] == "restored-default"
                    and peer.label in str(item["output"])
                    for item in items
                )
            without_mcp = Agent(instructions=initial.instructions, provider=provider(responses))
            responses.respond(assistant_output("no server"))
            async with without_mcp.session(state=state) as session:
                result = await (
                    await session.submit("restore with no selected servers")
                ).get_result()
                assert result.final_output == "no server"
            requests = responses.requests
            assert len(requests) == 5
            assert {request["model"] for request in requests} == {requests[0]["model"]}
            if initial_model is not None:
                assert requests[0]["model"] == initial_model
            continuation = requests[2]
            no_server = requests[4]
            assert set(declarations(continuation)) == {("mcp__docs", "search")}
            assert declarations(no_server) == {}
            assert "restore with current server" in str(continuation["input"][-1])
            assert "restore with no selected servers" in str(no_server["input"][-1])
            assert [call["_meta"]["callId"] for call in peer.calls] == [
                "fresh-default",
                "restored-default",
            ]
        finally:
            # Preserve exact wire inputs if a default-model restore differs.
            (tmp_path / "default-model-requests.json").write_text(
                json.dumps(responses.requests, indent=2)
            )


async def test_native_collision_names_survive_raw_step_narrowing(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("a.b", "a_b"))
    async with timeout(30), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["a.b", "a_b"])
        dotted, underscored = remote.tool("a.b"), remote.tool("a_b")
        responses.respond(mcp_call(dotted.name, dotted.namespace, "first"))
        responses.respond(mcp_call(underscored.name, underscored.namespace, "second"))
        responses.respond(mcp_call(dotted.name, dotted.namespace, "third"))
        responses.respond(assistant_output("complete"))
        selections = ([remote], [underscored], [dotted], [dotted])
        async with Agent(
            instructions="Use selected tools.", tools=[remote], provider=provider(responses)
        ).session() as session:
            handle = await session.submit(
                "read", before_step=lambda turn, step_index: selections[step_index]
            )
            assert (await handle.get_result()).final_output == "complete"
        assert [call["name"] for call in peer.calls] == ["a.b", "a_b", "a.b"]
        assert [(dotted.namespace, dotted.name)] == list(declarations(responses.requests[2]))


@pytest.mark.asyncio
async def test_partial_mcp_declaration_resolves_against_peer_collision_catalog(
    responses: ResponsesFixture,
) -> None:
    from agents import CapabilityError

    peer = Peer(names=("a.b", "a_b"))
    policies: list[str] = []

    def before(request: MCPCallContext[Any], call: Any) -> Allow:
        assert (call.namespace, call.name) == (actual.namespace, actual.name)
        policies.append(request.tool_name)
        return Allow()

    async with timeout(30), peer.serve() as url:
        complete = HttpMCP(name="docs", url=url, tools=["a.b", "a_b"])
        actual = complete.tool("a.b")
        partial = HttpMCP(
            name="docs",
            url=url,
            tools=["a.b"],
            before=before,
        )
        assert partial.tool("a.b").name != actual.name
        responses.respond(mcp_call(actual.name, actual.namespace, "partial"))
        responses.respond(assistant_output("complete"))
        async with Agent(
            instructions="Read the selected tool.", tools=[partial], provider=provider(responses)
        ).session() as session:
            handle = await session.submit(
                "read",
                tools=[partial.tool("a.b")],
                before_step=lambda turn, step_index: [partial.tool("a.b")],
            )
            assert (await handle.get_result()).final_output == "complete"
            denied = await session.submit(
                "deny", before_step=lambda turn, step_index: [complete.tool("a_b")]
            )
            with pytest.raises(CapabilityError):
                await denied.get_result()
        assert policies == ["a.b"]
        assert [call["name"] for call in peer.calls] == ["a.b"]
        assert len(responses.requests) == 2
        assert list(declarations(responses.requests[0])) == [(actual.namespace, actual.name)]


def test_raw_mcp_reference_selection_uses_captured_identity() -> None:
    from _agents_codex.session import _split_tools
    from agents import CapabilityError

    partial = HttpMCP(name="docs", url="http://localhost/mcp", tools=["a.b"])
    complete = HttpMCP(name="docs", url="http://localhost/mcp", tools=["a.b", "a_b"])
    assert partial.tool("a.b") != complete.tool("a.b")
    _, selected, _ = _split_tools((complete.tool("a.b"),), available=(partial,), ceiling=(partial,))
    assert len(selected) == 1
    assert selected[0].tools == ("a.b",)
    with pytest.raises(CapabilityError):
        _split_tools((complete.tool("a_b"),), available=(partial,), ceiling=(partial,))


@pytest.mark.parametrize("phase", ["before", "approval"])
async def test_partial_mcp_graph_policy_survives_accepted_update(
    responses: ResponsesFixture, phase: str
) -> None:
    from agents import CapabilityError

    peer = Peer(names=("a.b", "a_b"))
    calls: list[str] = []

    def stop(request: MCPCallContext[Any], call: Any) -> StopTurn:
        assert request.tool_name == "a.b"
        calls.append(phase)
        return StopTurn("private-policy-stop")

    async with timeout(30), peer.serve() as url:
        complete = HttpMCP(name="docs", url=url, tools=["a.b", "a_b"])
        actual = complete.tool("a.b")
        partial = HttpMCP(
            name="docs",
            url=url,
            tools=["a.b"],
            **{phase: stop},
        )
        native_provider = provider(responses)
        unused = Agent(name="unused", instructions="Unused target", provider=native_provider)
        responses.respond(assistant_output("ready"))
        responses.respond(mcp_call(actual.name, actual.namespace, "after-update"))
        responses.respond(assistant_output("must not continue"))
        async with Agent(
            instructions="Use only the selected record tool.",
            tools=[partial],
            handoffs=[unused],
            provider=native_provider,
        ).session() as session:
            assert (await (await session.submit("warm")).get_result()).final_output == "ready"
            await session.update(tools=[partial])
            handle = await session.submit(
                "after update",
                tools=[partial.tool("a.b")],
                before_step=lambda turn, step_index: [partial.tool("a.b")],
            )
            with pytest.raises((TurnStoppedError, NativeError)):
                await handle.get_result()
            assert calls == [phase]
            assert peer.calls == []
            assert len(responses.requests) == 2
            denied = await session.submit(
                "deny sibling", before_step=lambda turn, step_index: [complete.tool("a_b")]
            )
            with pytest.raises(CapabilityError):
                await denied.get_result()
            assert len(responses.requests) == 2


@pytest.mark.parametrize("tools", [["find-record", "find_record"], ["查找", "__"], ["x" * 122]])
def test_mcp_declarations_project_colliding_unicode_and_long_raw_names(tools: list[str]) -> None:
    descriptor = HttpMCP(name="docs", url="https://example.invalid/mcp", tools=tools)
    refs = [descriptor.tool(raw) for raw in tools]
    assert len({(ref.namespace, ref.name) for ref in refs}) == len(tools)
    for ref in refs:
        assert ref.namespace == "mcp__docs"
        assert ref.name.isascii()
        assert all(character.isalnum() or character == "_" for character in ref.name)
        assert len((ref.namespace + "::" + ref.name).encode("ascii")) <= 128
