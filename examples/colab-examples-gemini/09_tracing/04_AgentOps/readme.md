# AgentOps

This folder contains examples demonstrating the use of AgentOps for monitoring and managing AI agents.

## Official Resources
- [OpenAI Agents SDK Documentation](https://openai.github.io/openai-agents-python/tracing/)
- [AgentOps Documentation](https://docs.agentops.ai/)

## Features
- Agent performance monitoring
- Cost tracking
- Error analysis
- Usage analytics
- Agent debugging tools

## Setup Requirements
- OpenAI Agents SDK installed
- AgentOps account
- Python environment with async support

## Example Structure
This folder will contain examples showing:
1. Basic AgentOps setup
2. Agent monitoring
3. Performance tracking
4. Cost analysis
5. Debugging tools

## OpenAI Agents SDK Example with AgentOps
```python
from agents import Agent, Runner, trace
from agentops import AgentOps

async def main():
    # Initialize AgentOps
    agentops = AgentOps()
    
    # Create an agent
    agent = Agent(
        name="Code Reviewer",
        instructions="Review code and suggest improvements."
    )

    # Create a trace for the code review workflow
    with trace("Code Review Workflow") as review_trace:
        # Start monitoring the agent
        with agentops.monitor():
            # Run the agent
            result = await Runner.run(
                agent,
                "Review this code: def process_data(data): return data.strip()"
            )
            
            # Log metrics
            agentops.log_metric("code_complexity", "low")
            agentops.log_metric("review_duration", 2.5)
            agentops.log_metric("suggestions_count", 3)
            
            print(f"Review: {result.final_output}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

## Multi-Agent Monitoring Example
```python
from agents import Agent, Runner, trace
from agentops import AgentOps

async def main():
    agentops = AgentOps()
    
    # Create specialized agents
    code_agent = Agent(
        name="Code Generator",
        instructions="Generate Python code based on requirements."
    )
    
    test_agent = Agent(
        name="Test Generator",
        instructions="Generate unit tests for Python code."
    )

    # Create a trace for the multi-agent workflow
    with trace("Code and Test Generation Workflow"):
        # Monitor both agents
        with agentops.monitor():
            # Step 1: Generate code
            code_result = await Runner.run(
                code_agent,
                "Write a function to calculate factorial"
            )
            
            # Log code generation metrics
            agentops.log_metric("code_complexity", "medium")
            agentops.log_metric("code_length", len(code_result.final_output))
            
            # Step 2: Generate tests
            test_result = await Runner.run(
                test_agent,
                f"Write unit tests for this code: {code_result.final_output}"
            )
            
            # Log test generation metrics
            agentops.log_metric("test_count", 3)
            agentops.log_metric("test_coverage", 0.95)
            
            print(f"Generated code: {code_result.final_output}")
            print(f"Generated tests: {test_result.final_output}")
            
            # Get performance metrics
            metrics = agentops.get_metrics()
            print(f"Performance metrics: {metrics}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main()) 