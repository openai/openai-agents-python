import os, asyncio, logging, httpx

STRUCTURED_URL    = os.getenv("BUBBLE_STRUCTURED_URL")
CLARIFICATION_URL = os.getenv("BUBBLE_CHAT_URL")

async def post_webhook(url: str, data: dict, retries: int = 3):
    for i in range(retries):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json=data)
                r.raise_for_status()
            return
        except Exception as e:
            if i == retries - 1:
                logging.error("Webhook failed %s: %s", url, e)
            await asyncio.sleep(2 ** i)
