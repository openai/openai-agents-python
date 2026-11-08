"""Outbound metadata through the real native MCP preparation and spawned peer."""

from __future__ import annotations

import json

import pytest

from agents import Agent, NativeError, ReplaceArguments
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call
from tests.test_native_mcp_stdio import records, stdio
from tests.test_native_mcp_tools import mcp_call


@pytest.mark.parametrize("in_code", [False, True], ids=["direct", "code"])
@pytest.mark.parametrize("reserved", [False, True], ids=["success", "reserved"])
async def test_native_request_metadata_first_proof(
    responses: ResponsesFixture, tmp_path, in_code, reserved
):
    log = tmp_path / "metadata-peer"
    order = []
    contexts = []
    sentinel = "private-metadata-only-7461"

    def before(ctx, call):
        order.append("before")
        return ReplaceArguments({"key": "effective"})

    def approval(ctx, call):
        order.append("approval")
        return True

    async def metadata(ctx):
        order.append("metadata")
        contexts.append(ctx)
        assert ctx.submission is turn
        assert dict(ctx.arguments) == {"key": "effective"}
        assert ctx.call.route == ("code" if in_code else "direct")
        assert ctx.call.call_id == (None if in_code else "metadata-call")
        if in_code:
            assert ctx.call.cell_id and ctx.call.nested_call_id
        with pytest.raises(TypeError):
            ctx.arguments["key"] = "changed"
        defaults = {"app/default": "base"}
        return {**defaults, ("callId" if reserved else "app/current"): sentinel}

    first = (
        code_call('text(await tools.mcp__local__search({key:"original"}));', "metadata-call")
        if in_code
        else mcp_call("search", "mcp__local", "metadata-call")
    )
    if not in_code:
        first["item"]["arguments"] = json.dumps({"key": "original"})
    responses.respond(first)
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use the selected search tool",
        provider=provider(responses),
        programmatic_tool_calling=in_code,
        tools=[stdio(log, before=before, approval=approval, request_metadata=metadata)],
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("search")
        if reserved:
            with pytest.raises(NativeError) as caught:
                await turn.get_result()
            assert sentinel not in str(caught.value)
            assert len(responses.requests) == 1
        else:
            assert (await turn.get_result()).final_output == "done"
        calls = [
            row["message"]["params"]
            for row in records(log)
            if row.get("message", {}).get("method") == "tools/call"
        ]
        assert len(calls) == (0 if reserved else 1)
        assert order == ["before", "approval", "metadata"]
        assert len(contexts) == 1
        if calls:
            assert calls[0]["arguments"] == {"key": "effective"}
            assert calls[0]["_meta"]["app/default"] == "base"
            assert calls[0]["_meta"]["app/current"] == sentinel
            assert calls[0]["_meta"]["callId"] != sentinel
        assert sentinel not in json.dumps(responses.requests)


@pytest.mark.parametrize("value", [None, {}, {"app/value": [None, True, 2, "ok"]}])
async def test_metadata_optional_values_keep_native_identity(
    responses: ResponsesFixture, tmp_path, value
):
    log = tmp_path / "optional"
    seen = []

    def metadata(ctx):
        seen.append(ctx)
        return value

    responses.respond(mcp_call("search", "mcp__local", "optional-call"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(15),
        Agent(
            instructions="search",
            provider=provider(responses),
            tools=[stdio(log, request_metadata=metadata)],
        ).session() as session,
    ):
        assert (await (await session.submit("search")).get_result()).final_output == "done"
    calls = [
        r["message"]["params"]
        for r in records(log)
        if r.get("message", {}).get("method") == "tools/call"
    ]
    assert len(seen) == len(calls) == 1
    assert calls[0]["_meta"]["callId"] == "optional-call"
    if value:
        assert calls[0]["_meta"]["app/value"] == value["app/value"]
    else:
        assert "app/value" not in calls[0]["_meta"]


RESERVED = [
    "callId",
    "x-codex-turn-metadata",
    "_codex_apps",
    "plugin_id",
    "openai/confirmation_policies",
    "threadId",
    "sessionId",
    "windowId",
    "itemId",
    "codex/sandbox-state-meta",
    "codex_bridge_mcp_call_id",
    "openai/entitlementContext",
    "openai/readOnly",
    "traceparent",
    "tracestate",
    "progressToken",
]


@pytest.mark.parametrize(
    "kind",
    [*RESERVED, "array", "nan", "numeric_key", "nested_numeric_key", "oversized", "exception"],
)
async def test_metadata_invalid_values_never_dispatch(responses: ResponsesFixture, tmp_path, kind):
    log = tmp_path / "invalid"
    secret = "private-callback-error-8193"

    def metadata(ctx):
        if kind == "exception":
            raise RuntimeError(secret)
        if kind == "array":
            return []
        if kind == "nan":
            return {"app/value": float("nan")}
        if kind == "numeric_key":
            return {1: "coerced"}
        if kind == "nested_numeric_key":
            return {"app/value": {1: "coerced"}}
        if kind == "oversized":
            return {"app/value": "x" * (1024 * 1024 + 1)}
        return {kind: secret}

    responses.respond(mcp_call("search", "mcp__local", "invalid-call"))
    async with (
        timeout(15),
        Agent(
            instructions="search",
            provider=provider(responses),
            tools=[stdio(log, request_metadata=metadata)],
        ).session() as session,
    ):
        with pytest.raises(NativeError) as caught:
            await (await session.submit("search")).get_result()
        assert secret not in str(caught.value)
        assert len(responses.requests) == 1
    assert not [r for r in records(log) if r.get("message", {}).get("method") == "tools/call"]
    assert secret not in json.dumps(responses.requests)


@pytest.mark.parametrize("in_code", [False, True], ids=["direct", "code"])
async def test_metadata_cancel_joins_exact_callback_and_cannot_retarget_next_turn(
    responses: ResponsesFixture, tmp_path, in_code
):
    import asyncio

    from agents import TurnStoppedError

    log = tmp_path / "cancel"
    entered, cancelled, release, returned = (asyncio.Event() for _ in range(4))
    count = 0

    async def metadata(ctx):
        nonlocal count
        count += 1
        if count > 1:
            return {"app/owner": "next"}
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return {"app/owner": "old"}
        finally:
            returned.set()

    responses.respond(
        code_call('text(await tools.mcp__local__search({key:"first"}));', "same-call")
        if in_code
        else mcp_call("search", "mcp__local", "same-call")
    )
    async with (
        timeout(20),
        Agent(
            instructions="search",
            provider=provider(responses),
            programmatic_tool_calling=in_code,
            tools=[stdio(log, request_metadata=metadata)],
        ).session() as session,
    ):
        turn = await session.submit("first")
        await entered.wait()
        stopping = asyncio.create_task(turn.cancel())
        await cancelled.wait()
        assert not stopping.done()
        release.set()
        await stopping
        assert returned.is_set()
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert not [r for r in records(log) if r.get("message", {}).get("method") == "tools/call"]
        responses.respond(mcp_call("search", "mcp__local", "same-call"))
        responses.respond(assistant_output("next"))
        assert (await (await session.submit("second")).get_result()).final_output == "next"
    calls = [
        r["message"]["params"]
        for r in records(log)
        if r.get("message", {}).get("method") == "tools/call"
    ]
    assert len(calls) == 1 and calls[0]["_meta"]["app/owner"] == "next"
    assert count == 2


async def test_metadata_restore_uses_fresh_declaration_without_replaying_effect(
    responses: ResponsesFixture, tmp_path
):
    log = tmp_path / "restore"
    observed = []

    def old(ctx):
        observed.append("old")
        return {"app/owner": "old-metadata-5911"}

    def current(ctx):
        observed.append("current")
        return {"app/owner": "current-metadata-5911"}

    responses.respond(mcp_call("search", "mcp__local", "old-call"))
    responses.respond(assistant_output("old"))
    async with timeout(20):
        async with Agent(
            instructions="search",
            provider=provider(responses),
            tools=[stdio(log, request_metadata=old)],
        ).session() as session:
            assert (await (await session.submit("first")).get_result()).final_output == "old"
            saved = await session.save_state()
        assert "old-metadata-5911" not in saved.to_json()
        assert observed == ["old"]
        responses.respond(mcp_call("search", "mcp__local", "new-call"))
        responses.respond(assistant_output("current"))
        async with Agent(
            instructions="search",
            provider=provider(responses),
            tools=[stdio(log, request_metadata=current)],
        ).session(state=saved) as restored:
            assert observed == ["old"]
            assert (await (await restored.submit("second")).get_result()).final_output == "current"
    calls = [
        r["message"]["params"]
        for r in records(log)
        if r.get("message", {}).get("method") == "tools/call"
    ]
    assert observed == ["old", "current"]
    assert [c["_meta"]["app/owner"] for c in calls] == [
        "old-metadata-5911",
        "current-metadata-5911",
    ]


@pytest.mark.parametrize("failure", ["exception", "reserved"])
async def test_metadata_failure_joins_entered_work_and_allows_an_explicit_successor(
    responses: ResponsesFixture, tmp_path, failure
):
    import asyncio

    from agents import function_tool
    from tests.native_fixtures import function_call

    log = tmp_path / "fatal-join"
    entered, cancelled, release, cleaned = (asyncio.Event() for _ in range(4))
    effects = []
    secret = "private-rejected-metadata-9217"

    @function_tool
    async def held() -> str:
        effects.append("entered once")
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return "unaccepted-held-output-9217"
        finally:
            cleaned.set()

    async def metadata(ctx):
        await entered.wait()
        if failure == "exception":
            raise LookupError(secret)
        return {"codex/sandbox-state-meta": secret}

    responses.respond(
        function_call("held", {}, "held-call"),
        mcp_call("search", "mcp__local", "rejected-meta-call"),
    )
    responses.respond(assistant_output("explicit successor"))
    async with (
        timeout(30),
        Agent(
            instructions="Use the declared tools.",
            provider=provider(responses),
            tools=[held, stdio(log, request_metadata=metadata)],
        ).session() as session,
    ):
        turn = await session.submit("Run the controlled batch.")
        waiter = asyncio.create_task(turn.get_result())
        try:
            await entered.wait()
            await cancelled.wait()
            assert not waiter.done()
            assert not cleaned.is_set()
            assert effects == ["entered once"]
            assert len(responses.requests) == 1
            assert not any(
                row.get("message", {}).get("method") == "tools/call" for row in records(log)
            )
        finally:
            release.set()
            await asyncio.gather(waiter, return_exceptions=True)
        with pytest.raises(NativeError) as caught:
            await waiter
        assert secret not in str(caught.value)
        assert "MCP result policy" in str(caught.value)
        assert cleaned.is_set()
        assert "unaccepted-held-output-9217" not in json.dumps(
            [item async for item in session.items()]
        )
        assert (
            await (await session.submit("Now continue explicitly.")).get_result()
        ).final_output == ("explicit successor")
        assert len(responses.requests) == 2
        assert effects == ["entered once"]
