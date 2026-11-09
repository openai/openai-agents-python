"""Accept an incoming SIP call with the native Session's tool configuration.

Set OPENAI_API_KEY and pass the call ID from a verified incoming-call webhook:
python examples/codex/native_realtime_sip.py rtc_incoming_call
The application must verify its webhook before passing the ID to this example.
Use --drop-message-id only for a provider message you intend to remove. Native
validation preserves tool calls, results, approvals, and other control items.
"""

import argparse
import asyncio
import os
from urllib.parse import quote

import httpx

from agents import Agent
from agents.realtime import RealtimeConfig, RealtimeSIP


async def main(call_id: str, seconds: float, drop_message_id: str | None) -> None:
    agent = Agent(instructions="Answer the caller concisely and use tools when needed.")
    async with agent.session() as session:
        config = await session.build_realtime_sip_config(
            config=RealtimeConfig(instructions="Help the caller and speak concise answers.")
        )
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.openai.com/v1/realtime/calls/{quote(call_id, safe='')}/accept",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json=config,
            )
            response.raise_for_status()
        async with session.realtime(
            config=RealtimeConfig(transport=RealtimeSIP(call_id))
        ) as realtime:

            async def consume() -> None:
                async for _ in realtime.events():
                    pass

            consumer = asyncio.create_task(consume())
            try:
                await asyncio.sleep(seconds)
                # Only items observed since attachment are enumerated, including media.
                # Keep these private; known preattachment IDs can be retrieved explicitly.
                items = await realtime.retrieve_conversation_history()
                if drop_message_id is not None:
                    target = next(item for item in items if item["id"] == drop_message_id)
                    if target["type"] != "message":
                        raise ValueError("--drop-message-id must identify a provider message")
                    # Preserve tool/MCP/approval rows and keep messages between their anchors.
                    # The audit and Session context remain intact. On provider failure,
                    # retrieve observed history before retrying: edits may be partial.
                    await realtime.update_history(
                        [item for item in items if item["id"] != drop_message_id]
                    )
                elif items:
                    # Retrieve one complete item without changing the canonical audit view.
                    await realtime.retrieve_conversation_item(items[-1]["id"])
            finally:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("call_id")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--drop-message-id")
    options = parser.parse_args()
    if options.seconds <= 0:
        parser.error("--seconds must be positive")
    asyncio.run(main(options.call_id, options.seconds, options.drop_message_id))
