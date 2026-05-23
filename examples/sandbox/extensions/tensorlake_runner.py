"""
Minimal Tensorlake-backed sandbox example for manual validation.

This mirrors the other cloud extension examples: it creates a tiny workspace,
verifies stop/resume persistence, then asks a sandboxed agent to inspect the
workspace through one shell tool.
"""

from __future__ import annotations

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
from agents.sandbox.entries import File
from agents.sandbox.session import BaseSandboxSession

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import (
        DEFAULT_TENSORLAKE_WORKSPACE_ROOT,
        TensorlakeSandboxClient,
        TensorlakeSandboxClientOptions,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Tensorlake sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra tensorlake"
    ) from exc


DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."
SNAPSHOT_CHECK_PATH = Path("snapshot-check.txt")
SNAPSHOT_CHECK_CONTENT = "tensorlake snapshot round-trip ok\n"
LIVE_RESUME_CHECK_PATH = Path("live-resume-check.txt")
LIVE_RESUME_CHECK_CONTENT = "tensorlake live resume ok\n"


def _build_manifest() -> Manifest:
    files = {
        "README.md": (
            "# Tensorlake Demo Workspace\n\n"
            "This workspace exists to validate the Tensorlake sandbox backend manually.\n"
        ),
        "handoff.md": (
            "# Handoff\n\n"
            "- Customer: Northwind Traders.\n"
            "- Goal: validate Tensorlake sandbox exec and persistence flows.\n"
            "- Current status: non-PTY backend slice is wired and under test.\n"
        ),
        "todo.md": (
            "# Todo\n\n"
            "1. Inspect the workspace files.\n"
            "2. Summarize the current status in two sentences.\n"
        ),
    }
    return Manifest(
        root=DEFAULT_TENSORLAKE_WORKSPACE_ROOT,
        entries={path: File(content=contents.encode("utf-8")) for path, contents in files.items()},
    )


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


def _parse_env_pair(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--env value must be KEY=VAL (got {raw!r}).")
    key, value = raw.split("=", 1)
    if not key:
        raise argparse.ArgumentTypeError(f"--env key must be non-empty (got {raw!r}).")
    return key, value


async def _verify_stop_resume(
    *,
    manifest: Manifest,
    options: TensorlakeSandboxClientOptions,
) -> None:
    # Verification sandboxes should always terminate on shutdown so the example does not
    # leak suspended sandboxes; pause-on-exit is exercised by the main agent run instead.
    options = options.model_copy(update={"pause_on_exit": False})
    client = TensorlakeSandboxClient()
    with tempfile.TemporaryDirectory(prefix="tensorlake-snapshot-example-") as snapshot_dir:
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
                    f"Snapshot resume verification failed for {options.workspace_persistence!r}: "
                    f"expected {SNAPSHOT_CHECK_CONTENT!r}, got {restored_text!r}"
                )
        finally:
            await resumed_sandbox.aclose()

    print(f"snapshot round-trip ok ({options.workspace_persistence})")


async def _verify_resume_running_sandbox(
    *,
    manifest: Manifest,
    options: TensorlakeSandboxClientOptions,
) -> None:
    # Force terminate-on-shutdown for verification so we don't leave suspended sandboxes behind.
    options = options.model_copy(update={"pause_on_exit": False})
    client = TensorlakeSandboxClient()
    sandbox = await client.create(manifest=manifest, options=options)

    try:
        await sandbox.start()
        await sandbox.write(
            LIVE_RESUME_CHECK_PATH,
            io.BytesIO(LIVE_RESUME_CHECK_CONTENT.encode("utf-8")),
        )
        serialized = client.serialize_session_state(sandbox.state)
        resumed_sandbox = await client.resume(client.deserialize_session_state(serialized))
        try:
            restored_text = await _read_text(resumed_sandbox, LIVE_RESUME_CHECK_PATH)
            if restored_text != LIVE_RESUME_CHECK_CONTENT:
                raise RuntimeError(
                    "Running sandbox resume verification failed: "
                    f"expected {LIVE_RESUME_CHECK_CONTENT!r}, got {restored_text!r}"
                )
        finally:
            await resumed_sandbox.aclose()
    finally:
        await sandbox.shutdown()

    print(f"running sandbox resume ok ({options.workspace_persistence})")


async def main(
    *,
    model: str,
    question: str,
    options: TensorlakeSandboxClientOptions,
    stream: bool,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("TENSORLAKE_API_KEY")

    manifest = _build_manifest()

    await _verify_stop_resume(manifest=manifest, options=options)
    await _verify_resume_running_sandbox(manifest=manifest, options=options)

    agent = SandboxAgent(
        name="Tensorlake Sandbox Assistant",
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
            client=TensorlakeSandboxClient(),
            options=options,
        ),
        workflow_name="Tensorlake sandbox example",
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
        "--image",
        default=None,
        help="Optional Tensorlake registered image name. Falls back to the SDK default.",
    )
    parser.add_argument(
        "--timeout-secs",
        type=int,
        default=1800,
        help=(
            "Optional Tensorlake sandbox lifetime in seconds. Must be strictly greater "
            "than `checkpoint_timeout_s` (default 300) when "
            "--workspace-persistence=snapshot."
        ),
    )
    parser.add_argument(
        "--workspace-persistence",
        choices=("tar", "snapshot"),
        default="tar",
        help="Workspace persistence mode to verify before the agent run.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=None,
        type=_parse_env_pair,
        metavar="KEY=VAL",
        help="Environment variable to inject into the sandbox. Repeatable.",
    )
    parser.add_argument(
        "--secret",
        action="append",
        default=None,
        metavar="NAME",
        help="Tensorlake-managed secret name to inject into the sandbox. Repeatable.",
    )
    parser.add_argument(
        "--pause-on-exit",
        action="store_true",
        default=False,
        help="Pause the sandbox on shutdown instead of terminating it.",
    )
    parser.add_argument(
        "--cpus",
        type=float,
        default=None,
        help="Optional CPU allocation for the sandbox.",
    )
    parser.add_argument(
        "--memory-mb",
        type=int,
        default=None,
        help="Optional memory allocation for the sandbox, in megabytes.",
    )
    parser.add_argument(
        "--disk-mb",
        type=int,
        default=None,
        help="Optional disk allocation for the sandbox, in megabytes.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    options = TensorlakeSandboxClientOptions(
        image=args.image,
        timeout_secs=args.timeout_secs,
        workspace_persistence=cast(Literal["tar", "snapshot"], args.workspace_persistence),
        envs=dict(args.env) if args.env else None,
        secret_names=tuple(args.secret or ()),
        pause_on_exit=args.pause_on_exit,
        cpus=args.cpus,
        memory_mb=args.memory_mb,
        disk_mb=args.disk_mb,
    )

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            options=options,
            stream=args.stream,
        )
    )
