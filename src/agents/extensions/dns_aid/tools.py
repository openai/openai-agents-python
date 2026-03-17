"""DNS-AID tools for OpenAI Agents SDK.

Uses @function_tool decorator to create async-native tools
that agents can call for DNS-based agent discovery.
"""

from __future__ import annotations

import json
from typing import Optional

from agents import function_tool


@function_tool
async def discover_agents(
    domain: str,
    protocol: Optional[str] = None,
    name: Optional[str] = None,
    require_dnssec: bool = False,
) -> str:
    """Discover AI agents at a domain via DNS-AID SVCB records.

    Queries DNS to find published agents, optionally filtering by protocol or name.
    Returns JSON with agent names, endpoints, capabilities, and protocols.
    """
    import dns_aid

    result = await dns_aid.discover(
        domain=domain, protocol=protocol, name=name, require_dnssec=require_dnssec
    )
    return json.dumps(result.model_dump(), default=str)


@function_tool
async def publish_agent(
    agent_name: str,
    domain: str,
    protocol: str = "mcp",
    endpoint: str = "",
    port: int = 443,
    capabilities: Optional[list[str]] = None,
    version: str = "1.0.0",
    description: Optional[str] = None,
    ttl: int = 3600,
) -> str:
    """Publish an AI agent to DNS using DNS-AID protocol.

    Creates SVCB and TXT records so the agent becomes discoverable
    by other agents querying DNS.
    """
    import dns_aid

    result = await dns_aid.publish(
        name=agent_name,
        domain=domain,
        protocol=protocol,
        endpoint=endpoint,
        port=port,
        capabilities=capabilities,
        version=version,
        description=description,
        ttl=ttl,
    )
    return json.dumps(result.model_dump(), default=str)


@function_tool
async def unpublish_agent(
    agent_name: str,
    domain: str,
    protocol: str = "mcp",
) -> str:
    """Remove an AI agent's DNS-AID records, making it no longer discoverable."""
    import dns_aid

    deleted = await dns_aid.unpublish(
        name=agent_name, domain=domain, protocol=protocol
    )
    if deleted:
        return json.dumps(
            {"success": True, "message": f"Agent '{agent_name}' unpublished from {domain}"}
        )
    return json.dumps(
        {"success": False, "message": f"Agent '{agent_name}' not found at {domain}"}
    )
