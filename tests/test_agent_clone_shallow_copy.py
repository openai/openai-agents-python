from agents import Agent, ModelSettings, function_tool, handoff


@function_tool
def greet(name: str) -> str:
    return f"Hello, {name}!"


def test_agent_clone_shallow_copy():
    """Test that clone creates shallow copy with tools.copy() workaround"""
    target_agent = Agent(name="Target")
    original = Agent(
        name="Original",
        instructions="Testing clone shallow copy",
        tools=[greet],
        handoffs=[handoff(target_agent)],
    )

    cloned = original.clone(
        name="Cloned", tools=original.tools.copy(), handoffs=original.handoffs.copy()
    )

    # Basic assertions
    assert cloned is not original
    assert cloned.name == "Cloned"
    assert cloned.instructions == original.instructions

    # Shallow copy assertions
    assert cloned.tools is not original.tools, "Tools should be different list"
    assert cloned.tools[0] is original.tools[0], "Tool objects should be same instance"
    assert cloned.handoffs is not original.handoffs, "Handoffs should be different list"
    assert cloned.handoffs[0] is original.handoffs[0], "Handoff objects should be same instance"


def test_agent_clone_keeps_list_attributes_it_is_not_given():
    """An attribute that clone() is not given arrives as the original agent's own list."""
    target_agent = Agent(name="Target")
    original = Agent(name="Original", tools=[greet], handoffs=[handoff(target_agent)])

    cloned = original.clone(name="Cloned")

    assert cloned.tools is original.tools
    assert cloned.handoffs is original.handoffs


def test_agent_clone_uses_a_given_list_as_is():
    """An attribute passed to clone() is used exactly as given, entries included."""

    @function_tool
    def farewell(name: str) -> str:
        return f"Goodbye, {name}!"

    original = Agent(name="Original", tools=[greet])
    supplied = [farewell]

    cloned = original.clone(name="Cloned", tools=supplied)

    assert cloned.tools is supplied
    assert original.tools == [greet]
    # Passing a list does not by itself share entries with the original agent.
    assert all(tool is not greet for tool in cloned.tools)


def test_agent_clone_still_shares_when_given_the_original_list():
    """Passing the original agent's own list keeps both agents on that one list."""
    original = Agent(name="Original", tools=[greet])

    cloned = original.clone(name="Cloned", tools=original.tools)

    assert cloned.tools is original.tools


def test_agent_clone_shared_list_mutation_affects_both_agents():
    """Appending through either agent changes the other while they hold one list."""
    original = Agent(name="Original", tools=[greet])
    cloned = original.clone(name="Cloned")

    cloned.tools.append(greet)

    assert original.tools == cloned.tools
    assert len(original.tools) == 2


def test_agent_clone_shares_non_list_mutable_attributes():
    """`model_settings` and `mcp_config` are shared too, which the list wording does not cover."""
    agent = Agent(
        name="Original",
        model="gpt-4o",
        model_settings=ModelSettings(temperature=0.1),
        mcp_config={"convert_schemas_to_strict": True},
    )

    cloned = agent.clone(instructions="Changed")

    assert cloned.model_settings is agent.model_settings
    assert cloned.mcp_config is agent.mcp_config

    cloned.model_settings.temperature = 0.9
    cloned.mcp_config["convert_schemas_to_strict"] = False
    assert agent.model_settings.temperature == 0.9
    assert agent.mcp_config["convert_schemas_to_strict"] is False


def test_agent_clone_with_only_model_override_keeps_shared_model_settings():
    """Overriding `model` alone does not detach explicit settings from the original."""
    agent = Agent(name="Original", model="gpt-4o", model_settings=ModelSettings(temperature=0.1))

    cloned = agent.clone(model="gpt-4o-mini")

    assert cloned.model_settings is agent.model_settings
