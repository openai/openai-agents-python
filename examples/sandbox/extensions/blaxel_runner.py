"""
Blaxel-backed sandbox example for manual validation.

This example mirrors the other cloud extension runners. It supports:
- Standard agent run (non-streaming and streaming).
- PTY interactive session demo (agent-driven).
- Blaxel Drive mount demo (persistent storage).

Prerequisites:
  uv sync --extra blaxel
  export OPENAI_API_KEY=...
  export BL_API_KEY=...
  export BL_WORKSPACE=...

Run:
  # Basic agent run
  uv run python examples/sandbox/extensions/blaxel_runner.py --stream

  # With a specific image and region
  uv run python examples/sandbox/extensions/blaxel_runner.py \\
      --image blaxel/py-app --region us-pdx-1 --stream

  # PTY terminal demo (agent-driven interactive Python session)
  uv run python examples/sandbox/extensions/blaxel_runner.py --demo pty

  # Drive mount demo (requires an existing drive, defaults region to us-was-1)
  uv run python examples/sandbox/extensions/blaxel_runner.py \\
      --demo drive --drive-name my-drive
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner, set_tracing_disabled
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File
from agents.sandbox.manifest import Environment

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest, tool_call_name
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import (
        DEFAULT_BLAXEL_WORKSPACE_ROOT,
        BlaxelDriveMountStrategy,
        BlaxelSandboxClient,
        BlaxelSandboxClientOptions,
    )
    from agents.extensions.sandbox.blaxel import BlaxelDriveMount
except Exception as exc:
    raise SystemExit(
        "Blaxel sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra blaxel"
    ) from exc


DEFAULT_MODEL = "gpt-5.5"
DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."
DEFAULT_PTY_QUESTION = (
    "Start an interactive Python session with `tty=true`. In that same session, compute "
    "`5 + 5`, then add 5 more to the previous result. Briefly report the outputs and "
    "confirm that you stayed in one Python process."
)


def _build_manifest() -> Manifest:
    """Build a small demo manifest for the default agent run."""
    manifest = text_manifest(
        {
            "README.md": (
                "# Blaxel Demo Workspace\n\nThis workspace validates the Blaxel sandbox backend.\n"
            ),
            "project/status.md": (
                "# Project Status\n\n"
                "- Backend: Blaxel cloud sandbox\n"
                "- Region: auto-selected\n"
                "- Features: exec, file I/O, PTY, drives, preview URLs\n"
            ),
            "project/tasks.md": (
                "# Tasks\n\n"
                "1. Inspect the workspace files.\n"
                "2. List all features mentioned in status.md.\n"
                "3. Summarize in 2-3 sentences.\n"
            ),
        }
    )
    return Manifest(
        root=DEFAULT_BLAXEL_WORKSPACE_ROOT,
        entries=manifest.entries,
        environment=Environment(
            value={"DEMO_ENV": "blaxel-agent-demo"},
        ),
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise SystemExit(f"{name} must be set before running this example.")


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


# ---------------------------------------------------------------------------
# PTY demo (agent-driven)
# ---------------------------------------------------------------------------


async def _run_pty_demo(
    *,
    model: str,
    question: str,
    image: str | None,
    region: str | None,
) -> None:
    """Demonstrate PTY interaction: start an interactive Python process and continue it."""
    agent = SandboxAgent(
        name="Blaxel PTY Demo",
        model=model,
        instructions=(
            "Complete the task by interacting with the sandbox through the shell capability. "
            "Keep the final answer concise. "
            "Preserve process state when the task depends on it. If you start an interactive "
            "program, continue using that same process instead of launching a second one."
        ),
        default_manifest=Manifest(
            root=DEFAULT_BLAXEL_WORKSPACE_ROOT,
            entries=text_manifest(
                {
                    "README.md": (
                        "# Blaxel PTY Agent Example\n\n"
                        "This workspace is used by the Blaxel PTY demo.\n"
                    ),
                }
            ).entries,
        ),
        capabilities=[Shell()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    client = BlaxelSandboxClient()
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=client,
            options=BlaxelSandboxClientOptions(
                name=f"blaxel-demo-pty-{uuid.uuid4().hex[:8]}",
                image=image,
                region=region,
            ),
        ),
        workflow_name="Blaxel PTY sandbox example",
    )

    try:
        result = Runner.run_streamed(agent, question, run_config=run_config)

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
        await client.close()


# ---------------------------------------------------------------------------
# Drive demo
# ---------------------------------------------------------------------------


async def _run_drive_demo(
    *,
    model: str,
    question: str | None,
    image: str | None,
    region: str | None,
    drive_name: str | None,
    stream: bool,
) -> None:
    """Mount a Blaxel Drive and write a file to it."""
    if not drive_name:
        print("Usage: --demo drive --drive-name <name>")
        print()
        print("You need an existing Blaxel Drive. Create one at:")
        print("  https://app.blaxel.ai or via the Blaxel CLI.")
        return

    # Blaxel drives must be in the same region as the sandbox.
    effective_region = region or os.environ.get("BL_REGION") or "us-was-1"
    mount_path = "/mnt/demo-drive"

    manifest = Manifest(
        root=DEFAULT_BLAXEL_WORKSPACE_ROOT,
        entries={
            "README.md": File(
                content=(b"# Blaxel Drive Demo\n\nThe drive is mounted at /mnt/demo-drive.\n")
            ),
            "drive": BlaxelDriveMount(
                drive_name=drive_name,
                drive_mount_path=mount_path,
                mount_strategy=BlaxelDriveMountStrategy(),
            ),
        },
    )

    marker = f"demo-{uuid.uuid4().hex[:8]}"
    agent = SandboxAgent(
        name="Blaxel Drive Demo",
        model=model,
        instructions=(
            "Execute the exact shell commands the user gives you. "
            "Do not explore, do not run any other commands. "
            "Report the stdout and stderr of each command you ran. "
            "You must run the exact commands from the user message using the shell tool. "
            "Do not substitute, rewrite, or add any commands. Just execute and report output."
        ),
        default_manifest=manifest,
        capabilities=[Shell()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    client = BlaxelSandboxClient()
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=client,
            options=BlaxelSandboxClientOptions(
                name=f"blaxel-demo-drive-{uuid.uuid4().hex[:8]}",
                image=image,
                region=effective_region,
            ),
        ),
        workflow_name="Blaxel drive demo",
    )

    effective_question = question or (
        f"Run: echo 'drive persistence ok ({marker})' > {mount_path}/{marker}.txt && "
        f"cat {mount_path}/{marker}.txt && ls {mount_path}"
    )

    if not stream:
        result = await Runner.run(agent, effective_question, run_config=run_config)
        print(result.final_output)
    else:
        stream_result = Runner.run_streamed(agent, effective_question, run_config=run_config)
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

    await client.close()


# ---------------------------------------------------------------------------
# Standard agent run (streaming / non-streaming)
# ---------------------------------------------------------------------------


async def main(
    *,
    model: str,
    question: str | None,
    image: str | None,
    region: str | None,
    memory: int | None,
    ttl: str | None,
    pause_on_exit: bool,
    stream: bool,
    demo: str | None,
    drive_name: str | None,
) -> None:
    _require_env("OPENAI_API_KEY")

    # Handle dedicated demos.
    if demo == "pty":
        await _run_pty_demo(
            model=model,
            question=question or DEFAULT_PTY_QUESTION,
            image=image,
            region=region,
        )
        return

    if demo == "drive":
        await _run_drive_demo(
            model=model,
            question=question,
            image=image,
            region=region,
            drive_name=drive_name,
            stream=stream,
        )
        return

    manifest = _build_manifest()
    agent = SandboxAgent(
        name="Blaxel Sandbox Assistant",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the files before answering "
            "and keep the response concise. "
            "Do not invent files or statuses that are not present in the workspace. Cite the "
            "file names you inspected. Also run `echo $DEMO_ENV` to confirm environment "
            "variables are set."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=BlaxelSandboxClient(),
            options=BlaxelSandboxClientOptions(
                name=f"blaxel-demo-agent-{uuid.uuid4().hex[:8]}",
                image=image,
                region=region,
                memory=memory,
                ttl=ttl,
                labels={"purpose": "agent-demo", "source": "blaxel-runner"},
                pause_on_exit=pause_on_exit,
            ),
        ),
        workflow_name="Blaxel sandbox example",
    )

    effective_question = question or DEFAULT_QUESTION

    if not stream:
        result = await Runner.run(agent, effective_question, run_config=run_config)
        print(result.final_output)
        return

    stream_result = Runner.run_streamed(agent, effective_question, run_config=run_config)
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
        description="Blaxel sandbox demo -- showcases sandbox features.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "demos:\n"
            "  agent    Run a sandboxed agent (default)\n"
            "  pty      Agent-driven PTY interactive terminal\n"
            "  drive    Mount a Blaxel Drive (requires --drive-name)\n"
        ),
    )
    parser.add_argument(
        "--demo",
        choices=["agent", "pty", "drive"],
        default="agent",
        help="Which demo to run (default: agent).",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name.")
    parser.add_argument("--question", default=None, help="Override the default prompt.")
    parser.add_argument("--stream", action="store_true", help="Stream response.")
    parser.add_argument("--image", default=None, help="Sandbox image.")
    parser.add_argument("--region", default=None, help="Sandbox region.")
    parser.add_argument("--memory", type=int, default=None, help="Memory in MB.")
    parser.add_argument("--ttl", default=None, help="Sandbox TTL (e.g. '1h').")
    parser.add_argument("--pause-on-exit", action="store_true", help="Pause on exit.")
    parser.add_argument("--drive-name", default=None, help="Drive name for drive demo.")
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            image=args.image,
            region=args.region,
            memory=args.memory,
            ttl=args.ttl,
            pause_on_exit=args.pause_on_exit,
            stream=args.stream,
            demo=args.demo,
            drive_name=args.drive_name,
        )
    )
