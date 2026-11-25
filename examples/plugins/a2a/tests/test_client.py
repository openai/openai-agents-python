"""Check specialist replies and receipts without the SDK's optional A2A transport."""

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from a2a.types import Artifact, Part, Task, TaskState
from agents_specialist import DurableCall, Receipt, specialist

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, function_call


class TaskClient:
    """Supply peer-selected artifacts and simulate a mismatched receipt context."""

    card_json = b"{}"
    rpc_url = "https://specialist.invalid/rpc"
    receipt = None

    def __init__(self, artifacts, *, reply_context="context"):
        self.artifacts = artifacts
        self.reply_context = reply_context

    def rpc_token_provider(self):
        return "synthetic-token"

    async def send_message(self, text, *, message_id):
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.receipt = Receipt("pat", self.rpc_url, message_id, digest, "task", "context")
        return await self.get_task(task_id="task", context_id=self.reply_context)

    async def get_task(self, *, task_id, context_id):
        task = Task(id=task_id, context_id=context_id, artifacts=self.artifacts)
        task.status.state = TaskState.TASK_STATE_COMPLETED
        return task

    async def lookup(self, owner, endpoint, message_id):
        return self.receipt


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_artifact_selection(self):
        available = Part(text="available")
        reply = Artifact(artifact_id=str(uuid4()), parts=[available])
        other = Artifact(artifact_id="other", parts=[available])
        multipart = Artifact(artifact_id="other", parts=[available, available])
        rejected = Artifact(artifact_id="other", parts=[Part(text="private details")])
        for case, artifact_id, artifacts, expected in [
            ("peer-selected ID", None, [reply], "available"),
            ("exact ID", reply.artifact_id, [reply], "available"),
            ("wrong ID", "inventory", [reply], "unknown"),
            ("empty ID", None, [Artifact(parts=[available])], "unknown"),
            ("multiple artifacts", None, [reply, other], "unknown"),
            ("multiple parts", None, [multipart], "unknown"),
            ("unlisted verdict", None, [rejected], "unknown"),
        ]:
            with self.subTest(case=case):
                plugin = specialist(
                    TaskClient(artifacts),
                    question="Check stock",
                    artifact_id=artifact_id,
                    verdicts=("available",),
                )
                await self.check_outputs(plugin, {"ask_specialist": expected})

    def test_empty_configured_id_is_invalid(self):
        with self.assertRaises(ValueError):
            specialist(TaskClient([]), question="Check stock", artifact_id="", verdicts=("ok",))

    async def test_answer_must_match_receipt_context(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        reply = Artifact(artifact_id=str(uuid4()), parts=[Part(text="available")])
        client = TaskClient([reply], reply_context="unrelated-context")
        plugin = specialist(
            client,
            question="Check stock",
            artifact_id=None,
            verdicts=("available",),
            durable=DurableCall(
                owner="pat",
                operation_id="stock-42",
                db_path=Path(directory.name) / "calls.db",
                lookup_receipt=client.lookup,
            ),
        )
        await self.check_outputs(
            plugin, {"ask_specialist": "unknown", "refresh_specialist": "available"}
        )

    async def check_outputs(self, plugin, operations):
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
