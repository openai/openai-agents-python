from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Literal, cast

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.entries import File

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

Backend = Literal["docker", "modal"]
WorkspacePersistenceMode = Literal["tar", "snapshot_filesystem"]

DEFAULT_QUESTION = "Summarize this sandbox project in 2 sentences."
DEFAULT_BACKEND: Backend = "docker"
DEFAULT_MODAL_APP_NAME = "openai-agents-python-sandbox-example"
DEFAULT_MODAL_WORKSPACE_PERSISTENCE: WorkspacePersistenceMode = "tar"


def _stream_event_banner(event_name: str) -> str | None:
    if event_name == "tool_called":
        return "[tool call] shell"
    if event_name == "tool_output":
        return "[tool output] shell"
    return None


def _build_manifest(backend: Backend) -> Manifest:
    backend_label = "Docker" if backend == "docker" else "Modal"
    return Manifest(
        entries={
            "README.md": File(
                content=(
                    b"# Demo Project\n\n"
                    + (
                        f"This sandbox contains a tiny demo project for the {backend_label} "
                        "sandbox runner.\n"
                    ).encode()
                    + b"The goal is to show how Runner can prepare a sandbox workspace.\n"
                )
            ),
            "src/app.py": File(
                content=b'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n'
            ),
            "docs/notes.md": File(
                content=(
                    b"# Notes\n\n"
                    b"- The example is intentionally minimal.\n"
                    b"- The model should inspect files through the shell tool.\n"
                )
            ),
        }
    )


def _build_agent(*, model: str, manifest: Manifest, backend: Backend) -> SandboxAgent:
    backend_label = "Docker" if backend == "docker" else "Modal"
    return SandboxAgent(
        name=f"{backend_label} Sandbox Assistant",
        model=model,
        # `instructions` is the base agent instructions for this example's task.
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the project before answering, "
            "and keep the response concise."
        ),
        # `developer_instructions` is appended after that as additional deterministic instructions.
        # Here, the tiny-workspace constraint is kept in `developer_instructions`.
        developer_instructions=(
            "Do not guess file names like package.json or pyproject.toml. "
            "This demo intentionally contains a tiny workspace."
        ),
        # `default_manifest` tells the sandbox agent which workspace it should expect.
        default_manifest=manifest,
        # `WorkspaceShellCapability()` exposes one shell tool so the model can inspect files.
        capabilities=[WorkspaceShellCapability()],
        # `tool_choice="required"` makes the demo more deterministic by forcing the model
        # to look at the workspace instead of answering from prior assumptions.
        model_settings=ModelSettings(tool_choice="required"),
    )


def _require_modal_dependency() -> tuple[Any, Any]:
    try:
        from agents.extensions.sandbox import ModalSandboxClient, ModalSandboxClientOptions
    except Exception as exc:  # pragma: no cover - import path depends on optional extras
        raise SystemExit(
            "Modal-backed runs require the optional repo extra.\n"
            "Install it with: uv sync --extra modal"
        ) from exc

    return ModalSandboxClient, ModalSandboxClientOptions


def _require_docker_dependency() -> tuple[Any, Any, Any]:
    try:
        from docker import from_env as docker_from_env  # type: ignore[import-untyped]
    except Exception as exc:  # pragma: no cover - import path depends on local Docker setup
        raise SystemExit(
            "Docker-backed runs require the Docker SDK.\n"
            "Install the repo dependencies with: make sync"
        ) from exc

    from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions

    return docker_from_env, DockerSandboxClient, DockerSandboxClientOptions


async def _create_session(
    *,
    backend: Backend,
    manifest: Manifest,
    agent: SandboxAgent,
):
    if backend == "docker":
        docker_from_env, DockerSandboxClient, DockerSandboxClientOptions = (
            _require_docker_dependency()
        )
        client = DockerSandboxClient(docker_from_env())
        session = await client.create(
            manifest=manifest,
            codex=agent.codex,
            options=DockerSandboxClientOptions(image="python:3.14-slim"),
        )
        return client, session

    ModalSandboxClient, ModalSandboxClientOptions = _require_modal_dependency()
    client = ModalSandboxClient()
    session = await client.create(
        manifest=manifest,
        codex=agent.codex,
        options=ModalSandboxClientOptions(
            app_name=DEFAULT_MODAL_APP_NAME,
            workspace_persistence=DEFAULT_MODAL_WORKSPACE_PERSISTENCE,
        ),
    )
    return client, session


async def main(
    model: str,
    question: str,
    backend: Backend,
) -> None:
    manifest = _build_manifest(backend)
    agent = _build_agent(model=model, manifest=manifest, backend=backend)
    client, session = await _create_session(
        backend=backend,
        manifest=manifest,
        agent=agent,
    )

    await session.start()
    print(await session.ls(".codex_bin/codex"))

    try:
        # `async with session` keeps the example on the public session lifecycle API.
        # `Runner` reuses the already-running session without starting it a second time.
        async with session:
            # `Runner.run_streamed()` drives the model and yields text and tool events in real time.
            result = Runner.run_streamed(
                agent,
                question,
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(session=session),
                    workflow_name=f"{backend.title()} sandbox example",
                ),
            )
            saw_text_delta = False
            saw_any_text = False

            # The stream contains raw text deltas from the assistant plus structured tool events.
            async for event in result.stream_events():
                if event.type == "raw_response_event" and isinstance(
                    event.data, ResponseTextDeltaEvent
                ):
                    if not saw_text_delta:
                        print("assistant> ", end="", flush=True)
                        saw_text_delta = True
                    print(event.data.delta, end="", flush=True)
                    saw_any_text = True
                    continue

                if event.type != "run_item_stream_event":
                    continue

                banner = _stream_event_banner(event.name)
                if banner is not None:
                    if saw_text_delta:
                        print()
                        saw_text_delta = False
                    print(banner)

            if saw_text_delta:
                print()
            if not saw_any_text:
                print(result.final_output)
    finally:
        await client.delete(session)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        choices=["docker", "modal"],
        help="Sandbox backend to use for this example.",
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            args.model,
            args.question,
            cast(Backend, args.backend),
        )
    )
