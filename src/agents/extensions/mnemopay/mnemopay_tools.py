"""MnemoPay tools for OpenAI Agents SDK.

Gives any agent persistent cognitive memory and an escrow wallet via the
MnemoPay MCP server.  Each tool is returned as a ``FunctionTool`` that can be
passed straight into ``Agent(tools=[...])``.

Usage::

    from agents.extensions.mnemopay import mnemopay_tools

    agent = Agent(
        name="assistant",
        tools=mnemopay_tools(),
    )
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from ...tool import FunctionTool, ToolContext


# ---------------------------------------------------------------------------
# MCP client — lightweight JSON-RPC over stdio
# ---------------------------------------------------------------------------

@dataclass
class _McpClient:
    """Manages a long-lived stdio connection to the MnemoPay MCP server."""

    agent_id: str
    server_url: str | None
    _proc: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _request_id: int = field(default=0, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc

        env = {**os.environ, "MNEMOPAY_AGENT_ID": self.agent_id}
        if self.server_url:
            env["MNEMOPAY_SERVER_URL"] = self.server_url

        npx = "npx.cmd" if sys.platform == "win32" else "npx"
        self._proc = await asyncio.create_subprocess_exec(
            npx, "-y", "@mnemopay/sdk",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        # Wait for the server to be ready (it prints to stderr)
        return self._proc

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        async with self._lock:
            proc = await self._ensure_started()
            assert proc.stdin is not None and proc.stdout is not None

            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params or {},
            }
            proc.stdin.write(json.dumps(request).encode() + b"\n")
            await proc.stdin.drain()

            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("MnemoPay MCP server closed unexpectedly")

            response = json.loads(line)
            if "error" in response:
                err = response["error"]
                raise RuntimeError(f"MnemoPay error: {err.get('message', err)}")
            return response.get("result", {})


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "mnemopay_remember",
        "description": "Store a persistent memory. Memories decay over time unless reinforced.",
        "schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The information to remember"},
                "metadata": {
                    "type": "object",
                    "description": "Optional key-value metadata",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["content"],
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "remember",
    },
    {
        "name": "mnemopay_recall",
        "description": "Search memories by semantic similarity. Returns ranked matches.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results (default 5)"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "recall",
    },
    {
        "name": "mnemopay_forget",
        "description": "Delete a specific memory by its ID.",
        "schema": {
            "type": "object",
            "properties": {
                "memoryId": {"type": "string", "description": "The memory ID to delete"},
            },
            "required": ["memoryId"],
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "forget",
    },
    {
        "name": "mnemopay_reinforce",
        "description": "Boost a memory's importance so it decays more slowly.",
        "schema": {
            "type": "object",
            "properties": {
                "memoryId": {"type": "string", "description": "The memory ID to reinforce"},
            },
            "required": ["memoryId"],
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "reinforce",
    },
    {
        "name": "mnemopay_consolidate",
        "description": "Prune stale memories that have decayed below the threshold.",
        "schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "consolidate",
    },
    {
        "name": "mnemopay_charge",
        "description": "Create an escrow charge for work delivered.",
        "schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Charge amount"},
                "description": {"type": "string", "description": "What the charge is for"},
                "metadata": {
                    "type": "object",
                    "description": "Optional metadata",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["amount", "description"],
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "charge",
    },
    {
        "name": "mnemopay_settle",
        "description": "Finalize a pending escrow transaction.",
        "schema": {
            "type": "object",
            "properties": {
                "transactionId": {"type": "string", "description": "Transaction ID to settle"},
            },
            "required": ["transactionId"],
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "settle",
    },
    {
        "name": "mnemopay_refund",
        "description": "Refund a completed transaction.",
        "schema": {
            "type": "object",
            "properties": {
                "transactionId": {"type": "string", "description": "Transaction ID to refund"},
            },
            "required": ["transactionId"],
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "refund",
    },
    {
        "name": "mnemopay_balance",
        "description": "Check the agent's wallet balance and reputation score.",
        "schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "balance",
    },
    {
        "name": "mnemopay_profile",
        "description": "Get the agent's full profile including stats and capabilities.",
        "schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "profile",
    },
    {
        "name": "mnemopay_history",
        "description": "Retrieve the agent's transaction history.",
        "schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max transactions (default 10)"},
            },
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "history",
    },
    {
        "name": "mnemopay_logs",
        "description": "Get immutable audit trail of all agent actions.",
        "schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max log entries (default 10)"},
            },
            "additionalProperties": False,
        },
        "mcp_method": "tools/call",
        "mcp_tool": "logs",
    },
]


def _make_invoke_fn(
    client: _McpClient, mcp_tool: str
) -> Any:
    """Create an async on_invoke_tool closure for a given MCP tool name."""

    async def on_invoke_tool(ctx: ToolContext[Any], args_json: str) -> str:
        params = json.loads(args_json) if args_json else {}
        result = await client.call(
            "tools/call",
            {"name": mcp_tool, "arguments": params},
        )
        # The MCP response wraps content in a list
        content = result.get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(texts) if texts else json.dumps(result)

    return on_invoke_tool


def mnemopay_tools(
    *,
    agent_id: str = "openai-agent",
    server_url: str | None = None,
) -> list[FunctionTool]:
    """Return a list of MnemoPay ``FunctionTool`` instances.

    Args:
        agent_id: Identifier for this agent (default ``"openai-agent"``).
        server_url: Optional remote MnemoPay server URL. When ``None``, the
            MCP server runs locally via ``npx -y @mnemopay/sdk``.

    Returns:
        A list of 12 ``FunctionTool`` instances covering memory, payments,
        and observability.
    """
    client = _McpClient(
        agent_id=os.environ.get("MNEMOPAY_AGENT_ID", agent_id),
        server_url=os.environ.get("MNEMOPAY_SERVER_URL", server_url),
    )

    tools: list[FunctionTool] = []
    for defn in _TOOL_DEFS:
        tools.append(
            FunctionTool(
                name=defn["name"],
                description=defn["description"],
                params_json_schema=defn["schema"],
                on_invoke_tool=_make_invoke_fn(client, defn["mcp_tool"]),
                strict_json_schema=True,
            )
        )

    return tools
