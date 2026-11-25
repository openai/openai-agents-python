import json
import unittest

from agents_ticket_desk import ticket_desk
from agents_ticket_desk.__main__ import ExampleClient

from agents import Agent, RunConfig, Runner, RunState
from agents.testing import ScriptedModel, function_call


async def restore_for_user(agent, record, signed_in, revision):
    # Both fields in this record come from trusted application storage.
    owner, snapshot = record
    if not signed_in or signed_in != owner:
        raise PermissionError("Checkpoint owner mismatch")
    return await RunState.from_string(
        agent, snapshot, context_override=signed_in, plugin_revision=revision
    )


class TicketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = ExampleClient()
        self.config = RunConfig(tracing_disabled=True, plugin_revision="desk-v1")

    async def propose(self, user="alice", ticket="T-1"):
        call = function_call("close_ticket", {"ticket_id": ticket}, call_id="close-1")
        agent = Agent(
            "Desk",
            model=ScriptedModel([[call]]),
            plugins=[ticket_desk(self.client, owner=user)],
            tool_use_behavior="stop_on_first_tool",
        )
        result = await Runner.run(agent, "Close the ticket", context=user, run_config=self.config)
        (item,) = result.interruptions
        self.assertEqual(item.tool_name, "close_ticket")
        self.assertEqual(json.loads(item.arguments), {"ticket_id": ticket})
        self.assertNotIn(ticket, self.client.closed)
        return agent, result.to_state(), item

    async def resume(self, agent, state):
        return (await Runner.run(agent, state, run_config=self.config)).final_output

    async def test_approved_close(self):
        agent, state, item = await self.propose()
        state.approve(item)
        self.assertEqual(await self.resume(agent, state), "closed")
        self.assertEqual(self.client.closed, {"T-1"})

    async def test_rejected_close(self):
        agent, state, item = await self.propose()
        state.reject(item)
        await self.resume(agent, state)
        self.assertFalse(self.client.closed)

    async def test_revoked_grant_after_saved_approval(self):
        agent, state, item = await self.propose()
        state.approve(item)
        snapshot = state.to_string()
        self.client.grants.clear()
        restored = await RunState.from_string(
            agent, snapshot, context_override="alice", plugin_revision="desk-v1"
        )
        self.assertEqual(await self.resume(agent, restored), "denied")
        self.assertFalse(self.client.closed)

    async def test_changed_context_after_saved_approval(self):
        agent, state, item = await self.propose()
        state.approve(item)
        restored = await RunState.from_string(
            agent, state.to_string(), context_override="bob", plugin_revision="desk-v1"
        )
        self.assertEqual(await self.resume(agent, restored), "denied")
        self.assertFalse(self.client.closed)

    async def test_same_revision_cannot_replace_checkpoint_owner(self):
        agent, state, item = await self.propose()
        state.approve(item)
        record = ("alice", state.to_string())

        self.client.signed_in = "bob"
        self.client.grants.add(("bob", "T-1"))
        other, _, _ = await self.propose("bob")
        with self.assertRaises(PermissionError):
            await restore_for_user(other, record, self.client.signed_in, "desk-v1")
        self.assertFalse(self.client.closed)

        self.client.signed_in = "alice"
        restored = await restore_for_user(agent, record, self.client.signed_in, "desk-v1")
        self.assertEqual(await self.resume(agent, restored), "closed")

    async def test_revoked_grant_during_live_pause(self):
        agent, state, item = await self.propose()
        state.approve(item)
        self.client.grants.clear()
        self.assertEqual(await self.resume(agent, state), "denied")
        self.assertFalse(self.client.closed)

    async def test_changed_session_during_live_pause(self):
        agent, state, item = await self.propose()
        state.approve(item)
        self.client.signed_in = "bob"
        self.client.grants.add(("bob", "T-1"))
        self.assertEqual(await self.resume(agent, state), "denied")
        self.assertFalse(self.client.closed)

    async def test_users_with_the_same_call_id(self):
        self.client.grants.add(("bob", "T-2"))
        agent, state, item = await self.propose()
        state.approve(item)
        self.client.signed_in = "bob"
        other, pending, approval = await self.propose("bob")
        pending.approve(approval)
        self.assertEqual(await self.resume(other, pending), "denied")
        other, pending, approval = await self.propose("bob", "T-2")
        pending.approve(approval)
        self.assertEqual(await self.resume(other, pending), "closed")
        self.assertEqual(self.client.closed, {"T-2"})
        self.client.signed_in = "alice"
        self.assertEqual(await self.resume(agent, state), "closed")
        self.assertEqual(self.client.closed, {"T-1", "T-2"})
