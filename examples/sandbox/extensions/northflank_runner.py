"""Minimal Northflank-backed sandbox example for manual validation.

Creates a tiny workspace, lets the agent inspect it through one shell
tool, prints a short answer, then verifies a stop/resume snapshot round
trip against a real Northflank service.

Pass ``--workspace-persistence volume`` to provision a Northflank volume
mounted at the sandbox workspace root — the volume survives stop/resume
and is removed automatically by ``client.delete`` at the end. Pass
``--workspace-persistence tar`` to capture the workspace as a tar
embedded in session state on stop and replay it on resume.
"""

import argparse
import asyncio
import io
import os
import sys
from pathlib import Path
from typing import Any, Literal

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent, SandboxRunConfig

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox.northflank import (
        NorthflankSandboxClient,
        NorthflankSandboxClientOptions,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Northflank sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra northflank"
    ) from exc

from northflank import AsyncApiClient

DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."
SNAPSHOT_CHECK_PATH = Path("snapshot-check.txt")
SNAPSHOT_CHECK_CONTENT = "northflank snapshot round-trip ok\n"


def _build_manifest() -> Manifest:
    return text_manifest(
        {
            "README.md": (
                "# Renewal Notes\n\n"
                "This workspace contains a tiny account review packet for manual sandbox testing.\n"
            ),
            "customer.md": (
                "# Customer\n\n"
                "- Name: Northwind Health.\n"
                "- Renewal date: 2026-04-15.\n"
                "- Risk: unresolved SSO setup.\n"
            ),
            "next_steps.md": (
                "# Next steps\n\n"
                "1. Finish the SSO fix.\n"
                "2. Confirm legal language before procurement review.\n"
            ),
        }
    )


def _require_env(name: str) -> None:
    if os.environ.get(name):
        return
    raise SystemExit(f"{name} must be set before running this example.")


def _build_options(
    *,
    project_id: str,
    team_id: str | None,
    image_path: str,
    docker_command: str,
    workspace_persistence: Literal["volume", "tar"] | None = None,
    volume_storage_size_mb: int | None = None,
) -> NorthflankSandboxClientOptions:
    # When the caller hasn't overridden the size, let the provider apply
    # its default volume spec (small nf-multi-rw volume).
    volume_spec: dict[str, Any] | None = None
    if workspace_persistence == "volume" and volume_storage_size_mb is not None:
        volume_spec = {
            "storageSize": volume_storage_size_mb,
            "accessMode": "ReadWriteOnce",
            "storageClassName": "nf-multi-rw",
        }
    return NorthflankSandboxClientOptions(
        project_id=project_id,
        team_id=team_id,
        image_path=image_path,
        docker_command=docker_command,
        wait_for_ready=True,
        wait_timeout_s=420.0,
        exec_timeout_s=120.0,
        workspace_persistence=workspace_persistence,
        volume_spec=volume_spec,
    )


async def _verify_stop_resume(
    *,
    project_id: str,
    team_id: str | None,
    image_path: str,
    docker_command: str,
    workspace_persistence: Literal["volume", "tar"] | None = None,
    volume_storage_size_mb: int | None = None,
) -> None:
    import tempfile

    nf = AsyncApiClient()
    client = NorthflankSandboxClient(client=nf)
    options = _build_options(
        project_id=project_id,
        team_id=team_id,
        image_path=image_path,
        docker_command=docker_command,
        workspace_persistence=workspace_persistence,
        volume_storage_size_mb=volume_storage_size_mb,
    )
    with tempfile.TemporaryDirectory(prefix="nf-snapshot-example-") as snapshot_dir:
        # Track the handle that currently owns the service so the outer
        # ``finally`` can always delete it. Both the original and resumed
        # sessions share the same ``service_id``, so either works.
        cleanup_sandbox = None
        try:
            sandbox = await client.create(
                manifest=_build_manifest(),
                snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
                options=options,
            )
            cleanup_sandbox = sandbox

            try:
                await sandbox.start()
                await sandbox.write(
                    SNAPSHOT_CHECK_PATH,
                    io.BytesIO(SNAPSHOT_CHECK_CONTENT.encode("utf-8")),
                )
                await sandbox.stop()
            finally:
                await sandbox.shutdown()

            resumed_sandbox = await client.resume(sandbox.state)
            cleanup_sandbox = resumed_sandbox
            try:
                await resumed_sandbox.start()
                restored = await resumed_sandbox.read(SNAPSHOT_CHECK_PATH)
                restored_text = restored.read()
                if isinstance(restored_text, bytes):
                    restored_text = restored_text.decode("utf-8")
                if restored_text != SNAPSHOT_CHECK_CONTENT:
                    raise RuntimeError(
                        "Snapshot resume verification failed: expected "
                        f"{SNAPSHOT_CHECK_CONTENT!r}, got {restored_text!r}"
                    )
            finally:
                await resumed_sandbox.shutdown()
        finally:
            if cleanup_sandbox is not None:
                # Northflank shutdown does not tear down the deployment;
                # the only path that removes it is ``client.delete``.
                await client.delete(cleanup_sandbox)

    mode = workspace_persistence or "ephemeral"
    print(f"snapshot round-trip ok (northflank, {mode})")


async def main(
    *,
    model: str,
    question: str,
    project_id: str,
    team_id: str | None,
    image_path: str,
    docker_command: str,
    stream: bool,
    workspace_persistence: Literal["volume", "tar"] | None = None,
    volume_storage_size_mb: int | None = None,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("NF_API_TOKEN")

    await _verify_stop_resume(
        project_id=project_id,
        team_id=team_id,
        image_path=image_path,
        docker_command=docker_command,
        workspace_persistence=workspace_persistence,
        volume_storage_size_mb=volume_storage_size_mb,
    )

    manifest = _build_manifest()
    agent = SandboxAgent(
        name="Northflank Sandbox Assistant",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the files before answering "
            "and keep the response concise. "
            "Do not invent files or statuses that are not present in the workspace. Cite the "
            "file names you inspected."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    nf = AsyncApiClient()
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=NorthflankSandboxClient(client=nf),
            options=_build_options(
                project_id=project_id,
                team_id=team_id,
                image_path=image_path,
                docker_command=docker_command,
                workspace_persistence=workspace_persistence,
                volume_storage_size_mb=volume_storage_size_mb,
            ),
        ),
        workflow_name="Northflank sandbox example",
    )

    if not stream:
        result = await Runner.run(agent, question, run_config=run_config)
        print(result.final_output)
        return

    stream_result = Runner.run_streamed(agent, question, run_config=run_config)
    saw_text_delta = False
    async for event in stream_result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            if not saw_text_delta:
                print("assistant> ", end="", flush=True)
                saw_text_delta = True
            print(event.data.delta, end="", flush=True)

    if saw_text_delta:
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--project-id",
        default=os.environ.get("NF_PROJECT_ID"),
        help="Northflank project id (default: $NF_PROJECT_ID).",
    )
    parser.add_argument(
        "--team-id",
        default=os.environ.get("NF_TEAM_ID"),
        help="Optional Northflank team id (default: $NF_TEAM_ID).",
    )
    parser.add_argument(
        "--image-path",
        default="ubuntu:24.04",
        help="Base image for the ephemeral deployment.",
    )
    parser.add_argument(
        "--docker-command",
        default="sleep infinity",
        help="CMD override so the container stays alive long enough for exec.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    parser.add_argument(
        "--workspace-persistence",
        choices=["volume", "tar"],
        default=None,
        help=(
            "Workspace persistence strategy. 'volume' provisions a Northflank volume "
            "mounted at the workspace root (survives stop/resume; auto-deleted on "
            "client.delete). 'tar' captures the workspace tar into session state on "
            "stop and restores it on resume."
        ),
    )
    parser.add_argument(
        "--volume-storage-size-mb",
        type=int,
        default=None,
        help=(
            "Override the volume storageSize in MiB when "
            "--workspace-persistence=volume. Defaults to the provider's "
            "default volume spec (5120 MiB on nf-multi-rw — the minimum "
            "Northflank accepts for that class)."
        ),
    )
    args = parser.parse_args()

    if not args.project_id:
        raise SystemExit(
            "Set NF_PROJECT_ID or pass --project-id; this example needs a Northflank project."
        )

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            project_id=args.project_id,
            team_id=args.team_id,
            image_path=args.image_path,
            docker_command=args.docker_command,
            stream=args.stream,
            workspace_persistence=args.workspace_persistence,
            volume_storage_size_mb=args.volume_storage_size_mb,
        )
    )
