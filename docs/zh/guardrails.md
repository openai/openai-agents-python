---
search:
  exclude: true
---
# 安全防护措施

安全防护措施使你能够对用户输入和智能体输出进行检查与验证。例如，假设你有一个智能体使用一个非常智能（因此速度慢/成本高）的模型来帮助处理客户请求。你不会希望恶意用户要求模型帮他们做数学作业。因此，你可以用一个快速/低成本的模型运行安全防护措施。如果安全防护措施检测到恶意使用，它可以立即引发错误并阻止高成本模型运行，从而为你节省时间和费用（**当使用阻塞式安全防护措施时；对于并行安全防护措施，高成本模型可能在安全防护措施完成之前就已经开始运行。详情请参见下方“执行模式”**）。

安全防护措施有两种：

1. 输入安全防护措施在初始用户输入上运行
2. 输出安全防护措施在最终智能体输出上运行

## 工作流边界

安全防护措施会附加到智能体和工具上，但它们并不会都在工作流中的相同节点运行：

- **输入安全防护措施**仅对链中的第一个智能体运行。
- **输出安全防护措施**仅对生成最终输出的智能体运行。
- **工具安全防护措施**会在每次自定义工具调用调用时运行，其中输入安全防护措施在执行前运行，输出安全防护措施在执行后运行。

如果你需要在包含管理器、任务转移或委派专家的工作流中围绕每次自定义工具调用进行检查，请使用工具安全防护措施，而不是只依赖智能体级别的输入/输出安全防护措施。

## 输入安全防护措施

输入安全防护措施分 3 步运行：

1. 首先，安全防护措施会接收传递给智能体的同一份输入。
2. 接着，安全防护措施函数运行并生成一个 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后它会被包装在 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] 中
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。如果为 true，则会引发 [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 异常，因此你可以适当地回应用户或处理该异常。

!!! Note

    输入安全防护措施旨在针对用户输入运行，因此只有当某个智能体是*第一个*智能体时，该智能体的安全防护措施才会运行。你可能会疑惑，为什么 `guardrails` 属性是在智能体上，而不是传给 `Runner.run`？这是因为安全防护措施往往与实际的 Agent 相关——你会为不同的智能体运行不同的安全防护措施，因此将代码放在一起有助于提升可读性。

### 执行模式

输入安全防护措施支持两种执行模式：

- **并行执行**（默认，`run_in_parallel=True`）：安全防护措施与智能体的执行并发运行。由于二者同时启动，这可以提供最佳延迟表现。不过，如果安全防护措施失败，智能体在被取消之前可能已经消耗了 token 并执行了工具。

- **阻塞式执行**（`run_in_parallel=False`）：安全防护措施会在智能体启动*之前*运行并完成。如果安全防护措施的触发线被触发，智能体将永远不会执行，从而避免 token 消耗和工具执行。这非常适合成本优化，以及当你希望避免工具调用可能产生的副作用时。

## 输出安全防护措施

输出安全防护措施分 3 步运行：

1. 首先，安全防护措施会接收智能体生成的输出。
2. 接着，安全防护措施函数运行并生成一个 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后它会被包装在 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] 中
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。如果为 true，则会引发 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 异常，因此你可以适当地回应用户或处理该异常。

!!! Note

    输出安全防护措施旨在针对最终智能体输出运行，因此只有当某个智能体是*最后一个*智能体时，该智能体的安全防护措施才会运行。与输入安全防护措施类似，我们这样做是因为安全防护措施往往与实际的 Agent 相关——你会为不同的智能体运行不同的安全防护措施，因此将代码放在一起有助于提升可读性。

    输出安全防护措施始终在智能体完成后运行，因此它们不支持 `run_in_parallel` 参数。

## 工具安全防护措施

工具安全防护措施会包装**工具调用**，并让你在执行前后验证或阻止工具调用。它们配置在工具本身上，并在每次调用该工具时运行。

- 输入工具安全防护措施在工具执行前运行，可以跳过调用、用一条消息替换输出，或引发触发线。
- 输出工具安全防护措施在工具执行后运行，可以替换输出或引发触发线。
- 工具安全防护措施仅适用于使用 [`function_tool`][agents.tool.function_tool] 创建的工具调用。任务转移会通过 SDK 的任务转移流水线运行，而不是普通的工具调用流水线，因此工具安全防护措施不适用于任务转移调用本身。托管工具（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）和内置执行工具（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）也不使用此安全防护措施流水线，并且 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 目前也不会直接公开工具安全防护措施选项。

详情请参见下方代码片段。

## 触发线

如果输入或输出未通过安全防护措施，Guardrail 可以通过触发线来发出信号。一旦我们发现某个安全防护措施触发了触发线，就会立即引发 `{Input,Output}GuardrailTripwireTriggered` 异常，并停止 Agent 执行。

## 安全防护措施实现

你需要提供一个接收输入并返回 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] 的函数。在此示例中，我们将通过在底层运行一个 Agent 来实现这一点。

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

1. 我们将在安全防护措施函数中使用此智能体。
2. 这是接收智能体输入/上下文并返回结果的安全防护措施函数。
3. 我们可以在安全防护措施结果中包含额外信息。
4. 这是定义工作流的实际智能体。

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
3. 这是接收智能体输出并返回结果的安全防护措施函数。
4. 这是定义工作流的实际智能体。

最后，下面是一些工具安全防护措施的代码示例。

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