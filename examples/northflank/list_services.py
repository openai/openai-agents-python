"""Minimal example: ask an agent to list services in a Northflank project.

Run::

    OPENAI_API_KEY=... NF_API_TOKEN=... \\
        python examples/northflank/list_services.py demo-app
"""

from __future__ import annotations

import asyncio
import sys

from northflank import AsyncApiClient

from agents import Agent, Runner
from agents.extensions.northflank import NorthflankCtx, northflank_tools


async def main(project_id: str) -> None:
    client = AsyncApiClient()

    agent = Agent(
        name="northflank-ops",
        instructions=(
            "You manage Northflank services on the user's behalf. "
            "When the user asks about services, call nf_list_services and "
            "summarise the result in two or three sentences."
        ),
        tools=list(northflank_tools()),
    )

    result = await Runner.run(
        agent,
        f"Which services are running in project {project_id}? Group them by status.",
        context=NorthflankCtx(client=client, project_id=project_id),
    )
    print(result.final_output)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <project_id>")
    asyncio.run(main(sys.argv[1]))
