import asyncio

from openai import AsyncOpenAI

from agents import Agent, FileSearchTool, Runner, trace


async def main():
    file_id: str | None = None
    vector_store_id: str | None = None

    async with AsyncOpenAI() as client:
        try:
            print("### Preparing vector store:\n")
            # Create a temporary vector store and index a file.
            text = (
                "Arrakis, the desert planet in Frank Herbert's 'Dune,' was inspired by the "
                "scarcity of water as a metaphor for oil and other finite resources."
            )
            file_upload = await client.files.create(
                file=("example.txt", text.encode("utf-8")),
                purpose="assistants",
            )
            file_id = file_upload.id
            print(f"File uploaded: {file_upload.to_dict()}")

            vector_store = await client.vector_stores.create(
                name="example-vector-store",
                expires_after={"anchor": "last_active_at", "days": 1},
            )
            vector_store_id = vector_store.id
            print(f"Vector store created: {vector_store.to_dict()}")

            indexed = await client.vector_stores.files.create_and_poll(
                vector_store_id=vector_store_id,
                file_id=file_id,
            )
            print(f"Stored files in vector store: {indexed.to_dict()}")

            # Create an agent that can search the vector store.
            agent = Agent(
                name="File searcher",
                instructions=(
                    "You are a helpful agent. "
                    "You answer only based on the information in the vector store."
                ),
                tools=[
                    FileSearchTool(
                        max_num_results=3,
                        vector_store_ids=[vector_store_id],
                        include_search_results=True,
                    )
                ],
            )

            with trace("File search example"):
                result = await Runner.run(
                    agent, "Be concise, and tell me 1 sentence about Arrakis I might not know."
                )

                print("\n### Final output:\n")
                print(result.final_output)
                """
                Arrakis, the desert planet in Frank Herbert's "Dune," was inspired by the scarcity
                of water as a metaphor for oil and other finite resources.
                """

                print("\n### Output items:\n")
                print("\n".join([str(out.raw_item) + "\n" for out in result.new_items]))
                """
                {"id":"...", "queries":["Arrakis"], "results":[...]}
                """
        finally:
            try:
                if vector_store_id is not None:
                    await client.vector_stores.delete(vector_store_id)
                    print(f"Deleted vector store: {vector_store_id}")
            finally:
                if file_id is not None:
                    await client.files.delete(file_id)
                    print(f"Deleted file: {file_id}")


if __name__ == "__main__":
    asyncio.run(main())
