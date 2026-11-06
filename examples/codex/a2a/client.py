"""Call a configured remote specialist through the official A2A client.

Run with --card-url, --rpc-url and --question. Supply A2A_BEARER_TOKEN through
trusted host configuration. Endpoint URLs never come from a model or Agent Card.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from a2a.helpers.proto_helpers import new_text_message
from a2a.types import a2a_pb2 as p

from agents.a2a import A2AEndpoint, connect_a2a


async def ask(endpoint: A2AEndpoint, question: str) -> str:
    async def authorization() -> str:
        return os.environ["A2A_BEARER_TOKEN"]

    async with connect_a2a(endpoint, authorization=authorization) as client:
        # Returning early only stops observation. It does not cancel this task.
        async for event in client.send_message(
            p.SendMessageRequest(
                message=new_text_message(question, role=p.ROLE_USER),
                configuration=p.SendMessageConfiguration(return_immediately=True),
            )
        ):
            if event.HasField("task"):
                receipt = event.task.id
                break
        else:
            raise RuntimeError("The specialist did not acknowledge a task")
        # The application owns scheduling. Use the authoritative task snapshot,
        # rather than concatenating artifact chunks after reconnecting.
        for _ in range(60):
            task = await client.get_task(p.GetTaskRequest(id=receipt))
            if task.status.state == p.TASK_STATE_COMPLETED:
                results = [
                    part.text
                    for artifact in task.artifacts
                    if artifact.name == "Validated result"
                    for part in artifact.parts
                    if part.HasField("text")
                ]
                if len(results) != 1:
                    raise RuntimeError("Expected one validated text result")
                return results[0]
            if task.status.state not in {p.TASK_STATE_SUBMITTED, p.TASK_STATE_WORKING}:
                raise RuntimeError("The task needs host input, authentication or failure handling")
            await asyncio.sleep(1)
    raise TimeoutError("Observation deadline reached; remote work may still be running")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-url", required=True)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--question", required=True)
    args = parser.parse_args()
    endpoint = A2AEndpoint(
        name="configured-specialist", card_url=args.card_url, rpc_url=args.rpc_url
    )
    print(asyncio.run(ask(endpoint, args.question)))


if __name__ == "__main__":
    main()
