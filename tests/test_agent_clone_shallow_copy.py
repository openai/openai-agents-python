from agents import Agent, function_tool, handoff


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
        name="Cloned",
        tools=original.tools.copy(),
        handoffs=original.handoffs.copy()
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
