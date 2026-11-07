"""Run Python in an existing provider container and inspect artifact citations."""

import asyncio
import os

from agents import Agent, CodeInterpreter


async def main() -> None:
    agent = Agent(
        instructions="Use Code Interpreter for calculations and provide a downloadable CSV.",
        model="gpt-5.2",
        tools=[CodeInterpreter(container=os.environ["OPENAI_CONTAINER_ID"])],
    )
    async with agent.session() as session:
        turn = await session.submit(
            "Compute the squares of 1 through 5 and save them as squares.csv."
        )
        print((await turn.get_result()).final_output)
        async for item in session.items():
            if item["type"] == "code_interpreter_call":
                print("Container:", item["container_id"], "Status:", item["status"])
                print("Code:", item["code"])
                print("Outputs:", item["outputs"])
            elif item["type"] == "message" and item["role"] == "assistant":
                for part in item["content"]:
                    annotations = part.get("annotations")
                    if isinstance(annotations, list):
                        for citation in annotations:
                            if citation.get("type") == "container_file_citation":
                                print(
                                    "Artifact:",
                                    citation["container_id"],
                                    citation["file_id"],
                                    citation["filename"],
                                )


if __name__ == "__main__":
    asyncio.run(main())
