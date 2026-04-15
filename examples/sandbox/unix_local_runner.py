"""
Start here if you want the simplest Unix-local sandbox example.

This file mirrors the Docker example, but the sandbox runs as a temporary local
workspace on macOS or Linux instead of inside a Docker container.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from openai.types.responses import ResponseTextDeltaEvent

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

DEFAULT_QUESTION = (
    "Review this renewal packet. Summarize the customer's situation, the likely blockers, "
    "and the next two actions an account team should take."
)


async def main(model: str, question: str, stream: bool) -> None:
    # The manifest is the file tree that will be materialized into the sandbox workspace.
    manifest = text_manifest(
        {
            "account_brief.md": (
                "# Northwind Health\n\n"
                "- Segment: Mid-market healthcare analytics provider.\n"
                "- Annual contract value: $148,000.\n"
                "- Renewal date: 2026-04-15.\n"
                "- Executive sponsor: Director of Data Operations.\n"
            ),
            "renewal_request.md": (
                "# Renewal request\n\n"
                "Northwind requested a 12 percent discount in exchange for a two-year renewal. "
                "They also want a 45-day implementation timeline for a new reporting workspace.\n"
            ),
            "usage_notes.md": (
                "# Usage notes\n\n"
                "- Weekly active users increased 18 percent over the last quarter.\n"
                "- API traffic is stable.\n"
                "- The customer still has one unresolved SSO configuration issue from onboarding.\n"
            ),
            "implementation_risks.md": (
                "# Delivery risks\n\n"
                "- Security questionnaire for the new reporting workspace is not complete.\n"
                "- Customer procurement requires final legal language by April 1.\n"
            ),
        }
    )

    # The sandbox agent sees the manifest as its workspace and uses one shared shell tool
    # to inspect the files before answering.
    agent = SandboxAgent(
        name="Renewal Packet Analyst",
        model=model,
        instructions=(
            "You review renewal packets for an account team. Inspect the packet before answering. "
            "Keep the response concise, business-focused, and cite the file names that support "
            "each conclusion. "
            "If a conclusion depends on a file, mention that file by name. Do not invent numbers "
            "or statuses that are not present in the workspace."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
    )

    # With Unix-local sandboxes, the runner creates and cleans up the temporary workspace for us.
    run_config = RunConfig(
        sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()),
        workflow_name="Unix local sandbox review",
    )

    if not stream:
        result = await Runner.run(agent, question, run_config=run_config)
        print(result.final_output)
        return

    # The streaming path prints text deltas as they arrive so the example behaves like a demo.
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
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    asyncio.run(main(args.model, args.question, args.stream))
