from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from .agent import Agent
from .run_context import RunContextWrapper

if TYPE_CHECKING:
    from .handoffs import Handoff
    from .run_context import TContext


NO_CONTEXT: RunContextWrapper[None] = RunContextWrapper[None](context=None)


@dataclass
class AgentCardBuilder:
    """Builder class for creating AgentCard instances from Agent configurations.

    This class provides methods to extract and build agent capabilities, skills,
    and metadata into a structured AgentCard format. It handles complex agent
    hierarchies with tools and handoffs, generating comprehensive skill
    descriptions and orchestration capabilities.

    The builder supports:
    - Tool-based skill extraction
    - Handoff capability mapping
    - Orchestration skill generation
    - Recursive agent traversal for complex workflows
    """

    agent: Agent
    """The agent to build a card for."""

    url: str
    """The URL where the agent can be accessed."""

    version: str
    """Version string for the agent."""

    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    """Capabilities specification for the agent."""

    default_input_modes: list[str] = field(default_factory=lambda: ["text/plain"])
    """Default input modes for the agent, e.g., 'text', 'image', etc."""

    default_output_modes: list[str] = field(default_factory=lambda: ["text/plain"])
    """Default output modes for the agent, e.g., 'text', 'image', etc."""

    async def build_tool_skills(self, agent: Agent) -> list[AgentSkill]:
        """Build skills from the agent's available tools.

        Args:
            agent: The agent to extract tool skills from.

        Returns:
            A list of AgentSkill objects representing the agent's tools.
        """
        tools = await agent.get_all_tools(NO_CONTEXT)

        if not tools:
            return []

        skills = []
        for tool in tools:
            skill = AgentSkill(
                id=f"{agent.name}-{tool.name}",
                name=tool.name,
                description=getattr(tool, "description", None) or f"Tool: {tool.name}",
                tags=tool.name.split("_"),
            )
            skills.append(skill)

        return skills

    async def build_handoff_skills(self, agent: Agent) -> list[AgentSkill]:
        """Build skills from the agent's handoff capabilities.

        Args:
            agent: The agent to extract handoff skills from.

        Returns:
            A list of AgentSkill objects representing handoff capabilities.
        """
        if not agent.handoffs:
            return []

        skills = []
        visited_agents = {agent.name}  # Track to prevent circular dependencies

        for handoff in agent.handoffs:
            if getattr(handoff, "name", None) in visited_agents:
                continue

            handoff_skills = await self._build_handoff_skills_recursive(
                handoff, visited_agents.copy()
            )
            skills.extend(handoff_skills)

        return skills

    async def _build_handoff_skills_recursive(
        self, handoff: Agent[Any] | Handoff[TContext, Any], visited_agents: set[str]
    ) -> list[AgentSkill]:
        """Recursively build skills for a handoff agent.

        Args:
            handoff: The handoff to build skills for.
            visited_agents: Set of already visited agent names to prevent cycles.

        Returns:
            List of skills for the handoff agent.
        """
        handoff_name = getattr(handoff, "name", None)

        if handoff_name in visited_agents:
            # Circular dependency detected - return empty list to prevent infinite recursion
            return []

        if handoff_name:
            visited_agents.add(handoff_name)
            if hasattr(handoff, "name"):
                return await self.build_agent_skills(handoff)  # type: ignore[arg-type]

        return []

    async def build_orchestration_skill(self, agent: Agent) -> AgentSkill | None:
        """Build an orchestration skill that describes the agent's coordination capabilities.

        This method creates a comprehensive skill description that encompasses both
        tool usage and handoff capabilities, providing a high-level view of the
        agent's orchestration abilities.

        Args:
            agent: The agent to build orchestration skill for.

        Returns:
            An AgentSkill describing orchestration capabilities, or None if no coordination needed.
        """
        handoff_descriptions = self._build_handoff_descriptions(agent)
        tool_descriptions = await self._build_tool_descriptions(agent)

        if not handoff_descriptions and not tool_descriptions:
            return None

        sections = []
        if handoff_descriptions:
            sections.append("Handoffs:\n" + "\n".join(handoff_descriptions))

        if tool_descriptions:
            sections.append("Tools:\n" + "\n".join(tool_descriptions))

        description = (
            f"Orchestrates across multiple tools and agents for {agent.name}. "
            "Coordinates requests and delegates tasks appropriately:\n" + "\n\n".join(sections)
        ).strip()

        return AgentSkill(
            id=f"{agent.name}_orchestration",
            name=f"{agent.name}: Orchestration",
            description=description,
            tags=["handoff", "orchestration", "coordination"],
        )

    def _build_handoff_descriptions(self, agent: Agent) -> list[str]:
        """Build descriptions for agent handoffs."""
        return [
            f"- {getattr(handoff, 'name', 'Unknown')}: "
            f"{getattr(handoff, 'handoff_description', None) or 'No description available'}".strip()
            for handoff in agent.handoffs
        ]

    async def _build_tool_descriptions(self, agent: Agent) -> list[str]:
        """Build descriptions for agent tools."""
        tools = await agent.get_all_tools(NO_CONTEXT)
        return [
            f"- {tool.name}: "
            f"{getattr(tool, 'description', None) or 'No description available'}".strip()
            for tool in tools
        ]

    async def build_agent_skills(self, agent: Agent) -> list[AgentSkill]:
        """Build all skills for a given agent.

        This method coordinates the extraction of all skill types from an agent,
        including tool-based skills and orchestration capabilities. It ensures
        comprehensive coverage of the agent's functionality.

        Args:
            agent: The agent to build skills for.

        Returns:
            A list of all AgentSkill objects for the agent.
        """
        skills: list[AgentSkill] = []

        # Build tool-based skills
        tool_skills = await self.build_tool_skills(agent)
        skills.extend(tool_skills)

        # Build orchestration skill if the agent has coordination capabilities
        if agent.handoffs or tool_skills:
            orchestration = await self.build_orchestration_skill(agent)
            if orchestration:
                skills.append(orchestration)

        return skills

    async def build_skills(self) -> list[AgentSkill]:
        """Build all skills for the configured agent and its handoffs.

        This is the main coordination method that builds a comprehensive skill set
        including both direct agent capabilities and transitive handoff skills.

        Returns:
            A comprehensive list of all AgentSkill objects.
        """
        agent_skills_task = self.build_agent_skills(self.agent)
        handoff_skills_task = self.build_handoff_skills(self.agent)

        agent_skills, handoff_skills = await asyncio.gather(
            agent_skills_task, handoff_skills_task
        )

        all_skills = [*agent_skills, *handoff_skills]

        unique_skills_dict = {}
        for skill in all_skills:
            if skill.id not in unique_skills_dict:
                unique_skills_dict[skill.id] = skill

        return list(unique_skills_dict.values())

    async def build(self) -> AgentCard:
        """Build the complete AgentCard for the configured agent.

        This method creates a comprehensive agent card that includes all extracted
        skills, capabilities, and metadata. It serves as the main entry point for
        converting an Agent configuration into a structured AgentCard format.

        Returns:
            A fully constructed AgentCard with all skills and metadata.
        """
        skills = await self.build_skills()

        card = AgentCard(
            name=self.agent.name,
            capabilities=self.capabilities,
            default_input_modes=self.default_input_modes,
            default_output_modes=self.default_output_modes,
            description=self.agent.handoff_description or f"Agent: {self.agent.name}",
            skills=skills,
            url=self.url,
            version=self.version,
        )

        return card
