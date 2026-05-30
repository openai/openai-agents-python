"""
Minimal Sailbox-backed sandbox example for manual validation.

This mirrors the other cloud extension examples: it creates a tiny workspace, asks a sandboxed
agent to inspect it through one shell tool, and prints a short answer.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Literal, cast

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from sail.image import Image, ImageDefinition

    from agents.extensions.sandbox import SailboxSandboxClient, SailboxSandboxClientOptions
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Sailbox sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra sailbox"
    ) from exc


DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."


def _build_manifest() -> Manifest:
    return text_manifest(
        {
            "README.md": (
                "# Sailbox Demo Workspace\n\n"
                "This workspace exists to validate the Sailbox sandbox backend manually.\n"
            ),
            "handoff.md": (
                "# Handoff\n\n"
                "- Customer: Northwind Traders.\n"
                "- Goal: validate Sailbox sandbox exec and workspace flows.\n"
                "- Current status: the OpenAI Agents SDK provider is wired for manual smoke tests.\n"
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


def _image_from_name(name: Literal["debian-arm64", "debian-amd64"]) -> ImageDefinition:
    if name == "debian-amd64":
        return Image.debian_amd64
    return Image.debian_arm64


async def main(
    *,
    model: str,
    question: str,
    image: Literal["debian-arm64", "debian-amd64"],
    pause_on_exit: bool,
    stream: bool,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("SAIL_API_KEY")

    manifest = _build_manifest()
    agent = SandboxAgent(
        name="Sailbox Sandbox Assistant",
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

    client = SailboxSandboxClient()
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=client,
            options=SailboxSandboxClientOptions(
                image=_image_from_name(image),
                pause_on_exit=pause_on_exit,
            ),
        ),
        workflow_name="Sailbox sandbox example",
    )

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.5", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--image",
        choices=("debian-arm64", "debian-amd64"),
        default="debian-arm64",
        help="Sailbox base image to use.",
    )
    parser.add_argument(
        "--pause-on-exit",
        action="store_true",
        default=False,
        help="Pause the Sailbox on shutdown instead of terminating it.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            image=cast(Literal["debian-arm64", "debian-amd64"], args.image),
            pause_on_exit=args.pause_on_exit,
            stream=args.stream,
        )
    )
