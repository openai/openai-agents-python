"""Observable regressions for optional Classic examples."""

import socket

import pytest


@pytest.mark.asyncio
async def test_mongodb_example_closes_client_when_initial_ping_fails(monkeypatch):
    pymongo = pytest.importorskip("pymongo")
    from pymongo.asynchronous.mongo_client import AsyncMongoClient

    from examples.classic.memory import mongodb_session_example as example

    # Reserve a local port without listening so no external database is contacted.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        uri = f"mongodb://127.0.0.1:{reserved.getsockname()[1]}"
        client = AsyncMongoClient(uri, serverSelectionTimeoutMS=20, connectTimeoutMS=20)
        monkeypatch.setattr(example, "AsyncMongoClient", lambda *_args, **_kwargs: client)
        try:
            await example.main()
            # A closed real client rejects operations instead of selecting a server again.
            with pytest.raises(pymongo.errors.InvalidOperation, match="after close"):
                await client.admin.command("ping")
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_streamed_voice_keyboard_controls_do_not_require_a_button(monkeypatch):
    pytest.importorskip("textual")
    try:
        __import__("sounddevice")
    except (ImportError, OSError) as error:
        pytest.skip(f"Optional sounddevice/PortAudio unavailable: {error}")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-example-not-real")
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)

    from examples.classic.voice.streamed import main as example

    # Mount the actual interface while leaving unrelated microphone/network workers idle.
    async def idle_mount(self):
        pass

    monkeypatch.setattr(example.sd, "OutputStream", lambda **_kwargs: object())
    monkeypatch.setattr(example.RealtimeApp, "on_mount", idle_mount)
    app = example.RealtimeApp()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.is_running
        await pilot.press("k")
        assert app.should_send_audio.is_set()
        await pilot.press("k")
        assert not app.should_send_audio.is_set()
        await pilot.press("q")
