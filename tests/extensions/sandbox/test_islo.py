"""Unit tests for the islo sandbox client.

These tests stub the ``AsyncIslo`` SDK at the namespace level so we cover the integration's
control flow (gateway-profile lifecycle, command poll loop, file IO, error mapping) without
hitting api.islo.dev. Live integration tests against the real backend are gated on
``ISLO_API_KEY`` and run separately.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("islo", reason="islo extra not installed")

from agents.extensions.sandbox.islo import (  # noqa: E402
    IsloGatewayProfile,
    IsloGatewayRule,
    IsloSandboxClient,
    IsloSandboxClientOptions,
    IsloSandboxSession,
    IsloSandboxSessionState,
)
from agents.sandbox.errors import (  # noqa: E402
    ExecTimeoutError,
    ExecTransportError,
    WorkspaceReadNotFoundError,
)
from agents.sandbox.manifest import Manifest  # noqa: E402
from agents.sandbox.snapshot import NoopSnapshot  # noqa: E402


def _make_sdk_stub(**overrides: Callable[..., Awaitable[Any]]) -> Any:
    sdk = MagicMock()
    sdk.sandboxes = MagicMock()
    sdk.gateway_profiles = MagicMock()

    sdk.sandboxes.create_sandbox = AsyncMock(
        side_effect=overrides.get("create_sandbox", _default_create_sandbox)
    )
    sdk.sandboxes.get_sandbox = AsyncMock(
        side_effect=overrides.get("get_sandbox", _default_get_sandbox)
    )
    sdk.sandboxes.delete_sandbox = AsyncMock(
        side_effect=overrides.get("delete_sandbox", _default_noop)
    )
    sdk.sandboxes.exec_in_sandbox = AsyncMock(
        side_effect=overrides.get("run_command", _default_run_command)
    )
    sdk.sandboxes.get_exec_result = AsyncMock(
        side_effect=overrides.get("get_run_result", _default_get_run_result)
    )
    sdk.sandboxes.download_file = AsyncMock(
        side_effect=overrides.get("download_file", _default_download_file)
    )
    sdk.sandboxes.upload_file = AsyncMock(
        side_effect=overrides.get("upload_file", _default_noop)
    )
    sdk.sandboxes.download_archive = AsyncMock(
        side_effect=overrides.get("download_archive", _default_download_archive)
    )
    sdk.sandboxes.upload_archive = AsyncMock(
        side_effect=overrides.get("upload_archive", _default_noop)
    )

    sdk.gateway_profiles.create_gateway_profile = AsyncMock(
        side_effect=overrides.get("create_gateway_profile", _default_create_gateway_profile)
    )
    sdk.gateway_profiles.create_gateway_rule = AsyncMock(
        side_effect=overrides.get("create_gateway_rule", _default_create_gateway_rule)
    )
    sdk.gateway_profiles.delete_gateway_profile = AsyncMock(
        side_effect=overrides.get("delete_gateway_profile", _default_noop)
    )

    return sdk


async def _default_create_sandbox(**kwargs: Any) -> Any:
    sb = MagicMock()
    sb.name = kwargs.get("name") or "agents-fake"
    sb.status = "running"
    sb.image = kwargs.get("image") or "islo-runner:latest"
    return sb


async def _default_get_sandbox(name: str, **_: Any) -> Any:
    sb = MagicMock()
    sb.name = name
    sb.status = "running"
    return sb


async def _default_run_command(*_args: Any, **_kwargs: Any) -> Any:
    resp = MagicMock()
    resp.exec_id = "run_test"
    resp.status = "started"
    return resp


async def _default_get_run_result(*_args: Any, **_kwargs: Any) -> Any:
    res = MagicMock()
    res.status = "finished"
    res.exit_code = 0
    res.stdout = "hello\n"
    res.stderr = ""
    return res


async def _default_download_file(*_args: Any, **_kwargs: Any) -> bytes:
    return b"file contents"


async def _default_download_archive(*_args: Any, **_kwargs: Any) -> bytes:
    return b"tar bytes"


async def _default_create_gateway_profile(**kwargs: Any) -> Any:
    profile = MagicMock()
    profile.id = "gp_test"
    profile.name = kwargs.get("name", "")
    return profile


async def _default_create_gateway_rule(*_args: Any, **_kwargs: Any) -> Any:
    rule = MagicMock()
    rule.id = "gr_test"
    return rule


async def _default_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _make_state(name: str = "agents-fake") -> IsloSandboxSessionState:
    return IsloSandboxSessionState(
        sandbox_name=name,
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="snap-test"),
        wait_for_running_timeout_s=2.0,
        exec_poll_interval_s=0.01,
    )


@pytest.mark.asyncio
async def test_create_with_string_gateway_profile_does_not_create_or_delete() -> None:
    sdk = _make_sdk_stub()
    client = IsloSandboxClient(sdk=sdk)
    options = IsloSandboxClientOptions(gateway_profile="existing-profile")

    session = await client.create(options=options)
    try:
        assert sdk.gateway_profiles.create_gateway_profile.await_count == 0
        assert sdk.sandboxes.create_sandbox.await_count == 1
        kwargs = sdk.sandboxes.create_sandbox.await_args.kwargs
        assert kwargs.get("gateway_profile") == "existing-profile"
    finally:
        await client.delete(session)
    assert sdk.gateway_profiles.delete_gateway_profile.await_count == 0


@pytest.mark.asyncio
async def test_create_with_inline_gateway_profile_provisions_and_cleans_up() -> None:
    sdk = _make_sdk_stub()
    client = IsloSandboxClient(sdk=sdk)
    profile = IsloGatewayProfile(
        default_action="deny",
        internet_enabled=False,
        rules=(
            IsloGatewayRule(host_pattern="api.openai.com", action="allow", rate_limit_rpm=120),
            IsloGatewayRule(host_pattern="*.github.com", action="allow"),
        ),
    )
    options = IsloSandboxClientOptions(gateway_profile=profile)

    session = await client.create(options=options)
    try:
        assert sdk.gateway_profiles.create_gateway_profile.await_count == 1
        create_kwargs = sdk.gateway_profiles.create_gateway_profile.await_args.kwargs
        assert create_kwargs["default_action"] == "deny"
        assert create_kwargs["internet_enabled"] is False

        assert sdk.gateway_profiles.create_gateway_rule.await_count == 2
        rule_calls = sdk.gateway_profiles.create_gateway_rule.await_args_list
        first = rule_calls[0].kwargs
        assert first["host_pattern"] == "api.openai.com"
        assert first["action"] == "allow"
        assert first["rate_limit_rpm"] == 120

        sandbox_kwargs = sdk.sandboxes.create_sandbox.await_args.kwargs
        assert sandbox_kwargs["gateway_profile"] == "gp_test"
    finally:
        await client.delete(session)

    assert sdk.gateway_profiles.delete_gateway_profile.await_count == 1
    assert sdk.gateway_profiles.delete_gateway_profile.await_args.args[0] == "gp_test"


@pytest.mark.asyncio
async def test_create_cleans_up_inline_profile_on_sandbox_failure() -> None:
    async def boom(**_: Any) -> Any:
        raise RuntimeError("create failed")

    sdk = _make_sdk_stub(create_sandbox=boom)
    client = IsloSandboxClient(sdk=sdk)
    options = IsloSandboxClientOptions(
        gateway_profile=IsloGatewayProfile(
            rules=(IsloGatewayRule(host_pattern="example.com"),),
        ),
    )

    with pytest.raises(RuntimeError, match="create failed"):
        await client.create(options=options)

    assert sdk.gateway_profiles.create_gateway_profile.await_count == 1
    assert sdk.gateway_profiles.delete_gateway_profile.await_count == 1


@pytest.mark.asyncio
async def test_inline_rule_failure_tears_down_partial_profile() -> None:
    rule_calls: list[Any] = []

    async def rule_side_effect(*args: Any, **kwargs: Any) -> Any:
        rule_calls.append(kwargs)
        if len(rule_calls) == 1:
            return MagicMock(id="gr_first")
        raise RuntimeError("rate limit not supported")

    sdk = _make_sdk_stub(create_gateway_rule=rule_side_effect)
    client = IsloSandboxClient(sdk=sdk)
    options = IsloSandboxClientOptions(
        gateway_profile=IsloGatewayProfile(
            rules=(
                IsloGatewayRule(host_pattern="example.com"),
                IsloGatewayRule(host_pattern="other.com", rate_limit_rpm=10),
            ),
        ),
    )

    with pytest.raises(Exception):  # noqa: B017
        await client.create(options=options)

    assert sdk.gateway_profiles.create_gateway_profile.await_count == 1
    assert sdk.gateway_profiles.delete_gateway_profile.await_count == 1
    assert sdk.sandboxes.create_sandbox.await_count == 0


@pytest.mark.asyncio
async def test_run_polls_until_finished_and_returns_result() -> None:
    statuses = iter([("running", None), ("running", None), ("finished", 0)])

    async def get_run_result(*_: Any, **__: Any) -> Any:
        status, exit_code = next(statuses)
        res = MagicMock()
        res.status = status
        res.exit_code = exit_code
        res.stdout = "ok\n" if status == "finished" else ""
        res.stderr = ""
        return res

    sdk = _make_sdk_stub(get_run_result=get_run_result)
    state = _make_state()
    session = IsloSandboxSession.from_state(state, sdk=sdk)

    result = await session._exec_internal("echo", "ok")
    assert result.exit_code == 0
    assert result.stdout == b"ok\n"
    assert sdk.sandboxes.get_exec_result.await_count == 3


@pytest.mark.asyncio
async def test_run_times_out_when_status_never_finishes() -> None:
    async def get_run_result(*_: Any, **__: Any) -> Any:
        res = MagicMock()
        res.status = "running"
        res.exit_code = None
        res.stdout = ""
        res.stderr = ""
        return res

    sdk = _make_sdk_stub(get_run_result=get_run_result)
    state = _make_state()
    session = IsloSandboxSession.from_state(state, sdk=sdk)

    with pytest.raises(ExecTimeoutError):
        await session._exec_internal("sleep", "999", timeout=0.05)


@pytest.mark.asyncio
async def test_run_transport_error_when_run_command_fails() -> None:
    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("network down")

    sdk = _make_sdk_stub(run_command=boom)
    state = _make_state()
    session = IsloSandboxSession.from_state(state, sdk=sdk)

    with pytest.raises(ExecTransportError):
        await session._exec_internal("echo", "x")


def _make_session_with_response(
    response_status: int,
    response_content: bytes = b"",
) -> tuple[Any, IsloSandboxSession, list[dict[str, Any]]]:
    sdk = _make_sdk_stub()
    state = _make_state()
    session = IsloSandboxSession.from_state(state, sdk=sdk)
    calls: list[dict[str, Any]] = []

    async def fake_request(
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        calls.append(
            {"method": method, "path": path, "params": params, "files": files, "content": content}
        )
        resp = MagicMock()
        resp.status_code = response_status
        resp.content = response_content
        resp.text = response_content.decode("utf-8", errors="replace")
        return resp

    session._islo_request = fake_request  # type: ignore[assignment,method-assign]
    return sdk, session, calls


@pytest.mark.asyncio
async def test_read_returns_bytes_payload_and_uses_workspace_path() -> None:
    _, session, calls = _make_session_with_response(200, b"file contents")
    stream = await session.read("hello.txt")
    assert stream.read() == b"file contents"
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"].endswith("/files")
    assert calls[0]["params"]["path"].endswith("hello.txt")


@pytest.mark.asyncio
async def test_read_404_raises_workspace_read_not_found() -> None:
    _, session, _ = _make_session_with_response(404, b"")
    with pytest.raises(WorkspaceReadNotFoundError):
        await session.read("missing.txt")


@pytest.mark.asyncio
async def test_write_forwards_bytes_via_multipart() -> None:
    _, session, calls = _make_session_with_response(200, b"")
    await session.write("notes.txt", io.BytesIO(b"hello"))
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"].endswith("/files")
    assert calls[0]["params"]["path"].endswith("notes.txt")
    files = calls[0]["files"]
    assert files is not None
    file_field = files["file"]
    assert file_field[0] == "notes.txt"
    assert file_field[1] == b"hello"


@pytest.mark.asyncio
async def test_running_returns_false_when_status_not_running() -> None:
    async def get_sandbox(name: str, **_: Any) -> Any:
        sb = MagicMock()
        sb.name = name
        sb.status = "stopped"
        return sb

    sdk = _make_sdk_stub(get_sandbox=get_sandbox)
    state = _make_state()
    session = IsloSandboxSession.from_state(state, sdk=sdk)
    assert (await session.running()) is False


@pytest.mark.asyncio
async def test_persist_workspace_returns_archive_bytes() -> None:
    _, session, calls = _make_session_with_response(200, b"tar bytes")
    stream = await session.persist_workspace()
    assert stream.read() == b"tar bytes"
    assert calls[0]["path"].endswith("/archive")
    assert calls[0]["method"] == "GET"


@pytest.mark.asyncio
async def test_resume_reattaches_when_sandbox_still_running() -> None:
    sdk = _make_sdk_stub()
    client = IsloSandboxClient(sdk=sdk)
    state = _make_state(name="resume-target")

    session = await client.resume(state)
    inner = session._inner
    assert isinstance(inner, IsloSandboxSession)
    assert sdk.sandboxes.get_sandbox.await_count >= 1
    assert sdk.sandboxes.create_sandbox.await_count == 0
