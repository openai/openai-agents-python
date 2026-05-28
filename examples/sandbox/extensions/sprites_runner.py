"""
Minimal Sprites-backed sandbox example for manual validation.

This example creates a small in-memory workspace, lets the agent inspect it
through one shell tool, and prints a short answer. By default an ephemeral
sprite is created and deleted at the end; pass ``--sprite-name <name>`` to
attach to an existing sprite instead.
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

from examples.sandbox.misc.example_support import text_manifest  # noqa: E402
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability  # noqa: E402

try:
    from agents.extensions.sandbox import (
        SpritesSandboxClient,
        SpritesSandboxClientOptions,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Sprites sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra sprites"
    ) from exc


DEFAULT_QUESTION = "Summarize this sandbox workspace in 2 sentences."
SNAPSHOT_CHECK_PATH = Path("snapshot-check.txt")
SNAPSHOT_CHECK_CONTENT = "sprites snapshot round-trip ok\n"


def _build_manifest() -> Manifest:
    return text_manifest(
        {
            "README.md": (
                "# Sprites Demo Workspace\n\n"
                "This workspace exists to validate the Sprites sandbox backend manually.\n"
            ),
            "handoff.md": (
                "# Handoff\n\n"
                "- Customer: Northwind Traders.\n"
                "- Goal: validate Sprites sandbox exec and persistence flows.\n"
                "- Current status: v1 backend slice (exec + fs + PTY) is wired and under test.\n"
            ),
            "todo.md": (
                "# Todo\n\n"
                "1. Inspect the workspace files.\n"
                "2. Summarize the current status in two sentences.\n"
            ),
        }
    )


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


async def _verify_stop_resume(*, sprite_name: str | None) -> None:
    """Round-trip a workspace through tar persistence and reattach.

    With ``sprite_name=None`` an ephemeral sprite is created, persisted, and
    then resumed against itself. With a named sprite the same flow runs
    against the existing sprite (no create/delete on the API).
    """

    client = SpritesSandboxClient()
    options = SpritesSandboxClientOptions(sprite_name=sprite_name)

    with tempfile.TemporaryDirectory(prefix="sprites-snapshot-example-") as snapshot_dir:
        sandbox = await client.create(
            manifest=_build_manifest(),
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

        resumed = await client.resume(sandbox.state)
        try:
            await resumed.start()
            restored = await _read_text(resumed, SNAPSHOT_CHECK_PATH)
            if restored != SNAPSHOT_CHECK_CONTENT:
                raise RuntimeError(
                    f"Snapshot resume verification failed: expected "
                    f"{SNAPSHOT_CHECK_CONTENT!r}, got {restored!r}"
                )
        finally:
            await resumed.aclose()
            if sprite_name is None:
                # Ephemeral sandbox should clean up the sprite created by ``resume``.
                await client.delete(resumed)

    print("snapshot round-trip ok")


async def main(
    *,
    model: str,
    question: str,
    sprite_name: str | None,
    skip_snapshot_check: bool,
    stream: bool,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("SPRITES_API_TOKEN")

    if not skip_snapshot_check:
        await _verify_stop_resume(sprite_name=sprite_name)

    manifest = _build_manifest()
    agent = SandboxAgent(
        name="Sprites Sandbox Assistant",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the files before answering "
            "and keep the response concise. Cite the file names you inspected."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    client = SpritesSandboxClient()
    sandbox = await client.create(
        manifest=manifest,
        options=SpritesSandboxClientOptions(sprite_name=sprite_name),
    )

    run_config = RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        tracing_disabled=True,
        workflow_name="Sprites sandbox example",
    )

    try:
        async with sandbox:
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
        await client.delete(sandbox)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.5", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--sprite-name",
        default=None,
        help=(
            "Existing sprite to attach to. When omitted, an ephemeral sprite is "
            "created and deleted automatically."
        ),
    )
    parser.add_argument(
        "--skip-snapshot-check",
        action="store_true",
        default=False,
        help="Skip the tar workspace persistence verification before the agent run.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            sprite_name=args.sprite_name,
            skip_snapshot_check=args.skip_snapshot_check,
            stream=args.stream,
        )
    )
