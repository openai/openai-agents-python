"""Owned provider lifecycle events retain resource identity and bounded delivery."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from agents import Agent, Environment, ModelResponse, NativeProvider
from agents.classic.sandbox.session.manager import Instrumentation as ClassicInstrumentation
from agents.sandbox import (
    CallbackSink,
    ChainedSink,
    EventPayloadPolicy,
    EventSink,
    HttpProxySink,
    Instrumentation,
    JsonlOutboxSink,
    Manifest,
    SandboxSessionFinishEvent,
    SandboxSessionStartEvent,
    WorkspaceJsonlSink,
    sandbox_environment,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import responses as responses

if sys.platform != "win32":
    from agents.classic.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

unix = pytest.mark.skipif(sys.platform == "win32", reason="UnixLocal requires Unix")


class StalledSink(EventSink):
    mode = "best_effort"
    on_error = "ignore"
    payload_policy = None

    def __init__(self, expected=1):
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.count = 0
        self.finished = 0
        self.expected = expected

    async def handle(self, event):
        self.count += 1
        if self.count == self.expected:
            self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.finished += 1
            if self.finished == self.expected:
                self.cancelled.set()


async def test_optional_delivery_is_capped_and_sink_removal_cancels_it():
    sink = StalledSink(expected=2)
    instrumentation = Instrumentation(sinks=[sink], max_pending_events=2, delivery_timeout_s=30)
    event = SandboxSessionStartEvent(session_id=uuid4(), seq=1, op="start", span_id="fixture")
    async with timeout(5):
        for _ in range(100):
            await instrumentation.emit(event)
        await sink.entered.wait()
        assert sink.count == 2
        assert instrumentation.remove_sink(sink)
        await sink.cancelled.wait()
        assert not instrumentation.remove_sink(sink)
        await instrumentation.emit(event)
        assert sink.count == 2
        await instrumentation.flush()


async def test_optional_delivery_has_a_deadline():
    sink = StalledSink()
    instrumentation = Instrumentation(sinks=[sink], delivery_timeout_s=0.01)
    async with timeout(5):
        await instrumentation.emit(
            SandboxSessionStartEvent(session_id=uuid4(), seq=1, op="start", span_id="fixture")
        )
        await sink.entered.wait()
        await sink.cancelled.wait()
        await instrumentation.flush()


@unix
async def test_two_native_sources_do_not_cross_route_session_aware_sinks(tmp_path: Path):
    observed = []
    order = []

    def first(event, session):
        observed.append((event.session_id, session.state.session_id, event.op, event.phase))
        order.append((event.event_id, "first"))

    def second(event, session):
        assert event.session_id == session.state.session_id
        order.append((event.event_id, "second"))

    instrumentation = Instrumentation(
        sinks=[ChainedSink(CallbackSink(first), CallbackSink(second))]
    )
    client = UnixLocalSandboxClient(instrumentation=instrumentation, inherit_host_environment=False)
    selected = []

    @asynccontextmanager
    async def connect(session):
        selected.append(session)
        yield Environment(url="wss://executor.invalid", cwd=session.state.manifest.root)
        assert session in selected

    managers = [
        sandbox_environment(
            client, manifest=Manifest(root=str(tmp_path / str(i))), options=None, connect=connect
        )
        for i in range(2)
    ]
    async with timeout(10):
        await asyncio.gather(*(manager.__aenter__() for manager in managers))
        first_id, second_id = [session.state.session_id for session in selected]
        assert first_id != second_id
        await managers[0].__aexit__(None, None, None)
        await managers[1].__aexit__(None, None, None)
    assert len(observed) == 12
    assert all(event_id == resource_id for event_id, resource_id, _, _ in observed)
    for resource in (first_id, second_id):
        assert [(op, phase) for event, _, op, phase in observed if event == resource] == [
            (op, phase) for op in ("start", "stop", "shutdown") for phase in ("start", "finish")
        ]
    assert all(
        order[i][0] == order[i + 1][0]
        and order[i : i + 2][0][1] == "first"
        and order[i + 1][1] == "second"
        for i in range(0, len(order), 2)
    )


@unix
async def test_native_cleanup_does_not_wait_for_a_stalled_observer():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    selected = []

    async def observer(event, session):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await release.wait()

    instrumentation = Instrumentation(
        sinks=[CallbackSink(observer, mode="best_effort")], max_pending_events=1
    )
    client = UnixLocalSandboxClient(instrumentation=instrumentation, inherit_host_environment=False)

    @asynccontextmanager
    async def connect(session):
        selected.append(session)
        yield Environment(url="wss://executor.invalid", cwd=session.state.manifest.root)

    manager = sandbox_environment(client, manifest=Manifest(), options=None, connect=connect)
    try:
        async with timeout(10):
            await manager.__aenter__()
            await entered.wait()
            await manager.__aexit__(None, None, None)
            await cancelled.wait()
            assert not release.is_set()
            assert not Path(selected[0].state.manifest.root).exists()
    finally:
        release.set()
        await instrumentation.flush()


@unix
async def test_native_failure_events_exclude_provider_exception_details(
    tmp_path: Path, monkeypatch
):
    observed = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda event, session: observed.append(event))]
    )

    class Client(UnixLocalSandboxClient):
        async def create(self, **kwargs):
            session = await super().create(**kwargs)

            async def fail():
                error = RuntimeError("provider-private-token")
                error.private_token = "arbitrary-private-field"
                raise error

            monkeypatch.setattr(session._inner, "start", fail)
            return session

    client = Client(instrumentation=instrumentation, inherit_host_environment=False)

    @asynccontextmanager
    async def connect(session):
        pytest.fail("A failed resource start reached the gateway")
        yield

    with pytest.raises(RuntimeError, match="provider-private-token"):
        async with sandbox_environment(
            client, manifest=Manifest(root=str(tmp_path)), options=None, connect=connect
        ):
            pass
    encoded = json.dumps([event.model_dump(mode="json") for event in observed])
    assert "provider-private-token" not in encoded and "arbitrary-private-field" not in encoded
    assert any(
        event.op == "start" and event.phase == "finish" and not event.ok for event in observed
    )
    assert [(event.op, event.phase) for event in observed][-2:] == [
        ("shutdown", "start"),
        ("shutdown", "finish"),
    ]


@unix
@pytest.mark.parametrize("instrumentation_type", [ClassicInstrumentation, Instrumentation])
async def test_native_session_uses_provider_instrumentation_without_replacing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instrumentation_type
):
    seen = []
    instrumentation = instrumentation_type(
        sinks=[
            CallbackSink(
                lambda event, session: seen.append((event.session_id, session.state.session_id))
            )
        ]
    )
    client = UnixLocalSandboxClient(instrumentation=instrumentation, inherit_host_environment=False)
    peer = ExecutorPeer(tmp_path / "executor")

    class Model:
        def complete(self, request):
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ]
            )

    @asynccontextmanager
    async def connect(session):
        yield Environment(
            url=peer.url,
            cwd=session.state.manifest.root,
            headers={"Authorization": "Bearer " + peer.token},
        )

    async with timeout(20), peer.serve():
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        async with NativeProvider(model_backend=Model()) as provider:
            environment = sandbox_environment(
                client,
                manifest=Manifest(root=str(tmp_path / "workspace")),
                options=None,
                connect=connect,
            )
            async with Agent(instructions="Answer.", provider=provider).session(
                environment=environment
            ) as session:
                assert (await (await session.submit("once")).get_result()).final_output == "done"
    assert len(seen) == 6 and all(event == resource for event, resource in seen)


@unix
async def test_native_workspace_sink_retains_its_finite_budget(tmp_path: Path):
    sink = WorkspaceJsonlSink(mode="sync", on_error="ignore", max_bytes=1000)
    instrumentation = Instrumentation(
        sinks=[sink], payload_policy=EventPayloadPolicy(include_exec_output=False)
    )
    client = UnixLocalSandboxClient(instrumentation=instrumentation, inherit_host_environment=False)

    @asynccontextmanager
    async def connect(session):
        yield Environment(url="wss://executor.invalid", cwd=session.state.manifest.root)

    async with sandbox_environment(
        client, manifest=Manifest(root=str(tmp_path)), options=None, connect=connect
    ):
        pass
    paths = list((tmp_path / "logs").glob("*.jsonl"))
    assert len(paths) == 1 and paths[0].stat().st_size <= 1000


@unix
async def test_blocked_http_send_keeps_its_capacity_during_native_cleanup():
    release = threading.Event()
    posted = []

    class BlockedHttpSink(HttpProxySink):
        def _post(self, body, spool_line):
            posted.append(json.loads(body))
            release.wait(timeout=10)

    instrumentation = Instrumentation(
        sinks=[BlockedHttpSink("https://observer.invalid", on_error="ignore")],
        max_pending_events=2,
        delivery_timeout_s=0.01,
    )
    client = UnixLocalSandboxClient(instrumentation=instrumentation, inherit_host_environment=False)
    resources = []

    @asynccontextmanager
    async def connect(session):
        resources.append(session)
        yield Environment(url="wss://executor.invalid", cwd=session.state.manifest.root)

    manager = sandbox_environment(client, manifest=Manifest(), options=None, connect=connect)
    try:
        async with timeout(5):
            await manager.__aenter__()
            while len(posted) < 2:
                await asyncio.sleep(0.001)
            event = SandboxSessionStartEvent(
                session_id=resources[0].state.session_id, seq=10, op="start", span_id="capacity"
            )
            for _ in range(3):
                await asyncio.sleep(0.03)
                for _ in range(100):
                    await instrumentation.emit(event)
            await asyncio.sleep(0.03)
            assert len(posted) == 2
            await manager.__aexit__(None, None, None)
            assert not release.is_set()
            assert not Path(resources[0].state.manifest.root).exists()
    finally:
        release.set()
        await manager.__aexit__(None, None, None)
        await instrumentation.flush()


async def test_native_lifecycle_excludes_payloads_even_with_output_opt_in():
    observed = []

    class CaptureSink(EventSink):
        mode = "sync"
        on_error = "raise"
        payload_policy = None

        async def handle(self, event):
            observed.append(event)

    instrumentation = Instrumentation(
        sinks=[CaptureSink()], payload_policy=EventPayloadPolicy(include_exec_output=True)
    )
    original = SandboxSessionFinishEvent(
        session_id=uuid4(),
        seq=1,
        op="start",
        span_id="safe-headers",
        ok=False,
        duration_ms=1,
        data={"private": "provider-payload"},
        error_type="PrivateFailure",
        error_message="private-exception",
        stdout="private-output",
        stderr="private-error",
        stdout_bytes=b"private-output",
        stderr_bytes=b"private-error",
    )
    await instrumentation.emit(original)
    assert len(observed) == 1
    safe = observed[0]
    assert safe.data == {} and safe.error_type == "SandboxOperationError"
    assert safe.stdout is safe.stderr is safe.stdout_bytes is safe.stderr_bytes is None
    assert safe.error_message is None
    assert original.stdout == "private-output" and original.data == {"private": "provider-payload"}


@unix
@pytest.mark.parametrize("outcome", ["completed", "denied", "malformed"])
async def test_native_builtin_dispatch_retains_resource_identity_and_measured_finish(
    outcome: str, responses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from tests.native_fixtures import assistant_output, function_call
    from tests.test_native_agent import provider

    observed = []
    resources = []
    instrumentation = Instrumentation(
        sinks=[CallbackSink(lambda event, session: observed.append(event))]
    )
    client = UnixLocalSandboxClient(instrumentation=instrumentation, inherit_host_environment=False)
    responses.respond(
        function_call(
            "exec_command",
            {"cmd": "echo native-private-command" if outcome != "malformed" else 7},
            "builtin-operation",
        )
    )
    responses.respond(assistant_output("done"))
    async with (
        timeout(30),
        ExecutorPeer(
            tmp_path / "executor",
            synthetic_process_output="native-private-output",
            synthetic_sandbox_denied=outcome == "denied",
        ).serve() as peer,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))

        @asynccontextmanager
        async def connect(session):
            resources.append(session)
            yield Environment(
                url=peer.url,
                cwd=session.state.manifest.root,
                headers={"Authorization": "Bearer " + peer.token},
                tools=["exec_command", "write_stdin", "apply_patch"],
            )

        environment = sandbox_environment(
            client, manifest=Manifest(), options=None, connect=connect
        )
        async with Agent(
            instructions="Use the requested sandbox operation.", provider=provider(responses)
        ).session(environment=environment) as session:
            turn = await session.submit("once")
            if outcome == "malformed":
                from agents import NativeError

                with pytest.raises(NativeError, match="invalid arguments before dispatch"):
                    await turn.get_result()
            else:
                assert (await turn.get_result()).final_output == "done"
    operations = [event for event in observed if event.op == "exec"]
    if outcome == "malformed":
        assert operations == [] and peer.synthetic_process_starts == []
        return
    assert [event.phase for event in operations] == ["start", "finish"]
    assert all(event.session_id == resources[0].state.session_id for event in operations)
    assert operations[0].span_id == operations[1].span_id
    assert operations[1].ok is (outcome == "completed") and operations[1].duration_ms >= 0
    assert all(event.data == {} for event in operations)
    assert "native-private" not in "".join(event.model_dump_json() for event in operations)
    assert peer.synthetic_process_starts


@unix
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_native_inline_operation_sink_rejects_same_session_close(
    mode, responses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from tests.native_fixtures import assistant_output, function_call
    from tests.test_native_agent import provider

    current = None
    close_tasks = []
    attempts = []

    async def observe(event, resource):
        if event.op != "exec" or event.phase != "finish":
            return
        assert current is not None
        task = asyncio.create_task(current.aclose())
        close_tasks.append(task)
        try:
            await asyncio.wait_for(asyncio.shield(task), 0.2)
        except TimeoutError:
            # Release the reader after witnessing the cycle so cleanup can join.
            attempts.append("close waited for its own event reader")
        except RuntimeError as error:
            attempts.append(str(error))
        else:
            attempts.append("close was admitted")

    instrumentation = Instrumentation(sinks=[CallbackSink(observe, mode=mode, on_error="raise")])
    client = UnixLocalSandboxClient(instrumentation=instrumentation, inherit_host_environment=False)
    responses.respond(function_call("exec_command", {"cmd": "echo private-value"}, "self-close"))
    responses.respond(assistant_output("done"))
    responses.respond(assistant_output("still-open"))
    output = None
    async with (
        timeout(30),
        ExecutorPeer(
            tmp_path / "executor", synthetic_process_output="private-output"
        ).serve() as peer,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))

        @asynccontextmanager
        async def connect(resource):
            yield Environment(
                url=peer.url,
                cwd=resource.state.manifest.root,
                headers={"Authorization": "Bearer " + peer.token},
                tools=["exec_command", "write_stdin", "apply_patch"],
            )

        environment = sandbox_environment(
            client, manifest=Manifest(), options=None, connect=connect
        )
        async with Agent(
            instructions="Use the requested operation.", provider=provider(responses)
        ).session(environment=environment) as session:
            current = session
            try:
                output = (await (await session.submit("once")).get_result()).final_output
            except (Exception, asyncio.CancelledError):
                pass
            if attempts == ["A tool callback cannot close its own Session"]:
                assert (
                    await (await session.submit("again")).get_result()
                ).final_output == "still-open"
    await asyncio.gather(*close_tasks, return_exceptions=True)
    assert attempts == ["A tool callback cannot close its own Session"]
    assert output == "done"


@unix
async def test_native_jsonl_budget_retains_physical_append_until_resource_cleanup(
    tmp_path: Path,
):
    release = threading.Event()
    written = asyncio.Event()
    loop = asyncio.get_running_loop()
    appended = []

    class BlockedOutbox(JsonlOutboxSink):
        def _append_line(self, line):
            appended.append(line)
            release.wait(timeout=10)
            super()._append_line(line)
            loop.call_soon_threadsafe(written.set)

    path = tmp_path / "events.jsonl"
    instrumentation = Instrumentation(
        sinks=[BlockedOutbox(path, on_error="ignore")],
        max_pending_events=1,
        delivery_timeout_s=0.01,
    )
    client = UnixLocalSandboxClient(instrumentation=instrumentation, inherit_host_environment=False)
    resources = []

    @asynccontextmanager
    async def connect(resource):
        resources.append(resource)
        yield Environment(url="wss://executor.invalid", cwd=resource.state.manifest.root)

    manager = sandbox_environment(client, manifest=Manifest(), options=None, connect=connect)
    try:
        async with timeout(5):
            await manager.__aenter__()
            while not appended:
                await asyncio.sleep(0.001)
            event = SandboxSessionStartEvent(
                session_id=resources[0].state.session_id, seq=10, op="exec", span_id="append"
            )
            for _ in range(3):
                await asyncio.sleep(0.03)
                for _ in range(100):
                    await instrumentation.emit(event)
            await asyncio.sleep(0.03)
            assert len(appended) == 1
            await manager.__aexit__(None, None, None)
            assert not release.is_set()
            assert not Path(resources[0].state.manifest.root).exists()
    finally:
        release.set()
        await manager.__aexit__(None, None, None)
        await instrumentation.flush()
    async with timeout(5):
        await written.wait()
    assert len(path.read_text().splitlines()) == 1


@unix
async def test_native_provider_operations_deliver_paired_headers(tmp_path: Path):
    from io import BytesIO

    from agents.classic.sandbox.errors import ExposedPortUnavailableError

    seen = []
    client = UnixLocalSandboxClient(
        instrumentation=Instrumentation(sinks=[CallbackSink(lambda event, _: seen.append(event))]),
        inherit_host_environment=False,
    )
    observed = []

    @asynccontextmanager
    async def connect(resource):
        start = len(seen)
        path = Path("saved.txt")
        await resource.write(path, BytesIO(b"workspace content"))
        assert await resource.read_bounded(path, max_bytes=100) == b"workspace content"
        assert await resource.running()
        with pytest.raises(ExposedPortUnavailableError):
            await resource.resolve_exposed_port(8132)
        archive = await resource.persist_workspace()
        try:
            await resource.rm(path)
            await resource.hydrate_workspace(archive)
        finally:
            archive.close()
        assert (tmp_path / path).read_bytes() == b"workspace content"
        observed.extend(seen[start:])
        yield Environment(url="wss://executor.invalid", cwd=resource.state.manifest.root)

    async with sandbox_environment(
        client, manifest=Manifest(root=str(tmp_path)), options=None, connect=connect
    ):
        pass
    assert [(event.op, event.phase) for event in observed] == [
        (operation, phase)
        for operation in (
            "write",
            "read",
            "running",
            "resolve_exposed_port",
            "persist_workspace",
            "hydrate_workspace",
        )
        for phase in ("start", "finish")
    ]
    assert all(event.data == {} for event in observed)
    finishes = [event for event in observed if isinstance(event, SandboxSessionFinishEvent)]
    assert [(event.op, event.ok) for event in finishes if not event.ok] == [
        ("resolve_exposed_port", False)
    ]
    assert finishes[3].error_type == "SandboxOperationError"
    assert finishes[3].error_message is None
