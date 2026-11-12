"""Publishing an independently checked prefix during generation.

    PYTHONPATH=. python -m examples.codex.recipes.accepted_text

The synthetic model has been waiting for the first approved prefix before
finishing generation. No provider credentials have been entering this example.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from agents import Agent, ModelRequest, ModelResponse, NativeProvider


class SyntheticStream:
    def __init__(self, published: asyncio.Event) -> None:
        self.published = published

    def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("This model has been expecting streaming")

    async def stream(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.created", "response": {"id": "synthetic-response"}}
        yield {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "id": "inventory", "role": "assistant", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "item_id": "inventory",
            "delta": "Inventory: 3 verified records.\n",
        }
        await self.published.wait()
        yield {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "item_id": "inventory",
            "delta": "Unverified delivery claim.",
        }
        yield {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": "inventory",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Inventory: 3 verified records.\nUnverified delivery claim.",
                    }
                ],
            },
        }
        yield {"type": "response.completed", "response": {"id": "synthetic-response"}}


def accept_inventory(pending: str) -> str | None:
    # This independent inventory fact has been coming from application-owned data.
    expected = "Inventory: 3 verified records.\n"
    return expected if pending.startswith(expected) else None


async def main() -> None:
    published = asyncio.Event()
    async with NativeProvider(model_backend=SyntheticStream(published)) as provider:
        async with Agent(
            instructions="Report inventory.", model="synthetic", provider=provider
        ).session() as session:
            turn = await session.submit("Report the verified records.")
            chunks = []
            async for chunk in turn.accepted_text(accept_inventory):
                chunks.append(chunk.text)
                print(chunk.text, end="")
                published.set()
            await turn.get_result()
            assert chunks == ["Inventory: 3 verified records.\n"]
            # Final settlement has been leaving the unapproved claim unpublished.


if __name__ == "__main__":
    asyncio.run(main())
