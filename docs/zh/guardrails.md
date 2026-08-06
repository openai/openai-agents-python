---
search:
  exclude: true
---
# 安全防护措施

安全防护措施可用于检查和验证用户输入及智能体输出。例如，假设你有一个使用非常智能（因而速度较慢、成本较高）的模型来协助处理客户请求的智能体。你不会希望恶意用户要求模型帮助他们完成数学作业。因此，你可以使用一个快速且成本较低的模型来运行安全防护措施。如果安全防护措施检测到恶意使用，它可以立即引发错误并阻止高成本模型运行，从而为你节省时间和费用（**使用阻塞式安全防护措施时；对于并行安全防护措施，高成本模型可能在安全防护措施完成前就已开始运行。有关详细信息，请参阅下文的“执行模式”**）。

安全防护措施分为两类：

1. 输入安全防护措施针对初始用户输入运行
2. 输出安全防护措施针对智能体的最终输出运行

## 工作流边界

安全防护措施会附加到智能体和工具，但它们并不全都在工作流中的相同节点运行：

-   **输入安全防护措施**仅针对链中的第一个智能体运行。
-   **输出安全防护措施**仅针对生成最终输出的智能体运行。
-   **工具安全防护措施**会在每次自定义工具调用时运行，其中输入安全防护措施在执行前运行，输出安全防护措施在执行后运行。

如果需要检查包含管理智能体、任务转移或受委派专家的工作流中的每次自定义工具调用，请使用工具安全防护措施，而不要仅依赖智能体级别的输入/输出安全防护措施。

## 输入安全防护措施

输入安全防护措施分 3 个步骤运行：

1. 首先，安全防护措施接收传递给智能体的同一输入。
2. 接下来，安全防护措施函数运行并生成 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后将其封装到 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] 中
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。如果为 true，则会引发 [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 异常，以便你向用户作出适当响应或处理该异常。

!!! 注意

    输入安全防护措施旨在针对用户输入运行，因此只有当该智能体是*第一个*智能体时，它的安全防护措施才会运行。你可能会想，为什么 `guardrails` 属性位于智能体上，而不是传递给 `Runner.run`？这是因为安全防护措施通常与实际智能体相关——不同的智能体会运行不同的安全防护措施，因此将相关代码放在一起有助于提高可读性。

### 执行模式

输入安全防护措施支持两种执行模式：

- **并行执行**（默认，`run_in_parallel=True`）：安全防护措施与智能体并发执行。由于两者同时启动，因此这种模式可实现最低延迟。不过，如果安全防护措施未通过，智能体可能已经消耗了 token 并执行了工具，之后才被取消。

- **阻塞式执行**（`run_in_parallel=False`）：安全防护措施会在智能体启动*之前*运行并完成。如果触发了安全防护措施的触发器，智能体将完全不会执行，从而避免消耗 token 和执行工具。这种模式非常适合优化成本，以及避免工具调用可能产生的副作用。

## 输出安全防护措施

输出安全防护措施分 3 个步骤运行：

1. 首先，安全防护措施接收智能体生成的输出。
2. 接下来，安全防护措施函数运行并生成 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后将其封装到 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] 中
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。如果为 true，则会引发 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 异常，以便你向用户作出适当响应或处理该异常。

!!! 注意

    输出安全防护措施旨在针对智能体的最终输出运行，因此只有当该智能体是*最后一个*智能体时，它的安全防护措施才会运行。与输入安全防护措施类似，这样做是因为安全防护措施通常与实际智能体相关——不同的智能体会运行不同的安全防护措施，因此将相关代码放在一起有助于提高可读性。

    输出安全防护措施始终在智能体完成后运行，因此不支持 `run_in_parallel` 参数。

## 工具安全防护措施

工具安全防护措施会封装**工具调用**，让你能够在执行前后验证或阻止工具调用。它们配置在工具本身，并在每次调用该工具时运行。

- 工具输入安全防护措施在工具执行前运行，可以跳过调用、使用消息替换输出，或触发安全机制。
- 工具输出安全防护措施在工具执行后运行，可以替换输出或触发安全机制。
- 如果工具调用需要审批，工具输入安全防护措施通常会在审批后、即将执行前运行。如果希望这些输入检查在发出待审批中断之前运行，请将 [`RunConfig.tool_execution`][agents.run.RunConfig.tool_execution] 设置为 [`ToolExecutionConfig(pre_approval_tool_input_guardrails=True)`][agents.run.ToolExecutionConfig]。通过此次审批前检查的调用仍会在审批后、工具执行前再次接受检查。
- 工具安全防护措施仅适用于通过 [`function_tool`][agents.tool.function_tool] 创建的工具调用。任务转移通过 SDK 的任务转移管线运行，而不是通过常规工具调用管线，因此工具安全防护措施不适用于任务转移调用本身。托管工具（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）和内置执行工具（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）也不使用此安全防护措施管线，并且 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 目前不会直接公开工具安全防护措施选项。

有关详细信息，请参阅下面的代码片段。

## 触发机制

如果智能体输入或输出未通过安全防护措施，安全防护措施可以通过触发器发出信号。运行器会立即引发 `InputGuardrailTripwireTriggered` 或 `OutputGuardrailTripwireTriggered` 异常，并停止智能体执行。工具安全防护措施使用对应的 `ToolInputGuardrailTripwireTriggered` 和 `ToolOutputGuardrailTripwireTriggered` 异常。

对于智能体级别的触发器，异常的 `guardrail_result` 用于标识触发该机制的安全防护措施。对于运行器引发的输入触发器，`exception.run_data.input_guardrail_results` 包含运行停止前已完成的所有输入安全防护措施结果，其中包括触发该机制的结果。输出触发器通过 `exception.run_data.output_guardrail_results` 提供对应的累计结果。

工具触发器异常则会直接公开触发该机制的 `guardrail` 和 `output`。其 `run_data.tool_input_guardrail_results` 和 `run_data.tool_output_guardrail_results` 列表会保留失败前已完成轮次中累计的结果；触发该机制的结果可通过异常的 `output` 获取。其他由运行器管理的失败（例如 `MaxTurnsExceeded`）也会在这些列表中保留已完成的工具安全防护措施结果。在 `stream_events()` 引发异常后，流式结果会公开相同的智能体和工具安全防护措施累计结果列表。如果异常是在运行器管理的执行路径之外引发的，`run_data` 可能为 `None`。

## 安全防护措施实现

你需要提供一个接收输入并返回 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] 的函数。在此示例中，我们将通过在底层运行一个智能体来实现这一点。

```python
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
)
from agents.decorators import input_guardrail

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

输出安全防护措施与之类似。

```python
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
)
from agents.decorators import output_guardrail
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

最后，以下是工具安全防护措施的代码示例。

```python
import json
from agents import (
    Agent,
    Runner,
    ToolGuardrailFunctionOutput,
)
from agents.decorators import tool, tool_input_guardrail, tool_output_guardrail

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


@tool(
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