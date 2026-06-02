"""
AgentCard generator — build an A2A ``AgentCard`` from OpenAI Agent metadata.

This module inspects an OpenAI ``Agent`` instance and produces a compliant
A2A ``AgentCard`` that describes its name, description, skills (derived from
tools and handoffs), capabilities, and supported interfaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from a2a.types.a2a_pb2 import (  # type: ignore[import-untyped]
        AgentCapabilities,
        AgentCard,
        AgentInterface,
        AgentProvider,
        AgentSkill,
    )
    from agents.agent import Agent


def generate_agent_card(
    agent: Agent[Any],
    *,
    url: str,
    provider: AgentProvider | None = None,
    capabilities: AgentCapabilities | None = None,
    supported_interfaces: list[AgentInterface] | None = None,
    version: str = "1.0.0",
) -> AgentCard:
    """Generate an A2A ``AgentCard`` from an OpenAI ``Agent`` instance.

    The card includes:

    - The agent's ``name`` and ``instructions`` (or ``handoff_description``)
      as the card's ``description``.
    - One ``AgentSkill`` per ``FunctionTool`` (and per ``Agent.as_tool()``
      handoff) with its name, description, and tags.
    - Default ``input_modes`` / ``output_modes`` set to ``["text"]``.

    Args:
        agent: The OpenAI ``Agent`` to describe.
        url: The base URL where the agent will be served.
        provider: Optional ``AgentProvider`` (organisation metadata).
        capabilities: Optional ``AgentCapabilities``; defaults to streaming
            enabled with no push notifications.
        supported_interfaces: Optional list of ``AgentInterface`` entries;
            defaults to a single JSON-RPC interface at ``url``.
        version: Version string for the agent card (default ``"1.0.0"``).

    Returns:
        A populated A2A ``AgentCard`` protobuf message.
    """
    from a2a.types.a2a_pb2 import (  # type: ignore[import-untyped]
        AgentCapabilities,
        AgentCard,
        AgentInterface,
        AgentSkill,
    )

    from agents.handoffs import Handoff
    from agents.tool import FunctionTool

    # -- description --------------------------------------------------------
    description = agent.handoff_description or ""
    if not description and agent.instructions:
        if isinstance(agent.instructions, str):
            # Truncate very long instructions for the card.
            description = agent.instructions[:2000]
        else:
            description = "Dynamic instructions (callable)."

    # -- skills -------------------------------------------------------------
    skills: list[AgentSkill] = []

    for tool in agent.tools:
        if not isinstance(tool, FunctionTool):
            continue
        skill = AgentSkill(
            id=tool.name,
            name=tool.name,
            description=tool.description[:2000],
            tags=_tool_tags(tool),
            input_modes=["text"],
            output_modes=["text"],
        )
        skills.append(skill)

    for handoff in agent.handoffs:
        if isinstance(handoff, Handoff):
            skill = AgentSkill(
                id=handoff.tool_name,
                name=handoff.agent_name,
                description=handoff.handoff_description or handoff.tool_description or "",
                tags=["handoff"],
                input_modes=["text"],
                output_modes=["text"],
            )
            skills.append(skill)

    # -- capabilities -------------------------------------------------------
    if capabilities is None:
        capabilities = AgentCapabilities(
            streaming=True,
            push_notifications=False,
        )

    # -- interfaces ---------------------------------------------------------
    if supported_interfaces is None:
        supported_interfaces = [
            AgentInterface(url=url),
        ]

    return AgentCard(
        name=agent.name,
        description=description,
        version=version,
        supported_interfaces=supported_interfaces,
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=capabilities,
        skills=skills,
        provider=provider,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_tags(tool: Any) -> list[str]:
    """Infer A2A skill tags from tool metadata."""
    tags = ["tool"]

    # Tag known hosted tools
    tool_type = type(tool).__name__
    type_to_tag: dict[str, str] = {
        "FileSearchTool": "file-search",
        "WebSearchTool": "web-search",
        "CodeInterpreterTool": "code-interpreter",
        "HostedMCPTool": "mcp",
        "ShellTool": "shell",
    }
    if tag := type_to_tag.get(tool_type):
        tags.append(tag)

    # Include namespace if present
    namespace = getattr(tool, "_tool_namespace", None)
    if namespace:
        tags.append(f"namespace:{namespace}")

    return tags
