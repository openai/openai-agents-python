"""Read a temporary fixture through native Codex workspace tools.

Run with the matching native wheel and saved Codex login:
    python examples/codex/native_workspace.py

This example file is in development source; its public APIs support alpha.1.
It sends live model requests and uses the host's native sandbox. It registers
no SDK shell executor or Python function tools.
"""

import argparse
import asyncio
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory

from agents import Agent, RunConfig, Runner
from agents.models.codex import CodexProvider


async def main(model: str | None = None) -> None:
    with TemporaryDirectory(prefix="codex-workspace-") as directory:
        workspace = Path(directory).resolve()
        marker = f"fixture-{secrets.token_hex(8)}"
        fixture = workspace / "facts.txt"
        fixture.write_text(marker + "\n", encoding="utf-8")
        provider = CodexProvider(
            cwd=workspace,
            config_overrides=('approval_policy="never"', 'sandbox_mode="read-only"'),
        )
        try:
            agent = Agent(
                name="Workspace reader",
                instructions=(
                    "Use native exec_command to read facts.txt in the current workspace. "
                    "Return only its exact text. Do not modify files or use other tools."
                ),
            )
            result = await Runner.run(
                agent,
                "Read the local fixture and return its contents.",
                run_config=RunConfig(model=model, model_provider=provider, tracing_disabled=True),
                max_turns=3,
            )
            if str(result.final_output).strip() != marker:
                raise RuntimeError("The agent did not return the local fixture's contents.")
            if fixture.read_text(encoding="utf-8") != marker + "\n":
                raise RuntimeError("The fixture changed during the read-only run.")
            print("Native workspace read passed; the temporary fixture will be removed.")
        finally:
            await provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Override the Codex model from your configuration")
    args = parser.parse_args()
    asyncio.run(main(args.model))
