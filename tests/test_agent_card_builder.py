from typing import Any

import pytest
from a2a.types import AgentCapabilities, AgentCard

from agents.agent import Agent
from agents.agent_card_builder import AgentCardBuilder
from agents.tool import function_tool


# Test fixtures and mock tools
@function_tool
def mock_tool_1() -> str:
    """Test tool 1 description."""
    return "tool_1_result"


@function_tool
def mock_tool_2() -> str:
    """Test tool 2 description."""
    return "tool_2_result"


@function_tool
def complex_tool_name() -> str:
    """Complex tool with underscores."""
    return "complex_result"


@function_tool
def tool_without_description() -> str:
    return "no_description_result"


class TestAgentCardBuilder:
    """Test suite for AgentCardBuilder class."""

    @pytest.fixture
    def simple_agent(self) -> Agent:
        """Create a simple agent for testing."""
        return Agent(
            name="SimpleAgent",
            handoff_description="A simple test agent",
            tools=[mock_tool_1, mock_tool_2],
        )

    @pytest.fixture
    def agent_without_tools(self) -> Agent:
        """Create an agent without tools."""
        return Agent(name="EmptyAgent", handoff_description="Agent without tools")

    @pytest.fixture
    def agent_with_handoffs(self, simple_agent: Agent) -> Agent:
        """Create an agent with handoffs."""
        handoff_agent = Agent(
            name="HandoffTarget",
            handoff_description="Target for handoffs",
            tools=[complex_tool_name],
        )
        return Agent(
            name="MainAgent",
            handoff_description="Agent with handoffs",
            tools=[mock_tool_1],
            handoffs=[handoff_agent],
        )

    @pytest.fixture
    def complex_agent_hierarchy(self) -> Agent:
        """Create a complex agent hierarchy for testing."""
        # Leaf agents
        leaf_agent_1 = Agent(
            name="LeafAgent1", handoff_description="Leaf agent 1", tools=[mock_tool_1, mock_tool_2]
        )

        leaf_agent_2 = Agent(
            name="LeafAgent2", handoff_description="Leaf agent 2", tools=[complex_tool_name]
        )

        # Middle tier agent
        middle_agent = Agent(
            name="MiddleAgent",
            handoff_description="Middle tier agent",
            tools=[tool_without_description],
            handoffs=[leaf_agent_1, leaf_agent_2],
        )

        # Root agent
        root_agent = Agent(
            name="RootAgent",
            handoff_description="Root agent with complex hierarchy",
            tools=[mock_tool_1],
            handoffs=[middle_agent, leaf_agent_2],  # Includes duplicate leaf_agent_2
        )

        return root_agent

    @pytest.fixture
    def circular_dependency_agents(self) -> tuple[Agent, Agent]:
        """Create agents with circular dependencies."""
        # This will create a circular reference for testing
        agent_a = Agent(name="AgentA", handoff_description="Agent A")
        agent_b = Agent(name="AgentB", handoff_description="Agent B")

        # Create circular dependency
        agent_a.handoffs = [agent_b]
        agent_b.handoffs = [agent_a]

        return agent_a, agent_b

    @pytest.fixture
    def basic_builder(self, simple_agent: Agent) -> AgentCardBuilder:
        """Create a basic AgentCardBuilder."""
        return AgentCardBuilder(
            agent=simple_agent, url="https://example.com/agent", version="1.0.0"
        )

    @pytest.fixture
    def full_featured_builder(self, complex_agent_hierarchy: Agent) -> AgentCardBuilder:
        """Create a fully featured AgentCardBuilder."""
        capabilities = AgentCapabilities()
        return AgentCardBuilder(
            agent=complex_agent_hierarchy,
            url="https://example.com/complex-agent",
            capabilities=capabilities,
            default_input_modes=["text/plain", "image/jpeg"],
            default_output_modes=["text/plain", "application/json"],
            version="2.1.0",
        )

    @pytest.mark.parametrize(
        "agent_fixture,expected_count,expected_skills",
        [
            (
                "simple_agent",
                2,
                [
                    {
                        "id": "SimpleAgent-mock_tool_1",
                        "name": "mock_tool_1",
                        "description": "Test tool 1 description.",
                        "tags": ["mock", "tool", "1"],
                    },
                    {
                        "id": "SimpleAgent-mock_tool_2",
                        "name": "mock_tool_2",
                        "description": "Test tool 2 description.",
                        "tags": ["mock", "tool", "2"],
                    },
                ],
            ),
            ("agent_without_tools", 0, []),
        ],
        ids=["with_tools", "no_tools"],
    )
    @pytest.mark.asyncio
    async def test_build_tool_skills(
        self,
        basic_builder: AgentCardBuilder,
        agent_fixture: str,
        expected_count: int,
        expected_skills: list[dict[str, Any]],
        request: pytest.FixtureRequest,
    ) -> None:
        """Test building tool skills when agent has/doesn't have tools."""
        agent = request.getfixturevalue(agent_fixture)
        skills = await basic_builder.build_tool_skills(agent)

        assert len(skills) == expected_count

        for i, expected_skill in enumerate(expected_skills):
            skill = skills[i]
            assert skill.id == expected_skill["id"]
            assert skill.name == expected_skill["name"]
            assert skill.description == expected_skill["description"]
            assert skill.tags == expected_skill["tags"]

    @pytest.mark.asyncio
    async def test_build_tool_skills_complex_names(self, basic_builder: AgentCardBuilder) -> None:
        """Test building tool skills with complex tool names."""
        agent_with_complex_tool = Agent(
            name="ComplexAgent", tools=[complex_tool_name, tool_without_description]
        )

        skills = await basic_builder.build_tool_skills(agent_with_complex_tool)

        assert len(skills) == 2

        # Check complex tool
        complex_skill = skills[0]
        assert complex_skill.id == "ComplexAgent-complex_tool_name"
        assert complex_skill.name == "complex_tool_name"
        assert complex_skill.description == "Complex tool with underscores."
        assert complex_skill.tags == ["complex", "tool", "name"]

        # Check tool without description
        no_desc_skill = skills[1]
        assert no_desc_skill.id == "ComplexAgent-tool_without_description"
        assert no_desc_skill.name == "tool_without_description"
        assert no_desc_skill.description == "Tool: tool_without_description"  # Fallback description
        assert no_desc_skill.tags == ["tool", "without", "description"]

    @pytest.mark.parametrize(
        "agent_fixture,expected_skills_count",
        [
            ("simple_agent", 0),  # no handoffs
            ("agent_with_handoffs", 1),  # has handoffs
        ],
        ids=["no_handoffs", "with_handoffs"],
    )
    @pytest.mark.asyncio
    async def test_build_handoff_skills(
        self,
        basic_builder: AgentCardBuilder,
        agent_fixture: str,
        expected_skills_count: int,
        request: pytest.FixtureRequest,
    ) -> None:
        """Test building handoff skills when agent has/doesn't have handoffs."""
        agent = request.getfixturevalue(agent_fixture)
        skills = await basic_builder.build_handoff_skills(agent)

        if expected_skills_count == 0:
            assert skills == []
        else:
            assert len(skills) > 0

    @pytest.mark.asyncio
    async def test_build_handoff_skills_circular_dependency(
        self, basic_builder: AgentCardBuilder, circular_dependency_agents: tuple[Agent, Agent]
    ) -> None:
        """Test building handoff skills with circular dependencies."""
        agent_a, agent_b = circular_dependency_agents

        # Should handle circular dependencies gracefully
        skills = await basic_builder.build_handoff_skills(agent_a)

        # Should not hang or throw errors due to circular reference
        assert isinstance(skills, list)

    @pytest.mark.asyncio
    async def test_build_handoff_skills_recursive(self, basic_builder: AgentCardBuilder) -> None:
        """Test recursive handoff skill building."""
        # Create a three-level hierarchy
        level_3_agent = Agent(
            name="Level3Agent", handoff_description="Third level agent", tools=[mock_tool_1]
        )

        level_2_agent = Agent(
            name="Level2Agent",
            handoff_description="Second level agent",
            tools=[mock_tool_2],
            handoffs=[level_3_agent],
        )

        level_1_agent = Agent(
            name="Level1Agent", handoff_description="First level agent", handoffs=[level_2_agent]
        )

        skills = await basic_builder.build_handoff_skills(level_1_agent)

        # Should collect skills from all levels
        assert len(skills) > 0

    @pytest.mark.parametrize(
        "agent_type,expected_skill_not_none,expected_id,expected_name,expected_tags,expected_in_description,expected_not_in_description",
        [
            (
                "agent_with_handoffs",
                True,
                "MainAgent_orchestration",
                "MainAgent: Orchestration",
                ["handoff", "orchestration", "coordination"],
                [
                    "Orchestrates across multiple tools and agents for MainAgent",
                    "Handoffs:",
                    "Tools:",
                ],
                [],
            ),
            (
                "tools_only",
                True,
                "SimpleAgent_orchestration",
                "SimpleAgent: Orchestration",
                ["orchestration"],
                ["Tools:"],
                ["Handoffs:"],
            ),
        ],
        ids=["with_handoffs_and_tools", "tools_only"],
    )
    @pytest.mark.asyncio
    async def test_build_orchestration_skill_with_capabilities(
        self,
        basic_builder: AgentCardBuilder,
        simple_agent: Agent,
        agent_with_handoffs: Agent,
        agent_type: str,
        expected_skill_not_none: bool,
        expected_id: str,
        expected_name: str,
        expected_tags: list[str],
        expected_in_description: list[str],
        expected_not_in_description: list[str],
    ) -> None:
        """Test building orchestration skill when agent has coordination capabilities."""
        agent_map = {"agent_with_handoffs": agent_with_handoffs, "tools_only": simple_agent}

        agent = agent_map[agent_type]
        skill = await basic_builder.build_orchestration_skill(agent)

        if expected_skill_not_none:
            assert skill is not None
            assert skill.id == expected_id
            assert skill.name == expected_name

            for tag in expected_tags:
                assert tag in skill.tags

            for text in expected_in_description:
                assert text in skill.description

            for text in expected_not_in_description:
                assert text not in skill.description
        else:
            assert skill is None

    @pytest.mark.parametrize(
        "agent_config,expected_skill_none",
        [
            ({"name": "MinimalAgent", "handoff_description": "Agent without capabilities"}, True),
            (
                {
                    "name": "HandoffOnlyAgent",
                    "handoff_description": "Agent with only handoffs",
                    "handoffs": True,
                },
                False,
            ),
        ],
        ids=["no_capabilities", "handoffs_only"],
    )
    @pytest.mark.asyncio
    async def test_build_orchestration_skill_edge_cases(
        self,
        basic_builder: AgentCardBuilder,
        agent_config: dict[str, Any],
        expected_skill_none: bool,
    ) -> None:
        """Test building orchestration skill for edge cases."""
        if agent_config.get("handoffs"):
            handoff_target = Agent(name="Target", tools=[mock_tool_1])
            agent = Agent(
                name=agent_config["name"],
                handoff_description=agent_config["handoff_description"],
                handoffs=[handoff_target],
            )
        else:
            agent = Agent(
                name=agent_config["name"], handoff_description=agent_config["handoff_description"]
            )

        skill = await basic_builder.build_orchestration_skill(agent)

        if expected_skill_none:
            assert skill is None
        else:
            assert skill is not None
            assert "Handoffs:" in skill.description

    @pytest.mark.parametrize(
        "agent_config,expected_descriptions",
        [
            (
                {
                    "name": "MainAgent",
                    "handoffs": [
                        {"name": "HandoffTarget", "handoff_description": "Target for handoffs"}
                    ],
                },
                ["- HandoffTarget: Target for handoffs"],
            ),
            (
                {"name": "TestAgent", "handoffs": [{"name": "NoDescHandoff"}]},
                ["- NoDescHandoff: No description available"],
            ),
        ],
        ids=["with_description", "no_description"],
    )
    def test_build_handoff_descriptions(
        self,
        basic_builder: AgentCardBuilder,
        agent_config: dict[str, Any],
        expected_descriptions: list[str],
    ) -> None:
        """Test building handoff descriptions."""
        handoffs = []
        for handoff_config in agent_config["handoffs"]:
            handoff = Agent(
                name=handoff_config["name"],
                handoff_description=handoff_config.get("handoff_description"),
            )
            handoffs.append(handoff)

        agent = Agent(name=agent_config["name"], handoffs=handoffs)  # type: ignore[arg-type]

        descriptions = basic_builder._build_handoff_descriptions(agent)

        assert len(descriptions) == len(expected_descriptions)
        for expected_desc in expected_descriptions:
            assert expected_desc in descriptions[0]

    @pytest.mark.parametrize(
        "agent_fixture,expected_descriptions",
        [
            (
                "simple_agent",
                [
                    "- mock_tool_1: Test tool 1 description.",
                    "- mock_tool_2: Test tool 2 description.",
                ],
            ),
            ("agent_without_tools", []),
        ],
        ids=["with_tools", "no_tools"],
    )
    @pytest.mark.asyncio
    async def test_build_tool_descriptions(
        self,
        basic_builder: AgentCardBuilder,
        agent_fixture: str,
        expected_descriptions: list[str],
        request: pytest.FixtureRequest,
    ) -> None:
        """Test building tool descriptions."""
        agent = request.getfixturevalue(agent_fixture)
        descriptions = await basic_builder._build_tool_descriptions(agent)

        assert descriptions == expected_descriptions

    @pytest.mark.parametrize(
        "agent_fixture,min_expected_skills,has_orchestration",
        [
            ("simple_agent", 2, True),  # tools + orchestration
            ("agent_without_tools", 0, False),  # no tools, no handoffs = no skills
        ],
        ids=["with_tools", "without_capabilities"],
    )
    @pytest.mark.asyncio
    async def test_build_agent_skills(
        self,
        basic_builder: AgentCardBuilder,
        agent_fixture: str,
        min_expected_skills: int,
        has_orchestration: bool,
        request: pytest.FixtureRequest,
    ) -> None:
        """Test building all skills for an agent."""
        agent = request.getfixturevalue(agent_fixture)
        skills = await basic_builder.build_agent_skills(agent)

        assert len(skills) >= min_expected_skills

        orchestration_skills = [s for s in skills if "orchestration" in s.tags]
        if has_orchestration:
            assert len(orchestration_skills) == 1
        else:
            assert len(orchestration_skills) == 0

    @pytest.mark.asyncio
    async def test_build_skills_deduplication(
        self, full_featured_builder: AgentCardBuilder
    ) -> None:
        """Test that build_skills properly deduplicates skills."""
        skills = await full_featured_builder.build_skills()

        # Collect all skill IDs
        skill_ids = [skill.id for skill in skills]

        # Should have no duplicates
        assert len(skill_ids) == len(set(skill_ids))

    @pytest.mark.asyncio
    async def test_build_skills_comprehensive(
        self, full_featured_builder: AgentCardBuilder
    ) -> None:
        """Test comprehensive skill building."""
        skills = await full_featured_builder.build_skills()

        assert len(skills) > 0

        # Should contain both agent skills and handoff skills
        skill_names = [skill.name for skill in skills]

        # Check that we have skills from different sources
        assert any("orchestration" in name.lower() for name in skill_names)

    @pytest.mark.parametrize(
        "builder_fixture,expected_name,expected_description,expected_url,expected_version,expected_input_modes,expected_output_modes",
        [
            (
                "basic_builder",
                "SimpleAgent",
                "A simple test agent",
                "https://example.com/agent",
                "1.0.0",
                ["text/plain"],
                ["text/plain"],
            ),
            (
                "full_featured_builder",
                "RootAgent",
                "Root agent with complex hierarchy",
                "https://example.com/complex-agent",
                "2.1.0",
                ["text/plain", "image/jpeg"],
                ["text/plain", "application/json"],
            ),
        ],
        ids=["minimal_config", "full_featured_config"],
    )
    @pytest.mark.asyncio
    async def test_build_card(
        self,
        builder_fixture: str,
        expected_name: str,
        expected_description: str,
        expected_url: str,
        expected_version: str,
        expected_input_modes: list[str],
        expected_output_modes: list[str],
        request: pytest.FixtureRequest,
    ) -> None:
        """Test building agent card with different configurations."""
        builder: AgentCardBuilder = request.getfixturevalue(builder_fixture)
        card = await builder.build()

        assert isinstance(card, AgentCard)
        assert card.name == expected_name
        assert card.description == expected_description
        assert card.url == expected_url
        assert card.version == expected_version
        assert card.capabilities is not None
        assert card.default_input_modes == expected_input_modes
        assert card.default_output_modes == expected_output_modes
        assert len(card.skills) > 0

    @pytest.mark.asyncio
    async def test_build_card_fallback_description(self) -> None:
        """Test building agent card when no handoff_description is provided."""
        agent_no_desc = Agent(name="NoDescAgent", tools=[mock_tool_1])
        builder = AgentCardBuilder(agent=agent_no_desc, url="https://example.com", version="1.5.0")

        card = await builder.build()

        assert card.description == "Agent: NoDescAgent"

    @pytest.mark.parametrize(
        "builder_config,expected_version,has_custom_capabilities",
        [
            ({"url": "https://minimal.com", "version": "0.1.0"}, "0.1.0", False),
            (
                {"url": "https://custom.com", "version": "1.2.3", "capabilities": "custom"},
                "1.2.3",
                True,
            ),
        ],
        ids=["minimal_params", "custom_capabilities"],
    )
    @pytest.mark.asyncio
    async def test_builder_with_different_parameter_combinations(
        self, builder_config: dict[str, Any], expected_version: str, has_custom_capabilities: bool
    ) -> None:
        """Test builder with various parameter combinations."""
        agent = Agent(name="TestAgent", tools=[mock_tool_1])

        kwargs = {
            "agent": agent,
            "url": builder_config["url"],
            "version": builder_config["version"],
        }

        if has_custom_capabilities:
            custom_capabilities = AgentCapabilities()
            kwargs["capabilities"] = custom_capabilities

        builder = AgentCardBuilder(**kwargs)
        card = await builder.build()

        assert card.version == expected_version
        assert card.capabilities is not None

        if has_custom_capabilities:
            assert card.capabilities == custom_capabilities

    @pytest.mark.asyncio
    async def test_empty_agent_hierarchy(self) -> None:
        """Test with completely empty agent hierarchy."""
        empty_agent = Agent(name="EmptyAgent")
        builder = AgentCardBuilder(agent=empty_agent, url="https://empty.com", version="0.0.1")

        card = await builder.build()

        assert card.name == "EmptyAgent"
        assert len(card.skills) == 0  # No tools, no handoffs = no skills

    @pytest.mark.asyncio
    async def test_deeply_nested_handoffs(self) -> None:
        """Test with deeply nested handoff hierarchy."""
        # Create a 5-level deep hierarchy
        agents: list[Agent] = []
        for i in range(5):
            agent = Agent(
                name=f"Level{i}Agent",
                handoff_description=f"Agent at level {i}",
                tools=[mock_tool_1] if i % 2 == 0 else [mock_tool_2],
            )
            if i > 0:
                agent.handoffs = [agents[i - 1]]
            agents.append(agent)

        root_agent = agents[-1]
        builder = AgentCardBuilder(agent=root_agent, url="https://deep.com", version="2.0.0")

        skills = await builder.build_skills()

        # Should handle deep nesting without issues
        assert len(skills) > 0

    @pytest.mark.asyncio
    async def test_build_with_special_characters_in_names(self) -> None:
        """Test building skills with special characters in names."""

        @function_tool
        def tool_with_special_chars() -> str:
            """Tool with special characters in description: !@#$%^&*()"""
            return "special"

        agent = Agent(
            name="Agent-With-Dashes",
            handoff_description="Agent with special chars: !@#$%",
            tools=[tool_with_special_chars],
        )

        builder = AgentCardBuilder(agent=agent, url="https://special.com", version="1.2.3-alpha")
        card = await builder.build()

        assert card.name == "Agent-With-Dashes"
        assert len(card.skills) > 0

        # Check skill ID formation with special characters
        tool_skill = [s for s in card.skills if "tool_with_special_chars" in s.name][0]
        assert tool_skill.id == "Agent-With-Dashes-tool_with_special_chars"

    @pytest.mark.asyncio
    async def test_large_number_of_tools(self) -> None:
        """Test building skills with a large number of tools."""
        # Create many tools
        tools = []
        for i in range(50):
            # Create a properly named function for each tool
            def create_tool_func(index: int):
                def tool_func() -> str:
                    return f"tool_{index}"

                tool_func.__name__ = f"tool_{index}"
                tool_func.__doc__ = f"Tool number {index}"
                return tool_func

            tool = function_tool(create_tool_func(i))
            tools.append(tool)

        agent = Agent(name="ManyToolsAgent", tools=tools)  # type: ignore[arg-type]
        builder = AgentCardBuilder(agent=agent, url="https://many.com", version="1.0.0")

        skills = await builder.build_skills()

        # Should handle many tools efficiently
        # We expect at least 50 tool skills + 1 orchestration skill
        assert len(skills) >= 50  # At least one skill per tool

    @pytest.mark.parametrize(
        "builder_config,expected_capabilities_is_custom,expected_input_modes,expected_output_modes,expected_version",
        [
            (
                {"url": "https://test.com", "version": "1.0.0"},
                False,
                ["text/plain"],
                ["text/plain"],
                "1.0.0",
            ),
            (
                {
                    "url": "https://custom.com",
                    "version": "3.0.0",
                    "capabilities": "custom",
                    "default_input_modes": ["text/plain", "audio/wav"],
                    "default_output_modes": ["application/json"],
                },
                True,
                ["text/plain", "audio/wav"],
                ["application/json"],
                "3.0.0",
            ),
        ],
        ids=["default_factory_values", "custom_values"],
    )
    def test_builder_dataclass_fields(
        self,
        builder_config: dict[str, Any],
        expected_capabilities_is_custom: bool,
        expected_input_modes: list[str],
        expected_output_modes: list[str],
        expected_version: str,
    ) -> None:
        """Test that AgentCardBuilder dataclass fields work correctly."""
        agent = Agent(name="TestAgent")

        kwargs = {
            "agent": agent,
            "url": builder_config["url"],
            "version": builder_config["version"],
        }

        custom_capabilities = None
        if expected_capabilities_is_custom:
            custom_capabilities = AgentCapabilities()
            kwargs["capabilities"] = custom_capabilities

        if "default_input_modes" in builder_config:
            kwargs["default_input_modes"] = builder_config["default_input_modes"]

        if "default_output_modes" in builder_config:
            kwargs["default_output_modes"] = builder_config["default_output_modes"]

        builder = AgentCardBuilder(**kwargs)

        assert builder.capabilities is not None
        assert builder.default_input_modes == expected_input_modes
        assert builder.default_output_modes == expected_output_modes
        assert builder.version == expected_version

        if expected_capabilities_is_custom:
            assert builder.capabilities == custom_capabilities
