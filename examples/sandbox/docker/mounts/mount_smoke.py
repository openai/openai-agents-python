from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import docker  # type: ignore[import-untyped]

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.entries import Mount
from agents.sandbox.errors import MountCommandError
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxClientOptions,
)
from agents.sandbox.session.sandbox_session import SandboxSession
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

IMAGE = "agents-sandbox-docker-mount-example:latest"
DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile.mount"


@dataclass(frozen=True)
class MountSmokeCase:
    """One mount target to verify inside a shared Docker sandbox session."""

    name: str
    mount_dir: str
    mount: Mount


def require_env(name: str) -> str:
    """Return a required environment variable or stop with a clear message."""

    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def ensure_mount_image() -> None:
    """Build the Docker image with the in-container mount CLIs if it is missing."""

    docker_client = docker.from_env()
    try:
        docker_client.images.get(IMAGE)
        return
    except docker.errors.ImageNotFound:
        pass

    print(f"building {IMAGE} from {DOCKERFILE.name}...")
    docker_client.images.build(
        path=str(DOCKERFILE.parent),
        dockerfile=DOCKERFILE.name,
        tag=IMAGE,
        rm=True,
    )


def build_agent(name: str, manifest: Manifest) -> SandboxAgent:
    """Create the minimal shell-only agent used by these mount smoke tests."""

    return SandboxAgent(
        name=name,
        model=os.getenv("OPENAI_MODEL", "gpt-5.4"),
        instructions=(
            "Use the shell tool only. Write the requested exact content to the requested exact "
            "path, read the file back with cat, and then reply with only `done`."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )


async def _check_case(
    sandbox: SandboxSession,
    agent: SandboxAgent,
    provider: str,
    mount_case: MountSmokeCase,
) -> None:
    key = f"docker-{provider}-mount-example-{mount_case.mount_dir}-{uuid.uuid4().hex}.txt"
    path = Path("/workspace") / mount_case.mount_dir / key
    expected = f"hello from {mount_case.name} {uuid.uuid4().hex}"

    result = await Runner.run(
        agent,
        (
            f"Write exactly this content to {path} with `printf %s`, not `echo`: {expected}\n"
            f"Then read {path} back with cat."
        ),
        run_config=RunConfig(
            sandbox=SandboxRunConfig(session=sandbox),
            workflow_name=f"Docker {provider} mount smoke test ({mount_case.name})",
        ),
    )
    print(result.final_output)

    read_back = await sandbox.read(path)
    actual = read_back.read()
    if not isinstance(actual, bytes):
        raise TypeError(f"Expected bytes from session.read(), got {type(actual)!r}")

    actual_text = actual.decode("utf-8")
    if actual_text == f"{expected}\n":
        actual_text = expected

    assert actual_text == expected, f"read back {actual!r}, expected {expected!r}"
    print(f"{mount_case.name}: ok")


async def run_mount_smoke_test(
    *,
    provider: str,
    agent_name: str,
    mount_cases: Sequence[MountSmokeCase],
) -> None:
    """Start one Docker sandbox session and verify read/write on every mount target."""

    ensure_mount_image()

    manifest = Manifest(
        entries={mount_case.mount_dir: mount_case.mount for mount_case in mount_cases},
    )
    agent = build_agent(agent_name, manifest)
    client = DockerSandboxClient(docker.from_env())

    try:
        sandbox = await client.create(
            manifest=manifest,
            options=DockerSandboxClientOptions(image=IMAGE),
        )
    except docker.errors.NotFound as exc:
        if 'plugin "rclone" not found' in str(exc):
            raise SystemExit("rclone Docker volume plugin not found") from exc
        raise

    try:
        await sandbox.start()
    except MountCommandError as exc:
        print(f"mount command: {exc.context.get('command')}")
        print(f"mount stderr: {exc.context.get('stderr')}")
        raise

    try:
        for mount_case in mount_cases:
            await _check_case(sandbox, agent, provider, mount_case)
    finally:
        await client.delete(sandbox)
