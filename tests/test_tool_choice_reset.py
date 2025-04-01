import pytest

from agents import Agent, ModelSettings, Runner
from agents._run_impl import AgentToolUseTracker, RunImpl

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


class TestToolChoiceReset:
    def test_should_reset_tool_choice_direct(self):
        """
        Test the _should_reset_tool_choice method directly with various inputs
        to ensure it correctly identifies cases where reset is needed.
        """
        agent = Agent(name="test_agent")

        # Case 1: Empty tool use tracker should not change the "None" tool choice
        model_settings = ModelSettings(tool_choice=None)
        tracker = AgentToolUseTracker()
        new_settings = RunImpl.maybe_reset_tool_choice(agent, tracker, model_settings)
        assert new_settings.tool_choice == model_settings.tool_choice

        # Case 2: Empty tool use tracker should not change the "auto" tool choice
        model_settings = ModelSettings(tool_choice="auto")
        tracker = AgentToolUseTracker()
        new_settings = RunImpl.maybe_reset_tool_choice(agent, tracker, model_settings)
        assert model_settings.tool_choice == new_settings.tool_choice

        # Case 3: Empty tool use tracker should not change the "required" tool choice
        model_settings = ModelSettings(tool_choice="required")
        tracker = AgentToolUseTracker()
        new_settings = RunImpl.maybe_reset_tool_choice(agent, tracker, model_settings)
        assert model_settings.tool_choice == new_settings.tool_choice

        # Case 4: tool_choice = "required" with one tool should reset
        model_settings = ModelSettings(tool_choice="required")
        tracker = AgentToolUseTracker()
        tracker.add_tool_use(agent, ["tool1"])
        new_settings = RunImpl.maybe_reset_tool_choice(agent, tracker, model_settings)
        assert new_settings.tool_choice is None

        # Case 5: tool_choice = "required" with multiple tools should reset
        model_settings = ModelSettings(tool_choice="required")
        tracker = AgentToolUseTracker()
        tracker.add_tool_use(agent, ["tool1", "tool2"])
        new_settings = RunImpl.maybe_reset_tool_choice(agent, tracker, model_settings)
        assert new_settings.tool_choice is None

        # Case 6: Tool usage on a different agent should not affect the tool choice
        model_settings = ModelSettings(tool_choice="foo_bar")
        tracker = AgentToolUseTracker()
        tracker.add_tool_use(Agent(name="other_agent"), ["foo_bar", "baz"])
        new_settings = RunImpl.maybe_reset_tool_choice(agent, tracker, model_settings)
        assert new_settings.tool_choice == model_settings.tool_choice

        # Case 7: tool_choice = "foo_bar" with multiple tools should reset
        model_settings = ModelSettings(tool_choice="foo_bar")
        tracker = AgentToolUseTracker()
        tracker.add_tool_use(agent, ["foo_bar", "baz"])
        new_settings = RunImpl.maybe_reset_tool_choice(agent, tracker, model_settings)
        assert new_settings.tool_choice is None

    @pytest.mark.asyncio
    async def test_required_tool_choice_with_multiple_runs(self):
        """
        Test scenario 1: When multiple runs are executed with tool_choice="required", ensure each
        run works correctly and doesn't get stuck in an infinite loop. Also verify that tool_choice
        remains "required" between runs.
        """
        # Set up our fake model with responses for two runs
        fake_model = FakeModel()
        fake_model.add_multiple_turn_outputs(
            [[get_text_message("First run response")], [get_text_message("Second run response")]]
        )

        # Create agent with a custom tool and tool_choice="required"
        custom_tool = get_function_tool("custom_tool")
        agent = Agent(
            name="test_agent",
            model=fake_model,
            tools=[custom_tool],
            model_settings=ModelSettings(tool_choice="required"),
        )

        # First run should work correctly and preserve tool_choice
        result1 = await Runner.run(agent, "first run")
        assert result1.final_output == "First run response"
        assert fake_model.last_turn_args["model_settings"].tool_choice == "required", (
            "tool_choice should stay required"
        )

        # Second run should also work correctly with tool_choice still required
        result2 = await Runner.run(agent, "second run")
        assert result2.final_output == "Second run response"
        assert fake_model.last_turn_args["model_settings"].tool_choice == "required", (
            "tool_choice should stay required"
        )

    @pytest.mark.asyncio
    async def test_required_with_stop_at_tool_name(self):
        """
        Test scenario 2: When using required tool_choice with stop_at_tool_names behavior, ensure
        it correctly stops at the specified tool
        """
        # Set up fake model to return a tool call for second_tool
        fake_model = FakeModel()
        fake_model.set_next_output([get_function_tool_call("second_tool", "{}")])

        # Create agent with two tools and tool_choice="required" and stop_at_tool behavior
        first_tool = get_function_tool("first_tool", return_value="first tool result")
        second_tool = get_function_tool("second_tool", return_value="second tool result")

        agent = Agent(
            name="test_agent",
            model=fake_model,
            tools=[first_tool, second_tool],
            model_settings=ModelSettings(tool_choice="required"),
            tool_use_behavior={"stop_at_tool_names": ["second_tool"]},
        )

        # Run should stop after using second_tool
        result = await Runner.run(agent, "run test")
        assert result.final_output == "second tool result"

    @pytest.mark.asyncio
    async def test_specific_tool_choice(self):
        """
        Test scenario 3: When using a specific tool choice name, ensure it doesn't cause infinite
        loops.
        """
        # Set up fake model to return a text message
        fake_model = FakeModel()
        fake_model.set_next_output([get_text_message("Test message")])

        # Create agent with specific tool_choice
        tool1 = get_function_tool("tool1")
        tool2 = get_function_tool("tool2")
        tool3 = get_function_tool("tool3")

        agent = Agent(
            name="test_agent",
            model=fake_model,
            tools=[tool1, tool2, tool3],
            model_settings=ModelSettings(tool_choice="tool1"),  # Specific tool
        )

        # Run should complete without infinite loops
        result = await Runner.run(agent, "first run")
        assert result.final_output == "Test message"

    @pytest.mark.asyncio
    async def test_required_with_single_tool(self):
        """
        Test scenario 4: When using required tool_choice with only one tool, ensure it doesn't cause
        infinite loops.
        """
        # Set up fake model to return a tool call followed by a text message
        fake_model = FakeModel()
        fake_model.add_multiple_turn_outputs(
            [
                # First call returns a tool call
                [get_function_tool_call("custom_tool", "{}")],
                # Second call returns a text message
                [get_text_message("Final response")],
            ]
        )

        # Create agent with a single tool and tool_choice="required"
        custom_tool = get_function_tool("custom_tool", return_value="tool result")
        agent = Agent(
            name="test_agent",
            model=fake_model,
            tools=[custom_tool],
            model_settings=ModelSettings(tool_choice="required"),
        )

        # Run should complete without infinite loops
        result = await Runner.run(agent, "first run")
        assert result.final_output == "Final response"

    @pytest.mark.asyncio
    async def test_dont_reset_tool_choice_if_not_required(self):
        """
        Test scenario 5: When agent.reset_tool_choice is False, ensure tool_choice is not reset.
        """
        # Set up fake model to return a tool call followed by a text message
        fake_model = FakeModel()
        fake_model.add_multiple_turn_outputs(
            [
                # First call returns a tool call
                [get_function_tool_call("custom_tool", "{}")],
                # Second call returns a text message
                [get_text_message("Final response")],
            ]
        )

        # Create agent with a single tool and tool_choice="required" and reset_tool_choice=False
        custom_tool = get_function_tool("custom_tool", return_value="tool result")
        agent = Agent(
            name="test_agent",
            model=fake_model,
            tools=[custom_tool],
            model_settings=ModelSettings(tool_choice="required"),
            reset_tool_choice=False,
        )

        await Runner.run(agent, "test")

        assert fake_model.last_turn_args["model_settings"].tool_choice == "required"
