"""Run a sandboxed agent with Azure Container Apps Sandboxes."""

from __future__ import annotations

import argparse
import asyncio
import os

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.extensions.sandbox import ACASandboxesClient, ACASandboxesClientOptions
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File

DEFAULT_QUESTION = "Inspect README.md and src/app.py, then summarize their purpose."


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise SystemExit(f"{name} must be set before running this example.")


def _build_manifest() -> Manifest:
    return Manifest(
        entries={
            "README.md": File(
                content=(
                    b"# ACA Sandboxes example\n\n"
                    b"This workspace validates the hosted ACA provider.\n"
                )
            ),
            "src/app.py": File(content=b"print('hello from ACA Sandboxes')\n"),
        }
    )


async def main(*, model: str, question: str, stream: bool) -> None:
    _require_env("OPENAI_API_KEY")
    client = ACASandboxesClient(
        region=_require_env("ACA_SANDBOXGROUP_REGION"),
        subscription_id=_require_env("AZURE_SUBSCRIPTION_ID"),
        resource_group=_require_env("ACA_RESOURCE_GROUP"),
        sandbox_group=_require_env("ACA_SANDBOX_GROUP"),
    )
    agent = SandboxAgent(
        name="ACA Sandboxes Assistant",
        model=model,
        instructions=(
            "Inspect the sandbox workspace with the shell tool before answering. "
            "Mention the files you inspected and do not invent files."
        ),
        default_manifest=_build_manifest(),
        capabilities=[Shell()],
        model_settings=ModelSettings(tool_choice="required"),
    )
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=client,
            options=ACASandboxesClientOptions(
                disk="ubuntu",
                auto_suspend_seconds=300,
                auto_suspend_mode="Memory",
            ),
        ),
        workflow_name="ACA Sandboxes example",
    )

    try:
        if not stream:
            result = await Runner.run(agent, question, run_config=run_config)
            print(result.final_output)
            return

        stream_result = Runner.run_streamed(agent, question, run_config=run_config)
        async for event in stream_result.stream_events():
            if event.type == "raw_response_event" and isinstance(
                event.data, ResponseTextDeltaEvent
            ):
                print(event.data.delta, end="", flush=True)
        print()
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENAI_MODEL", "gpt-5.6-sol"),
        help="Model name to use.",
    )
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(model=args.model, question=args.question, stream=args.stream))
