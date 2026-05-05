"""
Minimal Runloop-backed sandbox example for manual validation.

This mirrors the other cloud extension examples: it creates a tiny workspace, asks a sandboxed
agent to inspect it through one shell tool, and prints a short answer.
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

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import (
        DEFAULT_RUNLOOP_ROOT_WORKSPACE_ROOT,
        DEFAULT_RUNLOOP_WORKSPACE_ROOT,
        RunloopSandboxClient,
        RunloopSandboxClientOptions,
        RunloopUserParameters,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Runloop sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra runloop"
    ) from exc


DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."


def _build_manifest(*, workspace_root: str) -> Manifest:
    manifest = text_manifest(
        {
            "README.md": (
                "# Runloop Demo Workspace\n\n"
                "This workspace exists to validate the Runloop sandbox backend manually.\n"
            ),
            "launch.md": (
                "# Launch\n\n"
                "- Customer: Contoso Logistics.\n"
                "- Goal: validate the remote sandbox agent path.\n"
                "- Current status: Runloop backend smoke and app-server connectivity are passing.\n"
            ),
            "tasks.md": (
                "# Tasks\n\n"
                "1. Inspect the workspace files.\n"
                "2. Summarize the setup and any notable status in two sentences.\n"
            ),
        }
    )
    return Manifest(root=workspace_root, entries=manifest.entries)


def _require_env(name: str) -> None:
    if os.environ.get(name):
        return
    raise SystemExit(f"{name} must be set before running this example.")


async def main(
    *,
    model: str,
    question: str,
    pause_on_exit: bool,
    blueprint_name: str | None,
    root: bool,
    stream: bool,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("RUNLOOP_API_KEY")

    workspace_root = DEFAULT_RUNLOOP_ROOT_WORKSPACE_ROOT if root else DEFAULT_RUNLOOP_WORKSPACE_ROOT
    manifest = _build_manifest(workspace_root=workspace_root)
    agent = SandboxAgent(
        name="Runloop Sandbox Assistant",
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

    client = RunloopSandboxClient()
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=client,
            options=RunloopSandboxClientOptions(
                blueprint_name=blueprint_name,
                pause_on_exit=pause_on_exit,
                user_parameters=(RunloopUserParameters(username="root", uid=0) if root else None),
            ),
        ),
        workflow_name="Runloop sandbox example",
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
    parser.add_argument("--model", default="gpt-5.5", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--pause-on-exit",
        action="store_true",
        default=False,
        help="Suspend the Runloop devbox on shutdown instead of deleting it.",
    )
    parser.add_argument(
        "--blueprint-name",
        default=None,
        help="Optional Runloop blueprint name to use when creating the devbox.",
    )
    parser.add_argument(
        "--root",
        action="store_true",
        default=False,
        help="Launch the Runloop devbox as root. The default home/workspace root becomes /root.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            pause_on_exit=args.pause_on_exit,
            blueprint_name=args.blueprint_name,
            root=args.root,
            stream=args.stream,
        )
    )
