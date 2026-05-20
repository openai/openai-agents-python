"""
Minimal Superserve-backed sandbox example for manual validation.

This example mirrors the other cloud extension runners: it creates a tiny workspace, asks a
sandboxed agent to inspect it through one shell tool, prints a short answer, and verifies that
pause/resume preserves workspace state.
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

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.session import BaseSandboxSession

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import (
        DEFAULT_SUPERSERVE_WORKSPACE_ROOT,
        SuperserveSandboxClient,
        SuperserveSandboxClientOptions,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Superserve sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra superserve"
    ) from exc


DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."
DEFAULT_TEMPLATE = "superserve/base"
SNAPSHOT_CHECK_PATH = Path("snapshot-check.txt")
SNAPSHOT_CHECK_CONTENT = "superserve snapshot round-trip ok\n"


def _build_manifest() -> Manifest:
    manifest = text_manifest(
        {
            "README.md": (
                "# Superserve Demo Workspace\n\n"
                "This workspace exists to validate the Superserve sandbox backend manually.\n"
            ),
            "renewal.md": (
                "# Renewal Notes\n\n"
                "- Customer: Northwind Health.\n"
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
    return Manifest(root=DEFAULT_SUPERSERVE_WORKSPACE_ROOT, entries=manifest.entries)


def _require_env(name: str) -> None:
    if os.environ.get(name):
        return
    raise SystemExit(f"{name} must be set before running this example.")


async def _read_text(session: BaseSandboxSession, path: Path) -> str:
    data = await session.read(path)
    text = cast(str | bytes, data.read())
    if isinstance(text, bytes):
        return text.decode("utf-8")
    return text


async def _verify_stop_resume(
    *,
    template: str,
    pause_on_exit: bool,
    timeout_seconds: int | None,
) -> None:
    client = SuperserveSandboxClient()
    manifest = _build_manifest()
    with tempfile.TemporaryDirectory(prefix="superserve-snapshot-example-") as snapshot_dir:
        sandbox = await client.create(
            manifest=manifest,
            snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
            options=SuperserveSandboxClientOptions(
                template=template,
                pause_on_exit=pause_on_exit,
                timeout_seconds=timeout_seconds,
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

        resumed = await client.resume(sandbox.state)
        try:
            await resumed.start()
            restored = await _read_text(resumed, SNAPSHOT_CHECK_PATH)
            if restored != SNAPSHOT_CHECK_CONTENT:
                raise RuntimeError(
                    "Snapshot resume verification failed: "
                    f"expected {SNAPSHOT_CHECK_CONTENT!r}, got {restored!r}"
                )
        finally:
            await resumed.aclose()

    print("snapshot round-trip ok")


async def main(
    *,
    model: str,
    question: str,
    template: str,
    pause_on_exit: bool,
    timeout_seconds: int | None,
    stream: bool,
    skip_snapshot_check: bool,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("SUPERSERVE_API_KEY")

    if not skip_snapshot_check:
        await _verify_stop_resume(
            template=template,
            pause_on_exit=pause_on_exit,
            timeout_seconds=timeout_seconds,
        )

    manifest = _build_manifest()
    agent = SandboxAgent(
        name="Superserve Sandbox Assistant",
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

    client = SuperserveSandboxClient()
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=client,
            options=SuperserveSandboxClientOptions(
                template=template,
                pause_on_exit=pause_on_exit,
                timeout_seconds=timeout_seconds,
            ),
        ),
        workflow_name="Superserve sandbox example",
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
    parser.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE,
        help=(
            "Superserve template name or UUID. Defaults to `superserve/base`. "
            "Other curated templates: superserve/python-3.11, superserve/node-22, "
            "superserve/code-interpreter, superserve/python-ml, superserve/claude-code."
        ),
    )
    parser.add_argument(
        "--pause-on-exit",
        action="store_true",
        default=False,
        help="Pause the Superserve sandbox on shutdown instead of killing it.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
        help=(
            "Optional inactivity timeout in seconds. Superserve sandboxes do not die on their own "
            "by default; set this to opt into automatic shutdown."
        ),
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    parser.add_argument(
        "--skip-snapshot-check",
        action="store_true",
        default=False,
        help="Skip the pause/resume snapshot round-trip verification.",
    )
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            template=args.template,
            pause_on_exit=args.pause_on_exit,
            timeout_seconds=args.timeout_seconds,
            stream=args.stream,
            skip_snapshot_check=args.skip_snapshot_check,
        )
    )
