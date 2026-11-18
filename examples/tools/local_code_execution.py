"""Stream local computation with the default provider or stock Codex.

UnixLocalSandboxClient runs generated commands as the current user. Use this
example only on a trusted, disposable development host.
"""

import argparse
import asyncio
import math
import os
import re

from pydantic import BaseModel

from agents import ModelSettings, RunConfig, Runner
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient


class Answer(BaseModel):
    answer: float
    code: str


async def main(use_codex: bool) -> None:
    provider = None
    if use_codex:
        from agents.models.codex import CodexProvider

        provider = CodexProvider()
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest(entries={}))
    agent = SandboxAgent(
        name="Local math tutor",
        model=os.environ.get("CODEX_MODEL" if use_codex else "OPENAI_MODEL"),
        instructions=(
            "Use the SDK exec_command tool to execute Python 3 code for every numeric "
            "calculation. Print the result to at least 8 decimal places. Return the computed "
            "answer and the Python code you ran."
        ),
        capabilities=[Shell()],
        model_settings=ModelSettings(tool_choice="required"),
        output_type=Answer,
    )
    config = RunConfig(sandbox=SandboxRunConfig(session=session))
    if provider is not None:
        config.model_provider = provider
    try:
        async with session:
            result = Runner.run_streamed(
                agent,
                "Calculate the square root of 273 * 312821 + 1782.",
                run_config=config,
            )
            async for event in result.stream_events():
                if event.type == "run_item_stream_event":
                    print(f"[{event.name}]")
            calls = [item for item in result.new_items if item.type == "tool_call_item"]
            outputs = [
                item.output for item in result.new_items if item.type == "tool_call_output_item"
            ]
            numbers = re.findall(r"\d+\.\d+", str(outputs))
            expected = math.sqrt(273 * 312821 + 1782)
            answer = result.final_output_as(Answer)
            if (
                not any(getattr(item.raw_item, "name", None) == "exec_command" for item in calls)
                or not any(abs(float(value) - expected) < 1e-6 for value in numbers)
                or not math.isclose(answer.answer, expected, rel_tol=0, abs_tol=1e-6)
            ):
                raise RuntimeError(
                    "Expected a real SDK command, matching output, and final answer."
                )
            print(answer.model_dump_json(indent=2))
            print("Verified local computation.")
    finally:
        await client.delete(session)
        if provider is not None:
            await provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true")
    asyncio.run(main(parser.parse_args().codex))
