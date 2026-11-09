"""Workspace audit outboxes stay bounded even when sandbox work changes them."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from agents.classic.sandbox import Manifest
from agents.classic.sandbox.session.events import SandboxSessionStartEvent
from agents.classic.sandbox.session.sinks import WorkspaceJsonlSink
from agents.classic.sandbox.snapshot import NoopSnapshot

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="UnixLocal requires Unix")

if sys.platform != "win32":
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )


def _session(root: Path) -> UnixLocalSandboxSession:
    session = UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(root)), snapshot=NoopSnapshot(id="workspace-jsonl-test")
        )
    )
    session._running = True
    return session


def _event(session: UnixLocalSandboxSession, seq: int) -> SandboxSessionStartEvent:
    return SandboxSessionStartEvent(
        session_id=session.state.session_id, seq=seq, op="exec", span_id=f"span-{seq}"
    )


async def test_default_workspace_sink_stops_before_rewriting_oversized_outbox(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    sink = WorkspaceJsonlSink()
    sink.bind(session)
    outbox = tmp_path / "logs" / f"events-{session.state.session_id}.jsonl"
    outbox.parent.mkdir()
    with outbox.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    with outbox.open("rb") as stream:
        original_digest = hashlib.sha256(stream.read()).hexdigest()

    with pytest.raises(RuntimeError, match="exceeded max_bytes"):
        await sink.handle(_event(session, 1))

    assert outbox.stat().st_size == 8 * 1024 * 1024 + 1
    with outbox.open("rb") as stream:
        assert hashlib.sha256(stream.read()).hexdigest() == original_digest
    await sink.handle(_event(session, 2))
    assert outbox.stat().st_size == 8 * 1024 * 1024 + 1


async def test_default_workspace_sink_creates_and_appends_jsonl(tmp_path: Path) -> None:
    session = _session(tmp_path)
    sink = WorkspaceJsonlSink()
    sink.bind(session)

    await sink.handle(_event(session, 1))
    await sink.handle(_event(session, 2))

    outbox = tmp_path / "logs" / f"events-{session.state.session_id}.jsonl"
    lines = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert [(line["seq"], line["op"], line["phase"]) for line in lines] == [
        (1, "exec", "start"),
        (2, "exec", "start"),
    ]


async def test_explicit_unlimited_workspace_sink_preserves_large_outbox(tmp_path: Path) -> None:
    session = _session(tmp_path)
    sink = WorkspaceJsonlSink(max_bytes=None)
    sink.bind(session)
    outbox = tmp_path / "logs" / f"events-{session.state.session_id}.jsonl"
    outbox.parent.mkdir()
    prefix = b"x" * (8 * 1024 * 1024 + 1)
    outbox.write_bytes(prefix)

    await sink.handle(_event(session, 1))

    payload = outbox.read_bytes()
    assert payload.startswith(prefix)
    assert json.loads(payload[len(prefix) :])["seq"] == 1


async def test_explicit_finite_workspace_sink_stops_before_rewriting_outbox(tmp_path: Path) -> None:
    session = _session(tmp_path)
    sink = WorkspaceJsonlSink(max_bytes=1024)
    sink.bind(session)
    outbox = tmp_path / "logs" / f"events-{session.state.session_id}.jsonl"
    outbox.parent.mkdir()
    outbox.write_bytes(b"x" * 1025)

    with pytest.raises(RuntimeError, match="exceeded max_bytes=1024"):
        await sink.handle(_event(session, 1))

    assert outbox.read_bytes() == b"x" * 1025
