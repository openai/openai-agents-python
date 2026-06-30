"""
Minimal Islo-backed sandbox example for manual validation.

This example creates a tiny workspace, verifies stop/resume persistence, and lets
the agent inspect the workspace through one shell tool.
"""

import argparse
import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path
from typing import Literal

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent, SandboxRunConfig

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import (
        IsloSandboxClient,
        IsloSandboxClientOptions,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Islo sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra islo"
    ) from exc


DEFAULT_QUESTION = "Summarize this Islo sandbox workspace in 2 sentences."
SNAPSHOT_CHECK_PATH = Path("snapshot-check.txt")
SNAPSHOT_CHECK_CONTENT = "islo snapshot round-trip ok\n"


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


def _options(
    *,
    base_url: str | None,
    compute_url: str | None,
    image: str | None,
    vcpus: int | None,
    memory_mb: int | None,
    disk_gb: int | None,
    snapshot_name: str | None,
    pause_on_exit: bool,
    workspace_persistence: Literal["tar", "snapshot"],
) -> IsloSandboxClientOptions:
    return IsloSandboxClientOptions(
        base_url=base_url,
        compute_url=compute_url,
        image=image,
        vcpus=vcpus,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        snapshot_name=snapshot_name,
        pause_on_exit=pause_on_exit,
        workspace_persistence=workspace_persistence,
    )


async def _verify_stop_resume(
    *,
    base_url: str | None,
    compute_url: str | None,
    image: str | None,
    vcpus: int | None,
    memory_mb: int | None,
    disk_gb: int | None,
    snapshot_name: str | None,
    pause_on_exit: bool,
    workspace_persistence: Literal["tar", "snapshot"],
) -> None:
    client = IsloSandboxClient(base_url=base_url, compute_url=compute_url)
    with tempfile.TemporaryDirectory(prefix="islo-snapshot-example-") as snapshot_dir:
        sandbox = await client.create(
            manifest=_build_manifest(),
            snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
            options=_options(
                base_url=base_url,
                compute_url=compute_url,
                image=image,
                vcpus=vcpus,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                snapshot_name=snapshot_name,
                pause_on_exit=pause_on_exit,
                workspace_persistence=workspace_persistence,
            ),
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
            restored = await resumed_sandbox.read(SNAPSHOT_CHECK_PATH)
            restored_text = restored.read()
            if isinstance(restored_text, bytes):
                restored_text = restored_text.decode("utf-8")
            if restored_text != SNAPSHOT_CHECK_CONTENT:
                raise RuntimeError(
                    "Snapshot resume verification failed: "
                    f"expected {SNAPSHOT_CHECK_CONTENT!r}, got {restored_text!r}"
                )
        finally:
            await resumed_sandbox.shutdown()

    print(f"snapshot round-trip ok (islo, {workspace_persistence})")


async def main(
    *,
    model: str,
    question: str,
    base_url: str | None,
    compute_url: str | None,
    image: str | None,
    vcpus: int | None,
    memory_mb: int | None,
    disk_gb: int | None,
    snapshot_name: str | None,
    pause_on_exit: bool,
    workspace_persistence: Literal["tar", "snapshot"],
    stream: bool,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("ISLO_API_KEY")

    await _verify_stop_resume(
        base_url=base_url,
        compute_url=compute_url,
        image=image,
        vcpus=vcpus,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        snapshot_name=snapshot_name,
        pause_on_exit=pause_on_exit,
        workspace_persistence=workspace_persistence,
    )

    agent = SandboxAgent(
        name="Islo Sandbox Assistant",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the files before answering "
            "and keep the response concise. Do not invent files or statuses that are not present "
            "in the workspace. Cite the file names you inspected."
        ),
        default_manifest=_build_manifest(),
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=IsloSandboxClient(base_url=base_url, compute_url=compute_url),
            options=_options(
                base_url=base_url,
                compute_url=compute_url,
                image=image,
                vcpus=vcpus,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                snapshot_name=snapshot_name,
                pause_on_exit=pause_on_exit,
                workspace_persistence=workspace_persistence,
            ),
        ),
        workflow_name="Islo sandbox example",
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
    parser.add_argument("--model", default="gpt-5.5", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument("--base-url", default=None, help="Optional Islo API base URL.")
    parser.add_argument("--compute-url", default=None, help="Optional Islo compute API base URL.")
    parser.add_argument("--image", default=None, help="Optional Islo sandbox image.")
    parser.add_argument("--vcpus", type=int, default=None, help="Optional Islo vCPU count.")
    parser.add_argument("--memory-mb", type=int, default=None, help="Optional Islo memory in MB.")
    parser.add_argument("--disk-gb", type=int, default=None, help="Optional Islo disk in GB.")
    parser.add_argument(
        "--snapshot-name",
        default=None,
        help="Optional Islo snapshot name to use as the sandbox base filesystem.",
    )
    parser.add_argument(
        "--pause-on-exit",
        action="store_true",
        default=False,
        help="Pause the sandbox on shutdown instead of deleting it.",
    )
    parser.add_argument(
        "--workspace-persistence",
        default="tar",
        choices=["tar", "snapshot"],
        help="Workspace persistence mode for the Islo sandbox.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            base_url=args.base_url,
            compute_url=args.compute_url,
            image=args.image,
            vcpus=args.vcpus,
            memory_mb=args.memory_mb,
            disk_gb=args.disk_gb,
            snapshot_name=args.snapshot_name,
            pause_on_exit=args.pause_on_exit,
            workspace_persistence=args.workspace_persistence,
            stream=args.stream,
        )
    )
