"""
Minimal Modal-backed sandbox example for manual validation.

This example mirrors the local and Docker sandbox demos, but it sends the
workspace to a Modal sandbox.
"""

import argparse
import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path
from typing import Literal, cast

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.entries import GCSMount, Mount, S3Mount
from agents.sandbox.session import BaseSandboxSession

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import (
        ModalCloudBucketMountStrategy,
        ModalSandboxClient,
        ModalSandboxClientOptions,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Modal sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra modal"
    ) from exc


DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."
SNAPSHOT_CHECK_PATH = Path("snapshot-check.txt")
SNAPSHOT_CHECK_CONTENT = "modal snapshot round-trip ok\n"
MOUNT_CHECK_FILENAME = "native-cloud-bucket-check.txt"
MOUNT_CHECK_CONTENT = "modal native cloud bucket read/write ok\n"
MOUNT_CHECK_UPDATED_CONTENT = "modal native cloud bucket read/write ok after resume\n"


def _build_manifest(
    *,
    native_cloud_bucket_name: str | None = None,
    native_cloud_bucket_provider: Literal["s3", "gcs-hmac"] = "s3",
    native_cloud_bucket_mount_path: str | None = None,
    native_cloud_bucket_endpoint_url: str | None = None,
    native_cloud_bucket_key_prefix: str | None = None,
    native_cloud_bucket_secret_name: str | None = None,
) -> Manifest:
    manifest = text_manifest(
        {
            "README.md": (
                "# Modal Demo Workspace\n\n"
                "This workspace exists to validate the Modal sandbox backend manually.\n"
            ),
            "incident.md": (
                "# Incident\n\n"
                "- Customer: Fabrikam Retail.\n"
                "- Issue: delayed reporting rollout.\n"
                "- Primary blocker: incomplete security questionnaire.\n"
            ),
            "plan.md": (
                "# Plan\n\n"
                "1. Close the questionnaire.\n"
                "2. Reconfirm the rollout date with the customer.\n"
            ),
        }
    )
    if native_cloud_bucket_name is None:
        return manifest

    mount_path = (
        Path(native_cloud_bucket_mount_path) if native_cloud_bucket_mount_path is not None else None
    )
    mount_strategy = ModalCloudBucketMountStrategy(
        secret_name=native_cloud_bucket_secret_name,
    )
    if native_cloud_bucket_provider == "gcs-hmac":
        manifest.entries["cloud-bucket"] = GCSMount(
            bucket=native_cloud_bucket_name,
            access_id=(
                None
                if native_cloud_bucket_secret_name is not None
                else (
                    os.environ.get("GCS_HMAC_ACCESS_KEY_ID")
                    or os.environ.get("GOOGLE_ACCESS_KEY_ID")
                )
            ),
            secret_access_key=(
                None
                if native_cloud_bucket_secret_name is not None
                else (
                    os.environ.get("GCS_HMAC_SECRET_ACCESS_KEY")
                    or os.environ.get("GOOGLE_ACCESS_KEY_SECRET")
                )
            ),
            endpoint_url=native_cloud_bucket_endpoint_url,
            prefix=native_cloud_bucket_key_prefix,
            mount_path=mount_path,
            read_only=False,
            mount_strategy=mount_strategy,
        )
    else:
        manifest.entries["cloud-bucket"] = S3Mount(
            bucket=native_cloud_bucket_name,
            access_key_id=(
                None
                if native_cloud_bucket_secret_name is not None
                else os.environ.get("AWS_ACCESS_KEY_ID")
            ),
            secret_access_key=(
                None
                if native_cloud_bucket_secret_name is not None
                else os.environ.get("AWS_SECRET_ACCESS_KEY")
            ),
            session_token=(
                None
                if native_cloud_bucket_secret_name is not None
                else os.environ.get("AWS_SESSION_TOKEN")
            ),
            endpoint_url=native_cloud_bucket_endpoint_url,
            prefix=native_cloud_bucket_key_prefix,
            mount_path=mount_path,
            read_only=False,
            mount_strategy=mount_strategy,
        )
    return manifest


def _native_cloud_bucket_mount_path(manifest: Manifest) -> Path | None:
    entry = manifest.entries.get("cloud-bucket")
    if not isinstance(entry, Mount):
        return None
    if entry.mount_path is None:
        return Path(manifest.root) / "cloud-bucket"
    if entry.mount_path.is_absolute():
        return entry.mount_path
    return Path(manifest.root) / entry.mount_path


async def _read_text(session: BaseSandboxSession, path: Path) -> str:
    data = await session.read(path)
    text = cast(str | bytes, data.read())
    if isinstance(text, bytes):
        return text.decode("utf-8")
    return text


def _require_env(name: str) -> None:
    if os.environ.get(name):
        return
    raise SystemExit(f"{name} must be set before running this example.")


async def _verify_stop_resume(
    *,
    manifest: Manifest,
    app_name: str,
    workspace_persistence: Literal["tar", "snapshot_filesystem", "snapshot_directory"],
    sandbox_create_timeout_s: float | None,
) -> None:
    client = ModalSandboxClient()
    mount_path = _native_cloud_bucket_mount_path(manifest)
    mount_check_path = mount_path / MOUNT_CHECK_FILENAME if mount_path is not None else None
    options = ModalSandboxClientOptions(
        app_name=app_name,
        workspace_persistence=workspace_persistence,
        sandbox_create_timeout_s=sandbox_create_timeout_s,
    )
    with tempfile.TemporaryDirectory(prefix="modal-snapshot-example-") as snapshot_dir:
        sandbox = await client.create(
            manifest=manifest,
            snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
            options=options,
        )

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
        try:
            await resumed_sandbox.start()
            restored_text = await _read_text(resumed_sandbox, SNAPSHOT_CHECK_PATH)
            if restored_text != SNAPSHOT_CHECK_CONTENT:
                raise RuntimeError(
                    f"Snapshot resume verification failed for {workspace_persistence!r}: "
                    f"expected {SNAPSHOT_CHECK_CONTENT!r}, got {restored_text!r}"
                )
        finally:
            await resumed_sandbox.aclose()

        print(f"native cloud bucket read/write ok ({mount_check_path})")
    print(f"snapshot round-trip ok ({workspace_persistence})")


async def main(
    *,
    model: str,
    question: str,
    app_name: str,
    workspace_persistence: Literal["tar", "snapshot_filesystem", "snapshot_directory"],
    sandbox_create_timeout_s: float | None,
    native_cloud_bucket_name: str | None,
    native_cloud_bucket_provider: Literal["s3", "gcs-hmac"],
    native_cloud_bucket_mount_path: str,
    native_cloud_bucket_endpoint_url: str | None,
    native_cloud_bucket_key_prefix: str | None,
    native_cloud_bucket_secret_name: str | None,
    stream: bool,
) -> None:
    _require_env("OPENAI_API_KEY")
    manifest = _build_manifest(
        native_cloud_bucket_name=native_cloud_bucket_name,
        native_cloud_bucket_provider=native_cloud_bucket_provider,
        native_cloud_bucket_mount_path=native_cloud_bucket_mount_path,
        native_cloud_bucket_endpoint_url=native_cloud_bucket_endpoint_url,
        native_cloud_bucket_key_prefix=native_cloud_bucket_key_prefix,
        native_cloud_bucket_secret_name=native_cloud_bucket_secret_name,
    )

    await _verify_stop_resume(
        manifest=manifest,
        app_name=app_name,
        workspace_persistence=workspace_persistence,
        sandbox_create_timeout_s=sandbox_create_timeout_s,
    )

    agent = SandboxAgent(
        name="Modal Sandbox Assistant",
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

    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=ModalSandboxClient(),
            options=ModalSandboxClientOptions(
                app_name=app_name,
                workspace_persistence=workspace_persistence,
                sandbox_create_timeout_s=sandbox_create_timeout_s,
            ),
        ),
        workflow_name="Modal sandbox example",
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
    parser.add_argument("--model", default="gpt-5.4", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--app-name",
        default="openai-agents-python-sandbox-example",
        help="Modal app name to create or reuse for the sandbox.",
    )
    parser.add_argument(
        "--workspace-persistence",
        default="tar",
        choices=["tar", "snapshot_filesystem", "snapshot_directory"],
        help="Workspace persistence mode for the Modal sandbox.",
    )
    parser.add_argument(
        "--sandbox-create-timeout-s",
        type=float,
        default=None,
        help="Optional timeout for creating the Modal sandbox.",
    )
    parser.add_argument(
        "--native-cloud-bucket-name",
        default=None,
        help="Optional cloud bucket name to mount with ModalCloudBucketMountStrategy.",
    )
    parser.add_argument(
        "--native-cloud-bucket-provider",
        default="s3",
        choices=["s3", "gcs-hmac"],
        help="Provider type for --native-cloud-bucket-name.",
    )
    parser.add_argument(
        "--native-cloud-bucket-mount-path",
        default="cloud-bucket",
        help=(
            "Mount path for --native-cloud-bucket-name. Relative paths are resolved under the "
            "workspace root."
        ),
    )
    parser.add_argument(
        "--native-cloud-bucket-endpoint-url",
        default=None,
        help="Optional endpoint URL for --native-cloud-bucket-name.",
    )
    parser.add_argument(
        "--native-cloud-bucket-key-prefix",
        default=None,
        help="Optional key prefix for --native-cloud-bucket-name.",
    )
    parser.add_argument(
        "--native-cloud-bucket-secret-name",
        default=None,
        help=(
            "Optional named Modal Secret to use for --native-cloud-bucket-name instead of "
            "reading raw credentials from environment variables."
        ),
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            app_name=args.app_name,
            workspace_persistence=args.workspace_persistence,
            sandbox_create_timeout_s=args.sandbox_create_timeout_s,
            native_cloud_bucket_name=args.native_cloud_bucket_name,
            native_cloud_bucket_provider=args.native_cloud_bucket_provider,
            native_cloud_bucket_mount_path=args.native_cloud_bucket_mount_path,
            native_cloud_bucket_endpoint_url=args.native_cloud_bucket_endpoint_url,
            native_cloud_bucket_key_prefix=args.native_cloud_bucket_key_prefix,
            native_cloud_bucket_secret_name=args.native_cloud_bucket_secret_name,
            stream=args.stream,
        )
    )
