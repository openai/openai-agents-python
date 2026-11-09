"""Actual remote-executor admission and joined uncooperative cleanup.

Run only with the final wheel/helpers and its owning exec-server fixture.
The callback suppresses cancellation for a bounded observation window and is
always released by the test; this does not claim forced retirement is possible.
"""

import asyncio
import json
import sys

import pytest

from agents import Agent, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import function_call, responses as responses
from tests.test_native_executor_tools import declaration


@pytest.mark.skipif(sys.platform != "linux", reason="Requires real Linux native confinement")
@pytest.mark.requires_native_linux_sandbox
async def test_function_failure_joins_native_executor_siblings_after_their_effects(
    responses, tmp_path, monkeypatch
):
    both_entered = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()
    entered, cleaned, effects = [], [], []
    problem = LookupError("selected sibling function failure")

    async def transform_result(context, value):
        entered.append(context.call_id)
        if len(entered) == 2:
            both_entered.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
            return value
        finally:
            cleaned.append(context.call_id)

    @function_tool
    async def fail_after_executor_admission() -> str:
        await both_entered.wait()
        effects.append("function failed once")
        raise problem

    responses.respond(
        function_call("fail_after_executor_admission", {}, "failed-function"),
        function_call("exec_command", {"cmd": "printf first > effect-first"}, "first-executor"),
        function_call("exec_command", {"cmd": "printf second > effect-second"}, "second-executor"),
    )
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certs"))
        async with NativeProvider(
            base_url=responses.url, api_key="synthetic-sibling-proof"
        ) as provider:
            agent = Agent(
                instructions="Use the three tools.",
                model="gpt-5.5",
                tools=[fail_after_executor_admission],
                provider=provider,
            )
            # The failed function supplies cancellation; process setup gets a normal budget.
            async with agent.session(
                environment=declaration(
                    peer,
                    transform_result=transform_result,
                    policy_timeout=10.0,
                    writable_roots=[str(peer.root)],
                )
            ) as session:
                turn = await session.submit("Run the admitted work.")
                waiter = asyncio.create_task(turn.get_result())
                try:
                    await both_entered.wait()
                    await cancellation_seen.wait()
                    assert effects == ["function failed once"]
                    assert len(entered) == 2 and len(set(entered)) == 2
                    assert (peer.root / "effect-first").read_text() == "first"
                    assert (peer.root / "effect-second").read_text() == "second"
                    for duration in (0.3, 0.5):
                        done, pending = await asyncio.wait({waiter}, timeout=duration)
                        assert not done and pending == {waiter}
                        assert cleaned == []
                    assert len(responses.requests) == 1
                finally:
                    release.set()
                    await asyncio.gather(waiter, return_exceptions=True)
                with pytest.raises(LookupError) as raised:
                    await waiter
                assert raised.value is problem
                assert sorted(cleaned) == sorted(entered)
                assert effects == ["function failed once"]
                assert len(responses.requests) == 1
                history = json.dumps([item async for item in session.items()])
                assert "selected sibling function failure" not in history
                assert len([r for r in peer.requests if r.get("method") == "process/start"]) == 2
