"""MCP result policies run after native execution and before native consumers."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

import pytest

from agents import Agent, HttpMCP, MCPResultContext, NativeError, TurnStoppedError
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call


@dataclass
class Tenant:
    name: str


def result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


@pytest.mark.parametrize("callback_form", ["function", "partial", "callable"])
async def test_selected_policy_uses_actual_identity_and_context_before_consumers(
    responses: ResponsesFixture,
    callback_form: str,
) -> None:
    protected = Peer(names=("find-record",), label="raw-policy-secret")
    ordinary = Peer(names=("find-record",), label="raw-ordinary-receipt")
    calls: list[MCPResultContext[Tenant]] = []

    async def redact(ctx: MCPResultContext[Tenant], value: dict[str, Any]) -> dict[str, Any]:
        assert value["content"] == [{"type": "text", "text": protected.label}]
        assert value["structuredContent"] == {"receipt": protected.label}
        calls.append(ctx)
        return result("accepted:" + ctx.context.name)

    class CallablePolicy:
        async def __call__(
            self, ctx: MCPResultContext[Tenant], value: dict[str, Any]
        ) -> dict[str, Any]:
            return await redact(ctx, value)

    callback = (
        redact
        if callback_form == "function"
        else (partial(redact) if callback_form == "partial" else CallablePolicy())
    )
    responses.respond(mcp_call("find_record", "mcp__private", "redact-me"))
    responses.respond(mcp_call("find_record", "mcp__ordinary", "native-pass"))
    responses.respond(assistant_output("complete"))
    async with timeout(30), protected.serve() as private_url, ordinary.serve() as plain_url:
        agent = Agent[Tenant](
            instructions="Use selected tools.",
            tools=[
                HttpMCP(
                    name="private",
                    url=private_url,
                    tools=["find-record"],
                    transform_result=callback,
                ),
                HttpMCP(name="ordinary", url=plain_url, tools=["find-record"]),
            ],
            model="gpt-5.5",
            provider=provider(responses),
        )
        tenant = Tenant("tenant-A")
        async with agent.session(context=tenant) as session:
            native = session._native
            native_events = []

            class Observed:
                def __getattr__(self, name):
                    return getattr(native, name)

                async def _next_session_event(self):
                    raw = await native._next_session_event()
                    native_events.append(raw)
                    return raw

            session._native = Observed()
            handle = await session.submit("read")
            assert (await (handle).get_result()).final_output == "complete"
            assert len(calls) == 1
            ctx = calls[0]
            assert ctx.context is tenant
            assert ctx.submission is handle
            assert (ctx.server_name, ctx.tool_name, ctx.call_id) == (
                "private",
                "find-record",
                "redact-me",
            )
            assert [(p["name"], p["_meta"]["callId"]) for p in protected.calls] == [
                ("find-record", "redact-me")
            ]
            assert [(p["name"], p["_meta"]["callId"]) for p in ordinary.calls] == [
                ("find-record", "native-pass")
            ]
            items = [item async for item in session.items()]
            outputs = {
                i["call_id"]: i["output"] for i in items if i["type"] == "function_call_output"
            }
            assert "accepted:tenant-A" in str(outputs["redact-me"])
            assert ordinary.label in str(outputs["native-pass"])
            state = (await session.save_state()).to_json()
            wire = json.dumps(responses.requests)
            events = "".join(native_events)
            for channel in (str(items), state, wire, events):
                assert protected.label not in channel
            for channel in (str(items), state, wire, events):
                assert "accepted:tenant-A" in channel
                assert ordinary.label in channel
            assert len(responses.requests) == 3


@pytest.mark.parametrize("decision", ["invalid", "timeout", "cancel"])
async def test_native_rejects_invalid_late_and_cancelled_decisions(
    responses: ResponsesFixture, decision: str
) -> None:
    peer = Peer(label="raw-unaccepted-mcp-content")
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def policy(ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        if decision == "invalid":
            return {"content": [{"type": "text", "text": 43}]}
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            # Even a callback that ignores cancellation has no authority to reply.
            return result("forbidden-late-result")

    responses.respond(mcp_call("search", "mcp__vault", "effect-once"))
    responses.respond(assistant_output("must-not-succeed"))
    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="Read a record",
            tools=[
                HttpMCP(
                    name="vault",
                    url=url,
                    tools=["search"],
                    transform_result=policy,
                    transform_result_timeout=0.05 if decision == "timeout" else 10,
                )
            ],
            model="gpt-5.5",
            provider=provider(responses),
        )
        async with agent.session() as session:
            handle = await session.submit("read")
            await entered.wait()
            if decision == "cancel":
                await handle.cancel()
                with pytest.raises(TurnStoppedError):
                    await (handle).get_result()
            else:
                with pytest.raises(NativeError, match="Native MCP result policy failed"):
                    await (handle).get_result()
            if decision != "invalid":
                assert cancelled.is_set()
            assert [(p["name"], p["_meta"]["callId"]) for p in peer.calls] == [
                ("search", "effect-once")
            ]
            wire = json.dumps(responses.requests)
            items = str([item async for item in session.items()])
            for channel in (wire, items):
                assert peer.label not in channel
                assert "forbidden-late-result" not in channel


async def test_cancellation_before_policy_starts_returns_native_notice(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents._runtime.session import _Submission

    peer = Peer()
    cancelled: list[asyncio.Task[None]] = []
    released: list[int] = []
    called: list[str] = []
    add_callback = _Submission.add_callback

    def cancel_at_dispatch(state: _Submission, task: asyncio.Task[None]) -> None:
        add_callback(state, task)
        if not cancelled:
            # The only callback is the MCP policy. Stop it before its first poll,
            # at the same boundary used by submission failure cleanup.
            cancelled.append(task)
            task.cancel()
            state.failure.set_result(NativeError("controlled MCP dispatch cancellation"))

    monkeypatch.setattr(_Submission, "add_callback", cancel_at_dispatch)

    async def policy(ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        called.append(ctx.call_id)
        return result("accepted")

    responses.respond(mcp_call("search", "mcp__docs", "cancelled-before-start"))
    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="Read.",
            tools=[HttpMCP(name="docs", url=url, tools=["search"], transform_result=policy)],
            model="gpt-5.5",
            provider=provider(responses),
        )
        async with agent.session() as session:
            native = session._native

            class Observed:
                def __getattr__(self, name):
                    return getattr(native, name)

                def _release_native_mcp_result(self, nonce: int) -> bool:
                    returned = native._release_native_mcp_result(nonce)
                    if returned:
                        released.append(nonce)
                    return returned

            session._native = Observed()
            with pytest.raises(NativeError, match="controlled MCP dispatch cancellation"):
                await (await session.submit("read")).get_result()
            assert len(cancelled) == 1 and cancelled[0].cancelled()
            assert called == []
            # Observe the real native release; the test never returns a slot itself.
            assert len(released) == 1

            responses.respond(mcp_call("search", "mcp__docs", "next-turn"))
            responses.respond(assistant_output("complete"))
            assert (
                await (await session.submit("read again")).get_result()
            ).final_output == "complete"
            assert called == ["next-turn"]
            assert len(set(released)) == 2
            assert [call["_meta"]["callId"] for call in peer.calls] == [
                "cancelled-before-start",
                "next-turn",
            ]


async def test_replacement_and_restore_use_current_policy_and_credentials(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = Peer(names=("search", "second"), label="raw-old-unaccepted")
    new = Peer(names=("search", "second"), label="raw-new-unaccepted")
    monkeypatch.setenv("MCP_POLICY_OLD_TOKEN", "synthetic-old-credential")
    monkeypatch.setenv("MCP_POLICY_NEW_TOKEN", "synthetic-new-credential")
    selected: list[tuple[str, str, str]] = []

    def first(ctx: MCPResultContext[Tenant], value: dict[str, Any]) -> dict[str, Any]:
        selected.append(("first", ctx.tool_name, ctx.context.name))
        return result("accepted-first")

    def current(ctx: MCPResultContext[Tenant], value: dict[str, Any]) -> dict[str, Any]:
        selected.append(("current", ctx.tool_name, ctx.context.name))
        return result("accepted-current")

    async with timeout(40), old.serve() as old_url, new.serve() as new_url:
        original = HttpMCP(
            name="docs",
            url=old_url,
            tools=["search", "second"],
            bearer_token=os.environ["MCP_POLICY_OLD_TOKEN"],
            transform_result=first,
        )
        replacement = replace(
            original,
            url=new_url,
            tools=["second"],
            bearer_token=os.environ["MCP_POLICY_NEW_TOKEN"],
            transform_result=current,
        )
        agent = Agent[Tenant](
            instructions="Read.", tools=[original], model="gpt-5.5", provider=provider(responses)
        )
        async with agent.session(context=Tenant("run-one")) as session:
            responses.respond(mcp_call("search", "mcp__docs", "old"))
            responses.respond(assistant_output("one"))
            first_handle = await session.submit("old")
            assert (await (first_handle).get_result()).final_output == "one"
            before_replace = await session.save_state()
            with pytest.raises(ValueError, match="captured"):
                await session.submit("select", tools=[replacement])
            assert new.requests == []
        current_original = replace(original, tools=["second"], transform_result=current)
        async with agent.session(
            context=Tenant("run-one"), state=before_replace, tools=[current_original]
        ) as session:
            responses.respond(mcp_call("second", "mcp__docs", "new"))
            responses.respond(assistant_output("two"))
            second_handle = await session.submit("new")
            assert (await (second_handle).get_result()).final_output == "two"
        restored = Agent[Tenant](
            instructions="Read.", tools=[replacement], model="gpt-5.5", provider=provider(responses)
        )
        async with restored.session(context=Tenant("run-two"), state=before_replace) as session:
            responses.respond(mcp_call("second", "mcp__docs", "restored"))
            responses.respond(assistant_output("three"))
            handle = await session.submit("restore")
            assert (await (handle).get_result()).final_output == "three"
            current_state = (await session.save_state()).to_json()
        assert selected == [
            ("first", "search", "run-one"),
            ("current", "second", "run-one"),
            ("current", "second", "run-two"),
        ]
        assert len(old.calls) == 2
        assert len(new.calls) == 1
        assert set(old.auth) == {"Bearer synthetic-old-credential"}
        assert set(new.auth) == {"Bearer synthetic-new-credential"}
        for secret in (
            old.label,
            new.label,
            "synthetic-old-credential",
            "synthetic-new-credential",
            "run-one",
            "run-two",
        ):
            assert secret not in current_state
            assert secret not in json.dumps(responses.requests)


async def test_context_requirement_rejected_before_native_connections(
    responses: ResponsesFixture,
) -> None:
    peer = Peer()

    def policy(ctx: MCPResultContext[Tenant], value: dict[str, Any]) -> dict[str, Any]:
        return result(ctx.context.name)

    async with timeout(10), peer.serve() as url:
        agent: Any = Agent[Tenant](
            instructions="Read",
            tools=[HttpMCP(name="docs", url=url, tools=["search"], transform_result=policy)],
            model="gpt-5.5",
            provider=provider(responses),
        )
        with pytest.raises(TypeError, match="requires application context"):
            async with agent.session() as session:
                await session.submit("Select protected")
        assert not peer.requests
        assert not responses.requests


@pytest.mark.parametrize("duration", [0, -1, True, float("inf"), float("nan"), 3600.1])
def test_result_policy_timeout_rejects_invalid_before_io(duration: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        HttpMCP(
            name="docs",
            url="https://example.invalid/mcp",
            tools=["search"],
            transform_result=lambda ctx, value: value,
            transform_result_timeout=duration,
        )


async def test_one_agent_two_live_contexts_do_not_share_policy_authority(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(label="private-mcp-result-two-roots")
    tenants = (Tenant("first-private-context"), Tenant("second-private-context"))
    entered: list[MCPResultContext[Tenant]] = []
    both_entered = asyncio.Event()

    async def policy(ctx: MCPResultContext[Tenant], value: dict[str, Any]) -> dict[str, Any]:
        entered.append(ctx)
        if len(entered) == 2:
            both_entered.set()
        await both_entered.wait()
        return result("accepted-first" if ctx.context is tenants[0] else "accepted-second")

    responses.respond(mcp_call("search", "mcp__docs", "one"))
    responses.respond(mcp_call("search", "mcp__docs", "two"))
    responses.respond(assistant_output("done"))
    responses.respond(assistant_output("done"))
    async with timeout(40), peer.serve() as url:
        agent = Agent[Tenant](
            instructions="Read.",
            tools=[HttpMCP(name="docs", url=url, tools=["search"], transform_result=policy)],
            model="gpt-5.5",
            provider=provider(responses),
        )
        async with (
            agent.session(context=tenants[0]) as first,
            agent.session(context=tenants[1]) as second,
        ):
            one, two = await asyncio.gather(first.submit("read"), second.submit("read"))
            a, b = await asyncio.gather(one.get_result(), two.get_result())
            assert a.final_output == b.final_output == "done"
            assert {id(c.context) for c in entered} == {id(t) for t in tenants}
            assert {c.submission.session_id for c in entered} == {first.id, second.id}
            first_items = str([item async for item in first.items()])
            second_items = str([item async for item in second.items()])
            assert "accepted-first" in first_items
            assert "accepted-second" not in first_items
            assert "accepted-second" in second_items
            assert "accepted-first" not in second_items
            assert len(peer.calls) == 2
            for secret in (peer.label, *(t.name for t in tenants)):
                assert secret not in first_items + second_items + json.dumps(responses.requests)


@pytest.mark.parametrize("callback_form", ["partial", "callable"])
def test_callable_forms_keep_context_requirement_at_admission(callback_form: str) -> None:
    def policy(ctx: MCPResultContext[Tenant], value: dict[str, Any]) -> dict[str, Any]:
        return result(ctx.context.name)

    class CallablePolicy:
        def __call__(self, ctx: MCPResultContext[Tenant], value: dict[str, Any]) -> dict[str, Any]:
            return policy(ctx, value)

    declared = HttpMCP(
        name="docs",
        url="https://example.invalid/mcp",
        tools=["search"],
        transform_result=partial(policy) if callback_form == "partial" else CallablePolicy(),
    )
    with pytest.raises(TypeError, match="requires application context"):
        declared._check_context(None)
    declared._check_context(Tenant("actual"))


@pytest.mark.parametrize("callback_form", ["partial", "callable"])
def test_no_context_callable_forms_admit_before_connection(callback_form: str) -> None:
    def policy(ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        return value

    class CallablePolicy:
        def __call__(self, ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
            return policy(ctx, value)

    declared = HttpMCP(
        name="docs",
        url="https://example.invalid/mcp",
        tools=["search"],
        transform_result=partial(policy) if callback_form == "partial" else CallablePolicy(),
    )
    declared._check_context(None)


async def test_callback_raised_cancellation_cannot_transport_unaccepted_peer_data(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(label="synthetic-unaccepted-cancellation-payload")

    async def policy(ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        secret = value["content"][0]["text"]
        raise asyncio.CancelledError(secret) from RuntimeError("callback-cause:" + secret)

    responses.respond(mcp_call("search", "mcp__docs", "cancel-from-policy"))
    responses.respond(assistant_output("never accept"))
    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="Read.",
            tools=[HttpMCP(name="docs", url=url, tools=["search"], transform_result=policy)],
            model="gpt-5.5",
            provider=provider(responses),
        )
        async with agent.session() as session:
            handle = await session.submit("read")
            with pytest.raises(NativeError, match="Native MCP result policy failed") as caught:
                await (handle).get_result()
            graph = [caught.value]
            seen: set[int] = set()
            while graph:
                error = graph.pop()
                if id(error) in seen:
                    continue
                seen.add(id(error))
                assert peer.label not in repr(error)
                assert peer.label not in str(error.args)
                for other in (error.__cause__, error.__context__):
                    if other is not None:
                        graph.append(other)
            assert peer.label not in str([item async for item in session.items()])
            assert peer.label not in json.dumps(responses.requests)
            assert len(peer.calls) == 1
