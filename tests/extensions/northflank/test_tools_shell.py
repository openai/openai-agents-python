"""Smoke tests: imports, namespace registration, tool schemas."""

from __future__ import annotations

from typing import Any

import pytest

# Skip the whole module on Python 3.14 CI legs where the ``northflank``
# optional extra isn't installed (the SDK pins ``python_version < '3.14'``).
pytest.importorskip("northflank")

from agents.extensions.northflank import (  # noqa: E402
    NorthflankCtx,
    NorthflankShellExecutor,
    nf_get_service,
    nf_list_projects,
    northflank_tools,
)
from agents.extensions.northflank._helpers import wrap_shell_command  # noqa: E402
from agents.tool import (  # noqa: E402
    FunctionTool,
    ShellActionRequest,
    ShellCallData,
    ShellCommandRequest,
    ShellResult,
)


def test_imports() -> None:
    assert callable(northflank_tools)
    assert isinstance(nf_list_projects, FunctionTool)
    assert isinstance(nf_get_service, FunctionTool)


def test_default_namespace_has_no_gated_tools() -> None:
    tools = northflank_tools()
    names = {t.name for t in tools}
    assert "nf_list_projects" in names
    assert "nf_create_deployment_service" in names
    assert "nf_run_service_command" in names
    assert "nf_delete_service" not in names
    assert "nf_put_secret" not in names


def test_namespace_metadata_applied() -> None:
    tools = northflank_tools(namespace="ops", description="custom desc")
    for tool in tools:
        # tool_namespace() copies tools and attaches private metadata fields.
        assert getattr(tool, "_tool_namespace", None) == "ops"
        assert getattr(tool, "_tool_namespace_description", None) == "custom desc"


def test_gated_tools_optin() -> None:
    base = {t.name for t in northflank_tools()}
    full = {
        t.name
        for t in northflank_tools(
            include_delete=True,
            include_secrets=True,
            include_volumes=True,
            include_domains=True,
        )
    }
    extras = full - base
    assert extras == {
        "nf_delete_service",
        "nf_put_secret",
        "nf_delete_secret",
        "nf_delete_volume",
        "nf_delete_domain",
    }


def test_mutating_tools_require_approval() -> None:
    tools = {t.name: t for t in northflank_tools(include_delete=True, include_secrets=True)}
    must_approve = {
        "nf_create_deployment_service",
        "nf_restart_service",
        "nf_pause_service",
        "nf_resume_service",
        "nf_scale_service",
        "nf_run_service_command",
        "nf_run_job_command",
        "nf_run_addon_command",
        "nf_delete_service",
        "nf_put_secret",
        "nf_delete_secret",
    }
    for name in must_approve:
        assert tools[name].needs_approval is True, f"{name} should require approval"


def test_read_tools_no_approval() -> None:
    tools = {t.name: t for t in northflank_tools()}
    for name in ("nf_list_projects", "nf_get_project", "nf_list_services", "nf_get_service"):
        assert tools[name].needs_approval is False, f"{name} should not require approval"


def test_tools_emit_json_schema_without_ctx_param() -> None:
    """Each tool exposes a JSON schema; the RunContextWrapper param is hidden."""
    for tool in northflank_tools(include_delete=True):
        schema = tool.params_json_schema
        assert isinstance(schema, dict)
        properties = schema.get("properties", {})
        # ``ctx`` (RunContextWrapper) should not leak into the wire schema.
        assert "ctx" not in properties, f"{tool.name} leaks ctx into its schema"


def test_shell_executor_construct() -> None:
    executor = NorthflankShellExecutor(service_id="svc-xyz")
    assert executor.service_id == "svc-xyz"
    assert executor.shell == "sh"
    assert callable(executor)


class _FakeExec:
    def __init__(self, recorder: list[dict[str, Any]]) -> None:
        self._recorder = recorder

    async def arun_service_command(self, **kwargs):
        self._recorder.append(kwargs)

        class _Result:
            exit_code = 0
            stdout = "hi\n"
            stderr = ""
            status = "completed"

        return _Result()


class _FakeClient:
    def __init__(self, recorder: list[dict[str, Any]]) -> None:
        self.exec = _FakeExec(recorder)


class _FakeRunCtx:
    def __init__(self, context: object) -> None:
        self.context = context


@pytest.mark.asyncio
async def test_shell_executor_wraps_in_sh_dash_lc() -> None:
    recorder: list[dict[str, Any]] = []
    client = _FakeClient(recorder)
    ctx = NorthflankCtx(client=client, project_id="proj-1")
    executor = NorthflankShellExecutor(service_id="svc-1")  # default shell="sh"
    request = ShellCommandRequest(
        ctx_wrapper=_FakeRunCtx(ctx),  # type: ignore[arg-type]
        data=ShellCallData(
            call_id="call-1",
            action=ShellActionRequest(commands=["echo hi", "uname -a"]),
        ),
    )
    result = await executor(request)
    assert isinstance(result, ShellResult)
    assert len(result.output) == 2
    assert result.output[0].stdout == "hi\n"
    assert result.output[0].outcome.type == "exit"
    assert result.output[0].outcome.exit_code == 0
    # The SDK's ``shell`` field is forwarded verbatim to the exec proxy and
    # only ``"none"`` is meaningful — we have to invoke the shell ourselves.
    assert all(c["shell"] == "none" for c in recorder)
    assert recorder[0]["command"] == ["sh", "-lc", "echo hi"]
    assert recorder[1]["command"] == ["sh", "-lc", "uname -a"]
    assert all(c["service_id"] == "svc-1" and c["project_id"] == "proj-1" for c in recorder)


def test_wrap_shell_command_translation() -> None:
    # ``none`` passes the string straight through.
    cmd, shell = wrap_shell_command("ls -la", "none")
    assert cmd == "ls -la"
    assert shell == "none"
    # ``sh`` and ``bash`` always set the SDK shell to ``none`` and wrap the
    # command as an explicit argv, since the proxy only understands ``none``.
    cmd, shell = wrap_shell_command("ls -la | head", "sh")
    assert cmd == ["sh", "-lc", "ls -la | head"]
    assert shell == "none"
    cmd, shell = wrap_shell_command("echo $FOO", "bash")
    assert cmd == ["bash", "-lc", "echo $FOO"]
    assert shell == "none"


@pytest.mark.asyncio
async def test_shell_executor_shell_none_passes_through() -> None:
    recorder: list[dict[str, Any]] = []
    client = _FakeClient(recorder)
    ctx = NorthflankCtx(client=client, project_id="proj-1")
    executor = NorthflankShellExecutor(service_id="svc-1", shell="none")
    request = ShellCommandRequest(
        ctx_wrapper=_FakeRunCtx(ctx),  # type: ignore[arg-type]
        data=ShellCallData(
            call_id="call-1",
            action=ShellActionRequest(commands=["ls"]),
        ),
    )
    await executor(request)
    assert recorder[0]["command"] == "ls"
    assert recorder[0]["shell"] == "none"


@pytest.mark.asyncio
async def test_shell_executor_raises_without_project() -> None:
    client = _FakeClient([])
    ctx = NorthflankCtx(client=client)  # no project_id
    executor = NorthflankShellExecutor(service_id="svc-1")
    request = ShellCommandRequest(
        ctx_wrapper=_FakeRunCtx(ctx),  # type: ignore[arg-type]
        data=ShellCallData(
            call_id="call-1",
            action=ShellActionRequest(commands=["echo hi"]),
        ),
    )
    with pytest.raises(RuntimeError, match="project_id"):
        await executor(request)
