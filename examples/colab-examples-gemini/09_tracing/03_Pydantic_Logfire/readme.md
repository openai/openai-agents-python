# Pydantic Logfire

This folder contains examples demonstrating the integration of Pydantic with Logfire for structured logging and tracing in LLM applications.

## Official Resources
- [OpenAI Agents SDK Documentation](https://openai.github.io/openai-agents-python/tracing/)
- [Pydantic Documentation](https://docs.pydantic.dev/)
- [Logfire Documentation](https://logfire.sh/docs)

## Features
- Structured logging with Pydantic models
- Real-time log aggregation
- Performance monitoring
- Error tracking
- Custom log attributes

## Setup Requirements
- OpenAI Agents SDK installed
- Python environment with Pydantic and Logfire
- Python 3.7+

## Example Structure
This folder will contain examples showing:
1. Basic Pydantic model logging
2. Custom log attributes
3. Performance tracking
4. Error handling
5. Log aggregation

## OpenAI Agents SDK Example with Pydantic Logfire
```python
from agents import Agent, Runner, trace
from pydantic import BaseModel
from logfire import Logfire
from typing import Dict, Any

class AgentLog(BaseModel):
    agent_name: str
    input: str
    output: str
    metadata: Dict[str, Any]
    processing_time: float
    tokens_used: int

async def main():
    # Initialize Logfire
    logfire = Logfire()
    
    # Create an agent
    agent = Agent(
        name="Code Generator",
        instructions="Generate Python code based on requirements."
    )

    # Create a trace for the code generation workflow
    with trace("Code Generation Workflow") as gen_trace:
        start_time = time.time()
        
        # Run the agent
        result = await Runner.run(
            agent,
            "Write a function to calculate factorial"
        )
        
        # Create structured log entry
        log_entry = AgentLog(
            agent_name=agent.name,
            input="Write a function to calculate factorial",
            output=result.final_output,
            metadata={
                "complexity": "medium",
                "language": "python",
                "model": "gpt-4"
            },
            processing_time=time.time() - start_time,
            tokens_used=result.usage.total_tokens
        )
        
        # Log the entry
        await logfire.log(log_entry)
        
        print(f"Generated code: {result.final_output}")

if __name__ == "__main__":
    import asyncio
    import time
    asyncio.run(main())
```

## Multi-Step Workflow Example
```python
from agents import Agent, Runner, trace
from pydantic import BaseModel
from logfire import Logfire
from typing import List, Dict, Any

class WorkflowStep(BaseModel):
    step_name: str
    input: str
    output: str
    duration: float
    metadata: Dict[str, Any]

class WorkflowLog(BaseModel):
    workflow_id: str
    steps: List[WorkflowStep]
    total_duration: float
    status: str

async def main():
    logfire = Logfire()
    
    # Create specialized agents
    code_agent = Agent(
        name="Code Generator",
        instructions="Generate Python code based on requirements."
    )
    
    test_agent = Agent(
        name="Test Generator",
        instructions="Generate unit tests for Python code."
    )

    # Create a trace for the multi-step workflow
    with trace("Code and Test Generation Workflow") as workflow_trace:
        workflow_start = time.time()
        steps = []
        
        # Step 1: Generate code
        code_start = time.time()
        code_result = await Runner.run(
            code_agent,
            "Write a function to calculate fibonacci numbers"
        )
        steps.append(WorkflowStep(
            step_name="code_generation",
            input="Write a function to calculate fibonacci numbers",
            output=code_result.final_output,
            duration=time.time() - code_start,
            metadata={"complexity": "medium"}
        ))
        
        # Step 2: Generate tests
        test_start = time.time()
        test_result = await Runner.run(
            test_agent,
            f"Write unit tests for this code: {code_result.final_output}"
        )
        steps.append(WorkflowStep(
            step_name="test_generation",
            input=f"Write unit tests for this code: {code_result.final_output}",
            output=test_result.final_output,
            duration=time.time() - test_start,
            metadata={"test_count": 3}
        ))
        
        # Create workflow log
        workflow_log = WorkflowLog(
            workflow_id=workflow_trace.id,
            steps=steps,
            total_duration=time.time() - workflow_start,
            status="completed"
        )
        
        # Log the workflow
        await logfire.log(workflow_log)
        
        print(f"Generated code: {code_result.final_output}")
        print(f"Generated tests: {test_result.final_output}")

if __name__ == "__main__":
    import asyncio
    import time
    asyncio.run(main()) 