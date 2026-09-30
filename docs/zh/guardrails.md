---
search:
  exclude: true
---
# 安全防护措施

安全防护措施支持对用户输入和智能体输出进行检查与验证。例如，假设有一个智能体使用非常智能（因此速度较慢、成本较高）的模型来协助处理客户请求。你不会希望恶意用户要求该模型帮助他们完成数学作业。因此，可以使用快速、低成本的模型运行安全防护措施。如果安全防护措施检测到恶意使用，便可立即引发错误，从而节省时间和成本。阻塞执行可以保证高成本模型不会启动；在并行执行模式下，高成本模型可能在安全防护措施完成之前就已经启动。有关详细信息，请参阅下文的“执行模式”。

安全防护措施分为两种：

1. 输入安全防护措施针对初始用户输入运行
2. 输出安全防护措施针对最终智能体输出运行

## 工作流边界 {#workflow-boundaries}

安全防护措施会附加到智能体和工具上，但并非都会在工作流中的相同节点运行：

-   **输入安全防护措施**仅针对链中的第一个智能体运行。
-   **输出安全防护措施**仅针对生成最终输出的智能体运行。
-   **工具安全防护措施**会针对每次受保护的函数工具调用运行，包括服务器已配置安全防护措施的本地 MCP 工具；输入安全防护措施在执行前运行，输出安全防护措施在执行后运行。

如果工作流包含管理器、任务转移或受委派的专用智能体，并且你需要在每次自定义函数工具调用之前和/或之后执行检查，请使用工具安全防护措施，而不要仅依赖智能体级别的输入/输出安全防护措施。

## 输入安全防护措施 {#input-guardrails}

输入安全防护措施分 3 个步骤运行：

1. 首先，安全防护措施接收传递给智能体的相同输入。
2. 接下来，安全防护措施函数运行并生成 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后将其封装到 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] 中
3. 最后，检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。如果为 true，则引发 [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 异常，以便你恰当地响应用户或处理异常。

!!! 注意

    输入安全防护措施旨在针对用户输入运行，因此仅当智能体是*第一个*智能体时，其安全防护措施才会运行。你可能会疑惑，为什么 `guardrails` 属性位于智能体上，而不是传递给 `Runner.run`？这是因为安全防护措施通常与实际的智能体相关——不同的智能体会运行不同的安全防护措施，因此将相关代码放在一起有助于提高可读性。

### 执行模式 {#execution-modes}

输入安全防护措施支持两种执行模式：

- **并行执行**（默认，`run_in_parallel=True`）：安全防护措施与智能体同时运行。由于二者同时启动，因此这种模式可以实现最低延迟。不过，如果安全防护措施的触发器被触发，智能体在被取消之前可能已经消耗了 token 并执行了工具。

- **阻塞执行**（`run_in_parallel=False`）：安全防护措施在智能体启动*之前*运行并完成。如果安全防护措施的触发器被触发，智能体将完全不会执行，从而避免消耗 token 和执行工具。此模式非常适合优化成本，以及需要避免工具调用产生潜在副作用的场景。

## 输出安全防护措施 {#output-guardrails}

输出安全防护措施分 3 个步骤运行：

1. 首先，安全防护措施接收智能体生成的输出。
2. 接下来，安全防护措施函数运行并生成 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后将其封装到 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] 中
3. 最后，检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。如果为 true，则引发 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 异常，以便你恰当地响应用户或处理异常。

!!! 注意

    输出安全防护措施旨在针对最终智能体输出运行，因此仅当智能体是*最后一个*智能体时，其安全防护措施才会运行。与输入安全防护措施类似，这样设计是因为安全防护措施通常与实际的智能体相关——不同的智能体会运行不同的安全防护措施，因此将相关代码放在一起有助于提高可读性。

    输出安全防护措施始终在智能体完成后运行，因此不支持 `run_in_parallel` 参数。

输出触发器和安全防护措施函数引发的异常具有不同的会话行为。触发器会拒绝候选最终输出。触发器触发时，运行器会要求已配置的会话持久化已完成的工具调用和工具输出项，以及重放这些调用所需的任何推理上下文，同时排除被拒绝的候选最终输出。运行器会将此触发器规则同时应用于流式和非流式运行。当安全防护措施函数引发异常而不是返回触发器结果时，运行器会将判定视为未知，并在抛出安全防护措施异常之前，要求已配置的会话持久化已完成的最终轮次项目。如果该会话写入也失败，则会话写入错误具有更高优先级。流式运行采用与非流式运行相同的持久化顺序，并从 `stream_events()` 引发终止异常。在输出安全防护措施运行期间立即调用 [`RunResultStreaming.cancel()`][agents.result.RunResultStreaming.cancel]，会取消正在运行的安全防护措施，并且不会启动最终轮次的会话写入。

终止型函数工具输出需要额外处理，因为在智能体级别的输出安全防护措施检查该值之前，工具已经运行。当 [`Agent.tool_use_behavior`][agents.agent.Agent.tool_use_behavior] 将该工具结果用作最终输出，而输出触发器将其拒绝时，只有在 SDK 能够根据已验证字段重建函数调用/输出对的情况下，才会保留可有效重放的函数调用/输出对。保留的 `function_call_output` 载荷会替换为默认文本 `"Output withheld by an output guardrail."`；原始工具输出载荷不会保留在会话、`RunState`、流式结果状态或沙箱内存输入中。SDK 会保留重放所需的已验证函数调用元数据，包括函数参数，因此这些元数据可能包含也曾出现在被拒绝输出中的数据。当前响应的 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] 对象也会将 `agent_output` 替换为最终确定的占位文本，并清除 `output_info`。当前响应的 [`ToolOutputGuardrailResult`][agents.tool_guardrails.ToolOutputGuardrailResult] 对象会保留允许/拒绝行为类型，但将承载载荷的 `output_info` 和拒绝消息替换为相同的占位文本。之前已接受的轮次和安全防护措施结果保持不变。如果响应包含推理内容或 SDK 无法安全清理的其他结构，SDK 会丢弃完整的当前响应后缀，而不会保留被拒绝的输出载荷。引发异常的安全防护措施函数并未返回拒绝判定，因此已完成的终止型工具轮次会遵循上述异常持久化行为。

当应用程序需要不同的不含数据的占位文本时，请将 [`RunConfig.output_guardrail_blocked_message`][agents.run.RunConfig.output_guardrail_blocked_message] 设置为非空字符串或同步格式化函数。格式化函数接收 [`OutputGuardrailBlockedMessageArgs`][agents.run.OutputGuardrailBlockedMessageArgs]，其中包含 SDK 默认值、安全防护措施名称、智能体和当前运行上下文。它永远不会接收被拒绝的工具输出或安全防护措施 `output_info`。返回的文本会在 SDK 保留已清理的终止型工具轮次的所有位置持久化并重放，因此请确保其中不含敏感数据，也不要从运行上下文中复制机密信息。如果格式化函数引发异常、返回 `None`、返回空值或非字符串值，或者生成可等待对象，SDK 将使用默认占位文本。构造 `RunConfig` 时会拒绝异步格式化函数。

```python
from agents import OutputGuardrailBlockedMessageArgs, RunConfig


def blocked_message(args: OutputGuardrailBlockedMessageArgs[dict[str, str]]) -> str:
    return f"Output blocked by policy: {args.guardrail_name}."


run_config = RunConfig(output_guardrail_blocked_message=blocked_message)
```

## 工具安全防护措施 {#tool-guardrails}

工具安全防护措施封装了 **`FunctionTool` 实例**，支持在这些工具执行前后验证或阻止对它们的调用。它们在工具本身上配置，并在每次调用该工具时运行。

- 输入工具安全防护措施在工具执行前运行，可以跳过调用、将输出替换为一条消息或触发触发器。
- 输出工具安全防护措施在工具执行后运行，可以替换输出或触发触发器。
- 如果函数工具需要审批，输入工具安全防护措施通常会在审批后、执行前立即运行。如果希望这些输入检查在发出待审批中断之前运行，请将 [`RunConfig.tool_execution`][agents.run.RunConfig.tool_execution] 设置为 [`ToolExecutionConfig(pre_approval_tool_input_guardrails=True)`][agents.run.ToolExecutionConfig]。通过此审批前检查的调用仍会在审批后、工具执行前再次接受检查。
- 工具安全防护措施使用 `FunctionTool` 执行管线。你可以将它们直接附加到使用 `tool` 或 [`function_tool`][agents.tool.function_tool] 创建的自定义工具上。还可以在本地 MCP 服务器上设置 `tool_input_guardrails` 和 `tool_output_guardrails`；SDK 会将这些列表附加到该服务器公开的每个工具上。任务转移通过 SDK 的任务转移管线运行，而不是通过函数工具管线运行，因此工具安全防护措施不适用于任务转移调用本身。托管工具（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）和内置执行工具（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）不使用此安全防护措施管线，并且 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 目前不会直接公开工具安全防护措施选项。有关本地 MCP 配置，请参阅 [MCP 服务器工具安全防护措施](mcp.md#tool-guardrails)。

有关详细信息，请参阅下方的代码片段。

## 触发器 {#tripwires}

如果智能体输入或输出未通过安全防护措施检查，安全防护措施可以通过触发器发出信号。运行器会立即引发 `InputGuardrailTripwireTriggered` 或 `OutputGuardrailTripwireTriggered` 异常，并停止智能体执行。工具安全防护措施使用对应的 `ToolInputGuardrailTripwireTriggered` 和 `ToolOutputGuardrailTripwireTriggered` 异常。

对于智能体级别的触发器，异常的 `guardrail_result` 用于标识触发该触发器的安全防护措施。对于运行器引发的输入触发器，`exception.run_data.input_guardrail_results` 包含运行停止前已完成的所有输入安全防护措施结果，包括触发该触发器的结果。输出触发器通过 `exception.run_data.output_guardrail_results` 提供相应的累积结果。

工具触发器异常则直接公开触发异常的 `guardrail` 和 `output`。其 `run_data.tool_input_guardrail_results` 和 `run_data.tool_output_guardrail_results` 列表会保留失败前已完成轮次所累积的结果；触发异常的结果可通过异常的 `output` 获取。其他由运行器管理的失败（例如 `MaxTurnsExceeded`）也会在这些列表中保留已完成的工具安全防护措施结果。`stream_events()` 引发异常后，流式结果会公开相同的智能体和工具安全防护措施累积结果列表。当异常在运行器管理的执行路径之外引发时，`run_data` 可以是 `None`。

## 安全防护措施的实现 {#implementing-a-guardrail}

你需要提供一个接收输入并返回 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] 的函数。在此代码示例中，我们将通过在底层运行一个智能体来实现。

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

输出安全防护措施与此类似。

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