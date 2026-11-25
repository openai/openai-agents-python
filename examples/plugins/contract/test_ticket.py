"""Test the installed ticket desk. Pass --codex for native calls to a loopback server."""

import argparse
import json
import os
import unittest
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from agents_ticket_desk import ticket_desk
from plugin_contract import expect_tool_output

from agents import Agent, Runner

OWNER = "tenant-a/alice"
OTHER = "tenant-b/alice"
TICKET = "T-1"


class Desk:
    def __init__(self):
        self.grants = {(OWNER, TICKET), (OTHER, TICKET)}
        self.attempts = []
        self.writes = []

    async def close_if_allowed(self, user: str, ticket_id: str) -> bool:
        key = (user, ticket_id)
        self.attempts.append(key)
        if key not in self.grants:
            return False
        self.writes.append(key)
        return True


class TicketContract(unittest.IsolatedAsyncioTestCase):
    codex_workspace: Path | None = None

    async def test_close(self):
        owner_write = [(OWNER, TICKET)]
        for case, output, attempts, writes in [
            ("approved", "closed", owner_write, owner_write),
            ("rejected", "declined", [], []),
            ("revoked", "denied", owner_write, []),
            ("other tenant", "denied", [], []),
        ]:
            with self.subTest(case=case):
                desk = Desk()
                arguments = {"ticket_id": TICKET}
                async with expect_tool_output(
                    "close_ticket", arguments, output, codex_workspace=self.codex_workspace
                ) as (model, config):
                    agent = Agent("Desk", model=model, plugins=[ticket_desk(desk, owner=OWNER)])
                    context = OTHER if case == "other tenant" else OWNER
                    result = await Runner.run(
                        agent, "Close the ticket", context=context, run_config=config
                    )
                    (pending,) = result.interruptions
                    self.assertEqual(pending.name, "close_ticket")
                    self.assertEqual(pending.call_id, "call_0")
                    self.assertEqual(json.loads(pending.arguments), arguments)
                    self.assertEqual(desk.attempts, [])
                    self.assertEqual(desk.writes, [])
                    state = result.to_state()
                    if case == "rejected":
                        state.reject(pending, rejection_message="declined")
                    else:
                        state.approve(pending)
                    if case == "revoked":
                        desk.grants.remove((OWNER, TICKET))
                    result = await Runner.run(agent, state, run_config=config)
                    self.assertFalse(result.interruptions)
                    self.assertEqual(result.final_output, "done")
                    self.assertEqual(desk.attempts, attempts)
                    self.assertEqual(desk.writes, writes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true")
    args = parser.parse_args()
    backend = "Codex (loopback)" if args.codex else "Python (ScriptedModel)"
    print(
        f"{backend}, openai-agents {version('openai-agents')}, "
        f"agents-ticket-desk {version('agents-ticket-desk')}"
    )
    with TemporaryDirectory() as tmp:
        if args.codex:
            os.environ["CODEX_HOME"] = tmp
            TicketContract.codex_workspace = Path(tmp)
        unittest.main(argv=[__file__], failfast=True)
