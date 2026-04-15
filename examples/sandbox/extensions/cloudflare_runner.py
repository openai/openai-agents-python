"""
Cloudflare-backed sandbox example for manual validation.

This example mirrors the Modal and E2B extension runners. It supports:
- Standard agent run (non-streaming and streaming).
- Snapshot stop/resume round-trip verification.
- PTY interactive session demo.
- Cloud bucket mount demo (R2/S3/GCS via CloudflareBucketMountStrategy).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path
from typing import cast

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner, set_tracing_disabled
from agents.run import RunConfig
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File, R2Mount, S3Mount
from agents.sandbox.session import BaseSandboxSession

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest, tool_call_name

try:
    from agents.extensions.sandbox import (
        CloudflareBucketMountStrategy,
        CloudflareSandboxClient,
        CloudflareSandboxClientOptions,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Cloudflare sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra cloudflare"
    ) from exc


DEFAULT_MODEL = "gpt-5.4"
DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."
DEFAULT_PTY_QUESTION = (
    "Start an interactive Python session with `tty=true`. In that same session, compute "
    "`5 + 5`, then add 5 more to the previous result. Briefly report the outputs and "
    "confirm that you stayed in one Python process."
)
SNAPSHOT_CHECK_PATH = Path("snapshot-check.txt")
SNAPSHOT_CHECK_CONTENT = "cloudflare snapshot round-trip ok\n"


def _build_manifest(
    *,
    native_cloud_bucket_name: str | None = None,
    native_cloud_bucket_mount_path: str | None = None,
    native_cloud_bucket_endpoint_url: str | None = None,
) -> Manifest:
    """Build a small demo manifest, optionally including a cloud bucket mount."""
    manifest = text_manifest(
        {
            "README.md": (
                "# Cloudflare Demo Workspace\n\n"
                "This workspace exists to validate the Cloudflare sandbox backend manually.\n"
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

    # Determine whether this looks like an R2 bucket (has account ID) or S3.
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if account_id:
        manifest.entries["cloud-bucket"] = R2Mount(
            bucket=native_cloud_bucket_name,
            account_id=account_id,
            access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
            secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
            mount_path=Path(native_cloud_bucket_mount_path)
            if native_cloud_bucket_mount_path is not None
            else None,
            read_only=False,
            mount_strategy=CloudflareBucketMountStrategy(),
        )
    else:
        manifest.entries["cloud-bucket"] = S3Mount(
            bucket=native_cloud_bucket_name,
            access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            endpoint_url=native_cloud_bucket_endpoint_url,
            mount_path=Path(native_cloud_bucket_mount_path)
            if native_cloud_bucket_mount_path is not None
            else None,
            read_only=False,
            mount_strategy=CloudflareBucketMountStrategy(),
        )
    return manifest


def _build_pty_manifest() -> Manifest:
    """Build a tiny manifest for the PTY demo."""
    return Manifest(
        entries={
            "README.md": File(
                content=(
                    b"# Cloudflare PTY Agent Example\n\n"
                    b"This workspace is used by the Cloudflare PTY demo.\n"
                )
            ),
        }
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise SystemExit(f"{name} must be set before running this example.")


async def _read_text(session: BaseSandboxSession, path: Path) -> str:
    data = await session.read(path)
    text = cast(str | bytes, data.read())
    if isinstance(text, bytes):
        return text.decode("utf-8")
    return text


# ---------------------------------------------------------------------------
# Stop/resume snapshot round-trip
# ---------------------------------------------------------------------------


async def _verify_stop_resume(*, worker_url: str, api_key: str | None) -> None:
    """Create a sandbox, write a file, stop, resume, and verify the file persisted."""
    client = CloudflareSandboxClient()
    manifest = text_manifest(
        {
            "README.md": "# Snapshot test\n",
        }
    )
    options = CloudflareSandboxClientOptions(worker_url=worker_url, api_key=api_key)

    with tempfile.TemporaryDirectory(prefix="cf-snapshot-example-") as snapshot_dir:
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
                    f"Snapshot resume verification failed: "
                    f"expected {SNAPSHOT_CHECK_CONTENT!r}, got {restored_text!r}"
                )
        finally:
            await resumed_sandbox.aclose()

    print("snapshot round-trip ok")


# ---------------------------------------------------------------------------
# PTY demo
# ---------------------------------------------------------------------------


def _stream_event_banner(event_name: str, raw_item: object) -> str | None:
    _ = raw_item
    if event_name == "tool_called":
        return "[tool call]"
    if event_name == "tool_output":
        return "[tool output]"
    return None


def _raw_item_call_id(raw_item: object) -> str | None:
    if isinstance(raw_item, dict):
        call_id = raw_item.get("call_id") or raw_item.get("id")
    else:
        call_id = getattr(raw_item, "call_id", None) or getattr(raw_item, "id", None)
    return call_id if isinstance(call_id, str) and call_id else None


async def _run_pty_demo(*, model: str, worker_url: str, api_key: str | None) -> None:
    """Demonstrate PTY interaction: start an interactive Python process and continue it."""
    agent = SandboxAgent(
        name="Cloudflare PTY Demo",
        model=model,
        instructions=(
            "Complete the task by interacting with the sandbox through the shell capability. "
            "Keep the final answer concise. "
            "Preserve process state when the task depends on it. If you start an interactive "
            "program, continue using that same process instead of launching a second one."
        ),
        default_manifest=_build_pty_manifest(),
        capabilities=[Shell()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    client = CloudflareSandboxClient()
    sandbox = await client.create(
        manifest=agent.default_manifest,
        options=CloudflareSandboxClientOptions(worker_url=worker_url, api_key=api_key),
    )

    try:
        async with sandbox:
            result = Runner.run_streamed(
                agent,
                DEFAULT_PTY_QUESTION,
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(session=sandbox),
                    workflow_name="Cloudflare PTY sandbox example",
                ),
            )

            saw_text_delta = False
            saw_any_text = False
            tool_names_by_call_id: dict[str, str] = {}

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

                raw_item = event.item.raw_item
                banner = _stream_event_banner(event.name, raw_item)
                if banner is None:
                    continue

                if saw_text_delta:
                    print()
                    saw_text_delta = False

                if event.name == "tool_called":
                    t_name = tool_call_name(raw_item)
                    call_id = _raw_item_call_id(raw_item)
                    if call_id is not None and t_name:
                        tool_names_by_call_id[call_id] = t_name
                    if t_name:
                        banner = f"{banner} {t_name}"
                elif event.name == "tool_output":
                    call_id = _raw_item_call_id(raw_item)
                    output_tool_name = tool_names_by_call_id.get(call_id or "")
                    if output_tool_name:
                        banner = f"{banner} {output_tool_name}"

                print(banner)

            if saw_text_delta:
                print()
            if not saw_any_text:
                print(result.final_output)
    finally:
        await client.delete(sandbox)


# ---------------------------------------------------------------------------
# Standard agent run (streaming / non-streaming)
# ---------------------------------------------------------------------------


async def main(
    *,
    model: str,
    question: str,
    worker_url: str,
    api_key: str | None,
    stream: bool,
    demo: str | None,
    skip_snapshot_check: bool,
    native_cloud_bucket_name: str | None,
    native_cloud_bucket_mount_path: str,
    native_cloud_bucket_endpoint_url: str | None,
) -> None:
    _require_env("OPENAI_API_KEY")

    # Handle dedicated demos.
    if demo == "pty":
        await _run_pty_demo(model=model, worker_url=worker_url, api_key=api_key)
        return

    # Snapshot stop/resume round-trip.
    if not skip_snapshot_check:
        await _verify_stop_resume(worker_url=worker_url, api_key=api_key)

    manifest = _build_manifest(
        native_cloud_bucket_name=native_cloud_bucket_name,
        native_cloud_bucket_mount_path=native_cloud_bucket_mount_path,
        native_cloud_bucket_endpoint_url=native_cloud_bucket_endpoint_url,
    )
    agent = SandboxAgent(
        name="Cloudflare Sandbox Assistant",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the files before answering "
            "and keep the response concise. "
            "Do not invent files or statuses that are not present in the workspace. Cite the "
            "file names you inspected."
        ),
        default_manifest=manifest,
        capabilities=[Shell()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=CloudflareSandboxClient(),
            options=CloudflareSandboxClientOptions(worker_url=worker_url, api_key=api_key),
        ),
        workflow_name="Cloudflare sandbox example",
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
    set_tracing_disabled(True)

    parser = argparse.ArgumentParser(
        description="Run a Cloudflare sandbox agent with optional PTY, streaming, and snapshot demos."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    parser.add_argument(
        "--question",
        default=DEFAULT_QUESTION,
        help="Prompt to send to the agent.",
    )
    parser.add_argument(
        "--worker-url",
        default=os.environ.get("CLOUDFLARE_SANDBOX_WORKER_URL"),
        help="Cloudflare Worker base URL. Defaults to CLOUDFLARE_SANDBOX_WORKER_URL.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("CLOUDFLARE_SANDBOX_API_KEY"),
        help="Optional bearer token for the worker. Defaults to CLOUDFLARE_SANDBOX_API_KEY.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    parser.add_argument(
        "--demo",
        default=None,
        choices=["pty"],
        help="Run a standalone demo instead of the standard agent flow.",
    )
    parser.add_argument(
        "--skip-snapshot-check",
        action="store_true",
        default=False,
        help="Skip the snapshot stop/resume round-trip verification.",
    )
    parser.add_argument(
        "--native-cloud-bucket-name",
        default=None,
        help="Optional R2/S3 bucket name to mount with CloudflareBucketMountStrategy.",
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
        help="Optional endpoint URL for --native-cloud-bucket-name (S3 only).",
    )
    args = parser.parse_args()

    if not args.worker_url:
        raise SystemExit(
            "A Cloudflare Worker URL is required. Pass --worker-url or set CLOUDFLARE_SANDBOX_WORKER_URL."
        )

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            worker_url=args.worker_url,
            api_key=args.api_key,
            stream=args.stream,
            demo=args.demo,
            skip_snapshot_check=args.skip_snapshot_check,
            native_cloud_bucket_name=args.native_cloud_bucket_name,
            native_cloud_bucket_mount_path=args.native_cloud_bucket_mount_path,
            native_cloud_bucket_endpoint_url=args.native_cloud_bucket_endpoint_url,
        )
    )
