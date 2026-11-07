"""Search an existing, indexed vector store and inspect retained evidence."""

import asyncio
import os

from agents import Agent, FileSearch


async def main() -> None:
    agent = Agent(
        instructions="Search the supplied store. Cite the files that support your answer.",
        model="gpt-5.2",
        tools=[
            FileSearch(
                vector_store_ids=[os.environ["OPENAI_VECTOR_STORE_ID"]],
                max_num_results=3,
                include_search_results=True,
            )
        ],
    )
    async with agent.session() as session:
        turn = await session.submit(
            os.environ.get("HOSTED_TOOL_PROMPT", "Summarize the main topics in this store.")
        )
        print((await turn.get_result()).final_output)
        async for item in session.items():
            if item["type"] == "file_search_call":
                print("Search:", item["id"], item["status"], item["queries"])
                for result in item["results"] or []:
                    print("Match:", result.get("filename"), result.get("score"), result.get("text"))
            elif item["type"] == "message" and item["role"] == "assistant":
                for part in item["content"]:
                    annotations = part.get("annotations")
                    if isinstance(annotations, list):
                        for citation in annotations:
                            if citation.get("type") == "file_citation":
                                print("Cited:", citation["file_id"], citation["filename"])


if __name__ == "__main__":
    asyncio.run(main())
