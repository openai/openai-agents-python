---
search:
  exclude: true
---
# 安全防护措施

安全防护措施使你能够对用户输入和智能体输出进行检查与验证。举例来说，假设你有一个智能体，它使用一个非常智能（因此也较慢/较昂贵）的模型来帮助处理客户请求。你不会希望恶意用户让模型帮他们做数学作业。因此，你可以用一个快速/便宜的模型运行安全防护措施。如果安全防护措施检测到恶意使用，它可以立即抛出错误并阻止昂贵模型运行，从而节省时间和成本（**在使用阻塞式安全防护措施时；对于并行安全防护措施，昂贵模型可能会在安全防护措施完成前就已开始运行。详情见下方“执行模式”**）。

安全防护措施有两种：

1. 输入安全防护措施：运行在初始用户输入上
2. 输出安全防护措施：运行在最终智能体输出上

## 输入安全防护措施

输入安全防护措施分 3 步运行：

1. 首先，安全防护措施接收与传给智能体相同的输入。
2. 接着，安全防护措施函数运行并产生一个 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后被包装为一个 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。若为 true，则会抛出 [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 异常，以便你适当地响应用户或处理该异常。

!!! Note

    输入安全防护措施用于处理用户输入，因此只有当该智能体是*第一个*智能体时，智能体上的安全防护措施才会运行。你可能会问，为什么 `guardrails` 属性放在智能体上，而不是传给 `Runner.run`？这是因为安全防护措施通常与实际的 Agent 相关——不同智能体会运行不同的安全防护措施，因此将代码放在一起有助于可读性。

### 执行模式

输入安全防护措施支持两种执行模式：

- **并行执行**（默认，`run_in_parallel=True`）：安全防护措施与智能体执行并发运行。由于二者同时开始，这可提供最佳延迟表现。但如果安全防护措施失败，智能体在被取消前可能已经消耗了 tokens 并执行了工具调用。

- **阻塞执行**（`run_in_parallel=False`）：安全防护措施会在智能体启动*之前*运行并完成。如果触发安全防护措施的跳闸条件，智能体将不会执行，从而避免 token 消耗和工具执行。这非常适合成本优化，以及你希望避免工具调用潜在副作用的场景。

## 输出安全防护措施

输出安全防护措施分 3 步运行：

1. 首先，安全防护措施接收智能体产生的输出。
2. 接着，安全防护措施函数运行并产生一个 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后被包装为一个 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。若为 true，则会抛出 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 异常，以便你适当地响应用户或处理该异常。

!!! Note

    输出安全防护措施用于处理最终智能体输出，因此只有当该智能体是*最后一个*智能体时，智能体上的安全防护措施才会运行。与输入安全防护措施类似，我们这样做是因为安全防护措施通常与实际的 Agent 相关——不同智能体会运行不同的安全防护措施，因此将代码放在一起有助于可读性。

    输出安全防护措施总是在智能体完成后运行，因此不支持 `run_in_parallel` 参数。

## 工具安全防护措施

工具安全防护措施封装**工具调用**，让你在执行前后验证或拦截工具调用。它们配置在工具本身上，并在每次调用该工具时运行。

- 输入工具安全防护措施在工具执行前运行，可跳过调用、用一条消息替换输出，或触发跳闸异常。
- 输出工具安全防护措施在工具执行后运行，可替换输出或触发跳闸异常。
- 工具安全防护措施仅适用于通过 [`function_tool`][agents.function_tool] 创建的工具调用；托管工具（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）和内置执行工具（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）不使用该安全防护流水线。

详见下方代码片段。

## 跳闸机制

如果输入或输出未通过安全防护措施，Guardrail 可通过 tripwire 发出信号。一旦我们发现某个安全防护措施已触发 tripwire，就会立即抛出 `{Input,Output}GuardrailTripwireTriggered` 异常并中止 Agent 执行。

## 安全防护措施实现

你需要提供一个接收输入并返回 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] 的函数。在这个示例中，我们会通过在底层运行一个 Agent 来实现。

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

1. 我们将在安全防护措施函数中使用这个智能体。
2. 这是安全防护措施函数，接收智能体的输入/上下文并返回结果。
3. 我们可以在安全防护结果中包含额外信息。
4. 这是真正定义工作流的智能体。

输出安全防护措施类似。

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
3. 这是安全防护措施函数，接收智能体输出并返回结果。
4. 这是真正定义工作流的智能体。

最后，以下是工具安全防护措施的示例。

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