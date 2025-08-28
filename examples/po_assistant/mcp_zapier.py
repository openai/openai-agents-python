from __future__ import annotations

import os

from agents.mcp import MCPServerStreamableHttp


def zapier_mcp_from_env() -> MCPServerStreamableHttp:
    url = os.getenv("ZAPIER_MCP_URL", "https://mcp.zapier.com/api/mcp/mcp")
    key = os.getenv("ZAPIER_MCP_KEY", "")
    if not key:
        raise RuntimeError("Missing ZAPIER_MCP_KEY env var.")
    headers = {"Authorization": f"Bearer {key}"}
    return MCPServerStreamableHttp(
        name="Zapier MCP",
        params={
            "url": url,
            "headers": headers,
        },
        cache_tools_list=True,
    )
