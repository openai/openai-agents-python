"""Read caller-owned data with a Linux native storage mount."""

import asyncio
import sys
from pathlib import Path

from agents import Agent, Environment, LocalStorageMount


async def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python main.py EXISTING_WORKSPACE EXISTING_DATA_DIRECTORY")
    workspace, data = (Path(value).resolve(strict=True) for value in sys.argv[1:])
    environment = Environment.local(
        cwd=workspace,
        mounts=[LocalStorageMount(data, data, strategy="local_bind")],
        tools=["exec_command"],
    )
    agent = Agent(instructions="Inspect the mounted data. Write results only in the workspace.")
    async with agent.session(environment=environment) as session:
        turn = await session.submit(f"List the files in {data} and describe their names.")
        print((await turn.get_result()).final_output)


if __name__ == "__main__":
    asyncio.run(main())
