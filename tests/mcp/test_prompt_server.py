from typing import Any

import pytest

from agents import Agent, Runner
from agents.mcp import MCPServer

from ..fake_model import FakeModel
from ..test_responses import get_text_message


class FakeMCPPromptServer(MCPServer):
    """Fake MCP server for testing prompt functionality"""

    def __init__(self, server_name: str = "fake_prompt_server"):
        self.prompts: list[Any] = []
        self.prompt_results: dict[str, str] = {}
        self._server_name = server_name

    def add_prompt(self, name: str, description: str, arguments: dict[str, Any] | None = None):
        """Add a prompt to the fake server"""
        from mcp.types import Prompt

        prompt = Prompt(name=name, description=description, arguments=[])
        self.prompts.append(prompt)

    def set_prompt_result(self, name: str, result: str):
        """Set the result that should be returned for a prompt"""
        self.prompt_results[name] = result

    async def connect(self):
        pass

    async def cleanup(self):
        pass

    async def list_prompts(self, run_context=None, agent=None):
        """List available prompts"""
        from mcp.types import ListPromptsResult

        return ListPromptsResult(prompts=self.prompts)

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None):
        """Get a prompt with arguments"""
        from mcp.types import GetPromptResult, PromptMessage, TextContent

        if name not in self.prompt_results:
            raise ValueError(f"Prompt '{name}' not found")

        content = self.prompt_results[name]

        # If it's a format string, try to format it with arguments
        if arguments and "{" in content:
            try:
                content = content.format(**arguments)
            except KeyError:
                pass  # Use original content if formatting fails

        message = PromptMessage(role="user", content=TextContent(type="text", text=content))

        return GetPromptResult(description=f"Generated prompt for {name}", messages=[message])

    async def list_tools(self, run_context=None, agent=None):
        return []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None):
        raise NotImplementedError("This fake server doesn't support tools")

    @property
    def name(self) -> str:
        return self._server_name


@pytest.mark.asyncio
async def test_list_prompts():
    """Test listing available prompts"""
    server = FakeMCPPromptServer()
    server.add_prompt(
        "generate_code_review_instructions", "Generate agent instructions for code review tasks"
    )

    result = await server.list_prompts()

    assert len(result.prompts) == 1
    assert result.prompts[0].name == "generate_code_review_instructions"
    assert "code review" in result.prompts[0].description


@pytest.mark.asyncio
async def test_get_prompt_without_arguments():
    """Test getting a prompt without arguments"""
    server = FakeMCPPromptServer()
    server.add_prompt("simple_prompt", "A simple prompt")
    server.set_prompt_result("simple_prompt", "You are a helpful assistant.")

    result = await server.get_prompt("simple_prompt")

    assert len(result.messages) == 1
    assert result.messages[0].content.text == "You are a helpful assistant."


@pytest.mark.asyncio
async def test_get_prompt_with_arguments():
    """Test getting a prompt with arguments"""
    server = FakeMCPPromptServer()
    server.add_prompt(
        "generate_code_review_instructions", "Generate agent instructions for code review tasks"
    )
    server.set_prompt_result(
        "generate_code_review_instructions",
        "You are a senior {language} code review specialist. Focus on {focus}.",
    )

    result = await server.get_prompt(
        "generate_code_review_instructions",
        {"focus": "security vulnerabilities", "language": "python"},
    )

    assert len(result.messages) == 1
    expected_text = (
        "You are a senior python code review specialist. Focus on security vulnerabilities."
    )
    assert result.messages[0].content.text == expected_text


@pytest.mark.asyncio
async def test_get_prompt_not_found():
    """Test getting a prompt that doesn't exist"""
    server = FakeMCPPromptServer()

    with pytest.raises(ValueError, match="Prompt 'nonexistent' not found"):
        await server.get_prompt("nonexistent")


@pytest.mark.asyncio
async def test_agent_with_prompt_instructions():
    """Test using prompt-generated instructions with an agent"""
    server = FakeMCPPromptServer()
    server.add_prompt(
        "generate_code_review_instructions", "Generate agent instructions for code review tasks"
    )
    server.set_prompt_result(
        "generate_code_review_instructions",
        "You are a code reviewer. Analyze the provided code for security issues.",
    )

    # Get instructions from prompt
    prompt_result = await server.get_prompt("generate_code_review_instructions")
    instructions = prompt_result.messages[0].content.text

    # Create agent with prompt-generated instructions
    model = FakeModel()
    agent = Agent(name="prompt_agent", instructions=instructions, model=model, mcp_servers=[server])

    # Mock model response
    model.add_multiple_turn_outputs(
        [[get_text_message("Code analysis complete. Found security vulnerability.")]]
    )

    # Run the agent
    result = await Runner.run(agent, input="Review this code: def unsafe_exec(cmd): os.system(cmd)")

    assert "Code analysis complete" in result.final_output
    assert (
        agent.instructions
        == "You are a code reviewer. Analyze the provided code for security issues."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_agent_with_prompt_instructions_streaming(streaming: bool):
    """Test using prompt-generated instructions with streaming and non-streaming"""
    server = FakeMCPPromptServer()
    server.add_prompt(
        "generate_code_review_instructions", "Generate agent instructions for code review tasks"
    )
    server.set_prompt_result(
        "generate_code_review_instructions",
        "You are a {language} code reviewer focusing on {focus}.",
    )

    # Get instructions from prompt with arguments
    prompt_result = await server.get_prompt(
        "generate_code_review_instructions", {"language": "Python", "focus": "security"}
    )
    instructions = prompt_result.messages[0].content.text

    # Create agent
    model = FakeModel()
    agent = Agent(
        name="streaming_prompt_agent", instructions=instructions, model=model, mcp_servers=[server]
    )

    model.add_multiple_turn_outputs([[get_text_message("Security analysis complete.")]])

    if streaming:
        streaming_result = Runner.run_streamed(agent, input="Review code")
        async for _ in streaming_result.stream_events():
            pass
        final_result = streaming_result.final_output
    else:
        result = await Runner.run(agent, input="Review code")
        final_result = result.final_output

    assert "Security analysis complete" in final_result
    assert agent.instructions == "You are a Python code reviewer focusing on security."


@pytest.mark.asyncio
async def test_multiple_prompts():
    """Test server with multiple prompts"""
    server = FakeMCPPromptServer()

    # Add multiple prompts
    server.add_prompt(
        "generate_code_review_instructions", "Generate agent instructions for code review tasks"
    )
    server.add_prompt(
        "generate_testing_instructions", "Generate agent instructions for testing tasks"
    )

    server.set_prompt_result("generate_code_review_instructions", "You are a code reviewer.")
    server.set_prompt_result("generate_testing_instructions", "You are a test engineer.")

    # Test listing prompts
    prompts_result = await server.list_prompts()
    assert len(prompts_result.prompts) == 2

    prompt_names = [p.name for p in prompts_result.prompts]
    assert "generate_code_review_instructions" in prompt_names
    assert "generate_testing_instructions" in prompt_names

    # Test getting each prompt
    review_result = await server.get_prompt("generate_code_review_instructions")
    assert review_result.messages[0].content.text == "You are a code reviewer."

    testing_result = await server.get_prompt("generate_testing_instructions")
    assert testing_result.messages[0].content.text == "You are a test engineer."


@pytest.mark.asyncio
async def test_prompt_with_complex_arguments():
    """Test prompt with complex argument formatting"""
    server = FakeMCPPromptServer()
    server.add_prompt(
        "generate_detailed_instructions", "Generate detailed instructions with multiple parameters"
    )
    server.set_prompt_result(
        "generate_detailed_instructions",
        "You are a {role} specialist. Your focus is on {focus}. "
        + "You work with {language} code. Your experience level is {level}.",
    )

    arguments = {
        "role": "security",
        "focus": "vulnerability detection",
        "language": "Python",
        "level": "senior",
    }

    result = await server.get_prompt("generate_detailed_instructions", arguments)

    expected = (
        "You are a security specialist. Your focus is on vulnerability detection. "
        "You work with Python code. Your experience level is senior."
    )
    assert result.messages[0].content.text == expected


@pytest.mark.asyncio
async def test_prompt_with_missing_arguments():
    """Test prompt with missing arguments in format string"""
    server = FakeMCPPromptServer()
    server.add_prompt("incomplete_prompt", "Prompt with missing arguments")
    server.set_prompt_result("incomplete_prompt", "You are a {role} working on {task}.")

    # Only provide one of the required arguments
    result = await server.get_prompt("incomplete_prompt", {"role": "developer"})

    # Should return the original string since formatting fails
    assert result.messages[0].content.text == "You are a {role} working on {task}."


@pytest.mark.asyncio
async def test_prompt_server_cleanup():
    """Test that prompt server cleanup works correctly"""
    server = FakeMCPPromptServer()
    server.add_prompt("test_prompt", "Test prompt")
    server.set_prompt_result("test_prompt", "Test result")

    # Test that server works before cleanup
    result = await server.get_prompt("test_prompt")
    assert result.messages[0].content.text == "Test result"

    # Cleanup should not raise any errors
    await server.cleanup()

    # Server should still work after cleanup (in this fake implementation)
    result = await server.get_prompt("test_prompt")
    assert result.messages[0].content.text == "Test result"
