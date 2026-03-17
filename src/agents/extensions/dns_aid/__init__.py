"""DNS-AID tools for OpenAI Agents SDK - agent discovery via DNS."""

from agents.extensions.dns_aid.handoff_resolver import DnsAidHandoffResolver
from agents.extensions.dns_aid.tools import (
    discover_agents,
    publish_agent,
    unpublish_agent,
)

__all__ = [
    "discover_agents",
    "publish_agent",
    "unpublish_agent",
    "DnsAidHandoffResolver",
]
