"""
OpenShell sandbox integration example.

This script exercises the OpenShell sandbox extension at two levels:

1. **Session-level** (no LLM needed): Creates a sandbox, writes files, reads them
   back, runs commands, and verifies workspace persistence. This validates the
   extension works end-to-end with a real OpenShell gateway.

2. **Agent-level** (requires OPENAI_API_KEY): Runs a SandboxAgent with a shell
   capability inside the OpenShell sandbox.

Prerequisites:
  - An OpenShell gateway running (local, remote, or cloud).
  - ``openshell`` Python package installed: ``uv sync --extra openshell``
  - For agent mode: ``OPENAI_API_KEY`` environment variable set.

Quick start:
  # Session-level only (no LLM):
  uv run python examples/sandbox/extensions/openshell_runner.py --session-only

  # Full agent run:
  uv run python examples/sandbox/extensions/openshell_runner.py

  # With a specific cluster:
  uv run python examples/sandbox/extensions/openshell_runner.py --cluster my-gateway

  # With a custom image:
  uv run python examples/sandbox/extensions/openshell_runner.py --image ubuntu:24.04
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
from pathlib import Path

try:
    from agents.extensions.sandbox import (
        OpenShellSandboxClient,
        OpenShellSandboxClientOptions,
    )
except Exception as exc:
    raise SystemExit(
        "OpenShell sandbox examples require the optional openshell extra.\n"
        "Install it with: uv sync --extra openshell"
    ) from exc


async def session_level_test(
    *,
    cluster: str | None,
    endpoint: str | None,
    image: str | None,
    gpu: bool,
) -> None:
    """Exercise the sandbox extension directly without an LLM."""

    from agents.sandbox import Manifest
    from agents.sandbox.entries import File

    print("=== OpenShell Session-Level Test ===\n")

    # Build a manifest with test files.
    # OpenShell sandboxes default to /sandbox as the working directory.
    manifest = Manifest(
        root="/sandbox",
        entries={
            "hello.txt": File(content=b"Hello from OpenShell sandbox!\n"),
            "data/numbers.csv": File(content=b"a,b,c\n1,2,3\n4,5,6\n"),
        },
    )

    client = OpenShellSandboxClient()
    options = OpenShellSandboxClientOptions(
        cluster=cluster,
        endpoint=endpoint,
        image=image,
        gpu=gpu,
    )

    print("1. Creating sandbox...")
    session = await client.create(manifest=manifest, options=options)

    try:
        print("2. Starting session (materializing workspace)...")
        await session.start()

        print("3. Running 'ls -la' in workspace...")
        result = await session.exec("ls", "-la", shell=False)
        print(f"   exit_code={result.exit_code}")
        print(f"   stdout:\n{result.stdout.decode()}")

        print("4. Reading hello.txt...")
        content = await session.read(Path("hello.txt"))
        text = content.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        print(f"   content: {text.strip()!r}")
        assert "Hello from OpenShell sandbox!" in text, "Read verification failed."

        print("5. Writing a new file...")
        await session.write(
            Path("output.txt"),
            io.BytesIO(b"Written by the OpenAI Agents SDK via OpenShell.\n"),
        )

        print("6. Verifying the written file...")
        result = await session.exec("cat", "output.txt", shell=False)
        assert result.exit_code == 0, f"cat failed: {result.stderr.decode()}"
        print(f"   content: {result.stdout.decode().strip()!r}")

        print("7. Running a multi-step shell command...")
        result = await session.exec("wc -l data/numbers.csv && echo 'done'")
        print(f"   output: {result.stdout.decode().strip()}")

        print("8. Checking sandbox is running...")
        is_running = await session.running()
        print(f"   running: {is_running}")
        assert is_running, "Sandbox should be running."

        print("9. Persisting workspace (tar snapshot)...")
        snapshot = await session.persist_workspace()
        snapshot_bytes = snapshot.read()
        print(f"   snapshot size: {len(snapshot_bytes)} bytes")
        assert len(snapshot_bytes) > 0, "Snapshot should not be empty."

        print("\nAll session-level checks passed.")

    finally:
        print("\n10. Shutting down sandbox...")
        await session.aclose()
        print("    Done.")


async def agent_level_test(
    *,
    model: str,
    cluster: str | None,
    endpoint: str | None,
    image: str | None,
    gpu: bool,
    question: str,
    stream: bool,
) -> None:
    """Run a SandboxAgent backed by OpenShell."""

    from openai.types.responses import ResponseTextDeltaEvent

    from agents import ModelSettings, Runner
    from agents.run import RunConfig
    from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
    from agents.sandbox.entries import File

    if __package__ is None or __package__ == "":
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

    from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

    print("\n=== OpenShell Agent-Level Test ===\n")

    manifest = Manifest(
        root="/sandbox",
        entries={
            "README.md": File(
                content=(
                    b"# Project Status\n\nThis workspace contains a sample project status report.\n"
                ),
            ),
            "status.md": File(
                content=(
                    b"# Sprint 42 Status\n\n"
                    b"- Auth service: on track, shipping Tuesday.\n"
                    b"- Search reindex: blocked on infra ticket INFRA-1234.\n"
                    b"- Dashboard v2: 80% complete, needs UX review.\n"
                ),
            ),
        },
    )

    agent = SandboxAgent(
        name="OpenShell Sandbox Assistant",
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

    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=OpenShellSandboxClient(),
            options=OpenShellSandboxClientOptions(
                cluster=cluster,
                endpoint=endpoint,
                image=image,
                gpu=gpu,
            ),
        ),
        workflow_name="OpenShell sandbox example",
    )

    if not stream:
        result = await Runner.run(agent, question, run_config=run_config)
        print(f"assistant> {result.final_output}")
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


async def main(
    *,
    model: str,
    cluster: str | None,
    endpoint: str | None,
    image: str | None,
    gpu: bool,
    question: str,
    stream: bool,
    session_only: bool,
) -> None:
    # Session-level test always runs (no LLM needed).
    await session_level_test(
        cluster=cluster,
        endpoint=endpoint,
        image=image,
        gpu=gpu,
    )

    if session_only:
        return

    # Agent-level test requires OPENAI_API_KEY.
    if not os.environ.get("OPENAI_API_KEY"):
        print("\nSkipping agent-level test (OPENAI_API_KEY not set).")
        print("Set OPENAI_API_KEY and remove --session-only to run the full test.")
        return

    await agent_level_test(
        model=model,
        cluster=cluster,
        endpoint=endpoint,
        image=image,
        gpu=gpu,
        question=question,
        stream=stream,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="OpenShell sandbox integration example for the OpenAI Agents SDK."
    )
    parser.add_argument("--model", default="gpt-4.1-mini", help="Model name to use.")
    parser.add_argument(
        "--question",
        default="Summarize the project status from the workspace files.",
        help="Prompt to send to the agent.",
    )
    parser.add_argument("--cluster", default=None, help="OpenShell gateway cluster name.")
    parser.add_argument("--endpoint", default=None, help="Explicit gateway endpoint (host:port).")
    parser.add_argument("--image", default=None, help="Container image for the sandbox.")
    parser.add_argument("--gpu", action="store_true", default=False, help="Request GPU.")
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    parser.add_argument(
        "--session-only",
        action="store_true",
        default=False,
        help="Run session-level test only (no LLM needed).",
    )
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            cluster=args.cluster,
            endpoint=args.endpoint,
            image=args.image,
            gpu=args.gpu,
            question=args.question,
            stream=args.stream,
            session_only=args.session_only,
        )
    )
