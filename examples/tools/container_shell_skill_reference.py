import argparse
import asyncio
import os

from openai.types.responses import ResponseFunctionShellToolCall
from openai.types.responses.response_container_reference import ResponseContainerReference

from agents import Agent, Runner, ShellTool, ShellToolSkillReference, trace
from agents.items import ModelResponse

SHELL_SKILL_ID_ENV = "OPENAI_SHELL_SKILL_ID"
SHELL_SKILL_VERSION_ENV = "OPENAI_SHELL_SKILL_VERSION"
DEFAULT_SKILL_REFERENCE: ShellToolSkillReference = {
    "type": "skill_reference",
    "skill_id": "skill_698bbe879adc81918725cbc69dcae7960bc5613dadaed377",
    "version": "1",
}


def resolve_skill_reference() -> ShellToolSkillReference:
    skill_id = os.environ.get(SHELL_SKILL_ID_ENV)
    if not skill_id:
        return DEFAULT_SKILL_REFERENCE

    reference: ShellToolSkillReference = {"type": "skill_reference", "skill_id": skill_id}
    skill_version = os.environ.get(SHELL_SKILL_VERSION_ENV)
    if skill_version:
        reference["version"] = skill_version
    return reference


def extract_container_id(raw_responses: list[ModelResponse]) -> str | None:
    for response in raw_responses:
        for item in response.output:
            if isinstance(item, ResponseFunctionShellToolCall) and isinstance(
                item.environment, ResponseContainerReference
            ):
                return item.environment.container_id

    return None


async def main(model: str) -> None:
    skill_reference = resolve_skill_reference()
    print(
        "[info] Using skill reference:",
        skill_reference["skill_id"],
        f"(version {skill_reference.get('version', 'default')})",
    )

    with trace("container_shell_skill_reference_example"):
        agent1 = Agent(
            name="Container Shell Agent (Skill Reference)",
            model=model,
            instructions="Use the available container skill to answer user requests.",
            tools=[
                ShellTool(
                    environment={
                        "type": "container_auto",
                        "network_policy": {"type": "disabled"},
                        "skills": [skill_reference],
                    }
                )
            ],
        )

        result1 = await Runner.run(
            agent1,
            (
                "Use the csv-workbench skill. Create /mnt/data/orders.csv with columns "
                "id,region,amount,status and at least 6 rows. Then report total amount by "
                "region and count failed orders."
            ),
        )
        print(f"Agent: {result1.final_output}")

        container_id = extract_container_id(result1.raw_responses)
        if not container_id:
            raise RuntimeError("Container ID was not returned in shell call output.")

        print(f"[info] Reusing container_id={container_id}")

        agent2 = Agent(
            name="Container Reference Shell Agent",
            model=model,
            instructions="Reuse the existing shell container and answer concisely.",
            tools=[
                ShellTool(
                    environment={
                        "type": "container_reference",
                        "container_id": container_id,
                    }
                )
            ],
        )

        result2 = await Runner.run(
            agent2,
            "Run `ls -la /mnt/data`, then summarize in one sentence.",
        )
        print(f"Agent (container reuse): {result2.final_output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default="gpt-5.2",
        help="Model name to use.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.model))
