---
search:
  exclude: true
---
# 安全防护措施

安全防护措施使你能够对用户输入和智能体输出进行检查和验证。例如，假设你有一个智能体使用非常智能（因而较慢/昂贵）的模型来帮助处理客户请求。你不会希望恶意用户要求模型帮他们做数学作业。因此，你可以使用快速/便宜的模型运行安全防护措施。如果安全防护措施检测到恶意使用，它可以立即引发错误，并阻止昂贵的模型运行，从而节省时间和成本（**当使用阻塞式安全防护措施时；对于并行安全防护措施，昂贵的模型可能已经在安全防护措施完成之前开始运行。详见下方“执行模式”**）。

安全防护措施有两种：

1. 输入安全防护措施会在初始用户输入上运行
2. 输出安全防护措施会在最终智能体输出上运行

## 工作流边界

安全防护措施会附加到智能体和工具上，但它们并不会全都在工作流中的同一位置运行：

-   **输入安全防护措施**仅对链中的第一个智能体运行。
-   **输出安全防护措施**仅对生成最终输出的智能体运行。
-   **工具安全防护措施**会在每次自定义函数工具调用时运行，其中输入安全防护措施在执行前运行，输出安全防护措施在执行后运行。

如果你的工作流包含管理者、任务转移或委托的专家，并且需要对每次自定义函数工具调用进行检查，请使用工具安全防护措施，而不要只依赖智能体级别的输入/输出安全防护措施。

## 输入安全防护措施

输入安全防护措施分 3 步运行：

1. 首先，安全防护措施会接收传递给智能体的相同输入。
2. 接下来，安全防护措施函数会运行并生成 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，该输出随后会包装为 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。如果为 true，则会引发 [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 异常，以便你可以适当地回应用户或处理该异常。

!!! Note

    输入安全防护措施旨在针对用户输入运行，因此只有当智能体是*第一个*智能体时，该智能体的安全防护措施才会运行。你可能会疑惑，为什么 `guardrails` 属性在智能体上，而不是传递给 `Runner.run`？这是因为安全防护措施往往与实际的智能体相关——你会为不同的智能体运行不同的安全防护措施，因此将代码放在一起有助于可读性。

### 执行模式

输入安全防护措施支持两种执行模式：

- **并行执行**（默认，`run_in_parallel=True`）：安全防护措施会与智能体的执行并发运行。这能提供最佳延迟，因为二者会同时启动。不过，如果安全防护措施失败，智能体在被取消之前可能已经消耗了 token 并执行了工具。

- **阻塞执行**（`run_in_parallel=False`）：安全防护措施会在智能体启动*之前*运行并完成。如果安全防护措施的警戒线被触发，智能体就永远不会执行，从而避免 token 消耗和工具执行。这非常适合成本优化，以及你希望避免工具调用可能产生副作用的场景。

## 输出安全防护措施

输出安全防护措施分 3 步运行：

1. 首先，安全防护措施会接收智能体生成的输出。
2. 接下来，安全防护措施函数会运行并生成 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，该输出随后会包装为 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。如果为 true，则会引发 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 异常，以便你可以适当地回应用户或处理该异常。

!!! Note

    输出安全防护措施旨在针对最终智能体输出运行，因此只有当智能体是*最后一个*智能体时，该智能体的安全防护措施才会运行。与输入安全防护措施类似，我们这样做是因为安全防护措施往往与实际的智能体相关——你会为不同的智能体运行不同的安全防护措施，因此将代码放在一起有助于可读性。

    输出安全防护措施总是在智能体完成后运行，因此它们不支持 `run_in_parallel` 参数。

## 工具安全防护措施

工具安全防护措施会封装**工具调用**，并允许你在执行前后验证或阻止工具调用。它们配置在工具本身上，并在每次调用该工具时运行。

- 输入工具安全防护措施会在工具执行前运行，可以跳过调用、用一条消息替换输出，或触发警戒线。
- 输出工具安全防护措施会在工具执行后运行，可以替换输出或触发警戒线。
- 如果某个函数工具需要审批，输入工具安全防护措施通常会在审批之后、执行之前立即运行。当你希望这些输入检查在发出待审批中断之前运行时，请将 [`RunConfig.tool_execution`][agents.run.RunConfig.tool_execution] 设置为 [`ToolExecutionConfig(pre_approval_tool_input_guardrails=True)`][agents.run.ToolExecutionConfig]。通过此预审批检查的调用，在审批之后、工具执行之前仍会再次接受检查。
- 工具安全防护措施仅适用于使用 [`function_tool`][agents.tool.function_tool] 创建的函数工具。任务转移会经过 SDK 的任务转移管道，而不是常规函数工具管道，因此工具安全防护措施不适用于任务转移调用本身。托管工具（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）和内置执行工具（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）也不使用此安全防护措施管道，并且 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 目前不直接暴露工具安全防护措施选项。

详情请参阅下面的代码片段。

## 警戒线

如果输入或输出未通过安全防护措施，安全防护措施可以通过警戒线发出信号。一旦我们发现某个安全防护措施触发了警戒线，就会立即引发 `{Input,Output}GuardrailTripwireTriggered` 异常并停止智能体执行。

## 安全防护措施的实现

你需要提供一个接收输入并返回 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] 的函数。在这个示例中，我们会通过在底层运行一个智能体来实现这一点。

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
2. 这是安全防护措施函数，它接收智能体的输入/上下文并返回结果。
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
3. 这是安全防护措施函数，它接收智能体的输出并返回结果。
4. 这是定义工作流的实际智能体。

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