"""Public lifecycle results retain application cleanup failures."""

from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest

from agents import (
    Agent,
    NativeAgents,
    NativeError,
    NativeProvider,
    PromptedOutput,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_child_agents import child_peer, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_prompted_repair import Report


def error_graph(error: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if any(current is previous for previous in found):
            continue
        found.append(current)
        for nested in (current.__cause__, current.__context__):
            if nested is not None:
                pending.append(nested)
    return found


@pytest.mark.parametrize("hook", ["before_step", "prepare_request"])
@pytest.mark.parametrize("action", ["cancel", "close"])
@pytest.mark.parametrize("fails", [False, True])
async def test_root_callback_cleanup_reaches_public_lifecycle(responses, hook, action, fails):
    entered, exited = asyncio.Event(), asyncio.Event()
    cleanup = LookupError("synthetic callback cleanup failed")

    async def callback(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
            if fails:
                raise cleanup

    async with (
        timeout(15),
        aclosing(
            NativeProvider(base_url=responses.url, api_key="synthetic", supports_websockets=False)
        ) as provider,
    ):
        scope = Agent(model="gpt-5.5", instructions="Wait.", provider=provider).session()
        session = await scope.__aenter__()
        try:
            turn = await session.submit("Hold", **{hook: callback})
            await entered.wait()
            if fails:
                with pytest.raises(NativeError if action == "cancel" else LookupError) as stopped:
                    await (turn.cancel() if action == "cancel" else session.aclose())
                assert cleanup in error_graph(stopped.value)
            else:
                await (turn.cancel() if action == "cancel" else session.aclose())
            assert exited.is_set()
            with pytest.raises(NativeError if fails else TurnStoppedError) as result:
                await turn.get_result()
            graph = error_graph(result.value)
            assert any(isinstance(error, asyncio.CancelledError) for error in graph)
            assert (cleanup in graph) is fails
            assert not responses.requests
        finally:
            # A retained root cleanup error can also surface at scope exit.
            if fails and action == "cancel":
                with pytest.raises(NativeError) as closing:
                    await scope.__aexit__(None, None, None)
                assert cleanup in error_graph(closing.value)
            else:
                await scope.__aexit__(None, None, None)


async def test_failed_child_result_retains_sibling_cleanup_without_replaying_at_close(
    tmp_path, monkeypatch
):
    primary = ValueError("synthetic child failure")
    cleanup = LookupError("synthetic sibling cleanup failed")
    entered, fail_gate, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool
    async def work(kind: str) -> str:
        """Run one coordinated application callback."""
        if kind == "fail":
            await fail_gate.wait()
            raise primary
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
            raise cleanup

    async with (
        timeout(30),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        original = peer.response

        def response():
            path = metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"]
            if path == "/root":
                return original()
            return [
                function_call("work", {"kind": "fail"}, "child-fail"),
                function_call("work", {"kind": "hold"}, "child-hold"),
            ]

        peer.response = response
        async with aclosing(peer.provider(websocket=False)) as provider:
            agent = Agent(
                instructions="Spawn one child.",
                model="gpt-5.5",
                tools=[work, NativeAgents(max_children=1)],
                provider=provider,
            )
            async with agent.session(environment=declaration(executor)) as session:
                root = await session.submit("Spawn the child")
                root_result = await root.get_result()
                await entered.wait()
                [child] = await handles(session, 1)
                fail_gate.set()
                for _ in range(2):
                    with pytest.raises(ValueError) as result:
                        await child.get_result()
                    assert result.value is primary
                    assert cleanup in error_graph(result.value)
                assert exited.is_set()
                await session.aclose()
                assert (await root.get_result()).final_output == root_result.final_output


@pytest.mark.parametrize("falsy", [False, True])
@pytest.mark.parametrize("needs_repair", [False, True])
async def test_prompted_output_repair_preserves_borrowed_provider(responses, falsy, needs_repair):
    class FalsyProvider(NativeProvider):
        def __bool__(self):
            return False

    provider_type = FalsyProvider if falsy else NativeProvider
    responses.respond(assistant_output('{"label":"x"}' if needs_repair else '{"label":"valid"}'))
    if needs_repair:
        responses.respond(assistant_output('{"label":"corrected"}'))
    async with aclosing(
        provider_type(base_url=responses.url, api_key="synthetic", supports_websockets=False)
    ) as provider:
        result = await Agent(
            instructions="Return a report.",
            provider=provider,
            output_type=PromptedOutput(Report, 1),
        ).run("Report")
        assert result.final_output == Report(label="corrected" if needs_repair else "valid")
        assert len(responses.requests) == (2 if needs_repair else 1)
        # Both the original and repair Session borrow the application provider.
        assert not vars(provider).get("_closed", False)
