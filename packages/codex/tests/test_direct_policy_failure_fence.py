"""Real Core AFTER-policy ordering with a synthetic non-executing executor reply."""

import asyncio
import json

import pytest

from agents import Agent, NativeError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_executor_policies import OutputEvents
from tests.test_native_executor_tools import declaration


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_direct_result_failure_fences_followup_before_python_interrupt(
    responses: ResponsesFixture, tmp_path, monkeypatch, failure
):
    private = "synthetic-private-policy-fence-d1b3"
    callback_values = []
    native_terminal = asyncio.Event()
    terminal_events = []
    interrupt_entered = asyncio.Event()
    release_interrupt = asyncio.Event()
    held = asyncio.Event()

    async def fail(context, value):
        callback_values.append(private in json.dumps(value))
        assert context.call_id == "direct-fail"
        if failure == "timeout":
            await asyncio.Future()
        raise ValueError(private)

    responses.respond(
        function_call(
            "exec_command", {"cmd": "synthetic-direct-policy-no-execution"}, "direct-fail"
        )
    )
    responses.respond(assistant_output("unexpected followup"))
    async with (
        timeout(20),
        ExecutorPeer(tmp_path / "remote", synthetic_process_output=private).serve() as peer,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        selected = declaration(
            peer,
            transform_result=fail,
            approval=lambda *_: True,
            policy_timeout=0.05 if failure == "timeout" else 10,
        )
        async with Agent(
            instructions="Use the synthetic result.", tools=[], provider=provider(responses)
        ).session(environment=selected) as session:

            class GatedInterrupt(OutputEvents):
                async def _next_session_event(self):
                    raw = await super()._next_session_event()
                    event = json.loads(raw)
                    if event.get("msg", {}).get("type") in {
                        "task_complete",
                        "turn_complete",
                        "turn_aborted",
                    }:
                        terminal_events.append(event)
                        native_terminal.set()
                    return raw

                def _hold_native_interception_for_stop(self, nonce):
                    accepted = self.native._hold_native_interception_for_stop(nonce)
                    if accepted:
                        held.set()
                    return accepted

                async def _interrupt_session(self, *args, **kwargs):
                    interrupt_entered.set()
                    await release_interrupt.wait()
                    return await self.native._interrupt_session(*args, **kwargs)

            captured = GatedInterrupt(session._native)
            session._native = captured
            turn = await session.submit("Inspect the controlled result.")
            result = asyncio.create_task(turn.get_result())
            try:
                await interrupt_entered.wait()
                if failure == "timeout":
                    # Native expiry can finish its rejected step while the
                    # Python interruption remains held at this barrier.
                    await asyncio.wait_for(native_terminal.wait(), 5)
                    assert any(event["id"] == turn.turn_id for event in terminal_events)
                else:
                    assert held.is_set()
                    assert not native_terminal.is_set()
                facts = {
                    "requests": len(responses.requests),
                    "held": held.is_set(),
                    "native_terminal": native_terminal.is_set(),
                    "public_result_pending": not result.done(),
                    "private_in_requests": private in json.dumps(responses.requests),
                    "withheld_marker_in_followup": any(
                        "Native tool output withheld by result policy" in json.dumps(req)
                        for req in responses.requests[1:]
                    ),
                    "followup_has_tool_output": any(
                        item.get("call_id") == "direct-fail"
                        and item.get("type") in {"function_call_output", "custom_tool_call_output"}
                        for req in responses.requests[1:]
                        for item in req.get("input", [])
                        if isinstance(item, dict)
                    ),
                }
                print("DIRECT-POLICY-FENCE: " + json.dumps(facts))
                assert len(responses.requests) == 1, facts
                assert not result.done()
            finally:
                release_interrupt.set()
                with pytest.raises(NativeError, match="^Executor policy failed$") as error:
                    await result
                assert error.value.__cause__ is error.value.__context__ is None
            history = json.loads(await captured.read_thread_history(None))
        assert callback_values == [True]
        assert len(peer.synthetic_process_starts) == 1
        assert sum(req.get("method") == "process/start" for req in peer.requests) == 1
        assert not (peer.root / "effect").exists()
        assert private not in json.dumps([captured.events, history, responses.requests])
