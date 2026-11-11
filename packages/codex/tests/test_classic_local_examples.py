"""Observable regressions for optional Classic examples."""

import socket
from types import SimpleNamespace

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
@pytest.mark.parametrize("with_credentials", [False, True])
async def test_docker_mount_example_reports_missing_driver(monkeypatch, capsys, with_credentials):
    docker = pytest.importorskip("docker")
    from agents.classic.sandbox.entries import DockerVolumeMountStrategy, S3Mount
    from examples.classic.sandbox.docker.mounts import mount_smoke as example

    error = docker.errors.NotFound('plugin "rclone" not found')

    def create_container(**_kwargs):
        raise error

    def get_volume(_name):
        raise docker.errors.NotFound("volume not found")

    docker_client = SimpleNamespace(
        images=SimpleNamespace(get=lambda _name: object()),
        containers=SimpleNamespace(create=create_container),
        volumes=SimpleNamespace(get=get_volume),
    )
    monkeypatch.setattr(example.docker, "from_env", lambda: docker_client)
    mount = S3Mount(
        bucket="test-bucket",
        access_key_id="test-access" if with_credentials else None,
        secret_access_key="test-secret" if with_credentials else None,
        mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
    )
    mount_cases = [example.MountSmokeCase(name="rclone", mount_dir="s3", mount=mount)]
    expected_error = RuntimeError if with_credentials else SystemExit

    with pytest.raises(expected_error) as caught:
        await example.run_mount_smoke_test(
            provider="s3", agent_name="test", mount_cases=mount_cases
        )

    if with_credentials:
        assert "protected mount configuration" in str(caught.value)
        assert "docker plugin ls" in capsys.readouterr().err
        assert caught.value.__cause__ is None
    else:
        assert str(caught.value) == "rclone Docker volume plugin not found"
        assert caught.value.__cause__ is error


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_docker_mount_example_deletes_sandbox_when_start_fails(
    monkeypatch, capsys, cleanup_fails
):
    pytest.importorskip("docker")
    from agents.classic.sandbox.errors import MountCommandError
    from examples.classic.sandbox.docker.mounts import mount_smoke as example

    live_sessions = []
    error = MountCommandError(command="mount", stderr="mount failed")

    async def start():
        raise error

    async def create(**_kwargs):
        session = SimpleNamespace(start=start)
        live_sessions.append(session)
        return session

    async def delete(session):
        live_sessions.remove(session)
        if cleanup_fails:
            raise RuntimeError("protected cleanup details")

    client = SimpleNamespace(create=create, delete=delete)
    monkeypatch.setattr(example, "ensure_mount_image", lambda: None)
    monkeypatch.setattr(example.docker, "from_env", lambda: object())
    monkeypatch.setattr(example, "DockerSandboxClient", lambda _docker: client)

    with pytest.raises(MountCommandError) as caught:
        await example.run_mount_smoke_test(provider="test", agent_name="test", mount_cases=[])

    assert caught.value is error
    assert not live_sessions
    captured = capsys.readouterr()
    assert "mount stderr: mount failed" in captured.out
    if cleanup_fails:
        assert "Docker mount sandbox cleanup also failed" in captured.err
        assert "protected cleanup details" not in captured.err


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
