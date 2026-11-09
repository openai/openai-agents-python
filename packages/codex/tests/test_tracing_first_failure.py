"""First-failure ownership and concurrent dynamic-script admission regressions."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from agents import telemetry
from agents.testing import ScriptedResponses, text
from tests.test_testing import request


def test_rejected_key_rotation_drops_exporter_from_library_traceback():
    exporter = telemetry.OpenAITraceExporter(api_key="synthetic-prior-private-key")
    original = exporter._credentials
    with pytest.raises(ValueError) as failure:
        exporter.set_api_key("bad\nreplacement")
    traceback = failure.value.__traceback__
    while traceback:
        frame = traceback.tb_frame
        if frame.f_globals.get("__name__") == "_agents_codex._trace_export":
            assert all(value is not exporter for value in frame.f_locals.values())
        traceback = traceback.tb_next
    assert exporter._credentials is original


@pytest.mark.parametrize("cancelled", [False, True])
async def test_fixture_preserves_primary_body_error_and_callback_context(cancelled):
    callback = ValueError("synthetic-callback-failure")
    primary = RuntimeError("synthetic-primary-body-failure")
    ready = asyncio.Event()

    def broken(_body):
        raise callback

    async def run():
        async with ScriptedResponses(broken) as peer:
            status, _, _ = await request(peer.provider, "POST", "/v1/responses", {"input": []})
            assert status == 400
            ready.set()
            if cancelled:
                await asyncio.Event().wait()
            raise primary

    task = asyncio.create_task(run())
    await asyncio.wait_for(ready.wait(), 2)
    if cancelled:
        task.cancel("synthetic-primary-cancellation")
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError) as raised:
        await task
    assert task.cancelled() is cancelled
    if cancelled:
        assert raised.value.args == ("synthetic-primary-cancellation",)
    else:
        assert raised.value is primary
    contexts = []
    current = raised.value.__context__
    while current is not None and current not in contexts:
        contexts.append(current)
        current = current.__context__
    assert callback in contexts


async def test_concurrent_callback_failure_fences_dequeue_and_allows_queue_mutation():
    entered, release, following = Event(), Event(), Event()
    callback = ValueError("synthetic-concurrent-callback-failure")
    peer = None

    def next_script(_body):
        following.set()
        return [text("must remain queued")]

    def broken(_body):
        entered.set()
        assert release.wait(3)
        peer.clear()
        peer.append(next_script)
        raise callback

    peer = ScriptedResponses(broken, next_script)
    with pytest.raises(ValueError) as raised:
        async with peer:
            first = asyncio.create_task(
                request(peer.provider, "POST", "/v1/responses", {"input": []})
            )
            assert await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(
                request(peer.provider, "POST", "/v1/responses", {"input": []})
            )
            try:

                async def both_reached():
                    while len(peer.requests) < 2:
                        await asyncio.sleep(0.001)

                await asyncio.wait_for(both_reached(), 2)
                ran_before_release = following.is_set()
            finally:
                release.set()
                results = await asyncio.gather(first, second)
    assert raised.value is callback
    assert not ran_before_release
    assert not following.is_set()
    assert [row[0] for row in results] == [400, 400]


async def native_body_primary():
    await telemetry.configure(processors=[])
    for primary in (
        RuntimeError("synthetic-private-body-error"),
        asyncio.CancelledError("synthetic-body-cancel"),
    ):
        scope = telemetry.span("end-and-body-failure")
        try:
            with scope:
                scope.set_outcome(status="ok", data={"private": "synthetic-private-outcome" * 300})
                raise primary
        except BaseException as error:
            assert error is primary
            assert scope._outcome is None
            assert scope._span_handle is None
            assert telemetry.current_context() is None
            assert isinstance(error.__context__, RuntimeError)
            assert str(error.__context__) == "Native trace span end failed"
            assert error.__context__.__traceback__ is None
        else:
            raise AssertionError("body failure suppressed")
    with telemetry.span("healthy-after-failure"):
        pass
    await telemetry.force_flush()
    await telemetry.shutdown()
    print("PASS real native primary span body errors")


def test_real_native_end_failure_preserves_body_error_and_cancellation():
    script = (
        "import asyncio,runpy; m=runpy.run_path("
        + repr(str(Path(__file__).resolve()))
        + ");asyncio.run(m['native_body_primary']())"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS real native primary span body errors" in result.stdout


async def test_successful_callback_releases_concurrent_requests_in_recorded_order():
    entered, release = Event(), Event()
    called = []

    def respond(body):
        called.append(body["tag"])
        if body["tag"] == 1:
            entered.set()
            assert release.wait(3)
        return [text(str(body["tag"]))]

    async with ScriptedResponses(respond, respond, respond) as peer:
        first = asyncio.create_task(request(peer.provider, "POST", "/v1/responses", {"tag": 1}))
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(request(peer.provider, "POST", "/v1/responses", {"tag": 2}))

        async def arrived(count):
            while len(peer.requests) < count:
                await asyncio.sleep(0.001)

        try:
            await asyncio.wait_for(arrived(2), 2)
            third = asyncio.create_task(request(peer.provider, "POST", "/v1/responses", {"tag": 3}))
            await asyncio.wait_for(arrived(3), 2)
        finally:
            release.set()
        results = await asyncio.gather(first, second, third)
    assert called == [1, 2, 3]
    assert [row[0] for row in results] == [200, 200, 200]
