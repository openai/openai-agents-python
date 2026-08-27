---
search:
  exclude: true
---
# 安全防护措施

安全防护措施可用于检查和验证用户输入及智能体输出。例如，假设你有一个智能体，它使用非常智能（因而速度较慢且成本较高）的模型来协助处理客户请求。你不会希望恶意用户要求该模型帮助他们完成数学作业。因此，你可以使用速度快、成本低的模型运行安全防护措施。如果安全防护措施检测到恶意使用，可以立即抛出错误，从而节省时间和成本。阻塞执行可保证高成本模型不会启动；使用并行执行时，高成本模型可能已在安全防护措施完成前启动。有关详情，请参阅下文的“执行模式”。

安全防护措施分为两类：

1. 输入安全防护措施针对初始用户输入运行
2. 输出安全防护措施针对智能体的最终输出运行

## 工作流边界 {#workflow-boundaries}

安全防护措施会附加到智能体和工具上，但它们并非都在工作流中的相同节点运行：

-   **输入安全防护措施**仅针对链中的第一个智能体运行。
-   **输出安全防护措施**仅针对生成最终输出的智能体运行。
-   **工具安全防护措施**在每次调用自定义函数工具时运行，其中输入安全防护措施在执行前运行，输出安全防护措施在执行后运行。

如果需要在包含管理者、任务转移或受委派专家的工作流中，于每次自定义函数工具调用前和/或调用后执行检查，请使用工具安全防护措施，而不要只依赖智能体级别的输入/输出安全防护措施。

## 输入安全防护措施 {#input-guardrails}

输入安全防护措施分 3 个步骤运行：

1. 首先，安全防护措施接收与传给智能体相同的输入。
2. 接下来，安全防护措施函数运行并生成一个 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后将其封装在 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] 中
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。如果为 true，则会抛出 [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 异常，以便你适当地响应用户或处理该异常。

!!! Note

    输入安全防护措施旨在针对用户输入运行，因此仅当某个智能体是*第一个*智能体时，才会运行该智能体的安全防护措施。你可能会疑惑，为什么 `guardrails` 属性位于智能体上，而不是传给 `Runner.run`？这是因为安全防护措施往往与实际的智能体相关——你会为不同智能体运行不同的安全防护措施，因此将代码放在一起有助于提高可读性。

### 执行模式 {#execution-modes}

输入安全防护措施支持两种执行模式：

- **并行执行**（默认，`run_in_parallel=True`）：安全防护措施与智能体并发执行。由于两者同时启动，因此这种模式可实现最低延迟。但是，如果安全防护措施的触发器被触发，智能体在取消前可能已经消耗了 token 并执行了工具。

- **阻塞执行**（`run_in_parallel=False`）：安全防护措施会在智能体启动*之前*运行并完成。如果安全防护措施的触发器被触发，智能体将完全不会执行，从而避免消耗 token 和执行工具。这种模式非常适合成本优化，以及需要避免工具调用产生潜在副作用的场景。

## 输出安全防护措施 {#output-guardrails}

输出安全防护措施分 3 个步骤运行：

1. 首先，安全防护措施接收智能体生成的输出。
2. 接下来，安全防护措施函数运行并生成一个 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后将其封装在 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] 中
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。如果为 true，则会抛出 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 异常，以便你适当地响应用户或处理该异常。

!!! Note

    输出安全防护措施旨在针对智能体的最终输出运行，因此仅当某个智能体是*最后一个*智能体时，才会运行该智能体的安全防护措施。与输入安全防护措施类似，我们这样做是因为安全防护措施往往与实际的智能体相关——你会为不同智能体运行不同的安全防护措施，因此将代码放在一起有助于提高可读性。

    输出安全防护措施总是在智能体完成后运行，因此不支持 `run_in_parallel` 参数。

输出触发器和安全防护措施函数抛出的异常会导致不同的会话行为。触发器会拒绝候选最终输出。当触发器触发时，运行器会要求已配置的会话持久化已完成的工具调用和工具输出项，以及重放这些调用所需的所有推理上下文，同时排除被拒绝的候选最终输出。运行器会将此触发器规则同时应用于流式传输和非流式传输运行。当安全防护措施函数抛出异常而不是返回触发器结果时，运行器会将判定视为未知，并要求已配置的会话在向上抛出安全防护措施异常之前持久化最终轮次中已完成的项。如果该会话写入也失败，则会话写入错误具有更高优先级。流式传输运行使用与非流式传输运行相同的持久化顺序，并从 `stream_events()` 抛出终止异常。在输出安全防护措施运行期间立即调用 [`RunResultStreaming.cancel()`][agents.result.RunResultStreaming.cancel]，会取消正在运行的安全防护措施，并且不会启动最终轮次的会话写入。

终止型函数工具输出需要额外处理，因为在智能体级别的输出安全防护措施检查该值之前，工具已经运行。当 [`Agent.tool_use_behavior`][agents.agent.Agent.tool_use_behavior] 将该工具结果设为最终输出，而输出触发器将其拒绝时，只有在可以根据已验证字段重建函数调用/输出对的情况下，SDK 才会保留可有效重放的函数调用/输出对。保留的 `function_call_output` 载荷会替换为固定文本 `"Output withheld by an output guardrail."`；原始工具输出载荷不会保留在会话、`RunState`、流式传输结果状态或沙箱内存输入中。SDK 会保留重放所需的已验证函数调用元数据，包括函数参数，因此该元数据可能包含也曾出现在被拒绝输出中的数据。当前响应的 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] 对象也会将 `agent_output` 替换为该固定文本，并清除 `output_info`。当前响应的 [`ToolOutputGuardrailResult`][agents.tool_guardrails.ToolOutputGuardrailResult] 对象会保留允许/拒绝行为类型，但会将包含载荷的 `output_info` 和拒绝消息替换为相同文本。此前已接受的轮次和安全防护措施结果保持不变。如果响应包含推理内容或其他 SDK 无法安全清理的结构，SDK 会丢弃当前响应的完整后缀，而不是保留被拒绝的输出载荷。抛出异常的安全防护措施函数并未返回拒绝判定，因此已完成的终止工具轮次会遵循上述异常持久化行为。

## 工具安全防护措施 {#tool-guardrails}

工具安全防护措施会包装**`FunctionTool` 实例**，使你能够在这些工具执行前后验证或阻止对它们的调用。它们配置在工具本身上，并在每次调用该工具时运行。

- 输入工具安全防护措施在工具执行前运行，可以跳过调用、将输出替换为消息，或触发触发器。
- 输出工具安全防护措施在工具执行后运行，可以替换输出或触发触发器。
- 如果函数工具需要审批，输入工具安全防护措施通常会在审批后、执行前立即运行。如果希望在发出待审批中断前运行这些输入检查，请将 [`RunConfig.tool_execution`][agents.run.RunConfig.tool_execution] 设置为 [`ToolExecutionConfig(pre_approval_tool_input_guardrails=True)`][agents.run.ToolExecutionConfig]。通过这项审批前检查的调用，在审批通过后、工具执行前仍会再次接受检查。
- 工具安全防护措施仅适用于使用 [`function_tool`][agents.tool.function_tool] 创建的函数工具。任务转移通过 SDK 的任务转移管道运行，而不是通过常规函数工具管道运行，因此工具安全防护措施不适用于任务转移调用本身。托管工具（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）和内置执行工具（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）也不使用此安全防护措施管道，并且 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 目前不直接提供工具安全防护措施选项。

有关详情，请参阅下方代码片段。

## 触发器 {#tripwires}

如果智能体输入或输出未通过安全防护措施，安全防护措施可以通过触发器发出信号。运行器会立即抛出 `InputGuardrailTripwireTriggered` 或 `OutputGuardrailTripwireTriggered` 异常，并停止执行智能体。工具安全防护措施使用对应的 `ToolInputGuardrailTripwireTriggered` 和 `ToolOutputGuardrailTripwireTriggered` 异常。

对于智能体级别的触发器，异常的 `guardrail_result` 会标识触发该触发器的安全防护措施。对于运行器抛出的输入触发器，`exception.run_data.input_guardrail_results` 包含运行停止前已完成的所有输入安全防护措施结果，其中包括触发该触发器的结果。输出触发器通过 `exception.run_data.output_guardrail_results` 提供对应的累积结果。

工具触发器异常则会直接公开触发它的 `guardrail` 和 `output`。其中的 `run_data.tool_input_guardrail_results` 和 `run_data.tool_output_guardrail_results` 列表会保留失败前已完成轮次中累积的结果；触发结果可通过异常的 `output` 获取。其他由运行器管理的故障（例如 `MaxTurnsExceeded`）也会在这些列表中保留已完成的工具安全防护措施结果。在 `stream_events()` 抛出异常后，流式传输结果会公开相同的智能体和工具安全防护措施累积结果列表。在运行器管理的执行路径之外抛出异常时，`run_data` 可以是 `None`。

## 安全防护措施实现 {#implementing-a-guardrail}

你需要提供一个接收输入并返回 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] 的函数。在此示例中，我们将在底层运行一个智能体来实现这一点。

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

最后，以下是工具安全防护措施的示例。

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