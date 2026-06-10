"""
Upstash Box-backed sandbox example for manual validation.

This example mirrors the other extension runners. It supports a standard agent
run (non-streaming and streaming) against an Upstash Box sandbox.

Requires ``OPENAI_API_KEY`` and ``UPSTASH_BOX_API_KEY`` in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner, set_tracing_disabled
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest

try:
    from agents.extensions.sandbox import (
        UpstashBoxSandboxClient,
        UpstashBoxSandboxClientOptions,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Upstash Box sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra upstash-box"
    ) from exc


DEFAULT_MODEL = "gpt-5.5"
DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."


def _build_manifest() -> Manifest:
    return text_manifest(
        {
            "README.md": (
                "# Upstash Box Demo Workspace\n\n"
                "This workspace exists to validate the Upstash Box sandbox backend manually.\n"
            ),
            "launch.md": (
                "# Launch\n\n"
                "- Customer: Contoso Logistics.\n"
                "- Goal: validate the remote sandbox agent path.\n"
            ),
            "tasks.md": (
                "# Tasks\n\n"
                "1. Inspect the workspace files.\n"
                "2. Summarize the setup in two sentences.\n"
            ),
        }
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise SystemExit(f"{name} must be set before running this example.")


async def main(*, model: str, question: str, api_key: str | None, stream: bool) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("UPSTASH_BOX_API_KEY")

    agent = SandboxAgent(
        name="Upstash Box Sandbox Assistant",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the files before answering "
            "and keep the response concise. Cite the file names you inspected."
        ),
        default_manifest=_build_manifest(),
        capabilities=[Shell()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=UpstashBoxSandboxClient(),
            options=UpstashBoxSandboxClientOptions(api_key=api_key),
        ),
        workflow_name="Upstash Box sandbox example",
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

    parser = argparse.ArgumentParser(description="Run an Upstash Box sandbox agent.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("UPSTASH_BOX_API_KEY"),
        help="Upstash Box API key. Defaults to UPSTASH_BOX_API_KEY.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            api_key=args.api_key,
            stream=args.stream,
        )
    )
