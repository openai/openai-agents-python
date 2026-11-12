"""Native provider policy precedence has been preserving private default callbacks."""

import sys
from io import BytesIO
from pathlib import Path

import pytest

from agents.sandbox import CallbackSink, ChainedSink, EventPayloadPolicy, Instrumentation, Manifest

if sys.platform != "win32":
    from agents.classic.sandbox.sandboxes.unix_local import UnixLocalSandboxClient


@pytest.mark.skipif(sys.platform == "win32", reason="UnixLocal provider")
@pytest.mark.parametrize("topology", ["direct", "chain", "nested"])
@pytest.mark.parametrize("scope", ["default", "global", "operation", "sink"])
async def test_native_provider_payload_policies_keep_callback_bytes_private(
    tmp_path: Path, topology: str, scope: str
):
    seen = {"selected": [], "private": []}
    enabled = EventPayloadPolicy(
        include_exec_output=True, include_write_len=True, max_stdout_chars=3
    )
    selected_policy = enabled if scope == "sink" else EventPayloadPolicy(max_stdout_chars=3)
    selected = CallbackSink(
        lambda event, _: seen["selected"].append(event), payload_policy=selected_policy
    )
    private = CallbackSink(
        lambda event, _: seen["private"].append(event),
        payload_policy=EventPayloadPolicy(include_exec_output=False, include_write_len=False),
    )
    if topology == "direct":
        sinks = [selected, private]
    elif topology == "chain":
        sinks = [ChainedSink(selected, private)]
    else:
        sinks = [ChainedSink(ChainedSink(selected), ChainedSink(private))]
    instrumentation = Instrumentation(
        sinks=sinks,
        payload_policy=enabled if scope == "global" else None,
        payload_policy_by_op={"exec": enabled, "write": enabled} if scope == "operation" else None,
    )
    client = UnixLocalSandboxClient(instrumentation=instrumentation, inherit_host_environment=False)
    resource = await client.create(manifest=Manifest(root=str(tmp_path)))
    try:
        await resource.start()
        await resource.exec("printf ABCDEF; printf GHIJKL >&2")
        await resource.write("private-path.txt", BytesIO(b"seven!!"))
    finally:
        await resource.aclose()
        await client.delete(resource)
    for name, events in seen.items():
        finish = next(event for event in events if event.op == "exec" and event.phase == "finish")
        write = next(event for event in events if event.op == "write" and event.phase == "start")
        if name == "selected" and scope != "default":
            assert finish.stdout == "ABC…" and finish.stderr == "GHIJKL"
            assert finish.stdout_bytes is finish.stderr_bytes is None
            assert write.data == {"bytes": 7}
        else:
            assert finish.stdout is finish.stderr is None
            assert finish.stdout_bytes is finish.stderr_bytes is None
            assert write.data == {}
        assert all("private-path.txt" not in str(event.data) for event in events)


@pytest.mark.parametrize("source", ["bytes", "text", "surrogate"])
async def test_native_output_capture_has_a_hard_ceiling_and_tolerates_unicode(source):
    from uuid import uuid4

    from agents.sandbox import EventSink, SandboxSessionFinishEvent

    seen = []

    class Capture(EventSink):
        mode = "sync"
        on_error = "raise"
        payload_policy = EventPayloadPolicy(include_exec_output=True, max_stdout_chars=10**9)

        async def handle(self, event):
            seen.append(event)

    text = "prefix\ud800suffix" if source == "surrogate" else "x" * (128 * 1024)
    event = SandboxSessionFinishEvent(
        session_id=uuid4(),
        seq=1,
        op="exec",
        span_id="fixture",
        ok=True,
        duration_ms=1,
        stdout_bytes=text.encode() if source == "bytes" else None,
        stdout=text if source != "bytes" else None,
    )
    await Instrumentation(sinks=[Capture()]).emit(event)
    assert len(seen) == 1
    assert seen[0].stdout_bytes is seen[0].stderr_bytes is None
    if source == "surrogate":
        assert seen[0].stdout == "prefix?suffix"
    else:
        assert seen[0].stdout == "x" * (64 * 1024)


@pytest.mark.parametrize("mode", ["sync", "async", "best_effort"])
@pytest.mark.parametrize("on_error", ["raise", "log", "ignore"])
@pytest.mark.parametrize("nested", [False, True])
async def test_native_sink_error_policies_keep_existing_delivery_contract(mode, on_error, nested):
    from uuid import uuid4

    from agents.sandbox import EventSink, SandboxSessionStartEvent

    seen = []

    class Failure(EventSink):
        payload_policy = None

        async def handle(self, event):
            raise ValueError("synthetic sink failure")

    class Following(EventSink):
        mode = "sync"
        on_error = "raise"
        payload_policy = None

        async def handle(self, event):
            seen.append(event)

    failure = Failure()
    failure.mode = mode
    failure.on_error = on_error
    sinks = [failure, Following()]
    if nested:
        sinks = [ChainedSink(ChainedSink(*sinks))]
    instrumentation = Instrumentation(sinks=sinks)
    event = SandboxSessionStartEvent(session_id=uuid4(), seq=1, op="start", span_id="fixture")
    if on_error == "raise" and mode != "best_effort":
        expected = ValueError if mode == "async" and not nested else RuntimeError
        with pytest.raises(expected):
            await instrumentation.emit(event)
        assert seen == []
    else:
        await instrumentation.emit(event)
        await instrumentation.flush()
        assert len(seen) == 1
