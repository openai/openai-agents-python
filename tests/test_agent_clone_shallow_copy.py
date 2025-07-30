from openai.agents import Agent, function_tool, handoff


@function_tool
def greet(name: str) -> str:
    return f"Hello, {name}!"


def test_agent_clone_shallow_copy():
    target_agent = Agent(name="Target")
    original = Agent(
        name="Original",
        instructions="Testing clone shallow copy",
        tools=[greet],
        handoffs=[handoff(target_agent)],
    )

    cloned = original.clone(name="Cloned")

    # Ensure new lists, but same inner objects
    assert cloned.tools is not original.tools
    assert cloned.tools[0] is original.tools[0]
    assert cloned.handoffs is not original.handoffs
    assert cloned.handoffs[0] is original.handoffs[0]
