"""Preserve public results, retained observations and joined cancellation."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import pytest

from agents import Agent, NativeProvider, Session
from agents.observation import ModelRequestDispatching, ModelResponseLinked, OutputTextDelta
from tests._asyncio import timeout
from tests.native_fixtures import responses as responses
from tests.test_native_agent_observation import text_response

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="prototype retains the Windows single-reader route"
)


class ReadProbe:
    """Observe the actual native API while retaining its real futures/owners."""

    def __init__(self, native: Any) -> None:
        self.native = native
        self.batches: list[list[dict[str, Any]]] = []
        self.native_events: list[dict[str, Any] | None] = []
        self.batch_entered = asyncio.Event()
        self.cancelled_reads = 0
        self.barriers: list[bool] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)

    async def _next_session_event(self) -> str:
        try:
            raw = await self.native._next_session_event()
        except asyncio.CancelledError:
            self.cancelled_reads += 1
            raise
        self.native_events.append(json.loads(raw))
        return raw

    async def _next_session_event_batch(self, turn_id: str) -> Any:
        self.batch_entered.set()
        try:
            result = await self.native._next_session_event_batch(turn_id)
        except asyncio.CancelledError:
            self.cancelled_reads += 1
            raise
        packet = json.loads(result[0])
        self.batches.append(packet)
        self.native_events.extend(packet)
        self.barriers.append(result[2])
        return result


def instrument_reader(monkeypatch: pytest.MonkeyPatch) -> list[ReadProbe]:
    original = Session._read_events
    probes: list[ReadProbe] = []

    async def read(self: Session) -> None:
        probe = ReadProbe(self._native)
        probes.append(probe)
        self._native = probe
        await original(self)

    monkeypatch.setattr(Session, "_read_events", read)
    return probes


def normalized_observations(events: list[Any]) -> list[dict[str, Any]]:
    # Fresh sessions necessarily have different identities. Preserve identity
    # relationships, every other field, order, and original sequence numbers.
    identities: dict[str, str] = {}
    id_fields = {
        "session_id",
        "thread_id",
        "turn_id",
        "submission_id",
        "parent_submission_id",
        "request_id",
        "response_observation_id",
    }

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    identities.setdefault(item, f"identity-{len(identities)}")
                    if key in id_fields and isinstance(item, str)
                    else normalize(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value

    return [normalize(asdict(event)) for event in events]


@pytest.mark.parametrize("chunks", [("one",), tuple(f"part-{i};" for i in range(80))])
async def test_plain_run_preserves_full_retained_stream_against_single_read_control(
    responses, monkeypatch, chunks
):
    probes = instrument_reader(monkeypatch)
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-batch-key")
    responses.replies.append(text_response("out", *chunks))
    completed = await Agent(instructions="Answer directly.", provider=provider).run("input")
    retained = [event async for event in completed.turn.events()]
    assert completed.final_output == "".join(chunks)
    assert [event.delta for event in retained if isinstance(event, OutputTextDelta)] == list(chunks)
    assert any(probe.batches for probe in probes), (
        "eligible public run never exercised the collector"
    )
    if len(chunks) > 32:
        assert sum(len(probe.batches) for probe in probes) >= 3

    class OrdinaryControl(Agent):
        pass

    # Repeat the same synthetic response with its same provider response ID.
    responses.requests.clear()
    responses.replies.append(text_response("out", *chunks))
    start = len(probes)
    control = await OrdinaryControl(instructions="Answer directly.", provider=provider).run("input")
    ordinary = [event async for event in control.turn.events()]
    assert not any(probe.batches for probe in probes[start:])
    assert completed.final_output == control.final_output
    assert completed.usage == control.usage
    metadata_types = (ModelRequestDispatching, ModelResponseLinked)
    for events, result, readers in (
        (retained, completed, probes[:start]),
        (ordinary, control, probes[start:]),
    ):
        assert len(readers) == 1
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        native_projection: list[tuple[Any, ...]] = []
        for event in readers[0].native_events:
            if event is None:
                continue
            message = event.get("msg", {})
            if message.get("type") == "_sdk_request_metadata":
                observation = message["observation"]
                native_projection.append(
                    (
                        observation["type"],
                        observation["request_id"],
                        observation.get("response_observation_id"),
                        observation["thread_id"],
                        observation["turn_id"],
                        message["submission_id"],
                    )
                )
            elif message.get("type") == "agent_message_content_delta":
                native_projection.append(
                    ("output_text_delta", message["item_id"], message["delta"])
                )
        retained_projection: list[tuple[Any, ...]] = []
        for event in events:
            if isinstance(event, metadata_types):
                retained_projection.append(
                    (
                        event.type,
                        event.request_id,
                        event.response_observation_id
                        if isinstance(event, ModelResponseLinked)
                        else None,
                        event.thread_id,
                        event.turn_id,
                        event.submission_id,
                    )
                )
            elif isinstance(event, OutputTextDelta):
                retained_projection.append((event.type, event.item_id, event.delta))
        assert native_projection == retained_projection
        dispatches = [event for event in events if isinstance(event, ModelRequestDispatching)]
        links = [event for event in events if isinstance(event, ModelResponseLinked)]
        assert len(dispatches) == len(links) == 1
        dispatch, link = dispatches[0], links[0]
        assert dispatch.sequence < link.sequence
        assert dispatch.request_id == link.request_id
        assert all(
            event.session_id == dispatch.session_id
            and event.turn_id == dispatch.turn_id
            and event.submission_id == dispatch.submission_id
            for event in events
        )
        assert dispatch.thread_id == dispatch.session_id == link.thread_id == link.session_id
        assert dispatch.turn_id == link.turn_id == result.turn.turn_id
        assert dispatch.submission_id == link.submission_id == result.turn.submission_id
        assert dispatch.parent_submission_id is link.parent_submission_id is None

    def ordered_payloads(events: list[Any], metadata: bool) -> list[dict[str, Any]]:
        # Keep actual sequences above. Only independent-run payload comparison
        # ignores positions where the two native queues can legitimately merge.
        selected = [event for event in events if isinstance(event, metadata_types) == metadata]
        payloads = normalized_observations(selected)
        for payload in payloads:
            del payload["sequence"]
        return payloads

    assert ordered_payloads(retained, False) == ordered_payloads(ordinary, False)
    assert ordered_payloads(retained, True) == ordered_payloads(ordinary, True)


@pytest.mark.parametrize("spoof", [False, True])
async def test_delegating_or_spoofed_factory_keeps_live_cursor_semantics(
    responses, monkeypatch, spoof
):
    probes = instrument_reader(monkeypatch)
    original = Agent.session
    live: list[Any] = []

    @asynccontextmanager
    async def delegate(self, **kwargs):
        async with original(self, **kwargs) as session:
            observer = asyncio.create_task(collect(session.events()))
            try:
                yield session
            finally:
                # The outer context closes the stream. Join below, after run.
                observers.append(observer)

    async def collect(stream):
        async for event in stream:
            live.append(event)

    observers = []
    agent = Agent(
        instructions="Answer directly.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-batch-key"),
    )
    if spoof:

        class Spoof:
            def __init__(self):
                self.__self__ = agent
                self.__func__ = original

            def __call__(self, **kwargs):
                return delegate(agent, **kwargs)

        monkeypatch.setattr(agent, "session", Spoof())
    else:
        # A class override is a genuine bound MethodType, but its function must
        # not become the captured original merely because the class changed.
        monkeypatch.setattr(Agent, "session", delegate)
    responses.replies.append(text_response("out", "first", "second"))
    completed = await agent.run("input")
    await asyncio.gather(*observers)
    assert completed.final_output == "firstsecond"
    assert [event.delta for event in live if isinstance(event, OutputTextDelta)] == [
        "first",
        "second",
    ]
    assert not any(probe.batches for probe in probes)


async def test_factory_present_at_reader_creation_declines_after_reset(responses, monkeypatch):
    probes = instrument_reader(monkeypatch)
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    original = Session._read_events

    async def reset_then_read(self):
        loop.set_task_factory(None)
        await original(self)

    monkeypatch.setattr(Session, "_read_events", reset_then_read)

    def factory(loop, coro, **kwargs):
        return asyncio.Task(coro, loop=loop, **kwargs)

    loop.set_task_factory(factory)
    try:
        responses.replies.append(text_response("out", "complete"))
        result = await Agent(
            instructions="Answer directly.",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-batch-key"),
        ).run("input")
    finally:
        loop.set_task_factory(previous)
    assert result.final_output == "complete"
    assert probes and not any(probe.batches for probe in probes)


async def test_cancel_owned_run_receives_abort_without_cancelling_reader(responses, monkeypatch):
    probes = instrument_reader(monkeypatch)
    responses.delta_gate = asyncio.Event()
    emitted = asyncio.Event()

    async def before_event(event):
        if event["type"] == "response.output_text.delta":
            emitted.set()

    responses.before_event = before_event
    responses.replies.append(text_response("out", "partial", "later"))
    agent = Agent(
        instructions="Answer directly.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-batch-key"),
    )
    running = asyncio.create_task(agent.run("input"))
    try:
        async with timeout(10):
            await emitted.wait()
            assert probes
            await probes[-1].batch_entered.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        assert all(probe.cancelled_reads == 0 for probe in probes)
        assert any(
            event["msg"]["type"] == "turn_aborted"
            for probe in probes
            for batch in probe.batches
            for event in batch
        )
    finally:
        responses.delta_gate.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize(
    "case", ["none", "logs", "traces", "metrics", "processors", "parent", "scope"]
)
def test_fresh_telemetry_configuration_eligibility(case):
    # Subscriber configuration is process-terminal. Reuse the existing
    # telemetry-test subprocess pattern rather than resetting native state.
    import os
    import subprocess
    from pathlib import Path

    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, sys; "
            "from tests.test_native_plain_event_collection import _telemetry_case; "
            "asyncio.run(asyncio.wait_for(_telemetry_case(sys.argv[1]), 30))",
            case,
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def _telemetry_case(case):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from agents import telemetry
    from tests.native_fixtures import ResponsesFixture

    class Processor:
        def on_trace_event(self, event):
            pass

    fixture = ResponsesFixture()
    fixture.replies.append(text_response("out", "first", "second"))
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)

    async def exported(request):
        await request.read()
        return web.Response(body=b"", content_type="application/x-protobuf")

    app.router.add_post("/otlp", exported)
    async with TestServer(app) as server:
        exporter = telemetry.OtlpHttp(endpoint=str(server.make_url("/otlp")))
        options = {}
        if case in {"logs", "traces", "metrics"}:
            options[case] = exporter
        if case in {"processors", "parent", "scope"}:
            options["processors"] = [Processor()]
        # The empty options case deliberately performs the same supported
        # all-None configure call as the unchanged frozen v1 workload.
        await telemetry.configure(**options)
        with pytest.MonkeyPatch.context() as patch:
            probes = instrument_reader(patch)
            eligible = []
            original = Session._plain_event_collection_ready

            def ready(self, state):
                result = original(self, state)
                if result:
                    eligible.append((state.tracing.context, state.tracing.scope))
                return result

            patch.setattr(Session, "_plain_event_collection_ready", ready)
            agent = Agent(
                instructions="Answer directly.",
                provider=NativeProvider(
                    base_url=str(server.make_url("/v1")), api_key="synthetic-batch-key"
                ),
            )
            if case == "parent":
                with telemetry.trace("parent"):
                    completed = await agent.run("input")
            elif case == "scope":
                completed = await agent.run("input", trace_options={})
            else:
                completed = await agent.run("input")
            assert completed.final_output == "firstsecond"
            if case == "none":
                assert eligible and all(pair == (None, None) for pair in eligible)
                assert any(len(packet) > 1 for probe in probes for packet in probe.batches), (
                    "all-None configure never drained a batch"
                )
            elif case in {"logs", "traces", "metrics"}:
                # The native process-configuration fact rejects collection. Its
                # one-record barrier permanently returns this reader to the
                # original route; Python never suppresses that record's yield.
                attempted = [probe for probe in probes if probe.batches]
                assert attempted
                assert all(
                    len(probe.batches) == 1
                    and len(probe.batches[0]) == 1
                    and probe.barriers == [True]
                    for probe in attempted
                )
            else:
                assert not eligible and not any(probe.batches for probe in probes)
        await telemetry.shutdown()


def test_before_compact_run_preserves_metrics_off_ordinary_route():
    _run_before_compact_case("run")


def test_fresh_provider_sessions_keep_compaction_callbacks_isolated():
    _run_before_compact_case("provider")


def _run_before_compact_case(case):
    # Keep native telemetry process-terminal, matching the existing cases above.
    import os
    import subprocess
    from pathlib import Path

    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, sys; "
            "from tests.test_native_plain_event_collection import _before_compact_case; "
            "asyncio.run(asyncio.wait_for(_before_compact_case(sys.argv[1]), 30))",
            case,
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=40,
    )
    assert child.returncode == 0, child.stdout + child.stderr


async def _before_compact_case(case):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from agents import telemetry
    from tests.native_fixtures import ResponsesFixture

    fixture = ResponsesFixture()
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    async with TestServer(app) as server:
        fixture.url = str(server.make_url("/v1"))
        await telemetry.configure()
        try:
            with pytest.MonkeyPatch.context() as patch:
                probes = instrument_reader(patch)
                async with NativeProvider(
                    base_url=fixture.url, api_key="synthetic-compaction-key"
                ) as provider:
                    if case == "run":
                        await _automatic_compaction_run(fixture, provider, probes, patch)
                    else:
                        assert case == "provider"
                        await _fresh_provider_compaction(fixture, provider, probes)
        finally:
            await telemetry.shutdown()


def _assert_compaction_session_retired(session):
    # Native close may end a read with "thread is closed" instead of null EOF.
    # The owner must still join its reader and retire the Session resources.
    assert session._closed and session._resources_retired
    assert session._reader.done() and not session._reader.cancelled()
    assert session._reader.exception() is None


async def _automatic_compaction_run(fixture, provider, probes, patch):
    from agents import _native
    from agents.observation import ContextCompacted, TurnFinished

    create = _native.create_engine
    start = _native.Engine.start_replaceable_thread

    def configure(config):
        # Exercise genuine post-turn compaction without tools, a return_state
        # option, or an overridden Session factory hiding callback eligibility.
        config["model_context_window"] = 100_000
        config["model_post_turn_compact_threshold_percent"] = 50

    def create_with_threshold(config_json, *args, **kwargs):
        config = json.loads(config_json)
        configure(config)
        return create(json.dumps(config), *args, **kwargs)

    async def start_with_threshold(engine, options_json, specs_json):
        options = json.loads(options_json)
        configure(options["config"])
        return await start(engine, json.dumps(options), specs_json)

    patch.setattr(_native, "create_engine", create_with_threshold)
    patch.setattr(_native.Engine, "start_replaceable_thread", start_with_threshold)
    snapshots = []
    exit_scope = Session._exit_scope

    async def save_before_close(self):
        try:
            snapshots.append((self.id, await self.save_state()))
        finally:
            await exit_scope(self)
            _assert_compaction_session_retired(self)

    patch.setattr(Session, "_exit_scope", save_before_close)
    calls = []
    summary = "Preserve the amber compaction witness."

    async def compact(request):
        calls.append(request)
        assert request.trigger == "auto" and request.subagent_id is None
        assert request.context is None
        assert "Remember amber." in json.dumps(request.items)
        assert "Remembered." in json.dumps(request.items)
        return summary

    async def high_usage(event):
        if event["type"] == "response.completed":
            event["response"]["usage"].update(
                input_tokens=79_000, output_tokens=1_000, total_tokens=80_000
            )

    fixture.before_event = high_usage
    fixture.respond(*text_response("remembered", "Remembered."))
    result = await Agent(
        instructions="Answer directly.", provider=provider, before_compact=compact
    ).run("Remember amber.")
    assert result.final_output == "Remembered."
    assert await result.turn.get_result() is result
    assert len(calls) == len(snapshots) == len(fixture.requests) == 1
    assert not fixture.replies
    session_id, saved = snapshots[0]
    assert calls[0].session_id == session_id
    assert calls[0].turn_id == result.turn.turn_id
    compacted = [row["payload"] for row in saved.history if row["type"] == "compacted"]
    assert len(compacted) == 1 and summary in compacted[0]["message"]
    assert compacted[0].get("compaction_response_id") is None
    assert "Remember amber." in saved.to_json()
    events = [event async for event in result.turn.events()]
    assert any(isinstance(event, ContextCompacted) for event in events)
    assert [event.status for event in events if isinstance(event, TurnFinished)] == ["completed"]
    assert len(probes) == 1 and not probes[0].batches
    assert any(
        event is not None and event["msg"]["type"] == "_sdk_before_compact"
        for event in probes[0].native_events
    )
    assert probes[0].cancelled_reads == 0


async def _fresh_provider_compaction(fixture, provider, probes):
    calls = []
    session_ids = []
    summaries = (
        "Keep the amber provider witness.",
        "Keep the violet provider witness.",
    )
    for index, summary in enumerate(summaries):
        # A fresh closure belongs to each Agent declaration.
        async def compact(request, *, expected=summary, owner=index):
            calls.append((owner, request.session_id, request.trigger))
            assert request.subagent_id is None and request.context is None
            assert f"Remember witness {owner}." in json.dumps(request.items)
            return expected

        fixture.respond(*text_response(f"remember-{index}", "Remembered."))
        fixture.respond(*text_response(f"continue-{index}", "Continued."))
        agent = Agent(instructions="Answer directly.", provider=provider, before_compact=compact)
        request_start = len(fixture.requests)
        async with agent.session() as session:
            session_ids.append(session.id)
            first = await (await session.submit(f"Remember witness {index}.")).get_result()
            assert first.final_output == "Remembered."
            original = await session.save_state()
            await session.compact()
            assert len(fixture.requests) == request_start + 1
            saved = await session.save_state()
            assert saved.history[: len(original.history)] == original.history
            compacted = [row["payload"] for row in saved.history if row["type"] == "compacted"]
            assert len(compacted) == 1 and summary in compacted[0]["message"]
            assert compacted[0].get("compaction_response_id") is None
            assert summaries[1 - index] not in saved.to_json()
            result = await (await session.submit("Continue.")).get_result()
            assert result.final_output == "Continued."
            assert summary in json.dumps(fixture.requests[-1]["input"])
            assert summaries[1 - index] not in json.dumps(fixture.requests[-1])
        _assert_compaction_session_retired(session)
        assert len(fixture.requests) == request_start + 2
    assert len(set(session_ids)) == 2
    assert calls == [(index, session_id, "manual") for index, session_id in enumerate(session_ids)]
    assert len(probes) == 2
    assert not any(probe.cancelled_reads for probe in probes)
    assert not fixture.replies
