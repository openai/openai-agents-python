"""
Minimal Modal-backed sandbox example for manual validation.

This example mirrors the local and Docker sandbox demos, but it sends the
workspace to a Modal sandbox.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Literal

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import ModalSandboxClient, ModalSandboxClientOptions
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Modal sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra modal"
    ) from exc


DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."


def _build_manifest() -> Manifest:
    return text_manifest(
        {
            "README.md": (
                "# Modal Demo Workspace\n\n"
                "This workspace exists to validate the Modal sandbox backend manually.\n"
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


def _require_env(name: str) -> None:
    if os.environ.get(name):
        return
    raise SystemExit(f"{name} must be set before running this example.")


async def main(
    *,
    model: str,
    question: str,
    app_name: str,
    workspace_persistence: Literal["tar", "snapshot_filesystem"],
    sandbox_create_timeout_s: float | None,
    stream: bool,
) -> None:
    _require_env("OPENAI_API_KEY")

    manifest = _build_manifest()
    agent = SandboxAgent(
        name="Modal Sandbox Assistant",
        model=model,
        # `instructions` is the base agent instructions for this sandbox task.
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the files before answering "
            "and keep the response concise."
        ),
        # `developer_instructions` is appended after that as additional deterministic instructions.
        # Here, the grounding constraints are kept in `developer_instructions`.
        developer_instructions=(
            "Do not invent files or statuses that are not present in the workspace. Cite the "
            "file names you inspected."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=ModalSandboxClient(),
            options=ModalSandboxClientOptions(
                app_name=app_name,
                workspace_persistence=workspace_persistence,
                sandbox_create_timeout_s=sandbox_create_timeout_s,
            ),
        ),
        workflow_name="Modal sandbox example",
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
    parser.add_argument("--model", default="gpt-5.4", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--app-name",
        default="openai-agents-python-sandbox-example",
        help="Modal app name to create or reuse for the sandbox.",
    )
    parser.add_argument(
        "--workspace-persistence",
        default="tar",
        choices=["tar", "snapshot_filesystem"],
        help="Workspace persistence mode for the Modal sandbox.",
    )
    parser.add_argument(
        "--sandbox-create-timeout-s",
        type=float,
        default=None,
        help="Optional timeout for creating the Modal sandbox.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            app_name=args.app_name,
            workspace_persistence=args.workspace_persistence,
            sandbox_create_timeout_s=args.sandbox_create_timeout_s,
            stream=args.stream,
        )
    )
