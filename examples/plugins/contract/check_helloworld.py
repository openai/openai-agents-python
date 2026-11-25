"""Test the upstream A2A Hello World server on 127.0.0.1:9999."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from agents_specialist import specialist
from plugin_contract import expect_tool_output

from agents import Agent, Runner
from agents.extensions.a2a import A2AClient

QUESTION = "ping"
ECHO = "Hello, World! I have received your request (ping)"


def effects(server_log: Path) -> tuple[int, int]:
    text = server_log.read_text()
    return text.count('"POST '), text.splitlines().count(f"Result:  {ECHO}")


async def check(card: bytes, server_log: Path, workspace: Path | None) -> None:
    # The upstream sample runs without authentication. Never send a real token.
    client = A2AClient(
        rpc_url="http://127.0.0.1:9999",
        card_json=card,
        rpc_token_provider=lambda: "local-test-only",
    )
    for approve in (False, True):
        plugin = specialist(client, question=QUESTION, artifact_id=None, verdicts=(ECHO,))
        expected = ECHO if approve else "declined"
        posts, echoes = effects(server_log)
        async with expect_tool_output(
            "ask_specialist", {}, expected, codex_workspace=workspace
        ) as (model, config):
            agent = Agent("Checker", model=model, plugins=[plugin])
            run = await Runner.run(agent, "Ask the specialist", run_config=config)
            (pending,) = run.interruptions
            assert pending.name == "ask_specialist" and json.loads(pending.arguments) == {}
            assert effects(server_log) == (posts, echoes)
            state = run.to_state()
            if approve:
                state.approve(pending)
            else:
                state.reject(pending, rejection_message="declined")
            result = await Runner.run(agent, state, run_config=config)
            assert not result.interruptions and result.final_output == "done"
        delta = int(approve)
        assert effects(server_log) == (posts + delta, echoes + delta)
        print(f"{expected} ({delta} POST, {delta} echo)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("card", type=Path, help="Saved public Agent Card")
    parser.add_argument("server_log", type=Path, help="Unbuffered output from the sample server")
    parser.add_argument("--codex", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory() as tmp:
        if args.codex:
            os.environ["CODEX_HOME"] = tmp
        workspace = Path(tmp) if args.codex else None
        asyncio.run(check(args.card.read_bytes(), args.server_log, workspace))
