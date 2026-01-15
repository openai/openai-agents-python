---
search:
  exclude: true
---
# 安全防护措施

安全防护措施允许你对用户输入和智能体输出进行检查与验证。举例来说，假设你有一个使用非常智能（因此也更慢/更昂贵）模型来处理客户请求的智能体。你不希望恶意用户让模型帮他们做数学作业。于是，你可以用一个更快/更便宜的模型来运行安全防护措施。如果安全防护措施检测到恶意使用，它可以立即抛出错误并阻止昂贵模型的运行，从而节省时间和成本（**在使用阻塞式安全防护措施时；对于并行安全防护措施，可能在安全防护措施完成之前，昂贵模型就已经开始运行。详见下文“执行模式”**）。

安全防护措施有两种类型：

1. 输入安全防护措施运行于初始用户输入
2. 输出安全防护措施运行于最终智能体输出

## 输入安全防护措施

输入安全防护措施分3步运行：

1. 首先，安全防护措施接收与智能体相同的输入。
2. 接着，安全防护函数运行以生成一个[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后被包装进一个[`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]
3. 最后，我们检查[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]是否为 true。若为 true，则抛出[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]异常，以便你适当回复用户或处理异常。

!!! Note

    输入安全防护措施旨在对用户输入运行，因此只有当智能体是*第一个*智能体时，才会运行该智能体的安全防护措施。你可能会疑惑：为什么 `guardrails` 属性在智能体上，而不是通过 `Runner.run` 传入？这是因为安全防护措施通常与具体智能体相关——不同智能体会运行不同的安全防护措施，因此将代码与智能体放在一起有助于可读性。

### 执行模式

输入安全防护措施支持两种执行模式：

- **并行执行**（默认，`run_in_parallel=True`）：安全防护措施与智能体的执行并发运行。由于二者同时启动，这提供了最佳延迟表现。然而，如果安全防护失败，智能体可能在被取消前已经消耗了 tokens 并执行了工具。

- **阻塞执行**（`run_in_parallel=False`）：安全防护措施在智能体启动前运行并完成。如果绊线被触发，智能体将不会执行，从而避免 token 消耗与工具执行。这对于成本优化以及需要避免工具调用潜在副作用的场景非常理想。

## 输出安全防护措施

输出安全防护措施分3步运行：

1. 首先，安全防护措施接收由智能体生成的输出。
2. 接着，安全防护函数运行以生成一个[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后被包装进一个[`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]
3. 最后，我们检查[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]是否为 true。若为 true，则抛出[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]异常，以便你适当回复用户或处理异常。

!!! Note

    输出安全防护措施旨在对最终的智能体输出运行，因此只有当智能体是*最后一个*智能体时，才会运行该智能体的安全防护措施。类似于输入安全防护措施，我们这样设计是因为安全防护措施往往与具体智能体相关——不同智能体会运行不同的安全防护措施，因此将代码与智能体放在一起有助于可读性。

    输出安全防护措施总是在智能体完成之后运行，因此不支持 `run_in_parallel` 参数。

## 工具安全防护措施

工具安全防护措施包装**工具调用**，允许你在执行工具前后验证或阻止工具调用。它们在工具本身上进行配置，并在该工具每次被调用时运行。

- 输入工具安全防护措施在工具执行之前运行，可以跳过调用、用一条消息替换输出，或触发绊线。
- 输出工具安全防护措施在工具执行之后运行，可以替换输出或触发绊线。
- 工具安全防护措施仅适用于使用[`function_tool`][agents.function_tool]创建的工具调用；托管工具（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）和本地运行时工具（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）不使用此安全防护流程。

```python
import json
from agents import (
    Agent,
    Runner,
    ToolGuardrailFunctionOutput,
    function_tool,
    tool_input_guardrail,
    tool_output_guardrail,
)

@tool_input_guardrail
def block_secrets(data):
    args = json.loads(data.context.tool_arguments or "{}")
    if "sk-" in json.dumps(args):
        return ToolGuardrailFunctionOutput.reject_content(
            "Remove secrets before calling this tool."
        )
    return ToolGuardrailFunctionOutput.allow()


@tool_output_guardrail
def redact_output(data):
    text = str(data.output or "")
    if "sk-" in text:
        return ToolGuardrailFunctionOutput.reject_content("Output contained sensitive data.")
    return ToolGuardrailFunctionOutput.allow()


@function_tool(
    tool_input_guardrails=[block_secrets],
    tool_output_guardrails=[redact_output],
)
def classify_text(text: str) -> str:
    """Classify text for internal routing."""
    return f"length:{len(text)}"


agent = Agent(name="Classifier", tools=[classify_text])
result = Runner.run_sync(agent, "hello world")
print(result.final_output)
```

## 绊线

如果输入或输出未通过安全防护措施，安全防护措施可以通过绊线来发出信号。一旦我们发现某个安全防护措施触发了绊线，我们会立即抛出 `{Input,Output}GuardrailTripwireTriggered` 异常并停止智能体执行。

## 实现安全防护措施

你需要提供一个接收输入并返回[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]的函数。在此示例中，我们将通过在底层运行一个智能体来实现。

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

1. 我们会在安全防护函数中使用这个智能体。
2. 这是接收智能体输入/上下文并返回结果的安全防护函数。
3. 我们可以在安全防护结果中包含额外信息。
4. 这是定义工作流的实际智能体。

输出安全防护措施与此类似。

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

1. 这是实际智能体的输出类型。
2. 这是安全防护措施的输出类型。
3. 这是接收智能体输出并返回结果的安全防护函数。
4. 这是定义工作流的实际智能体。