"""Docker rejects GitRepo before provider work, including after manifest mutation."""

import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agents.classic.run_config import SandboxRunConfig
from agents.classic.sandbox.capabilities import Skills
from agents.classic.sandbox.entries import BaseEntry, Dir, GitRepo
from agents.classic.sandbox.manifest import Manifest
from agents.classic.sandbox.runtime_session_manager import SandboxRuntimeSessionManager
from agents.classic.sandbox.sandbox_agent import SandboxAgent
from agents.classic.sandbox.session import Dependencies, SandboxSession, manifest_ops
from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.classic.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.classic.sandbox.snapshot import NoopSnapshot
from agents.classic.sandbox.types import User

pytest.importorskip("docker")
from agents.classic.sandbox.sandboxes.docker import (  # noqa: E402
    DockerSandboxClient,
    DockerSandboxClientOptions,
    DockerSandboxSession,
    DockerSandboxSessionState,
)

_REJECTION = "Docker sandboxes do not support GitRepo entries."
_INVALID_HOST = "https://user:synthetic-secret@example.test"


def assert_rejection(error: ValueError, *, invalid_host: bool) -> None:
    if not invalid_host:
        assert str(error) == _REJECTION
        return
    assert str(error) == "GitRepo host must be an HTTPS authority without userinfo"
    assert error.__cause__ is None
    assert error.__context__ is None

    def contains_configuration(value: object) -> bool:
        if isinstance(value, (BaseEntry, Manifest, BaseSandboxSession, SandboxSessionState)):
            return True
        if isinstance(value, (list, tuple)):
            return any(contains_configuration(item) for item in value)
        if isinstance(value, dict):
            return any(contains_configuration(item) for item in value.values())
        return False

    for frame, _ in traceback.walk_tb(error.__traceback__):
        if frame.f_globals["__name__"].startswith("agents.classic."):
            assert not any(contains_configuration(value) for value in frame.f_locals.values())


def add_nested_repo(manifest: Manifest, *, invalid_host: bool) -> GitRepo:
    # Mutate an already constructed tree, bypassing constructor-only validation.
    outer = manifest.entries["nested"]
    assert isinstance(outer, Dir)
    repo = GitRepo(repo="fixture/repository", ref="main", ephemeral=True)
    if invalid_host:
        outer.children["earlier-repo"] = GitRepo(repo="fixture/earlier", ref="main")
        repo.host = _INVALID_HOST
    outer.children["deeper"] = Dir(children={"repo": repo})
    return repo


@pytest.fixture
def docker_boundary(monkeypatch: pytest.MonkeyPatch):
    launcher = AsyncMock(side_effect=AssertionError("application launcher ran"))
    provider = Mock(side_effect=AssertionError("Docker provider I/O ran"))
    client = DockerSandboxClient(
        SimpleNamespace(), dependencies=Dependencies(git_repo_copy_launcher=launcher)
    )
    create_container = AsyncMock(side_effect=AssertionError("Docker create ran"))
    monkeypatch.setattr(client, "_create_container", create_container)
    monkeypatch.setattr(client, "get_container", provider)
    manifest = Manifest(
        root="/workspace",
        entries={"first": Dir(), "nested": Dir()},
        users=[User(name="fixture-user")],
    )
    state = DockerSandboxSessionState(
        image="synthetic/image",
        container_id="synthetic-container",
        manifest=manifest,
        snapshot=NoopSnapshot(id="synthetic-snapshot"),
    )
    inner = DockerSandboxSession(
        docker_client=SimpleNamespace(), container=SimpleNamespace(), state=state
    )
    session = SandboxSession(inner, dependencies=client._resolve_dependencies())
    io = []
    for name in (
        "exec",
        "mkdir",
        "_exec_checked_nonzero",
        "_ensure_backend_started",
        "_ensure_runtime_helpers",
        "_prepare_backend_workspace",
        "_after_start_failed",
        "provision_manifest_accounts",
        "running",
    ):
        operation = AsyncMock(side_effect=AssertionError(f"Docker {name} ran"))
        monkeypatch.setattr(inner, name, operation)
        io.append(operation)
    yield SimpleNamespace(client=client, session=session, manifest=manifest)
    create_container.assert_not_awaited()
    provider.assert_not_called()
    launcher.assert_not_awaited()
    for operation in io:
        operation.assert_not_awaited()


@pytest.mark.parametrize("invalid_host", [False, True], ids=["valid-host", "invalid-host"])
@pytest.mark.parametrize("route", ["create", "restored-resume"])
async def test_docker_client_rejects_nested_git_before_provider_work(
    docker_boundary, route: str, invalid_host: bool
) -> None:
    client, manifest = docker_boundary.client, docker_boundary.manifest
    add_nested_repo(manifest, invalid_host=invalid_host)
    with pytest.raises(ValueError) as caught:
        if route == "create":
            await client.create(
                manifest=manifest, options=DockerSandboxClientOptions(image="synthetic/image")
            )
        else:
            payload = client.serialize_session_state(docker_boundary.session.state)
            restored = client.deserialize_session_state(payload)
            await client.resume(restored)
    assert_rejection(caught.value, invalid_host=invalid_host)


async def test_live_docker_rejects_git_added_by_a_capability_before_probing(
    docker_boundary,
) -> None:
    skills = Skills(from_=GitRepo(repo="fixture/skills", ref="main"))
    agent = SandboxAgent[None](name="example", capabilities=[skills])
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=docker_boundary.session),
        run_state=None,
    )
    with pytest.raises(ValueError) as caught:
        await manager.ensure_session(agent=agent, capabilities=[skills], is_resumed_state=False)
    assert_rejection(caught.value, invalid_host=False)


@pytest.mark.parametrize("invalid_host", [False, True], ids=["valid-host", "invalid-host"])
@pytest.mark.parametrize("route", ["git", "dir"])
async def test_direct_docker_entries_reject_before_materialization(
    docker_boundary, route: str, invalid_host: bool
) -> None:
    repo = add_nested_repo(docker_boundary.manifest, invalid_host=invalid_host)
    entry = repo if route == "git" else docker_boundary.manifest.entries["nested"]
    with pytest.raises(ValueError) as caught:
        await entry.apply(docker_boundary.session, Path("/workspace/output"), Path("/"))
    assert_rejection(caught.value, invalid_host=invalid_host)


@pytest.mark.parametrize("invalid_host", [False, True], ids=["valid-host", "invalid-host"])
@pytest.mark.parametrize("route", ["start", "apply", "ephemeral", "manifest-ops", "entry-batch"])
async def test_live_docker_rejects_mutated_nested_git_before_work(
    docker_boundary, route: str, invalid_host: bool
) -> None:
    session = docker_boundary.session
    add_nested_repo(docker_boundary.manifest, invalid_host=invalid_host)
    with pytest.raises(ValueError) as caught:
        if route == "start":
            await session.start()
        elif route == "apply":
            await session.apply_manifest()
        elif route == "ephemeral":
            await session.apply_manifest(only_ephemeral=True)
        elif route == "manifest-ops":
            await manifest_ops.apply_manifest(session)
        else:
            await manifest_ops.apply_entry_batch(
                session,
                [
                    (Path("/workspace") / name, entry)
                    for name, entry in session.state.manifest.entries.items()
                ],
                base_dir=Path("/"),
            )
    assert_rejection(caught.value, invalid_host=invalid_host)
