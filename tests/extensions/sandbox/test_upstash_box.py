from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest

from agents.extensions.sandbox.upstash_box import (
    UpstashBoxSandboxClient,
    UpstashBoxSandboxClientOptions,
    UpstashBoxSandboxSession,
    UpstashBoxSandboxSessionState,
)
from agents.sandbox.errors import ConfigurationError, ExposedPortUnavailableError
from agents.sandbox.manifest import Manifest
from agents.sandbox.snapshot import resolve_snapshot
from agents.sandbox.types import ExposedPortEndpoint


class _FakeResponse:
    def __init__(self, status: int = 200, json_body: Any = None, raw_body: bytes = b"") -> None:
        self.status = status
        self._json_body = json_body
        self._raw_body = raw_body

    async def json(self, *, content_type: str | None = None) -> Any:
        _ = content_type
        if self._json_body is not None:
            return self._json_body
        return json.loads(self._raw_body)

    async def read(self) -> bytes:
        if self._json_body is not None:
            return json.dumps(self._json_body).encode()
        return self._raw_body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        _ = args


class _FakeHttp:
    def __init__(
        self,
        responses: dict[str, _FakeResponse | list[_FakeResponse]] | None = None,
    ) -> None:
        self._responses: dict[tuple[str, str], _FakeResponse | list[_FakeResponse]] = {}
        self.default_response = _FakeResponse(status=200, json_body={"ok": True})
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        if responses:
            for key, val in responses.items():
                method, _, suffix = key.partition(" ")
                self._responses[(method.upper(), suffix)] = val

    def _match(self, method: str, url: str) -> _FakeResponse:
        # Prefer the most specific (longest) matching suffix.
        best_key: tuple[str, str] | None = None
        best: _FakeResponse | list[_FakeResponse] | None = None
        best_len = -1
        for (m, suffix), resp in self._responses.items():
            if m == method and suffix in url and len(suffix) > best_len:
                best_key = (m, suffix)
                best, best_len = resp, len(suffix)
        if best is None:
            return self.default_response
        if isinstance(best, list):
            if not best:
                return self.default_response
            response = best.pop(0)
            if best_key is not None and not best:
                self._responses[best_key] = response
            return response
        return best

    def _record(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._match(method, url)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._record("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._record("GET", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._record("DELETE", url, **kwargs)

    async def close(self) -> None:
        self.closed = True


def _make_session(
    http: _FakeHttp,
    *,
    keep_alive: bool = False,
    pause_on_exit: bool = False,
) -> UpstashBoxSandboxSession:
    snapshot = resolve_snapshot(None, "00000000-0000-0000-0000-000000000000")
    state = UpstashBoxSandboxSessionState(
        manifest=Manifest(),
        snapshot=snapshot,
        box_id="box-test",
        base_url="https://box.example.test",
        keep_alive=keep_alive,
        pause_on_exit=pause_on_exit,
        exposed_ports=(3000,),
    )
    return UpstashBoxSandboxSession.from_state(state, api_key="key", http=http)


@pytest.mark.asyncio
async def test_exec_internal_posts_argv_with_folder() -> None:
    http = _FakeHttp({"POST exec": _FakeResponse(json_body={"exit_code": 0, "output": "hi\n"})})
    session = _make_session(http)

    result = await session._exec_internal("echo", "hi")

    assert result.exit_code == 0
    assert result.stdout == b"hi\n"
    exec_call = next(c for c in http.calls if c["url"].endswith("/exec"))
    assert exec_call["json"]["command"] == ["echo", "hi"]
    assert exec_call["json"]["folder"] == "/workspace"


@pytest.mark.asyncio
async def test_exec_internal_reports_nonzero_exit() -> None:
    http = _FakeHttp(
        {"POST exec": _FakeResponse(json_body={"exit_code": 2, "output": "", "error": "boom"})}
    )
    session = _make_session(http)

    result = await session._exec_internal("false")

    assert result.exit_code == 2
    assert result.stderr == b"boom"
    assert not result.ok()


@pytest.mark.asyncio
async def test_box_read_file_decodes_base64() -> None:
    payload = bytes([0x89, 0x50, 0x00, 0xFF])
    body = {"content": base64.b64encode(payload).decode("ascii")}
    http = _FakeHttp({"GET files/read": _FakeResponse(json_body=body)})
    session = _make_session(http)

    data = await session._box_read_file("/workspace/bin.dat")

    assert data == payload


@pytest.mark.asyncio
async def test_box_write_file_sends_base64() -> None:
    http = _FakeHttp({"POST files/write": _FakeResponse(status=200, json_body={})})
    session = _make_session(http)

    await session._box_write_file("/workspace/a.txt", b"hello")

    write_call = next(c for c in http.calls if c["url"].endswith("/files/write"))
    assert write_call["json"]["path"] == "/workspace/a.txt"
    assert write_call["json"]["encoding"] == "base64"
    assert base64.b64decode(write_call["json"]["content"]) == b"hello"


@pytest.mark.asyncio
async def test_running_reflects_status() -> None:
    http = _FakeHttp({"GET status": _FakeResponse(json_body={"status": "running"})})
    assert await _make_session(http).running() is True

    http2 = _FakeHttp({"GET status": _FakeResponse(json_body={"status": "paused"})})
    assert await _make_session(http2).running() is False


@pytest.mark.asyncio
async def test_resolve_exposed_port_parses_preview_url() -> None:
    body = {"url": "https://3000-box.example.test/?token=abc", "port": 3000}
    http = _FakeHttp({"POST preview": _FakeResponse(json_body=body)})
    session = _make_session(http)

    endpoint = await session.resolve_exposed_port(3000)

    assert endpoint == ExposedPortEndpoint(
        host="3000-box.example.test", port=443, tls=True, query="token=abc"
    )


@pytest.mark.asyncio
async def test_resolve_exposed_port_rejects_unconfigured_port() -> None:
    session = _make_session(_FakeHttp())
    with pytest.raises(ExposedPortUnavailableError):
        await session.resolve_exposed_port(9999)


@pytest.mark.asyncio
async def test_shutdown_deletes_by_default() -> None:
    http = _FakeHttp()
    session = _make_session(http)

    await session.shutdown()

    assert any(c["method"] == "DELETE" for c in http.calls)
    assert not any(c["url"].endswith("/pause") for c in http.calls)


@pytest.mark.asyncio
async def test_shutdown_pauses_when_pause_on_exit() -> None:
    http = _FakeHttp()
    session = _make_session(http, pause_on_exit=True)

    await session.shutdown()

    assert any(c["url"].endswith("/pause") for c in http.calls)
    assert not any(c["method"] == "DELETE" for c in http.calls)


@pytest.mark.asyncio
async def test_shutdown_leaves_keep_alive_box_running() -> None:
    http = _FakeHttp()
    session = _make_session(http, keep_alive=True)

    await session.shutdown()

    assert not any(c["method"] == "DELETE" for c in http.calls)
    assert not any(c["url"].endswith("/pause") for c in http.calls)


@pytest.mark.asyncio
async def test_persist_and_hydrate_roundtrip_via_tar() -> None:
    tar_bytes = _make_tar_bytes()
    persist_http = _FakeHttp(
        {
            "POST exec": _FakeResponse(json_body={"exit_code": 0, "output": ""}),
            "GET files/read": _FakeResponse(
                json_body={"content": base64.b64encode(tar_bytes).decode("ascii")}
            ),
        }
    )
    session = _make_session(persist_http)

    stream = await session.persist_workspace()
    assert stream.read() == tar_bytes

    hydrate_http = _FakeHttp(
        {
            "POST exec": _FakeResponse(json_body={"exit_code": 0, "output": ""}),
            "POST files/write": _FakeResponse(status=200, json_body={}),
        }
    )
    hydrate_session = _make_session(hydrate_http)
    await hydrate_session.hydrate_workspace(io.BytesIO(tar_bytes))
    assert any(c["url"].endswith("/files/write") for c in hydrate_http.calls)


@pytest.mark.asyncio
async def test_client_create_builds_box(monkeypatch: pytest.MonkeyPatch) -> None:
    client = UpstashBoxSandboxClient(api_key="key")

    async def _fake_create_box(**kwargs: Any) -> str:
        return "box-created"

    monkeypatch.setattr(client, "_create_box", _fake_create_box)
    options = UpstashBoxSandboxClientOptions(size="medium", env_vars={"K": "V"})

    session = await client.create(options=options)
    inner = session._inner
    assert isinstance(inner, UpstashBoxSandboxSession)
    assert inner.state.box_id == "box-created"
    assert inner.state.base_env_vars == {"K": "V"}


@pytest.mark.asyncio
async def test_client_post_box_create_accepts_created_response() -> None:
    client = UpstashBoxSandboxClient(api_key="key")
    http = _FakeHttp({"POST v2/box": _FakeResponse(status=201, json_body={"id": "box-created"})})

    data = await client._post_box_create(
        http,  # type: ignore[arg-type]
        "https://box.example.test/v2/box",
        {},
        timeout=None,  # type: ignore[arg-type]
        is_snapshot=False,
    )

    assert data == {"id": "box-created"}


@pytest.mark.asyncio
async def test_client_resume_reconnects_to_running_box() -> None:
    client = UpstashBoxSandboxClient(api_key="key")
    state = UpstashBoxSandboxSessionState(
        manifest=Manifest(),
        snapshot=resolve_snapshot(None, "11111111-1111-1111-1111-111111111111"),
        box_id="box-test",
        base_url="https://box.example.test",
    )
    http = _FakeHttp({"GET v2/box/box-test": _FakeResponse(json_body={"status": "running"})})

    # Inject the fake transport for the reconnect probe.
    state_session = UpstashBoxSandboxSession.from_state(state, api_key="key", http=http)
    # Succeeds without raising when the box is reachable.
    await client._reconnect(state_session, state)


@pytest.mark.asyncio
async def test_client_resume_resumes_paused_box() -> None:
    client = UpstashBoxSandboxClient(api_key="key")
    state = UpstashBoxSandboxSessionState(
        manifest=Manifest(),
        snapshot=resolve_snapshot(None, "33333333-3333-3333-3333-333333333333"),
        box_id="box-test",
        base_url="https://box.example.test",
    )
    http = _FakeHttp(
        {
            "GET v2/box/box-test": [
                _FakeResponse(json_body={"status": "paused"}),
                _FakeResponse(json_body={"status": "running"}),
            ],
            "POST v2/box/box-test/resume": _FakeResponse(status=200, json_body={}),
        }
    )
    state_session = UpstashBoxSandboxSession.from_state(state, api_key="key", http=http)

    await client._reconnect(state_session, state)
    assert any(c["url"].endswith("/resume") for c in http.calls)


@pytest.mark.asyncio
async def test_client_resume_waits_for_creating_box() -> None:
    client = UpstashBoxSandboxClient(api_key="key")
    state = UpstashBoxSandboxSessionState(
        manifest=Manifest(),
        snapshot=resolve_snapshot(None, "55555555-5555-5555-5555-555555555555"),
        box_id="box-test",
        base_url="https://box.example.test",
    )
    http = _FakeHttp(
        {
            "GET v2/box/box-test": [
                _FakeResponse(json_body={"status": "creating"}),
                _FakeResponse(json_body={"status": "idle"}),
            ],
        }
    )
    state_session = UpstashBoxSandboxSession.from_state(state, api_key="key", http=http)

    await client._reconnect(state_session, state)
    assert len([c for c in http.calls if c["method"] == "GET"]) == 2


@pytest.mark.asyncio
async def test_client_resume_rejects_error_status() -> None:
    client = UpstashBoxSandboxClient(api_key="key")
    state = UpstashBoxSandboxSessionState(
        manifest=Manifest(),
        snapshot=resolve_snapshot(None, "66666666-6666-6666-6666-666666666666"),
        box_id="box-test",
        base_url="https://box.example.test",
    )
    http = _FakeHttp({"GET v2/box/box-test": _FakeResponse(json_body={"status": "error"})})
    state_session = UpstashBoxSandboxSession.from_state(state, api_key="key", http=http)

    with pytest.raises(ConfigurationError):
        await client._reconnect(state_session, state)


@pytest.mark.asyncio
async def test_client_resume_raises_when_box_missing() -> None:
    client = UpstashBoxSandboxClient(api_key="key")
    state = UpstashBoxSandboxSessionState(
        manifest=Manifest(),
        snapshot=resolve_snapshot(None, "22222222-2222-2222-2222-222222222222"),
        box_id="gone",
        base_url="https://box.example.test",
    )
    http = _FakeHttp({"GET v2/box/gone": _FakeResponse(status=404)})
    state_session = UpstashBoxSandboxSession.from_state(state, api_key="key", http=http)

    # A genuinely missing box is surfaced, not silently treated as a fresh workspace.
    with pytest.raises(ConfigurationError):
        await client._reconnect(state_session, state)


@pytest.mark.asyncio
async def test_client_resume_fails_fast_on_transient_error() -> None:
    client = UpstashBoxSandboxClient(api_key="key")
    state = UpstashBoxSandboxSessionState(
        manifest=Manifest(),
        snapshot=resolve_snapshot(None, "44444444-4444-4444-4444-444444444444"),
        box_id="box-test",
        base_url="https://box.example.test",
    )
    # A transient 503 must raise (fail fast) rather than degrade to a manifest re-apply.
    http = _FakeHttp({"GET v2/box/box-test": _FakeResponse(status=503)})
    state_session = UpstashBoxSandboxSession.from_state(state, api_key="key", http=http)

    with pytest.raises(ConfigurationError):
        await client._reconnect(state_session, state)


@pytest.mark.asyncio
async def test_shutdown_failure_is_best_effort_but_not_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A failed delete during cleanup must not raise, but must be logged (not silently swallowed).
    http = _FakeHttp(
        {"DELETE v2/box/box-test": _FakeResponse(status=500, json_body={"error": "boom"})}
    )
    session = _make_session(http)

    with caplog.at_level("WARNING"):
        await session.shutdown()

    assert any("box-test" in record.getMessage() for record in caplog.records)


def _make_tar_bytes() -> bytes:
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"hello\n"
        info = tarfile.TarInfo(name="hello.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
