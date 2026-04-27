"""Run multiple agent turns against the same Unix-local sandbox session.

This example creates one live Unix-local sandbox session, then runs the same
`SandboxAgent` twice against it. The second run can see the file written by the
first run because both runs receive `SandboxRunConfig(session=sandbox)`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_MODEL = os.environ.get("MODEL_NAME", "gpt-5.5")
FIRST_TURN = (
    "Turn 1: use the shell in the sandbox to create `state/session_counter.txt` with exactly "
    "one line: `turn-1: alpha`. Briefly report the path you wrote."
)
SECOND_TURN = (
    "Turn 2: this is a follow-up using the same live sandbox session. Use the shell to read "
    "`state/session_counter.txt`, append exactly one line `turn-2: beta`, then report the full "
    "file contents. Do not recreate the file from memory."
)


def _build_manifest() -> Manifest:
    return Manifest(
        entries={
            "README.md": File(
                content=(
                    b"# Unix-local PTY Multiturn Example\n\n"
                    b"The example reuses one live Unix-local sandbox session across two "
                    b"Runner.run calls.\n"
                )
            ),
        }
    )


def _build_agent(model: str) -> SandboxAgent:
    return SandboxAgent(
        name="Unix-local Multiturn Demo",
        model=model,
        instructions=(
            "Use the sandbox shell for each turn. Treat the workspace as persistent across "
            "turns because the caller is reusing the same live sandbox session. Keep final "
            "answers concise and mention the exact file content when asked."
        ),
        default_manifest=_build_manifest(),
        capabilities=[Shell()],
        model_settings=ModelSettings(tool_choice="required"),
    )


async def _read_text(sandbox: Any, path: str) -> str:
    handle = await sandbox.read(Path(path))
    try:
        payload = handle.read()
    finally:
        handle.close()
    if isinstance(payload, str):
        return payload
    return payload.decode("utf-8", errors="replace")


async def main(
    model: str,
    first_turn: str,
    second_turn: str,
) -> None:
    agent = _build_agent(model)
    client = UnixLocalSandboxClient()
    sandbox = await client.create(manifest=agent.default_manifest)

    run_config = RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        tracing_disabled=True,
        workflow_name="Unix-local PTY multiturn example",
    )

    try:
        async with sandbox:
            print("turn 1> running")
            first_result = await Runner.run(agent, first_turn, run_config=run_config)
            print(f"assistant 1> {first_result.final_output}")

            after_first = await _read_text(sandbox, "state/session_counter.txt")
            print(f"workspace after turn 1> {after_first.strip()}")

            second_input = first_result.to_input_list()
            second_input.append({"role": "user", "content": second_turn})

            print("turn 2> running")
            second_result = await Runner.run(agent, second_input, run_config=run_config)
            print(f"assistant 2> {second_result.final_output}")

            after_second = await _read_text(sandbox, "state/session_counter.txt")
            print(f"workspace after turn 2> {after_second.strip()}")

            expected = "turn-1: alpha\nturn-2: beta\n"
            if after_second != expected:
                raise RuntimeError(
                    "Expected the second turn to preserve and append to the first turn's file, "
                    f"got {after_second!r}"
                )
    finally:
        await client.delete(sandbox)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Run two agent turns against one Unix-local sandbox session and verify that the "
            "second turn sees the first turn's workspace file."
        )
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    parser.add_argument("--first-turn", default=FIRST_TURN, help="First user turn.")
    parser.add_argument("--second-turn", default=SECOND_TURN, help="Second user turn.")
    args = parser.parse_args()

    asyncio.run(main(args.model, args.first_turn, args.second_turn))
