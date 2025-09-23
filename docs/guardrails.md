# Guardrails

Guardrails run _in parallel_ to your agents, enabling you to do checks and validations of user input. For example, imagine you have an agent that uses a very smart (and hence slow/expensive) model to help with customer requests. You wouldn't want malicious users to ask the model to help them with their math homework. So, you can run a guardrail with a fast/cheap model. If the guardrail detects malicious usage, it can immediately raise an error, which stops the expensive model from running and saves you time/money.

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

## Output guardrails

Output guardrails run in 3 steps:

1. First, the guardrail receives the output produced by the agent.
2. Next, the guardrail function runs to produce a [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput], which is then wrapped in an [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]
3. Finally, we check if [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] is true. If true, an [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] exception is raised, so you can appropriately respond to the user or handle the exception.

!!! Note

    Output guardrails are intended to run on the final agent output, so an agent's guardrails only run if the agent is the *last* agent. Similar to the input guardrails, we do this because guardrails tend to be related to the actual Agent - you'd run different guardrails for different agents, so colocating the code is useful for readability.

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

Tool guardrails provide fine-grained control over individual tool calls, allowing you to validate inputs and outputs at the tool level. This is particularly useful for:

- Blocking sensitive data in tool arguments
- Preventing unauthorized access to certain tools
- Sanitizing tool outputs before they're returned
- Implementing custom validation logic for specific tools

There are two types of tool guardrails:

1. **Tool input guardrails** run before a tool is executed, validating the tool call arguments
2. **Tool output guardrails** run after a tool is executed, validating the tool's output

### Tool input guardrails

Tool input guardrails run in 3 steps:

1. First, the guardrail receives the tool call data including arguments, context, and agent information
2. Next, the guardrail function runs to produce a [`ToolGuardrailFunctionOutput`][agents.tool_guardrails.ToolGuardrailFunctionOutput]
3. Finally, we check if [`.tripwire_triggered`][agents.tool_guardrails.ToolGuardrailFunctionOutput.tripwire_triggered] is true. If true, a [`ToolInputGuardrailTripwireTriggered`][agents.exceptions.ToolInputGuardrailTripwireTriggered] exception is raised

### Tool output guardrails

Tool output guardrails run in 3 steps:

1. First, the guardrail receives the tool call data plus the tool's output
2. Next, the guardrail function runs to produce a [`ToolGuardrailFunctionOutput`][agents.tool_guardrails.ToolGuardrailFunctionOutput]
3. Finally, we check if [`.tripwire_triggered`][agents.tool_guardrails.ToolGuardrailFunctionOutput.tripwire_triggered] is true. If true, a [`ToolOutputGuardrailTripwireTriggered`][agents.exceptions.ToolOutputGuardrailTripwireTriggered] exception is raised

### Implementing tool guardrails

You can create tool guardrails using the `@tool_input_guardrail` and `@tool_output_guardrail` decorators:

```python
from agents import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    tool_input_guardrail,
    tool_output_guardrail,
)

@tool_input_guardrail
def block_sensitive_words(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Block tool calls that contain sensitive words in arguments."""
    # Check arguments for sensitive content
    if "password" in data.tool_call.arguments.lower():
        return ToolGuardrailFunctionOutput(
            tripwire_triggered=True,
            model_message="🚨 Tool call blocked: contains sensitive word",
            output_info={"blocked_word": "password"},
        )
    return ToolGuardrailFunctionOutput(tripwire_triggered=False, output_info="Input validated")

@tool_output_guardrail
def block_sensitive_output(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Block tool outputs that contain sensitive data."""
    if "ssn" in str(data.output).lower():
        return ToolGuardrailFunctionOutput(
            tripwire_triggered=True,
            model_message="🚨 Tool output blocked: contains sensitive data",
            output_info={"blocked_pattern": "SSN"},
        )
    return ToolGuardrailFunctionOutput(tripwire_triggered=False, output_info="Output validated")

# Apply guardrails to tools
my_tool.tool_input_guardrails = [block_sensitive_words]
my_tool.tool_output_guardrails = [block_sensitive_output]
```

For a complete working example, see [tool_guardrails.py](https://github.com/openai/openai-agents-python/blob/main/examples/basic/tool_guardrails.py).

### Key differences from agent guardrails

- **Scope**: Tool guardrails operate on individual tool calls, while agent guardrails operate on the entire agent input/output
- **Timing**: Tool guardrails run immediately before/after tool execution, while agent guardrails run at the beginning/end of agent execution
- **Data access**: Tool guardrails have access to the specific tool call arguments and outputs, plus the tool context
- **Application**: Tool guardrails are applied directly to function tools via the `tool_input_guardrails` and `tool_output_guardrails` attributes
