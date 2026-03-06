---
search:
  exclude: true
---
# 安全防护措施

安全防护措施让你能够对用户输入和智能体输出进行检查与校验。比如，假设你有一个智能体，使用一个非常智能（因此也较慢/昂贵）的模型来帮助处理客户请求。你肯定不希望恶意用户让这个模型帮他们做数学作业。因此，你可以先用一个快速/便宜的模型运行安全防护措施。如果安全防护措施检测到恶意使用，它可以立即抛出错误并阻止昂贵模型运行，从而节省时间和成本（**在使用阻塞式安全防护措施时；对于并行安全防护措施，昂贵模型可能在安全防护措施完成前就已经开始运行。详情见下方“执行模式”**）。

安全防护措施有两种：

1. 输入安全防护措施：作用于初始用户输入
2. 输出安全防护措施：作用于最终智能体输出

## 工作流边界

安全防护措施会附加在智能体和工具上，但它们在工作流中的运行时机并不相同：

-   **输入安全防护措施**仅对链路中的第一个智能体运行。
-   **输出安全防护措施**仅对产出最终输出的智能体运行。
-   **工具安全防护措施**会在每次自定义 function-tool 调用时运行，执行前运行输入安全防护措施，执行后运行输出安全防护措施。

如果你的工作流包含管理者、任务转移或被委派的专家，并且需要围绕每次自定义 function-tool 调用做检查，请使用工具安全防护措施，而不要只依赖智能体级别的输入/输出安全防护措施。

## 输入安全防护措施

输入安全防护措施分 3 步运行：

1. 首先，安全防护措施接收与传给智能体相同的输入。
2. 接着，运行安全防护函数，产出一个 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后被封装为 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。若为 true，会抛出 [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 异常，以便你恰当地响应用户或处理异常。

!!! Note

    输入安全防护措施旨在作用于用户输入，因此只有当该智能体是*第一个*智能体时，它的安全防护措施才会运行。你可能会疑惑：为什么 `guardrails` 属性放在智能体上，而不是传给 `Runner.run`？这是因为安全防护措施通常与具体的 Agent 相关——不同智能体通常会使用不同的安全防护措施，把代码就近放置有助于提升可读性。

### 执行模式

输入安全防护措施支持两种执行模式：

- **并行执行**（默认，`run_in_parallel=True`）：安全防护措施与智能体执行并发运行。由于两者同时开始，这能提供最佳延迟表现。不过，如果安全防护措施失败，智能体在被取消前可能已经消耗了 token 并执行了工具调用。

- **阻塞执行**（`run_in_parallel=False`）：安全防护措施会在智能体启动*之前*运行并完成。如果触发了安全防护触发器，智能体将不会执行，从而避免 token 消耗和工具执行。这非常适合成本优化，以及你希望避免工具调用潜在副作用的场景。

## 输出安全防护措施

输出安全防护措施分 3 步运行：

1. 首先，安全防护措施接收智能体生成的输出。
2. 接着，运行安全防护函数，产出一个 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后被封装为 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]
3. 最后，我们检查 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] 是否为 true。若为 true，会抛出 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 异常，以便你恰当地响应用户或处理异常。

!!! Note

    输出安全防护措施旨在作用于最终智能体输出，因此只有当该智能体是*最后一个*智能体时，它的安全防护措施才会运行。与输入安全防护措施类似，这样设计是因为安全防护措施通常与具体 Agent 相关——不同智能体通常会使用不同的安全防护措施，把代码就近放置有助于提升可读性。

    输出安全防护措施总是在智能体完成后运行，因此不支持 `run_in_parallel` 参数。

## 工具安全防护措施

工具安全防护措施会包裹**工具调用**，让你能够在执行前后校验或拦截工具调用。它们配置在工具本身上，并在每次调用该工具时运行。

- 输入工具安全防护措施在工具执行前运行，可跳过调用、用一条消息替换输出，或抛出触发器。
- 输出工具安全防护措施在工具执行后运行，可替换输出或抛出触发器。
- 工具安全防护措施仅适用于通过 [`function_tool`][agents.tool.function_tool] 创建的 function tools。任务转移通过 SDK 的 handoff 管线运行，而不是普通 function-tool 管线，因此工具安全防护措施不适用于任务转移调用本身。托管工具（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）和内置执行工具（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）也不使用这条安全防护措施管线，且 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 目前也不直接暴露工具安全防护措施选项。

详情见下方代码片段。

## 触发器

如果输入或输出未通过安全防护措施，安全防护措施可通过触发器发出信号。一旦检测到某个安全防护措施触发了触发器，我们会立即抛出 `{Input,Output}GuardrailTripwireTriggered` 异常并终止智能体执行。

## 安全防护措施实现

你需要提供一个函数来接收输入，并返回一个 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]。在这个示例中，我们会通过在底层运行一个智能体来实现。

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
2. 这是安全防护函数，它接收智能体的输入/上下文，并返回结果。
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
3. 这是安全防护函数，它接收智能体的输出，并返回结果。
4. 这是真正定义工作流的智能体。

最后，这里是工具安全防护措施的示例。

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