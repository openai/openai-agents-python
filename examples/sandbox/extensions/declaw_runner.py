"""Minimal declaw-backed sandbox example for manual validation.

Creates a tiny workspace, lets the agent inspect it through the shell
tool, and prints a short answer. Exercises the ``declaw`` backend end
to end via the ``[declaw]`` repo extra.

Credentials (env):
    DECLAW_API_KEY      your declaw API key
    DECLAW_DOMAIN       e.g. ``api.declaw.ai``
    OPENAI_API_KEY      for the OpenAI model

Install:
    uv sync --extra declaw --extra dev
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from agents import Runner, set_tracing_disabled
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest

try:
    from agents.extensions.sandbox import (
        DeclawSandboxClient,
        DeclawSandboxClientOptions,
    )
except Exception as exc:  # pragma: no cover - depends on optional extras
    raise SystemExit(
        "Declaw sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra declaw"
    ) from exc


DEFAULT_QUESTION = "Summarize the files in this workspace in 2 sentences."
DEFAULT_TEMPLATE = "base"


def _build_manifest() -> Manifest:
    return text_manifest(
        {
            "README.md": (
                "# Notes\n\nSmall workspace used to validate the declaw sandbox backend.\n"
            ),
            "todo.md": (
                "# Todo\n\n"
                "- Confirm the agent can read files.\n"
                "- Confirm the shell tool round-trips cleanly.\n"
            ),
        }
    )


def _require_env(name: str) -> None:
    if not os.environ.get(name):
        raise SystemExit(f"{name} must be set before running this example.")


async def _run(*, question: str, template: str, timeout: int) -> None:
    # Tracing goes to OpenAI's trace endpoint by default. ZDR-enabled
    # orgs get a 403 on every upload — silence the noise.
    set_tracing_disabled(True)

    for env in ("DECLAW_API_KEY", "DECLAW_DOMAIN", "OPENAI_API_KEY"):
        _require_env(env)

    client = DeclawSandboxClient()
    options = DeclawSandboxClientOptions(template=template, timeout=timeout)

    session = await client.create(options=options, manifest=_build_manifest())

    try:
        agent = SandboxAgent(
            name="declaw-runner",
            model="gpt-5-mini",
            instructions=(
                "You are running inside a declaw sandbox. Use the shell "
                "tool to list files under /workspace and read anything "
                "relevant. Keep the final answer to two sentences."
            ),
        )

        result = await Runner.run(
            agent,
            question,
            run_config=RunConfig(sandbox=SandboxRunConfig(session=session)),
            max_turns=15,
        )
        print(result.final_output)
    finally:
        await client.delete(session=session)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    asyncio.run(
        _run(
            question=args.question,
            template=args.template,
            timeout=args.timeout,
        )
    )


if __name__ == "__main__":
    main()
