"""Use only with an independently offline, job-private Linux or macOS executor.

Install the SDK preview wheel following docs/sdk/installation.md and use example
files from the same release. Run from the root of that release's source archive:
    PYTHONPATH=. python -m examples.codex.offline_unix_sockets

The backend must put the exec-server, proxy and DNS resolver inside the offline
boundary. Exclude host sockets, even on read-only mounts, and close inherited
connections. This example grants access to every reachable Unix socket. Linux
executors require this grant for any explicit network policy. The macOS proxy
can also send HTTP to these sockets. Workspace write restrictions still apply.
The SDK cannot verify the executor's network or mounts.

Point EXECUTOR_URL at the job's authenticated WSS gateway and EXECUTOR_CWD at
its writable workspace. Set EXECUTOR_TOKEN through your normal credential
injection for that gateway. Set RESPONSES_FIXTURE_URL to your local synthetic
Responses peer. Run the application outside the job.
"""

import asyncio
import os

from agents import Agent, Environment, NativeProvider, NetworkPolicy


async def main() -> None:
    environment = Environment(
        url=os.environ["EXECUTOR_URL"],
        cwd=os.environ["EXECUTOR_CWD"],
        headers={"Authorization": "Bearer " + os.environ["EXECUTOR_TOKEN"]},
        tools=["exec_command"],
        network=NetworkPolicy(allow_all_unix_sockets=True),
    )
    async with NativeProvider(
        base_url=os.environ["RESPONSES_FIXTURE_URL"], api_key="fixture"
    ) as provider:
        agent = Agent(instructions="Use the selected job for commands.", provider=provider)
        async with agent.session(environment=environment) as session:
            run = await session.submit(
                "Use Python to start a Unix socket server in the workspace and exchange "
                "a request and reply with a second process. Report the reply."
            )
            print((await run.get_result()).final_output)


if __name__ == "__main__":
    asyncio.run(main())
