import asyncio
import os
import shutil
import subprocess
import time
from typing import Any

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServer, MCPServerStreamableHttp
from agents.model_settings import ModelSettings


async def get_resource_content(mcp_server: MCPServer, uri: str) -> str:
    """Get resource content by URI"""
    print(f"Reading resource: {uri}")

    try:
        resource_result = await mcp_server.read_resource(uri)
        if resource_result.contents:
            content = resource_result.contents[0]
            # Handle both text and blob content
            if hasattr(content, "text"):
                return content.text
            else:
                return str(content)
        return "Resource content not available"
    except Exception as e:
        print(f"Failed to read resource: {e}")
        return f"Error reading resource: {e}"


async def show_available_resources(mcp_server: MCPServer):
    """Show available resources"""
    print("=== AVAILABLE RESOURCES ===")

    resources_result = await mcp_server.list_resources()
    print(f"Found {len(resources_result.resources)} resource(s):")
    for i, resource in enumerate(resources_result.resources, 1):
        description = (
            resource.description
            if hasattr(resource, "description") and resource.description
            else "No description"
        )
        mime_type = resource.mimeType if hasattr(resource, "mimeType") else "unknown"
        print(f"  {i}. {resource.name}")
        print(f"     URI: {resource.uri}")
        print(f"     Type: {mime_type}")
        print(f"     Description: {description}")
    print()


async def demo_config_assistant(mcp_server: MCPServer):
    """Demo: Assistant that uses configuration resource"""
    print("=== CONFIGURATION ASSISTANT DEMO ===")

    # Read configuration resource
    config = await get_resource_content(mcp_server, "config://app/settings")

    # Create agent with configuration context
    agent = Agent(
        name="Config Assistant",
        instructions=f"""You are a helpful assistant that knows about the application configuration.

Here is the current configuration:
{config}

Answer questions about the configuration clearly and concisely.""",
        model_settings=ModelSettings(tool_choice="auto"),
    )

    question = "What database is the application using and what's the connection pool size?"
    print(f"Question: {question}")

    result = await Runner.run(starting_agent=agent, input=question)
    print(f"Answer: {result.final_output}")
    print("\n" + "=" * 50 + "\n")


async def demo_api_documentation_assistant(mcp_server: MCPServer):
    """Demo: Assistant that uses API documentation resource"""
    print("=== API DOCUMENTATION ASSISTANT DEMO ===")

    # Read API documentation resource
    api_docs = await get_resource_content(mcp_server, "docs://api/overview")

    # Create agent with API documentation context
    agent = Agent(
        name="API Documentation Assistant",
        instructions=f"""You are an API documentation assistant.

Here is the API documentation:
{api_docs}

Help users understand and use the API effectively.""",
        model_settings=ModelSettings(tool_choice="auto"),
    )

    question = "How do I authenticate with the API and what are the rate limits?"
    print(f"Question: {question}")

    result = await Runner.run(starting_agent=agent, input=question)
    print(f"Answer: {result.final_output}")
    print("\n" + "=" * 50 + "\n")


async def demo_security_reviewer(mcp_server: MCPServer):
    """Demo: Security reviewer that uses security guidelines resource"""
    print("=== SECURITY REVIEWER DEMO ===")

    # Read security guidelines resource
    security_guidelines = await get_resource_content(mcp_server, "docs://security/guidelines")

    # Create agent with security context
    agent = Agent(
        name="Security Reviewer",
        instructions=f"""You are a security expert reviewing code and configurations.

Security Guidelines:
{security_guidelines}

Review code and provide security recommendations based on these guidelines.""",
        model_settings=ModelSettings(tool_choice="auto"),
    )

    code_to_review = """
def login(username, password):
    query = f"SELECT * FROM users WHERE username='{username}' AND password='{password}'"
    result = db.execute(query)
    return result
"""

    print(f"Code to review: {code_to_review}")

    result = await Runner.run(
        starting_agent=agent,
        input=f"Please review this login function for security issues:\n{code_to_review}",
    )
    print(f"Security Review: {result.final_output}")
    print("\n" + "=" * 50 + "\n")


async def demo_metrics_analyzer(mcp_server: MCPServer):
    """Demo: Metrics analyzer that uses metrics resource"""
    print("=== METRICS ANALYZER DEMO ===")

    # Read metrics resource
    metrics = await get_resource_content(mcp_server, "data://metrics/summary")

    # Create agent with metrics context
    agent = Agent(
        name="Metrics Analyzer",
        instructions=f"""You are a system performance analyst.

Current System Metrics:
{metrics}

Analyze the metrics and provide insights about system performance and health.""",
        model_settings=ModelSettings(tool_choice="auto"),
    )

    question = "What's the current system health and are there any concerning metrics?"
    print(f"Question: {question}")

    result = await Runner.run(starting_agent=agent, input=question)
    print(f"Analysis: {result.final_output}")
    print("\n" + "=" * 50 + "\n")


async def main():
    async with MCPServerStreamableHttp(
        name="Resource Server",
        params={"url": "http://localhost:8000/mcp"},
    ) as server:
        trace_id = gen_trace_id()
        with trace(workflow_name="MCP Resource Demo", trace_id=trace_id):
            print(f"Trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")

            await show_available_resources(server)
            await demo_config_assistant(server)
            await demo_api_documentation_assistant(server)
            await demo_security_reviewer(server)
            await demo_metrics_analyzer(server)


if __name__ == "__main__":
    if not shutil.which("uv"):
        raise RuntimeError("uv is not installed")

    process: subprocess.Popen[Any] | None = None
    try:
        this_dir = os.path.dirname(os.path.abspath(__file__))
        server_file = os.path.join(this_dir, "server.py")

        print("Starting Resource Server...")
        process = subprocess.Popen(["uv", "run", server_file])
        time.sleep(3)
        print("Server started\n")
    except Exception as e:
        print(f"Error starting server: {e}")
        exit(1)

    try:
        asyncio.run(main())
    finally:
        if process:
            process.terminate()
            print("Server terminated.")
