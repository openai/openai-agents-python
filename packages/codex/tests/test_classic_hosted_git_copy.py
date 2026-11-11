"""Hosted copies retain application launch authority across create and resume."""

import asyncio
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.classic.extensions.sandbox.daytona import sandbox as daytona
from agents.classic.sandbox.entries.artifacts import _GIT_COPY
from agents.classic.sandbox.manifest import Environment, Manifest
from agents.classic.sandbox.session.dependencies import Dependencies
from agents.classic.sandbox.session.sandbox_session import SandboxSession
from agents.classic.sandbox.snapshot import NoopSnapshot
from agents.classic.sandbox.types import ExecResult

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX executable fixtures")


@pytest.fixture
def image(tmp_path: Path):
    workspace, tools = tmp_path / "workspace", tmp_path / "image-tools"
    workspace.mkdir()
    tools.mkdir()
    marker, poison = tmp_path / "image-helper-used", tmp_path / "workspace-code-used"
    shell = tools / "image-shell"
    for name, target in (("bash", shell), ("cp", tools / "image-copy-native")):
        source = shutil.which(name)
        assert source is not None
        shutil.copyfile(source, target)
        target.chmod(0o755)

    def script(path: Path, body: str) -> None:
        path.write_text(f"#!{shell}\n{body}", encoding="utf-8")
        path.chmod(0o755)

    script(tools / "cp", 'exec image-copy-helper "$@"\n')
    script(
        tools / "image-copy-helper",
        f'printf image > {shlex.quote(str(marker))}\nexec image-copy-native "$@"\n',
    )
    startup = workspace / "startup"
    script(startup, f"printf hostile > {shlex.quote(str(poison))}\nexit 91\n")
    for name in ("sh", "env", "cp", "image-copy-helper"):
        shutil.copyfile(startup, workspace / name)
        (workspace / name).chmod(0o755)
    environment = {"PATH": str(workspace), "BASH_ENV": str(startup)}
    calls = []

    async def launch(session, copy_script, arguments):
        calls.append(session)
        process = await asyncio.create_subprocess_exec(
            str(shell),
            "-c",
            copy_script,
            "application-copy",
            *arguments,
            cwd=workspace,
            env={"PATH": str(tools)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode is not None
        return ExecResult(stdout=stdout, stderr=stderr, exit_code=process.returncode)

    state = daytona.DaytonaSandboxSessionState(
        manifest=Manifest(root=str(workspace), environment=Environment(value=environment)),
        snapshot=NoopSnapshot(id="hosted-copy-fixture"),
        sandbox_id="synthetic-provider",
        base_env_vars=environment,
    )
    session = daytona.DaytonaSandboxSession(state=state, sandbox=SimpleNamespace())
    return SimpleNamespace(
        session=session,
        launch=launch,
        calls=calls,
        marker=marker,
        poison=poison,
        workspace=workspace,
        environment=environment,
    )


async def test_hosted_copy_uses_physical_image_tools_with_poisoned_workspace(image, monkeypatch):
    for name, value in image.environment.items():
        monkeypatch.setenv(name, value)
    image.session.set_dependencies(Dependencies(git_repo_copy_launcher=image.launch))
    destination = image.workspace / "output"
    destination.mkdir()
    with tempfile.TemporaryDirectory(prefix="sandbox-git-hosted-", dir="/tmp") as root:
        (Path(root) / "payload").write_bytes(b"actual working-tree data\x00\xff")
        result = await image.session._exec_git_copy(_GIT_COPY, root, ".", str(destination))
    assert result.ok(), result.stderr
    assert (destination / "payload").read_bytes() == b"actual working-tree data\x00\xff"
    assert image.marker.read_text() == "image"
    assert image.calls == [image.session]
    assert not image.poison.exists()


async def test_hosted_create_resume_uses_current_binding_without_serialized_authority(
    image, monkeypatch
):
    template = Dependencies(git_repo_copy_launcher=image.launch)
    provider = SimpleNamespace(id="synthetic-provider", state="started")
    client = object.__new__(daytona.DaytonaSandboxClient)
    client._dependencies, client._instrumentation = template, None
    client._daytona = SimpleNamespace(
        create=AsyncMock(return_value=provider), get=AsyncMock(return_value=provider)
    )
    monkeypatch.setattr(client, "_build_create_params", AsyncMock(return_value=None))
    monkeypatch.setattr(
        daytona, "_import_sandbox_state", lambda: SimpleNamespace(STARTED="started")
    )
    created = await client.create(
        manifest=image.session.state.manifest,
        options=daytona.DaytonaSandboxClientOptions(image="synthetic/custom-image"),
    )
    payload = client.serialize_session_state(created.state)
    assert "git_repo_copy_launcher" not in str(payload)
    payload["git_repo_copy_launcher"] = str(image.workspace / "cp")
    fresh_calls = []

    async def fresh_launcher(session, script, arguments):
        fresh_calls.append(session)
        return await image.launch(session, script, arguments)

    fresh_template = Dependencies(git_repo_copy_launcher=fresh_launcher)
    client._dependencies = fresh_template
    resumed = await client.resume(client.deserialize_session_state(payload))
    for wrapped, configured in ((created, template), (resumed, fresh_template)):
        assert isinstance(wrapped, SandboxSession)
        assert wrapped.dependencies is not configured
        result = await wrapped._exec_git_copy('printf "%s" "$1"', "current application")
        assert result.ok(), result.stderr
        assert result.stdout == b"current application"
    assert created.dependencies is not resumed.dependencies
    assert image.calls == [created._inner, resumed._inner]
    assert fresh_calls == [resumed._inner]
    client._dependencies = None
    unbound = await client.resume(client.deserialize_session_state(payload))
    safe_default = ExecResult(stdout=b"default route", stderr=b"", exit_code=0)
    ordinary_exec = AsyncMock(return_value=safe_default)
    monkeypatch.setattr(unbound._inner, "exec", ordinary_exec)
    assert await unbound._exec_git_copy("exit 0") is safe_default
    ordinary_exec.assert_awaited_once()
    assert fresh_calls == [resumed._inner]
    assert not image.poison.exists()


@pytest.mark.parametrize("raises", [False, True], ids=["nonzero", "transport-error"])
async def test_bound_launcher_failure_never_uses_ambient_exec(image, monkeypatch, raises):
    failure = ExecResult(stdout=b"", stderr=b"synthetic copy failure", exit_code=72)
    transport_error = RuntimeError("synthetic provider transport failure")

    async def failed_launcher(session, script, arguments):
        if raises:
            raise transport_error
        return failure

    image.session.set_dependencies(Dependencies(git_repo_copy_launcher=failed_launcher))
    ambient_exec = AsyncMock(side_effect=AssertionError("ambient exec has been attempted"))
    monkeypatch.setattr(image.session, "exec", ambient_exec)
    if raises:
        with pytest.raises(RuntimeError) as caught:
            await image.session._exec_git_copy("exit 0")
        assert caught.value is transport_error
    else:
        assert await image.session._exec_git_copy("exit 0") is failure
    ambient_exec.assert_not_awaited()
