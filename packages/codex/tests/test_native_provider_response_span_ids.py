"""Provider-response ID admission uses the host loop and exact callback custody."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import Any, cast

import pytest

from _agents_codex import session as session_module
from _agents_codex.telemetry import _native_provider_response_span_id
from agents import telemetry
from tests.test_native_callback_lifetime import callback_owner
from tests.test_native_trace_exporter import native_trace_child_env


def message():
    return {"step_id": 7, "thread": "thread", "instance": None, "parent_context": "parent"}


def preparation_receipt():
    return {"kind": "preparation", "thread": "thread", "id": "7"}


@pytest.mark.parametrize(
    "outcome", ["valid", "invalid", "retired-during-allocation", "cancel-before-entry"]
)
async def test_host_response_id_admission_uses_exact_owner(monkeypatch, outcome):
    session, state, released = callback_owner()
    replies = []
    entered = []
    revoked = False
    loop = asyncio.get_running_loop()
    problem = ValueError("Tracing ID generator returned an invalid identifier")

    def policy(_context):
        nonlocal revoked
        # A worker-thread call or an extra callback execution path fails here.
        assert asyncio.get_running_loop() is loop
        entered.append(True)
        if outcome == "invalid":
            raise problem
        if outcome == "retired-during-allocation":
            revoked = True
        return {"span_id": "f" * 16}

    def check_step(*_args):
        if revoked:
            raise RuntimeError("Native owner retired")

    def reply(*args, **kwargs):
        replies.append((args, kwargs))

    monkeypatch.setattr(session_module, "_native_provider_response_span_id", policy)
    session._native._check_step = check_step
    session._native._reply_provider_response_span = reply
    task = session._start_native_callback(
        state, session._admit_provider_response_span(state, message()), preparation_receipt()
    )
    if outcome == "cancel-before-entry":
        session._cancel_native_callback(preparation_receipt())
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert released == [preparation_receipt()]
    if outcome == "cancel-before-entry":
        assert entered == [] and replies == [] and not state.failure.done()
    elif outcome == "valid":
        assert entered == [True] and not state.failure.done()
        assert len(replies) == 1
        args, kwargs = replies[0]
        assert args[0] == 7 and json.loads(args[1]) == {"span_id": "f" * 16}
        assert args[2:] == ("thread", None) and kwargs == {}
    else:
        assert entered == [True]
        assert replies == [((7, None, "thread", None), {"error": True})]
        if outcome == "invalid":
            assert state.failure.result() is problem
        else:
            assert not state.failure.done()


def test_host_response_id_policy_preserves_parent_without_an_extra_span():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; "
            "from tests.test_native_provider_response_span_ids import host_policy_case; "
            "asyncio.run(host_policy_case())",
        ],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS host provider-response policy" in result.stdout


async def host_policy_case():
    events = []

    class Recorder:
        def on_trace_event(self, event):
            events.append(event)

    await telemetry.configure(processors=[Recorder()])
    try:
        with telemetry.trace(
            "allocator-parent",
            options=telemetry.TraceOptions(trace_id="1" * 32, span_id="a" * 16),
        ) as parent:
            await telemetry.force_flush()
            before = list(events)
            calls = []
            loop = asyncio.get_running_loop()

            def generated():
                assert asyncio.get_running_loop() is loop
                assert telemetry.current_context().span_id == parent.context.span_id
                calls.append(True)
                return "b" * 16

            def wrong_trace():
                raise AssertionError("A model child must not generate a new trace ID")

            telemetry.set_tracing_id_generator(
                telemetry.TracingIdGenerator(
                    generate_trace_id=wrong_trace,
                    generate_span_id=generated,
                )
            )
            assert _native_provider_response_span_id(parent.context._handle) == {
                "span_id": "b" * 16
            }
            assert calls == [True]
            assert parent.context.trace_id == "1" * 32 and parent.context.span_id == "a" * 16
            await telemetry.force_flush()
            assert events == before
            for invalid in ["0" * 16, "z" * 16, "1" * 15, 17]:
                telemetry.set_tracing_id_generator(
                    telemetry.TracingIdGenerator(
                        generate_span_id=lambda value=invalid: cast(Any, value),
                    )
                )
                with pytest.raises((TypeError, ValueError), match="Tracing ID generator"):
                    _native_provider_response_span_id(parent.context._handle)
            telemetry.set_tracing_id_generator()
            assert _native_provider_response_span_id(parent.context._handle) == {"span_id": None}
        await telemetry.force_flush()
        print("PASS host provider-response policy")
    finally:
        telemetry.set_tracing_id_generator()
        await telemetry.shutdown()
