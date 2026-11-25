"""Check A2A recovery and revocation through an installed SDK and a local model."""

import argparse
import asyncio
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from a2a.types import Task, TaskState
from agents_permissions import permissions
from agents_specialist import DurableCall, Receipt, specialist
from contract.loopback import MODEL, route

from agents import Agent, RunConfig, Runner, RunState

OWNER = "fixture-owner"
ENDPOINT = "https://specialist.invalid"
QUESTION = "Is release R42 ready?"
REVISION = "release-review-v1"


class Peer:
    """Keep the receiver alive while the application rebuilds its client and plugins."""

    def __init__(self):
        self.effects = []
        self.receipts = {}
        self.allowed = True

    async def lookup(self, owner, endpoint, message_id):
        self.effects.append("lookup")
        if not self.allowed or owner != OWNER or endpoint != ENDPOINT:
            return None
        return self.receipts.get(message_id)


class Client:
    """Simulate a partner-owned client that loses the reply after the receiver commits."""

    rpc_url = ENDPOINT
    card_json = b"{}"

    def __init__(self, peer):
        self.peer = peer

    def rpc_token_provider(self):
        if not self.peer.allowed:
            raise PermissionError
        return "fixture"

    async def send_message(self, text, *, message_id):
        self.peer.effects.append("send")
        self.rpc_token_provider()
        self.peer.receipts[message_id] = Receipt(
            OWNER, ENDPOINT, message_id, hashlib.sha256(text.encode()).hexdigest(), "T1", "C1"
        )
        raise ConnectionError("Reply lost after commit")

    async def get_task(self, *, task_id, context_id):
        self.peer.effects.append(("read", task_id, context_id))
        self.rpc_token_provider()
        task = Task(id=task_id, context_id=context_id)
        task.status.state = TaskState.TASK_STATE_COMPLETED
        task.artifacts.add(artifact_id="readiness").parts.add(text="ready")
        return task


async def scenario(codex: bool, workspace: Path, revoke: bool) -> None:
    peer = Peer()

    def manager():
        operation = DurableCall(
            owner=OWNER,
            operation_id="release-R42",
            db_path=workspace / "calls.sqlite",
            lookup_receipt=peer.lookup,
        )
        check = specialist(
            Client(peer),
            question=QUESTION,
            artifact_id="readiness",
            verdicts=("ready", "blocked"),
            durable=operation,
        )
        return Agent(
            "Release manager",
            model=MODEL,
            plugins=[check, permissions()],
            tool_use_behavior="stop_on_first_tool",
        )

    def resolve(agent, policy, call, owner):
        if (
            owner == OWNER
            and policy == "permissions"
            and call.owner == "specialist"
            and peer.allowed
        ):
            return {"decision": "allow"}
        return {}

    requests = []
    calls = [("ask_specialist", {}), ("refresh_specialist", {})]
    async with route(codex, workspace, requests, calls=calls) as provider:
        config = RunConfig(
            model_provider=provider,
            plugin_revision=REVISION,
            policy_context=resolve,
            tracing_disabled=True,
        )
        agent = manager()
        proposed = await Runner.run(agent, "Ask once", context=OWNER, run_config=config)
        (send,) = proposed.interruptions
        assert send.name == "ask_specialist" and not peer.effects
        state = proposed.to_state()
        state.approve(send)
        sent = await Runner.run(agent, state, run_config=config)
        assert sent.final_output == "unknown" and peer.effects == ["send"]

        proposed = await Runner.run(
            agent, "Check the previous attempt", context=OWNER, run_config=config
        )
        (read,) = proposed.interruptions
        assert read.name == "refresh_specialist" and peer.effects == ["send"]
        state = proposed.to_state()
        state.approve(read)
        snapshot = state.to_string()

        # A real host authenticates the snapshot and its owner before restoring it.
        agent = manager()
        peer.allowed = not revoke
        restored = await RunState.from_string(
            agent, snapshot, context_override=OWNER, plugin_revision=REVISION
        )
        result = await Runner.run(agent, restored, run_config=config)
        assert len(requests) == 2
        if revoke:
            assert result.final_output == "Tool call refused." and peer.effects == ["send"]
        else:
            assert result.final_output == "ready"
            assert peer.effects == ["send", "lookup", ("read", "T1", "C1")]
        print("codex" if codex else "python", result.final_output, peer.effects)


async def main(codex: bool) -> None:
    for revoke in (False, True):
        with TemporaryDirectory() as tmp:
            os.environ["CODEX_HOME"] = tmp
            await scenario(codex, Path(tmp), revoke)


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Run this check without Python optimization.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true")
    asyncio.run(asyncio.wait_for(main(parser.parse_args().codex), 60))
