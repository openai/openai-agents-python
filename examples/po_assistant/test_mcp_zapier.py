from __future__ import annotations

import asyncio
import json
from typing import Any

from agents.mcp import MCPServerStreamableHttp


async def main() -> None:
    # Expect env vars: ZAPIER_MCP_URL, ZAPIER_MCP_KEY
    # We keep the key in env; do not log it.
    server = MCPServerStreamableHttp(
        name="Zapier MCP",
        # Provide minimal placeholder values to satisfy type checker; real values set below.
        params={"url": "", "headers": {}},
    )
    # Workaround: the class takes params at init. We'll reconstruct with env here for clarity.
    import os

    url = os.getenv("ZAPIER_MCP_URL", "https://mcp.zapier.com/api/mcp/mcp")
    key = os.getenv("ZAPIER_MCP_KEY")
    if not key:
        raise RuntimeError("ZAPIER_MCP_KEY not set")

    # Recreate with correct params
    server = MCPServerStreamableHttp(
        name="Zapier MCP",
        params={
            "url": url,
            "headers": {"Authorization": f"Bearer {key}"},
            "timeout": 10,
        },
        cache_tools_list=True,
    )

    async with server:
        tools = await server.list_tools()
        print("Tools (count=", len(tools), "):")
        for t in tools[:20]:
            print(" -", t.name)

        # Prefer an Airtable read tool
        base_id = os.getenv("AIRTABLE_BASE_ID", "appIQpYvYVDlVtAPS")
        target_name_order = [
            "airtable_get_base_schema",
            "airtable_find_base",
            "airtable_find_table",
        ]
        name_to_tool = {t.name: t for t in tools}
        chosen_name = next((n for n in target_name_order if n in name_to_tool), None)
        if not chosen_name:
            print("No Airtable read tool found to call.")
            return

        chosen = name_to_tool[chosen_name]
        print("\nCalling tool:", chosen.name)
        args: dict[str, Any] = {}
        # Inspect schema to set parameter names correctly
        schema = chosen.inputSchema or {}
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        for candidate_key in ("base_id", "baseId", "id"):
            if candidate_key in props:
                args[candidate_key] = base_id
                break
        try:
            result = await server.call_tool(chosen.name, args)
            out = getattr(result, "content", None)
            if out is None:
                print("No content returned.")
            else:
                try:
                    print(json.dumps(out, indent=2))
                except Exception:
                    print(str(out))
        except Exception as e:
            print("Tool call failed:", type(e).__name__, str(e))


if __name__ == "__main__":
    asyncio.run(main())
