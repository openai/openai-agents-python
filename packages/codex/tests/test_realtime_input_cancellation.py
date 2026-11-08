"""Cancellation joins admitted Realtime input without cancelling native acceptance."""

import asyncio
import base64
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession
from agents import Session

pytestmark = pytest.mark.asyncio


def owner() -> RealtimeSession:
    session = cast(Session[Any, Any], SimpleNamespace())
    voice = RealtimeSession(session, config=RealtimeConfig())
    voice._live = True
    voice._ready.set_result(None)
    return voice


@pytest.mark.parametrize("blocked_kind", ["audio", "audio_commit"])
async def test_cancelled_commit_joins_all_chunks_before_releasing_input(
    blocked_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    frames: list[tuple[str, dict[str, Any]]] = []
    source = b"\x01\x02" * 32769

    async def call(kind: str, **kwargs: Any) -> None:
        frames.append((kind, kwargs))
        if kind == blocked_kind and not entered.is_set():
            entered.set()
            await release.wait()

    monkeypatch.setattr(voice, "_call", call)
    async with asyncio.timeout(5):
        sending = asyncio.create_task(voice.send_audio(source, commit=True))
        following: asyncio.Task[None] | None = None
        try:
            await entered.wait()
            sending.cancel("stop waiting")
            await asyncio.sleep(0)
            following = asyncio.create_task(voice.send_text("next input"))
            await asyncio.sleep(0)
            assert not sending.done()
            assert voice._pending_audio_commits == 1
            assert all(kind != "text" for kind, _ in frames)
            # Repeated waiter cancellation must not cancel the admitted sequence.
            sending.cancel("stop waiting again")
            await asyncio.sleep(0)
            assert not sending.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await sending
            await following
            assert [kind for kind, _ in frames] == ["audio", "audio", "audio_commit", "text"]
            assert (
                b"".join(
                    base64.b64decode(payload["frame"]["data"])
                    for kind, payload in frames
                    if kind == "audio"
                )
                == source
            )
            assert voice._pending_audio_commits == 0
        finally:
            release.set()
            await asyncio.gather(
                sending, *([following] if following else []), return_exceptions=True
            )


async def test_append_only_audio_retains_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    voice = owner()
    entered = asyncio.Event()
    kinds: list[str] = []

    async def call(kind: str, **kwargs: Any) -> None:
        kinds.append(kind)
        if kind == "audio":
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(voice, "_call", call)
    async with asyncio.timeout(5):
        sending = asyncio.create_task(voice.send_audio(b"\x01\x02" * 32769))
        await entered.wait()
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        await voice.send_text("next input")
        assert kinds == ["audio", "text"]
        assert voice._pending_audio_commits == 0


async def test_cancelled_update_joins_native_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    voice = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    accepted: list[dict[str, Any]] = []

    async def call(kind: str, **kwargs: Any) -> None:
        assert kind == "update"
        entered.set()
        await release.wait()
        accepted.append(kwargs["update"])

    monkeypatch.setattr(voice, "_call", call)
    async with asyncio.timeout(5):
        updating = asyncio.create_task(voice.update({"instructions": "new instructions"}))
        try:
            await entered.wait()
            updating.cancel("stop waiting")
            await asyncio.sleep(0)
            assert not updating.done()
            assert accepted == []
            updating.cancel("stop waiting again")
            await asyncio.sleep(0)
            assert not updating.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await updating
            assert accepted == [{"instructions": "new instructions"}]
            await voice.update({"instructions": "next instructions"})
            assert accepted[-1] == {"instructions": "next instructions"}
        finally:
            release.set()
            await asyncio.gather(updating, return_exceptions=True)


async def test_cancelled_queued_commit_retains_input_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice = owner()
    kinds: list[str] = []

    async def call(kind: str, **kwargs: Any) -> None:
        kinds.append(kind)

    monkeypatch.setattr(voice, "_call", call)
    async with asyncio.timeout(5):
        await voice._input_lock.acquire()
        sending = asyncio.create_task(voice.send_audio(b"\x01\x02", commit=True))
        try:
            await asyncio.sleep(0)
            sending.cancel()
            await asyncio.sleep(0)
            assert not sending.done()
            assert voice._pending_audio_commits == 1
            assert kinds == []
        finally:
            voice._input_lock.release()
            await asyncio.gather(sending, return_exceptions=True)
        assert sending.cancelled()
        assert kinds == ["audio", "audio_commit"]
        assert voice._pending_audio_commits == 0
