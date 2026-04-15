"""
Answer questions over a synthetic dataroom.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from textwrap import dedent

from agents import Runner, RunResultStreaming, TResponseInputItem
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File, LocalDir

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from examples.sandbox.tutorials.misc import (
    DEFAULT_SANDBOX_IMAGE,
    create_sandbox_client_and_session,
    load_env_defaults,
    print_event,
    run_interactive_loop,
)

DEMO_DIR = Path(__file__).resolve().parent
DATAROOM_DATA_DIR = DEMO_DIR.parent / "data" / "dataroom"
DEFAULT_QUESTION = (
    "How did revenue, gross margin, operating income, and operating cash flow change in "
    "FY2025 versus FY2024, and which segment contributed the most revenue?"
)
AGENTS_MD = dedent(
    """\
    # AGENTS.md

    Answer the user's financial question using only the synthetic 10-K packet in `data/`.

    ## Evidence & citations

    - Cite every material claim with markdown links in these formats (no bare links):
      - `[1](data/source-file.txt:line:14)` for text sources
      - `[2](data/source-file.pdf:page:1)` for PDF sources (each synthetic PDF is one page)
    - Use `rg` and `sed` to find and quote exact evidence; do not use `data/setup.py`.

    Keep the final answer direct and finance-oriented.
    """
)


async def print_streamed_result(result: RunResultStreaming) -> list[TResponseInputItem]:
    async for event in result.stream_events():
        print_event(event)
    print_event(str(result.final_output).strip())
    return result.to_input_list()


async def main(
    model: str, question: str, use_docker: bool, image: str, no_interactive: bool
) -> None:
    if not (DATAROOM_DATA_DIR / "10-k-mdna-overview.txt").exists():
        raise SystemExit(
            "Run `uv run python examples/sandbox/tutorials/data/dataroom/setup.py` "
            "before starting this demo."
        )

    manifest = Manifest(
        entries={
            "AGENTS.md": File(content=AGENTS_MD.encode("utf-8")),
            "data": LocalDir(src=DATAROOM_DATA_DIR),
        }
    )
    agent = SandboxAgent(
        name="Dataroom Analyst",
        model=model,
        instructions=AGENTS_MD,
        capabilities=[Shell()],
    )

    client, sandbox = await create_sandbox_client_and_session(
        manifest=manifest,
        use_docker=use_docker,
        image=image,
    )
    try:
        async with sandbox:

            async def run_turn(
                conversation: list[TResponseInputItem],
            ) -> list[TResponseInputItem]:
                result = Runner.run_streamed(
                    agent,
                    conversation,
                    max_turns=20,
                    run_config=RunConfig(
                        sandbox=SandboxRunConfig(session=sandbox),
                        tracing_disabled=True,
                        workflow_name="Dataroom Q&A example",
                    ),
                )
                return await print_streamed_result(result)

            conversation: list[TResponseInputItem] = [{"role": "user", "content": question}]
            conversation = await run_turn(conversation)
            await run_interactive_loop(
                conversation=conversation,
                no_interactive=no_interactive,
                run_turn=run_turn,
            )
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
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Run the scripted turn and skip follow-up terminal input.",
    )
    args = parser.parse_args()

    asyncio.run(main(args.model, args.question, args.docker, args.image, args.no_interactive))
