"""Serve one synthetic thumbnail order on loopback, then close automatically."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from agents.testing import ScriptedResponses, text, tool_call


async def main() -> None:
    async with ScriptedResponses(
        [tool_call("render_thumbnail", {"color": "blue"}, call_id="thumbnail")],
        [text("The thumbnail contains 3085 bytes.")],
    ) as peer:
        port = urlsplit(peer.provider.base_url).port
        print(f"MODEL_PORT={port}", flush=True)
        # The HTTP server owns a separate thread. Wait until the resumed CLI
        # sends the real tool result before joining the server's request handlers.
        while peer.tool_output("thumbnail") is None:
            await asyncio.sleep(0.05)
        assert json.loads(peer.tool_output("thumbnail") or "null") == {"size_bytes": 3085}
    print("Verified thumbnail result; model endpoint closed.")


if __name__ == "__main__":
    asyncio.run(main())
