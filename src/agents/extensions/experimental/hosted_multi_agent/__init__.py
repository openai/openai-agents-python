"""Experimental OpenAI Responses hosted multi-agent support."""

from .model import (
    HostedAgentMetadata,
    HostedMultiAgentConfig,
    OpenAIHostedMultiAgentModel,
    get_hosted_agent_metadata,
)

__all__ = [
    "HostedAgentMetadata",
    "HostedMultiAgentConfig",
    "OpenAIHostedMultiAgentModel",
    "get_hosted_agent_metadata",
]
