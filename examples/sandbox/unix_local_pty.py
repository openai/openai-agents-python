"""Show how a sandbox agent can keep using the same interactive Python process.

This example uses the Unix-local sandbox with the `Shell` capability. The task only asks
for a stateful interaction, but the streamed output shows the actual shell tools the agent
chooses, including the follow-up writes that keep the same process alive.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.sandbox.misc.example_support import tool_call_name

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_QUESTION = (
    "Start an interactive Python session. In that same session, compute `5 + 5`, then add "
    "5 more to the previous result. Briefly report the outputs and confirm that you stayed "
    "in one Python process."
)


def _build_manifest() -> Manifest:
    return Manifest(
        entries={
            "README.md": File(
                content=(
                    b"# Unix-local PTY Agent Example\n\n"
                    b"This workspace is used by examples/sandbox/unix_local_pty.py.\n"
                )
            ),
        }
    )


def _build_agent(model: str) -> SandboxAgent:
    return SandboxAgent(
        name="Unix-local PTY Demo",
        model=model,
        instructions=(
            "Complete the task by inspecting and interacting with the sandbox through the shell "
            "capability. Keep the final answer concise. "
            "Preserve process state when the task depends on it. If you start an interactive "
            "program, continue using that same process instead of launching a second one."
        ),
        default_manifest=_build_manifest(),
        capabilities=[Shell()],
        model_settings=ModelSettings(tool_choice="required"),
    )


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


async def main(model: str, question: str) -> None:
    agent = _build_agent(model)
    client = UnixLocalSandboxClient()
    sandbox = await client.create(manifest=agent.default_manifest)

    try:
        async with sandbox:
            result = Runner.run_streamed(
                agent,
                question,
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(session=sandbox),
                    tracing_disabled=True,
                    workflow_name="Unix-local PTY example",
                ),
            )

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
                    tool_name = tool_call_name(raw_item)
                    call_id = _raw_item_call_id(raw_item)
                    if call_id is not None and tool_name:
                        tool_names_by_call_id[call_id] = tool_name
                    if tool_name:
                        banner = f"{banner} {tool_name}"
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
        await client.delete(sandbox)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Run a Unix-local sandbox agent that demonstrates PTY interaction through the "
            "shell capability."
        )
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    parser.add_argument(
        "--question",
        default=DEFAULT_QUESTION,
        help="Prompt to send to the agent.",
    )
    args = parser.parse_args()

    asyncio.run(main(args.model, args.question))
