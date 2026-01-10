# Guardrails

Guardrails enable you to do checks and validations of user input and agent output. For example, imagine you have an agent that uses a very smart (and hence slow/expensive) model to help with customer requests. You wouldn't want malicious users to ask the model to help them with their math homework. So, you can run a guardrail with a fast/cheap model. If the guardrail detects malicious usage, it can immediately raise an error and prevent the expensive model from running, saving you time and money (**when using blocking guardrails; for parallel guardrails, the expensive model may have already started running before the guardrail completes. See "Execution modes" below for details**).

There are two kinds of guardrails:

1. Input guardrails run on the initial user input
2. Output guardrails run on the final agent output

## Input guardrails

Input guardrails run in 3 steps:

1. First, the guardrail receives the same input passed to the agent.
2. Next, the guardrail function runs to produce a [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput], which is then wrapped in an [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]
3. Finally, we check if [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] is true. If true, an [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] exception is raised, so you can appropriately respond to the user or handle the exception.

!!! Note

    Input guardrails are intended to run on user input, so an agent's guardrails only run if the agent is the *first* agent. You might wonder, why is the `guardrails` property on the agent instead of passed to `Runner.run`? It's because guardrails tend to be related to the actual Agent - you'd run different guardrails for different agents, so colocating the code is useful for readability.

### Execution modes

Input guardrails support two execution modes:

- **Parallel execution** (default, `run_in_parallel=True`): The guardrail runs concurrently with the agent's execution. This provides the best latency since both start at the same time. However, if the guardrail fails, the agent may have already consumed tokens and executed tools before being cancelled.

- **Blocking execution** (`run_in_parallel=False`): The guardrail runs and completes *before* the agent starts. If the guardrail tripwire is triggered, the agent never executes, preventing token consumption and tool execution. This is ideal for cost optimization and when you want to avoid potential side effects from tool calls.

## Output guardrails

Output guardrails run in 3 steps:

1. First, the guardrail receives the output produced by the agent.
2. Next, the guardrail function runs to produce a [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput], which is then wrapped in an [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]
3. Finally, we check if [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] is true. If true, an [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] exception is raised, so you can appropriately respond to the user or handle the exception.

!!! Note

    Output guardrails are intended to run on the final agent output, so an agent's guardrails only run if the agent is the *last* agent. Similar to the input guardrails, we do this because guardrails tend to be related to the actual Agent - you'd run different guardrails for different agents, so colocating the code is useful for readability.

    Output guardrails always run after the agent completes, so they don't support the `run_in_parallel` parameter.

## Tripwires

If the input or output fails the guardrail, the Guardrail can signal this with a tripwire. As soon as we see a guardrail that has triggered the tripwires, we immediately raise a `{Input,Output}GuardrailTripwireTriggered` exception and halt the Agent execution.

## Implementing a guardrail

You need to provide a function that receives input, and returns a [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]. In this example, we'll do this by running an Agent under the hood.

```python
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
    input_guardrail,
)

class MathHomeworkOutput(BaseModel):
    is_math_homework: bool
    reasoning: str

guardrail_agent = Agent( # (1)!
    name="Guardrail check",
    instructions="Check if the user is asking you to do their math homework.",
    output_type=MathHomeworkOutput,
)


@input_guardrail
async def math_guardrail( # (2)!
    ctx: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    result = await Runner.run(guardrail_agent, input, context=ctx.context)

    return GuardrailFunctionOutput(
        output_info=result.final_output, # (3)!
        tripwire_triggered=result.final_output.is_math_homework,
    )


agent = Agent(  # (4)!
    name="Customer support agent",
    instructions="You are a customer support agent. You help customers with their questions.",
    input_guardrails=[math_guardrail],
)

async def main():
    # This should trip the guardrail
    try:
        await Runner.run(agent, "Hello, can you help me solve for x: 2x + 3 = 11?")
        print("Guardrail didn't trip - this is unexpected")

    except InputGuardrailTripwireTriggered:
        print("Math homework guardrail tripped")
```

1. We'll use this agent in our guardrail function.
2. This is the guardrail function that receives the agent's input/context, and returns the result.
3. We can include extra information in the guardrail result.
4. This is the actual agent that defines the workflow.

Output guardrails are similar.

```python
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    output_guardrail,
)
class MessageOutput(BaseModel): # (1)!
    response: str

class MathOutput(BaseModel): # (2)!
    reasoning: str
    is_math: bool

guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the output includes any math.",
    output_type=MathOutput,
)

@output_guardrail
async def math_guardrail(  # (3)!
    ctx: RunContextWrapper, agent: Agent, output: MessageOutput
) -> GuardrailFunctionOutput:
    result = await Runner.run(guardrail_agent, output.response, context=ctx.context)

    return GuardrailFunctionOutput(
        output_info=result.final_output,
        tripwire_triggered=result.final_output.is_math,
    )

agent = Agent( # (4)!
    name="Customer support agent",
    instructions="You are a customer support agent. You help customers with their questions.",
    output_guardrails=[math_guardrail],
    output_type=MessageOutput,
)

async def main():
    # This should trip the guardrail
    try:
        await Runner.run(agent, "Hello, can you help me solve for x: 2x + 3 = 11?")
        print("Guardrail didn't trip - this is unexpected")

    except OutputGuardrailTripwireTriggered:
        print("Math output guardrail tripped")
```

1. This is the actual agent's output type.
2. This is the guardrail's output type.
3. This is the guardrail function that receives the agent's output, and returns the result.
4. This is the actual agent that defines the workflow.

## Tool guardrails

In addition to agent-level input and output guardrails, you can apply guardrails directly to individual tools. **Tool guardrails** validate the arguments passed to a tool or the results it returns, rather than the overall agent input/output.

There are two types of tool guardrails:

1. **Tool input guardrails** run *before* the tool executes, validating its arguments
2. **Tool output guardrails** run *after* the tool executes, validating its return value

!!! Note

    Tool guardrails are different from agent-level guardrails:

    - **Agent guardrails** run on user input (first agent) or final output (last agent)
    - **Tool guardrails** run on every invocation of a specific tool, regardless of which agent calls it

### Tool input guardrails

Tool input guardrails run before the tool function executes. They receive a [`ToolInputGuardrailData`][agents.tool_guardrails.ToolInputGuardrailData] object containing:

- The [`ToolContext`][agents.tool_context.ToolContext] with tool name, arguments, and call ID
- The [`Agent`][agents.agent.Agent] that is executing the tool

```python
import json

from agents import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    function_tool,
    tool_input_guardrail,
)


@tool_input_guardrail
def validate_email_args(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Block emails containing suspicious keywords."""
    args = json.loads(data.context.tool_arguments) if data.context.tool_arguments else {}

    blocked_words = ["password", "hack", "exploit"]
    for key, value in args.items():
        value_str = str(value).lower()
        for word in blocked_words:
            if word in value_str:
                return ToolGuardrailFunctionOutput.reject_content(
                    message=f"Email blocked: contains '{word}'",
                    output_info={"blocked_word": word},
                )

    return ToolGuardrailFunctionOutput(output_info="Validated")


@function_tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email."""
    return f"Email sent to {to}"


# Attach the guardrail to the tool
send_email.tool_input_guardrails = [validate_email_args]
```

### Tool output guardrails

Tool output guardrails run after the tool function executes. They receive a [`ToolOutputGuardrailData`][agents.tool_guardrails.ToolOutputGuardrailData] object which extends the input data with:

- The `output` produced by the tool function

```python
from agents import (
    ToolGuardrailFunctionOutput,
    ToolOutputGuardrailData,
    ToolOutputGuardrailTripwireTriggered,
    function_tool,
    tool_output_guardrail,
)


@tool_output_guardrail
def block_sensitive_data(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Block outputs containing sensitive information like SSNs."""
    output_str = str(data.output).lower()

    if "ssn" in output_str or "123-45-6789" in output_str:
        # Halt execution completely for sensitive data.
        return ToolGuardrailFunctionOutput.raise_exception(
            output_info={"blocked_pattern": "SSN", "tool": data.context.tool_name},
        )

    return ToolGuardrailFunctionOutput(output_info="Output validated")


@function_tool
def get_user_data(user_id: str) -> dict:
    """Get user data by ID."""
    return {"user_id": user_id, "name": "John", "ssn": "123-45-6789"}


# Attach the guardrail to the tool
get_user_data.tool_output_guardrails = [block_sensitive_data]
```

### Guardrail behavior types

Tool guardrails return a [`ToolGuardrailFunctionOutput`][agents.tool_guardrails.ToolGuardrailFunctionOutput] that specifies how the system should respond:

| Behavior | Method | Effect |
|----------|--------|--------|
| **Allow** | `ToolGuardrailFunctionOutput(output_info=...)` | Continue normal execution (default) |
| **Reject content** | `.reject_content(message, output_info)` | Block the tool call but continue agent execution with a message |
| **Raise exception** | `.raise_exception(output_info)` | Halt execution by raising `ToolInputGuardrailTripwireTriggered` or `ToolOutputGuardrailTripwireTriggered` |

Use `reject_content` when you want to gracefully handle a violation and let the agent continue. Use `raise_exception` for critical violations that must stop all execution immediately.

### Handling tool guardrail exceptions

When a guardrail uses `raise_exception()`, you can catch it to handle the violation:

```python
from agents import Agent, Runner, ToolOutputGuardrailTripwireTriggered


agent = Agent(
    name="Assistant",
    instructions="You help users retrieve data.",
    tools=[get_user_data],  # Tool with output guardrail attached
)


async def main():
    try:
        result = await Runner.run(agent, "Get data for user123")
        print(result.final_output)
    except ToolOutputGuardrailTripwireTriggered as e:
        print(f"Blocked: {e.output.output_info}")
        # Handle the violation appropriately
```