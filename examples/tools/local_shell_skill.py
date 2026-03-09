import argparse
import asyncio
from pathlib import Path

from agents import Agent, Runner, ShellTool, ShellToolLocalSkill, trace
from examples.tools.shell import ShellExecutor

SKILL_NAME = "csv-workbench"
SKILL_DIR = Path(__file__).resolve().parent / "skills" / SKILL_NAME


def build_local_skill() -> ShellToolLocalSkill:
    return {
        "name": SKILL_NAME,
        "description": "Analyze CSV files and return concise numeric summaries.",
        "path": str(SKILL_DIR),
    }


async def main(model: str) -> None:
    local_skill = build_local_skill()

    with trace("local_shell_skill_example"):
        agent1 = Agent(
            name="Local Shell Agent (Local Skill)",
            model=model,
            instructions="Use the available local skill to answer user requests.",
            tools=[
                ShellTool(
                    environment={
                        "type": "local",
                        "skills": [local_skill],
                    },
                    executor=ShellExecutor(),
                )
            ],
        )

        result1 = await Runner.run(
            agent1,
            (
                "Use the csv-workbench skill. Create /tmp/test_orders.csv with columns "
                "id,region,amount,status and at least 6 rows. Then report total amount by "
                "region and count failed orders."
            ),
        )
        print(f"Agent: {result1.final_output}")

        agent2 = Agent(
            name="Local Shell Agent (Reuse)",
            model=model,
            instructions="Reuse the existing local shell and answer concisely.",
            tools=[
                ShellTool(
                    environment={
                        "type": "local",
                    },
                    executor=ShellExecutor(),
                )
            ],
        )

        result2 = await Runner.run(
            agent2,
            "Run `ls -la /tmp/test_orders.csv`, then summarize in one sentence.",
        )
        print(f"Agent (reuse): {result2.final_output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default="gpt-5.4",
        help="Model name to use.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.model))
