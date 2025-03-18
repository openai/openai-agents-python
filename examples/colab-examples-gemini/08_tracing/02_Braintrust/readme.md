# Braintrust

This folder contains examples demonstrating the use of Braintrust for LLM application development and evaluation.

## Official Resources
- [OpenAI Agents SDK Documentation](https://openai.github.io/openai-agents-python/tracing/)
- [Braintrust Documentation](https://www.braintrustdata.com/docs)

## Features
- LLM application development framework
- Evaluation tools
- Performance tracking
- Cost monitoring
- A/B testing capabilities

## Setup Requirements
- OpenAI Agents SDK installed
- Braintrust account
- Python environment with async support

## Example Structure
This folder will contain examples showing:
1. Basic Braintrust setup
2. Evaluation workflows
3. Performance tracking
4. A/B testing
5. Cost analysis

## OpenAI Agents SDK Example with Braintrust
```python
from agents import Agent, Runner, trace
from braintrust import Braintrust

async def main():
    # Initialize Braintrust
    braintrust = Braintrust()
    
    # Create an agent
    agent = Agent(
        name="Code Evaluator",
        instructions="Evaluate code quality and suggest improvements."
    )

    # Create a trace for the evaluation workflow
    with trace("Code Evaluation Workflow") as eval_trace:
        # Run the agent
        result = await Runner.run(
            agent,
            "Review this code: def add(a,b): return a+b"
        )
        
        # Log evaluation metrics to Braintrust
        await braintrust.log(
            input="def add(a,b): return a+b",
            output=result.final_output,
            metadata={
                "complexity": "low",
                "language": "python",
                "evaluation_score": 0.85
            }
        )
        
        print(f"Evaluation: {result.final_output}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

## Multi-Agent Evaluation Example
```python
from agents import Agent, Runner, trace
from braintrust import Braintrust

async def main():
    braintrust = Braintrust()
    
    # Create multiple agents for different aspects of evaluation
    code_agent = Agent(
        name="Code Quality Evaluator",
        instructions="Evaluate code quality and best practices."
    )
    
    security_agent = Agent(
        name="Security Evaluator",
        instructions="Evaluate code security and potential vulnerabilities."
    )

    # Create a trace for the multi-agent evaluation
    with trace("Multi-Agent Code Evaluation"):
        # Run code quality evaluation
        quality_result = await Runner.run(
            code_agent,
            "Review this code: def process_data(data): return data.strip()"
        )
        
        # Run security evaluation
        security_result = await Runner.run(
            security_agent,
            "Review this code for security: def process_data(data): return data.strip()"
        )
        
        # Log combined evaluation to Braintrust
        await braintrust.log(
            input="def process_data(data): return data.strip()",
            output={
                "quality_review": quality_result.final_output,
                "security_review": security_result.final_output
            },
            metadata={
                "language": "python",
                "quality_score": 0.9,
                "security_score": 0.95
            }
        )
        
        print(f"Quality Review: {quality_result.final_output}")
        print(f"Security Review: {security_result.final_output}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
``` 