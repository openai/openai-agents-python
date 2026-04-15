"""
Start here if you are new to Docker-backed sandbox examples.

This file keeps the flow explicit:

1. Build a manifest for the files that should appear in the sandbox workspace.
2. Create a sandbox agent that can inspect that workspace through one shell tool.
3. Start a Docker-backed sandbox session, stream the run, and print what happens.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from docker import from_env as docker_from_env  # type: ignore[import-untyped]
from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.config import DEFAULT_PYTHON_SANDBOX_IMAGE
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest, tool_call_name
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

DEFAULT_QUESTION = "Summarize this sandbox project in 2 sentences."
MAX_STREAM_TOOL_OUTPUT_CHARS = 2000


def _format_tool_arguments(raw_item: object) -> str | None:
    arguments = raw_item.get("arguments") if isinstance(raw_item, dict) else None
    if isinstance(arguments, str) and arguments:
        return arguments

    action = raw_item.get("action") if isinstance(raw_item, dict) else None
    commands = action.get("commands") if isinstance(action, dict) else None
    if isinstance(commands, list):
        return "; ".join(command for command in commands if isinstance(command, str))

    return None


def _format_tool_call(raw_item: object) -> str:
    name = tool_call_name(raw_item) or "tool"
    arguments = _format_tool_arguments(raw_item)
    if arguments:
        return f"[tool call] {name}: {arguments}"
    return f"[tool call] {name}"


def _format_tool_output(output: object) -> str:
    output_text = str(output)
    if len(output_text) > MAX_STREAM_TOOL_OUTPUT_CHARS:
        output_text = f"{output_text[:MAX_STREAM_TOOL_OUTPUT_CHARS]}..."
    if output_text:
        return f"[tool output]\n{output_text}"
    return "[tool output]"


async def main(model: str, question: str) -> None:
    # A manifest is the starting file tree for the sandbox workspace.
    # Each key is a path inside the workspace and each value is the file content.
    # `text_manifest()` keeps small text examples readable by hiding the bytes boilerplate.
    manifest = text_manifest(
        {
            "README.md": (
                "# Demo Project\n\n"
                "This sandbox contains a tiny demo project for the sandbox runner.\n"
                "The goal is to show how Runner can prepare a Docker-backed workspace.\n"
            ),
            "src/app.py": 'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n',
            "docs/notes.md": (
                "# Notes\n\n"
                "- The example is intentionally minimal.\n"
                "- The model should inspect files through the shell tool.\n"
            ),
        }
    )

    agent = SandboxAgent(
        name="Docker Sandbox Assistant",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the project before answering, "
            "and keep the response concise. "
            "Do not guess file names like package.json or pyproject.toml. "
            "This demo intentionally contains a tiny workspace."
        ),
        # `default_manifest` tells the sandbox agent which workspace it should expect.
        default_manifest=manifest,
        # `WorkspaceShellCapability()` exposes one shell tool so the model can inspect files.
        capabilities=[WorkspaceShellCapability()],
        # `tool_choice="required"` makes the demo more deterministic by forcing the model
        # to look at the workspace instead of answering from prior assumptions.
        model_settings=ModelSettings(tool_choice="required"),
    )

    # The Docker client owns the container lifecycle for the sandbox session.
    docker_client = DockerSandboxClient(docker_from_env())

    # `create()` allocates a fresh sandbox session backed by a Docker container.
    # We pass the same manifest here so the container knows which files to materialize.
    sandbox = await docker_client.create(
        manifest=manifest,
        options=DockerSandboxClientOptions(image=DEFAULT_PYTHON_SANDBOX_IMAGE),
    )
    try:
        # `async with sandbox` keeps the example on the public session lifecycle API.
        # `Runner` reuses the already-running session without starting it a second time.
        async with sandbox:
            # `Runner.run_streamed()` drives the model and yields text and tool events in real time.
            result = Runner.run_streamed(
                agent,
                question,
                run_config=RunConfig(sandbox=SandboxRunConfig(session=sandbox)),
            )
            saw_text_delta = False
            saw_any_text = False

            # The stream contains raw text deltas from the assistant plus structured tool events.
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

                if event.name == "tool_called" and event.item.type == "tool_call_item":
                    if saw_text_delta:
                        print()
                        saw_text_delta = False
                    print(_format_tool_call(event.item.raw_item))
                elif event.name == "tool_output" and event.item.type == "tool_call_output_item":
                    if saw_text_delta:
                        print()
                        saw_text_delta = False
                    print(_format_tool_output(event.item.output))

            if saw_text_delta:
                print()
            if not saw_any_text:
                print(result.final_output)
    finally:
        # The client still owns deleting the underlying Docker container.
        await docker_client.delete(sandbox)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    args = parser.parse_args()
    asyncio.run(main(args.model, args.question))
