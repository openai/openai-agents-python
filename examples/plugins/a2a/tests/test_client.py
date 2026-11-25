"""Check receipt validation without the SDK's optional A2A transport."""

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from a2a.types import Task, TaskState
from agents_specialist import DurableCall, Receipt, specialist

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, function_call


class SwappedTaskClient:
    """Simulate an adapter handing back an unrelated, otherwise valid verdict."""

    card_json = b"{}"
    rpc_url = "https://specialist.invalid/rpc"
    receipt = None

    def rpc_token_provider(self):
        return "synthetic-token"

    async def send_message(self, text, *, message_id):
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.receipt = Receipt("pat", self.rpc_url, message_id, digest, "task", "context")
        return await self.get_task(task_id="task", context_id="unrelated-context")

    async def get_task(self, *, task_id, context_id):
        task = Task(id=task_id, context_id=context_id)
        task.status.state = TaskState.TASK_STATE_COMPLETED
        task.artifacts.add(artifact_id="inventory").parts.add(text="available")
        return task

    async def lookup(self, owner, endpoint, message_id):
        return self.receipt


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_must_match_receipt_context(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        client = SwappedTaskClient()
        plugin = specialist(
            client,
            question="Check stock",
            artifact_id="inventory",
            verdicts=("available",),
            durable=DurableCall(
                owner="pat",
                operation_id="stock-42",
                db_path=Path(directory.name) / "calls.db",
                lookup_receipt=client.lookup,
            ),
        )
        operations = {"ask_specialist": "unknown", "refresh_specialist": "available"}
        agent = Agent(
            "Manager",
            plugins=[plugin],
            model=ScriptedModel([[function_call(op, {}, call_id=op)] for op in operations]),
            tool_use_behavior="stop_on_first_tool",
        )
        config = RunConfig(tracing_disabled=True)
        for op, expected in operations.items():
            step = await Runner.run(agent, op, run_config=config)
            (call,) = step.interruptions
            state = step.to_state()
            state.approve(call)
            result = await Runner.run(agent, state, run_config=config)
            self.assertEqual(result.final_output, expected)
