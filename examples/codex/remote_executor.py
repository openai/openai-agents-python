"""Connect the current SDK to one pre-provisioned Linux executor.

Install the SDK preview wheel following docs/sdk/installation.md and use example
files from the same release. Run from the root of that release's source archive.

This example supports a Linux executor running the matching Codex exec-server
behind an authenticated WSS endpoint. The application provisions the executor
and its workspace. The wheel provides the SDK client, not a sandbox service.
Set EXECUTOR_URL (wss://...), EXECUTOR_CWD (absolute remote workspace path),
EXECUTOR_TOKEN and OPENAI_API_KEY through your normal credential injection.
Set SSL_CERT_FILE to your executor CA if it uses a private certificate.
Optionally set EXECUTOR_CAPABILITIES to one absolute remote capability directory
and OPENAI_BASE_URL for your configured model provider. Run:
    PYTHONPATH=. python -m examples.codex.remote_executor "Inspect the project and list the next steps"

The executor owns file and process isolation. Do not assign a host workspace
or an executor shared with an untrusted tenant. Close the Session before the
application deprovisions the executor. No workspace, endpoint or credential
is recovered from a saved checklist.
"""

import asyncio
import os
import sys

from agents import Agent, Checklist, Environment, NativeProvider


async def main(task: str) -> None:
    capability_root = os.getenv("EXECUTOR_CAPABILITIES")
    environment = Environment(
        url=os.environ["EXECUTOR_URL"],
        cwd=os.environ["EXECUTOR_CWD"],
        headers={"Authorization": "Bearer " + os.environ["EXECUTOR_TOKEN"]},
        tools=("exec_command", "write_stdin", "apply_patch"),
        capability_directories=() if capability_root is None else (capability_root,),
    )
    async with NativeProvider(
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=os.environ["OPENAI_API_KEY"],
        supports_websockets=False,
    ) as provider:
        agent = Agent(
            instructions="Inspect the assigned workspace. Record your progress in the checklist.",
            model="gpt-5.5",
            tools=[Checklist()],
            provider=provider,
        )
        async with agent.session(environment=environment) as session:
            result = await (await session.submit(task)).get_result()
            print(result.final_output)
            checklist = await session.checklist()
            if checklist is not None:
                for entry in checklist.plan:
                    print(f"{entry.status}: {entry.step}")


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "Inspect the assigned workspace"))
