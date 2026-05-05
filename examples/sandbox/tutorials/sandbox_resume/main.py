"""
Show the smallest Unix-local sandbox flow with workspace instructions.

The manifest includes an AGENTS.md file that tells the agent how to build the
app, and the prompt asks for a tiny FastAPI operations status API with a health
check.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from textwrap import dedent

from agents import Runner, RunResultStreaming, TResponseInputItem
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Shell
from agents.sandbox.entries import File

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from examples.sandbox.tutorials.misc import (
    DEFAULT_SANDBOX_IMAGE,
    create_sandbox_client_and_session,
    load_env_defaults,
    print_event,
)

DEFAULT_QUESTION = (
    "Build a small warehouse-robot operations status API with FastAPI. Include a health "
    "check, a typed `/robots/{robot_id}/status` endpoint backed by a tiny in-memory "
    "fixture, and clear 404 behavior. Install dependencies with uv, smoke test it locally "
    "with `uv run python` and `urllib.request`, and summarize what you built."
)
DEMO_DIR = Path(__file__).resolve().parent
RESUME_QUESTION = (
    "Now add pytest coverage for the health check, robot status success case, and unknown "
    "robot 404 case. Install any missing dependencies with uv, run the tests locally, and "
    "summarize the files you changed."
)
AGENTS_MD = dedent(
    """\
    # AGENTS.md

    - When asked to build an app, make it a FastAPI app.
    - Use type hints and Pydantic models.
    - Use `uv` when installing dependencies.
    - Run Python commands as `uv run python ...`, not bare `python`.
    - Smoke test local HTTP endpoints with `uv run python` and `urllib.request`, not `curl`.
    - Test the app locally before finishing.
    """
)


async def run_step(result: RunResultStreaming) -> list[TResponseInputItem]:
    async for event in result.stream_events():
        print_event(event)
    print_event(str(result.final_output).strip())
    return result.to_input_list()


async def main(model: str, question: str, use_docker: bool, image: str) -> None:
    manifest = Manifest(entries={"AGENTS.md": File(content=AGENTS_MD.encode("utf-8"))})
    agent = SandboxAgent(
        name="Vibe Coder",
        model=model,
        instructions=AGENTS_MD,
        capabilities=[Shell(), Filesystem()],
    )

    client, sandbox = await create_sandbox_client_and_session(
        manifest=manifest,
        use_docker=use_docker,
        image=image,
    )
    conversation: list[TResponseInputItem] = [{"role": "user", "content": question}]

    try:
        async with sandbox:
            result = Runner.run_streamed(
                agent,
                conversation,
                max_turns=20,
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(session=sandbox),
                    tracing_disabled=True,
                    workflow_name="Sandbox resume example",
                ),
            )
            conversation = await run_step(result)

        frozen_session_state = client.deserialize_session_state(
            client.serialize_session_state(sandbox.state)
        )
        conversation.append({"role": "user", "content": RESUME_QUESTION})

        resumed_sandbox = await client.resume(frozen_session_state)
        try:
            async with resumed_sandbox:
                resumed_result = Runner.run_streamed(
                    agent,
                    conversation,
                    max_turns=20,
                    run_config=RunConfig(
                        sandbox=SandboxRunConfig(session=resumed_sandbox),
                        tracing_disabled=True,
                        workflow_name="Sandbox resume example",
                    ),
                )
                conversation = await run_step(resumed_result)
        finally:
            await client.delete(resumed_sandbox)
    finally:
        await client.delete(sandbox)


if __name__ == "__main__":
    load_env_defaults(DEMO_DIR / ".env")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default="gpt-5.4-mini",
        help="Model name to use.",
    )
    parser.add_argument(
        "--question",
        default=DEFAULT_QUESTION,
        help="Prompt to send to the agent.",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Run this example in Docker instead of Unix-local.",
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_SANDBOX_IMAGE,
        help="Docker image to use when --docker is set.",
    )
    args = parser.parse_args()

    asyncio.run(main(args.model, args.question, args.docker, args.image))
