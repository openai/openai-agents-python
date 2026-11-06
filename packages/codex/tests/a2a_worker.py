"""Fresh-process synthetic native continuation and publication fault worker."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel

from agents import Agent, FunctionTool
from agents.a2a import NativeA2AAgent, NativeA2ABinding, NativeA2AExecutor
from agents.testing import ScriptedResponses, text, tool_call
from tests.a2a_helpers import HostStore, call_context, invoke, request_context


class Approval(BaseModel):
    reason: str


async def main():
    directory, mode = Path(sys.argv[1]), sys.argv[2]
    store = HostStore(directory)
    tool = FunctionTool(
        name="approve", description="Host approval", input_type=Approval, output_type=str
    )
    replies = (
        [[tool_call("approve", {"reason": "synthetic"}, call_id="call-a")]]
        if mode == "wait"
        else [[text("fresh worker result")]]
        if mode == "reply"
        else []
    )
    async with ScriptedResponses(*replies) as peer:
        factory_calls = []

        def factory(binding, context):
            factory_calls.append(binding)
            return NativeA2AAgent(
                agent=Agent(
                    instructions="Use the synthetic response", tools=[tool], provider=peer.provider
                )
            )

        executor = NativeA2AExecutor(agent_factory=factory, store=store)
        binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
        if mode == "recover-first":
            with store.db() as db:
                payload = json.loads(
                    db.execute("SELECT payload FROM private LIMIT 1").fetchone()[0]
                )
            binding = NativeA2ABinding(**payload["binding"])
        if mode == "wait":
            stored, _ = await invoke(executor, request_context())
            proposal = MessageToDict(stored.task.status.message.parts[0].data)
            print(
                json.dumps(
                    {"pid": os.getpid(), "proposal": proposal, "state": store.read(binding).status}
                ),
                flush=True,
            )
            await asyncio.Event().wait()
        elif mode == "reply":
            record = store.read(binding)
            reply = {
                "challenge": record.challenge,
                "generation": record.generation,
                "result": "yes",
            }
            current = await store.get(binding.task_id, call_context())
            stored, _ = await invoke(executor, request_context(reply=reply, current=current.task))
            print(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "state": store.read(binding).status,
                        "result": stored.task.artifacts[0].parts[0].text,
                    }
                ),
                flush=True,
            )
        else:
            record = await executor.recover(binding, call_context())
            stored = await store.get(binding.task_id, call_context())
            print(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "state": record.status,
                        "snapshot_matches": record.acknowledges(stored.task),
                        "factory_calls": len(factory_calls),
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
