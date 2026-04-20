from __future__ import annotations

import importlib
import io
import sys
import types
from typing import Any

import pytest


class _FakeExecResult:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class _FakeSimpleBox:
    """Minimal stand-in for boxlite.SimpleBox used in tests."""

    instances: list[_FakeSimpleBox] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.id = "fake-box-id"
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        _FakeSimpleBox.instances.append(self)

    async def start(self) -> _FakeSimpleBox:
        self.started = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.closed = True

    def info(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(state="running")

    async def exec(self, *args: Any, **kwargs: Any) -> _FakeExecResult:
        self.calls.append((args, kwargs))
        return _FakeExecResult(stdout=b"hi", exit_code=0)


@pytest.fixture(autouse=True)
def _install_fake_boxlite(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = types.ModuleType("boxlite")
    fake_module.SimpleBox = _FakeSimpleBox  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boxlite", fake_module)
    _FakeSimpleBox.instances = []

    for mod_name in (
        "agents.extensions.sandbox.boxlite.sandbox",
        "agents.extensions.sandbox.boxlite",
    ):
        sys.modules.pop(mod_name, None)
    importlib.import_module("agents.extensions.sandbox.boxlite")


def _load_module() -> Any:
    return importlib.import_module("agents.extensions.sandbox.boxlite.sandbox")


def test_options_serialize_includes_type() -> None:
    mod = _load_module()
    opts = mod.BoxliteSandboxClientOptions(image="alpine:latest", cpus=2)
    data = opts.model_dump(mode="json")
    assert data["type"] == "boxlite"
    assert data["image"] == "alpine:latest"
    assert data["cpus"] == 2


def test_session_state_roundtrip_via_registry() -> None:
    mod = _load_module()
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.session import SandboxSessionState
    from agents.sandbox.snapshot import NoopSnapshot

    state = mod.BoxliteSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        image="alpine:latest",
    )
    payload = state.model_dump(mode="json")
    assert payload["type"] == "boxlite"

    restored = SandboxSessionState.parse(payload)
    assert isinstance(restored, mod.BoxliteSandboxSessionState)
    assert restored.image == "alpine:latest"


@pytest.mark.asyncio
async def test_create_requires_image_or_rootfs() -> None:
    mod = _load_module()
    from agents.sandbox.errors import ConfigurationError

    client = mod.BoxliteSandboxClient()
    with pytest.raises(ConfigurationError):
        await client.create(options=mod.BoxliteSandboxClientOptions())


@pytest.mark.asyncio
async def test_internal_command_converts_result() -> None:
    mod = _load_module()
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.snapshot import NoopSnapshot

    state = mod.BoxliteSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        image="alpine:latest",
    )
    session = mod.BoxliteSandboxSession.from_state(state)
    result = await session._exec_internal("echo", "hello")
    assert result.exit_code == 0
    assert result.stdout == b"hi"
    box = _FakeSimpleBox.instances[-1]
    assert box.started is True
    assert box.calls and box.calls[0][0] == ("echo", "hello")


@pytest.mark.asyncio
async def test_running_reports_true_when_box_info_running() -> None:
    mod = _load_module()
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.snapshot import NoopSnapshot

    state = mod.BoxliteSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        image="alpine:latest",
    )
    session = mod.BoxliteSandboxSession.from_state(state)
    assert await session.running() is False
    await session._ensure_box()
    assert await session.running() is True


@pytest.mark.asyncio
async def test_shutdown_closes_the_box() -> None:
    mod = _load_module()
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.snapshot import NoopSnapshot

    state = mod.BoxliteSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        image="alpine:latest",
    )
    session = mod.BoxliteSandboxSession.from_state(state)
    await session._ensure_box()
    box = _FakeSimpleBox.instances[-1]
    await session.shutdown()
    assert box.closed is True


@pytest.mark.asyncio
async def test_read_base64_decodes_stdout() -> None:
    import base64 as _b64
    import pathlib

    mod = _load_module()
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.snapshot import NoopSnapshot

    class _Box(_FakeSimpleBox):
        async def exec(self, *args: Any, **kwargs: Any) -> _FakeExecResult:
            self.calls.append((args, kwargs))
            return _FakeExecResult(stdout=_b64.b64encode(b"payload"), exit_code=0)

    state = mod.BoxliteSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        image="alpine:latest",
    )
    session = mod.BoxliteSandboxSession.from_state(state, box=_Box())
    buf = await session.read(pathlib.Path("/workspace/file.txt"))
    assert isinstance(buf, io.BytesIO)
    assert buf.read() == b"payload"
