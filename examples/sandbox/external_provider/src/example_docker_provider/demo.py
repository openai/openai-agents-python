"""Run a real container through an installed external client without a model API call."""

import argparse
import asyncio

import docker

from agents import RunConfig, Runner
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.sandboxes.docker import DockerSandboxClient
from agents.testing import ScriptedModel, assistant_message, function_call

from . import ExampleDockerClient, ExampleDockerOptions


async def run(image: str) -> None:
    connection = docker.from_env()
    try:
        client = ExampleDockerClient(DockerSandboxClient(connection))
        model = ScriptedModel(
            [
                [
                    function_call(
                        "exec_command",
                        {"cmd": "printf 'external provider works\\n'"},
                        call_id="example-command",
                    )
                ],
                [assistant_message("Demo complete.")],
            ]
        )
        agent = SandboxAgent(name="External provider demo", model=model, capabilities=[Shell()])
        result = await Runner.run(
            agent,
            "Run the example command.",
            run_config=RunConfig(
                tracing_disabled=True,
                sandbox=SandboxRunConfig(client=client, options=ExampleDockerOptions(image=image)),
            ),
        )
        model.assert_complete()
        # Inspect the actual tool output rather than trusting the scripted final response.
        outputs = [item.output for item in result.new_items if item.type == "tool_call_output_item"]
        if not any("external provider works" in str(output) for output in outputs):
            raise RuntimeError(f"The container command did not succeed: {outputs!r}")
        print("external provider works")
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.14-slim")
    args = parser.parse_args()
    asyncio.run(run(args.image))


if __name__ == "__main__":
    main()
