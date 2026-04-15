"""
Minimal Daytona-backed sandbox example for manual validation.

This mirrors the E2B and Modal extension examples: it creates a tiny workspace,
asks a sandboxed agent to inspect it through one shell tool, and prints a short
answer.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.entries import S3Mount

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import (
        DEFAULT_DAYTONA_WORKSPACE_ROOT,
        DaytonaCloudBucketMountStrategy,
        DaytonaSandboxClient,
        DaytonaSandboxClientOptions,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Daytona sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra daytona"
    ) from exc


DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."


def _build_manifest(
    *,
    cloud_bucket_name: str | None = None,
    cloud_bucket_mount_path: str | None = None,
    cloud_bucket_endpoint_url: str | None = None,
    cloud_bucket_key_prefix: str | None = None,
) -> Manifest:
    """Build a small demo manifest, optionally including a cloud bucket mount."""
    manifest = text_manifest(
        {
            "README.md": (
                "# Daytona Demo Workspace\n\n"
                "This workspace exists to validate the Daytona sandbox backend manually.\n"
            ),
            "launch.md": (
                "# Launch\n\n"
                "- Customer: Contoso Logistics.\n"
                "- Goal: validate the remote sandbox agent path.\n"
                "- Current status: Daytona backend smoke and app-server connectivity are passing.\n"
            ),
            "tasks.md": (
                "# Tasks\n\n"
                "1. Inspect the workspace files.\n"
                "2. Summarize the setup and any notable status in two sentences.\n"
            ),
        }
    )
    if cloud_bucket_name is None:
        return Manifest(root=DEFAULT_DAYTONA_WORKSPACE_ROOT, entries=manifest.entries)

    manifest.entries["cloud-bucket"] = S3Mount(
        bucket=cloud_bucket_name,
        access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        session_token=os.environ.get("AWS_SESSION_TOKEN"),
        endpoint_url=cloud_bucket_endpoint_url,
        prefix=cloud_bucket_key_prefix,
        mount_path=Path(cloud_bucket_mount_path) if cloud_bucket_mount_path is not None else None,
        read_only=False,
        mount_strategy=DaytonaCloudBucketMountStrategy(),
    )
    return Manifest(root=DEFAULT_DAYTONA_WORKSPACE_ROOT, entries=manifest.entries)


def _require_env(name: str) -> None:
    if os.environ.get(name):
        return
    raise SystemExit(f"{name} must be set before running this example.")


async def main(
    *,
    model: str,
    question: str,
    pause_on_exit: bool,
    stream: bool,
    cloud_bucket_name: str | None = None,
    cloud_bucket_mount_path: str | None = None,
    cloud_bucket_endpoint_url: str | None = None,
    cloud_bucket_key_prefix: str | None = None,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("DAYTONA_API_KEY")

    manifest = _build_manifest(
        cloud_bucket_name=cloud_bucket_name,
        cloud_bucket_mount_path=cloud_bucket_mount_path,
        cloud_bucket_endpoint_url=cloud_bucket_endpoint_url,
        cloud_bucket_key_prefix=cloud_bucket_key_prefix,
    )
    agent = SandboxAgent(
        name="Daytona Sandbox Assistant",
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

    client = DaytonaSandboxClient()
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=client,
            options=DaytonaSandboxClientOptions(pause_on_exit=pause_on_exit),
        ),
        workflow_name="Daytona sandbox example",
    )

    try:
        if not stream:
            result = await Runner.run(agent, question, run_config=run_config)
            print(result.final_output)
            return

        stream_result = Runner.run_streamed(agent, question, run_config=run_config)
        saw_text_delta = False
        async for event in stream_result.stream_events():
            if event.type == "raw_response_event" and isinstance(
                event.data, ResponseTextDeltaEvent
            ):
                if not saw_text_delta:
                    print("assistant> ", end="", flush=True)
                    saw_text_delta = True
                print(event.data.delta, end="", flush=True)

        if saw_text_delta:
            print()
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--pause-on-exit",
        action="store_true",
        default=False,
        help="Pause the Daytona sandbox on shutdown instead of deleting it.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    parser.add_argument(
        "--cloud-bucket-name",
        default=None,
        help="S3 bucket name to mount into the sandbox.",
    )
    parser.add_argument(
        "--cloud-bucket-mount-path",
        default=None,
        help=(
            "Mount path for --cloud-bucket-name. Relative paths are resolved under the "
            "workspace root. Defaults to the mount class default."
        ),
    )
    parser.add_argument(
        "--cloud-bucket-endpoint-url",
        default=None,
        help="Optional endpoint URL for --cloud-bucket-name (S3 only, e.g. MinIO).",
    )
    parser.add_argument(
        "--cloud-bucket-key-prefix",
        default=None,
        help="Optional key prefix for --cloud-bucket-name.",
    )
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            pause_on_exit=args.pause_on_exit,
            stream=args.stream,
            cloud_bucket_name=args.cloud_bucket_name,
            cloud_bucket_mount_path=args.cloud_bucket_mount_path,
            cloud_bucket_endpoint_url=args.cloud_bucket_endpoint_url,
            cloud_bucket_key_prefix=args.cloud_bucket_key_prefix,
        )
    )
