# OpenAI Traces Dashboard

This folder contains examples demonstrating the use of OpenAI's Traces Dashboard for monitoring and analyzing LLM applications.

## Official Resources
- [OpenAI Agents SDK Documentation](https://openai.github.io/openai-agents-python/tracing/)
- [OpenAI Traces Dashboard](https://platform.openai.com/traces)

## Features
- Real-time monitoring of LLM calls
- Performance analytics
- Cost tracking
- Error analysis
- Request/response visualization

## Setup Requirements
- OpenAI API key
- OpenAI Agents SDK installed
- Python environment with async support

## Example Structure
This folder will contain examples showing:
1. Basic tracing setup
2. Custom trace attributes
3. Performance monitoring
4. Error tracking
5. Cost analysis

## OpenAI Agents SDK Example
```python
from agents import Agent, Runner, trace

async def main():
    # Create an agent
    agent = Agent(
        name="Code Assistant",
        instructions="You are a helpful coding assistant."
    )

    # Create a trace for the workflow
    with trace("Code Generation Workflow") as my_trace:
        # Run the agent
        result = await Runner.run(
            agent,
            "Write a Python function to calculate fibonacci numbers"
        )
        
        # Add custom metadata to the trace
        my_trace.metadata["complexity"] = "medium"
        my_trace.metadata["language"] = "python"
        
        print(f"Generated code: {result.final_output}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

## Higher Level Traces Example
```python
from agents import Agent, Runner, trace

async def main():
    agent = Agent(name="Code Reviewer", instructions="Review code and suggest improvements.")

    # Wrap multiple runs in a single trace
    with trace("Code Review Workflow"):
        # First run: Generate code
        code_result = await Runner.run(
            agent,
            "Write a function to sort a list"
        )
        
        # Second run: Review the code
        review_result = await Runner.run(
            agent,
            f"Review this code and suggest improvements: {code_result.final_output}"
        )
        
        print(f"Generated code: {code_result.final_output}")
        print(f"Review: {review_result.final_output}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
``` 