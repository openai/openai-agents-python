"""Managed resource receipts and the native retirement boundary."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from agents import Agent, Environment
from agents.sandbox import (
    LocalSnapshotSpec,
    ManagedWorkspaceCheckpoint,
    Manifest,
    sandbox_environment,
)
from agents.state import SessionState
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_sandbox_environment import Client, Options


def conversation(name="a"):
    return SessionState.from_json(
        json.dumps(
            {
                "format_version": 2,
                "codex_revision": "revision",
                "source_session_id": name,
                "output_version": 1,
                "output_fingerprint": "0" * 64,
                "root_metadata": {},
                "history": [{"type": "message", "content": name}],
            }
        )
    ).to_json()


def test_pair_detaches_and_rejects_mixed_or_corrupted_receipts():
    workspace = '{"backend_id":"docker","provider_state":{"id":"workspace-a"}}'
    saved = ManagedWorkspaceCheckpoint._capture(conversation(), workspace)
    assert ManagedWorkspaceCheckpoint.from_json(saved.to_json()) == saved
    saved.workspace["provider_state"]["id"] = "changed"
    assert saved.workspace["provider_state"]["id"] == "workspace-a"
    with pytest.raises(ValueError, match="association"):
        replace(saved, conversation_json=conversation("b"))
    damaged = json.loads(saved.to_json())
    damaged["workspace"] = workspace.replace("workspace-a", "workspace-b")
    with pytest.raises(ValueError, match="association"):
        ManagedWorkspaceCheckpoint.from_json(json.dumps(damaged))


def test_recomputed_pair_cannot_borrow_workspace_only_authorization():
    from _agents_codex._managed_workspace import _ManagedWorkspaceSource

    workspace = '{"backend_id":"fixture","provider_state":{"id":"B"}}'
    pair = ManagedWorkspaceCheckpoint._capture(conversation("B"), workspace)
    data = json.loads(pair.to_json())
    data["conversation"] = conversation("A")
    data["association"] = hashlib.sha256(
        (data["conversation"] + "\0" + workspace).encode()
    ).hexdigest()
    forged = ManagedWorkspaceCheckpoint.from_json(json.dumps(data))
    managed = _ManagedWorkspaceSource(
        gateway(None), backend_id="fixture", authorized_payload={"id": "B"}
    )
    with pytest.raises(ValueError, match="trusted.*checkpoint"):
        managed.validate_resume(forged)


def test_capture_provenance_and_trusted_full_pair_grants_are_distinct():
    from dataclasses import asdict

    from _agents_codex._managed_workspace import _capture_retired_workspace, _ManagedWorkspaceSource

    workspace = '{"backend_id":"fixture","provider_state":{"id":"B"}}'
    captured = _capture_retired_workspace(conversation("B"), workspace)
    imported = ManagedWorkspaceCheckpoint.from_json(captured.to_json())
    managed = _ManagedWorkspaceSource(
        gateway(None), backend_id="fixture", authorized_payload={"id": "B"}
    )
    assert managed.validate_resume(captured) == imported
    for ordinary in (imported, ManagedWorkspaceCheckpoint._capture(conversation("B"), workspace)):
        with pytest.raises(ValueError, match="trusted.*checkpoint"):
            managed.validate_resume(ordinary)
    granted = _ManagedWorkspaceSource(
        gateway(None),
        backend_id="fixture",
        authorized_payload={"id": "B"},
        authorized_checkpoint=imported,
    )
    assert granted.validate_resume(imported) == imported
    other = ManagedWorkspaceCheckpoint._capture(conversation("A"), workspace)
    with pytest.raises(ValueError, match="trusted.*checkpoint"):
        granted.validate_resume(other)
    with pytest.raises(ValueError, match="trusted.*checkpoint"):
        granted.validate_resume(_capture_retired_workspace(conversation("A"), workspace))
    wrong_workspace = _ManagedWorkspaceSource(
        gateway(None),
        backend_id="fixture",
        authorized_payload={"id": "other"},
        authorized_checkpoint=imported,
    )
    with pytest.raises(ValueError, match="current authorized"):
        wrong_workspace.validate_resume(captured)
    assert "_capture_provenance" not in repr(captured)
    assert set(asdict(captured)) == {"conversation_json", "workspace_json", "association"}
    assert set(json.loads(captured.to_json())) == {
        "format",
        "conversation",
        "workspace",
        "association",
    }
    assert "conversation" not in repr(granted)


async def test_public_opening_denies_serialized_pair_before_any_acquisition():
    from _agents_codex._managed_workspace import _ManagedWorkspaceSource

    events = []

    @asynccontextmanager
    async def acquire():
        events.append("acquire")
        yield Environment(url="wss://fixture.invalid", cwd="/workspace")

    workspace = '{"backend_id":"fixture","provider_state":{"id":"B"}}'
    pair = ManagedWorkspaceCheckpoint.from_json(
        ManagedWorkspaceCheckpoint._capture(conversation("A"), workspace).to_json()
    )
    managed = _ManagedWorkspaceSource(
        acquire(), backend_id="fixture", authorized_payload={"id": "B"}
    )
    with pytest.raises(ValueError, match="trusted.*checkpoint"):
        async with Agent(instructions="").session(state=pair, environment=managed):
            pytest.fail("Untrusted pair reached native opening")
    assert events == []


async def test_public_source_captures_trusted_checkpoint_without_serializing_during_validation(
    tmp_path,
):
    (tmp_path / "value").write_text("B")
    client = SnapshotClient([], tmp_path)
    live = await client.create(
        manifest=Manifest(root=str(tmp_path)),
        options=Options(),
        snapshot=LocalSnapshotSpec(base_path=tmp_path / "archives"),
    )
    payload = client.serialize_session_state(live.state)
    workspace = json.dumps(
        {"backend_id": client.backend_id, "provider_state": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    pair = ManagedWorkspaceCheckpoint._capture(conversation("B"), workspace)
    managed = sandbox_environment(
        client,
        manifest=Manifest(root=str(tmp_path)),
        options=Options(),
        connect=gateway,
        managed_workspace=True,
        authorized_resume_state=live.state,
        authorized_resume_checkpoint=pair,
    )
    before = list(client.events)
    imported = ManagedWorkspaceCheckpoint.from_json(pair.to_json())
    assert managed.validate_resume(imported) == imported
    assert client.events == before


class SnapshotClient(Client):
    def __init__(self, events, root, *, fail=""):
        super().__init__(events)
        self.backend_id = "scripted"
        self.root = root
        self.fail = fail
        self.stop_entered = asyncio.Event()
        self.stop_release = asyncio.Event()
        self.stop_release.set()
        self.saved_state = None

    async def create(self, *, manifest, options, snapshot=None):
        selected = await super().create(manifest=manifest, options=options)
        selected.state.snapshot = snapshot.build(selected.state.session_id.hex)
        return self.prepare(selected)

    def prepare(self, selected):
        async def close():
            self.events.append("stop")
            self.stop_entered.set()
            await self.stop_release.wait()
            if self.fail == "stop":
                raise RuntimeError("snapshot failed")
            await selected.state.snapshot.persist(io.BytesIO((self.root / "value").read_bytes()))

        selected.aclose = close
        return selected

    async def resume(self, state):
        self.events.append("resume")
        # This fixture restores through the real snapshot owner without a provider process.
        from agents.classic.testing.sandbox import scripted_sandbox_session

        selected = self._wrap_session(scripted_sandbox_session(manifest=state.manifest))
        selected._inner.state = state
        self.session = selected

        async def start():
            self.events.append("start")
            archive = await state.snapshot.restore()
            try:
                (self.root / "value").write_bytes(archive.read())
            finally:
                archive.close()

        selected.start = start
        return self.prepare(selected)

    def serialize_session_state(self, state):
        self.events.append("serialize")
        if self.fail == "serialize":
            raise RuntimeError("serialization failed")
        self.saved_state = state.model_copy(deep=True)
        return super().serialize_session_state(state)


@asynccontextmanager
async def gateway(selected):
    yield Environment(url="wss://executor.invalid", cwd=str(selected.state.manifest.root), tools=[])


def source(client, *, state=None, expected=None):
    return sandbox_environment(
        client,
        manifest=Manifest(root=str(client.root)),
        options=Options(),
        connect=getattr(client, "gateway", gateway),
        managed_workspace=True,
        **(
            {"authorized_resume_state": state, "authorized_resume_checkpoint": expected}
            if state is not None
            else {"snapshot": LocalSnapshotSpec(base_path=client.root / "archives")}
        ),
    )


@pytest.mark.parametrize("failure", ["", "stop", "serialize"])
async def test_resource_retirement_serializes_after_snapshot_and_always_deletes(tmp_path, failure):
    (tmp_path / "value").write_text("A")
    events = []
    client = SnapshotClient(events, tmp_path, fail=failure)
    managed = source(client)
    managed.capture_requested = True

    async def retire():
        async with managed:
            events.append("body")

    if failure:
        with pytest.raises(RuntimeError):
            await retire()
        assert managed.receipt is None
    else:
        await retire()
        assert json.loads(managed.receipt)["provider_state"]
    assert events[:4] == ["create", "start", "body", "stop"]
    assert events[-1] == "delete"
    assert ("serialize" in events) == (failure != "stop")


async def test_wrong_pair_authority_fails_before_resource_or_native_opening(tmp_path):
    (tmp_path / "value").write_text("A")
    client = SnapshotClient([], tmp_path)
    managed = source(client)
    pair = ManagedWorkspaceCheckpoint._capture(
        conversation(), '{"backend_id":"docker","provider_state":{}}'
    )
    with pytest.raises(ValueError, match="current authorized"):
        async with Agent(instructions="").session(state=pair, environment=managed):
            pytest.fail("mismatched pair opened")
    assert client.events == []


@pytest.fixture
def native_managed_client(tmp_path, monkeypatch):
    from tests.native_executor_fixtures import ExecutorPeer

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "value").write_text("A")
    client = SnapshotClient([], root)

    @asynccontextmanager
    async def native_gateway(selected):
        peer = ExecutorPeer(tmp_path / ("gateway-" + str(len(client.events))))
        async with peer.serve():
            monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
            monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
            yield Environment(
                url=peer.url,
                cwd=str(root),
                tools=[],
                headers={"Authorization": "Bearer " + peer.token},
            )
            await peer.wait_disconnected()

    client.gateway = native_gateway
    return client


async def test_native_pair_a_survives_resumed_workspace_b(responses, native_managed_client):
    client = native_managed_client
    root = client.root
    agent = Agent(instructions="Keep the workspace.", provider=provider(responses))
    responses.respond(assistant_output("A"))
    async with agent.session(environment=source(client)) as native:
        assert isinstance(await native.save_state(), SessionState)
        assert (await (await native.submit("Remember A")).get_result()).final_output == "A"
        pair_a = await native.suspend_workspace()
    authorized_a = client.saved_state.model_copy(deep=True)
    original_path = authorized_a.snapshot._path()
    original_bytes = original_path.read_bytes()
    decoded_a = ManagedWorkspaceCheckpoint.from_json(pair_a.to_json())
    async with agent.session(
        state=decoded_a, environment=source(client, state=authorized_a, expected=pair_a)
    ) as native:
        assert (root / "value").read_text() == "A"
        (root / "value").write_text("B")
        pair_b = await native.suspend_workspace()
    assert pair_b.workspace_json != pair_a.workspace_json
    assert original_path.read_bytes() == original_bytes
    async with agent.session(state=pair_a, environment=source(client, state=authorized_a)):
        assert (root / "value").read_text() == "A"


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_native_capture_intent_survives_racing_close_and_blocks_admission(
    responses, native_managed_client, monkeypatch, cancel_waiter
):
    client = native_managed_client
    managed = source(client)
    entered, release = asyncio.Event(), asyncio.Event()
    async with Agent(instructions="Capture.", provider=provider(responses)).session(
        environment=managed
    ) as native:
        original = type(native)._capture_checkpoint

        async def blocked(owner, *args, **kwargs):
            entered.set()
            await release.wait()
            return await original(owner, *args, **kwargs)

        monkeypatch.setattr(type(native), "_capture_checkpoint", blocked)
        waiter = asyncio.create_task(native.suspend_workspace())
        await asyncio.wait_for(entered.wait(), 10)
        assert managed.capture_requested
        if cancel_waiter:
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        closing = asyncio.create_task(native.aclose())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError):
            await native.submit("Do not start")
        assert not responses.requests
        assert "stop" not in client.events
        release.set()
        pair = await asyncio.wait_for(native.suspend_workspace(), 10)
        await closing
        if not cancel_waiter:
            assert await waiter == pair
    assert client.events.count("serialize") == 1
    assert client.events[-3:] == ["stop", "serialize", "delete"]


def test_managed_default_fails_without_silent_noop_fallback(tmp_path, monkeypatch):
    import _agents_codex.sandbox as owner
    from agents.sandbox import NoopSnapshotSpec

    client = SnapshotClient([], tmp_path)

    def unavailable():
        raise OSError("snapshot allocation failed")

    monkeypatch.setattr(owner, "resolve_default_local_snapshot_spec", unavailable)
    with pytest.raises(OSError, match="snapshot allocation"):
        sandbox_environment(
            client, manifest=Manifest(), options=Options(), connect=gateway, managed_workspace=True
        )
    with pytest.raises(ValueError, match="factory"):
        sandbox_environment(
            client,
            manifest=Manifest(),
            options=Options(),
            connect=gateway,
            managed_workspace=True,
            snapshot=NoopSnapshotSpec(),
        )
    assert client.events == []
