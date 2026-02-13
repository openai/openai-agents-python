---
search:
  exclude: true
---
# 安全防护措施

安全防护措施可用于检查和验证用户输入与智能体输出。比如，假设你有一个智能体使用非常智能（因此也很慢/昂贵）的模型来处理客户请求。你不希望恶意用户让该模型帮他们做数学作业。因此，你可以用一个快速/廉价的模型先运行一层安全防护措施。如果检测到恶意使用，它可以立刻抛出错误并阻止昂贵模型的运行，从而节省时间和成本（使用阻塞式安全防护措施时；在并行安全防护措施下，昂贵模型可能会在防护完成前就已开始运行。详见下文“执行模式”）。

安全防护措施分为两类：

1. 输入安全防护措施运行在初始用户输入上
2. 输出安全防护措施运行在最终智能体输出上

## 输入安全防护措施

输入安全防护措施分三步运行：

1. 首先，安全防护措施接收传给智能体的相同输入。
2. 接着，防护函数运行以产生一个[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后被包装为[`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]。
3. 最后，我们检查[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]是否为 true。若为 true，将抛出[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]异常，以便你恰当地响应用户或处理异常。

!!! 注意

    输入安全防护措施用于运行在用户输入上，因此只有当该智能体是“第一个”智能体时，它的安全防护措施才会运行。你或许会好奇，为什么是把`guardrails`属性放在智能体上，而不是传给`Runner.run`？这是因为安全防护措施往往与具体的智能体相关——不同智能体会运行不同的防护措施，因此将代码就近放置有助于可读性。

### 执行模式

输入安全防护措施支持两种执行模式：

- **并行执行**（默认，`run_in_parallel=True`）：安全防护与智能体执行并发进行。由于两者同时启动，延迟最佳。然而，如果防护失败，智能体在被取消前可能已经消耗了 token 并执行了工具。
- **阻塞执行**（`run_in_parallel=False`）：安全防护在智能体启动之前运行并完成。如果触发了绊线，智能体将不会执行，从而避免 token 消耗与工具调用。这在成本优化以及需要避免工具调用副作用时尤为理想。

## 输出安全防护措施

输出安全防护措施分三步运行：

1. 首先，安全防护措施接收智能体产生的输出。
2. 接着，防护函数运行以产生一个[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后被包装为[`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]。
3. 最后，我们检查[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]是否为 true。若为 true，将抛出[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]异常，以便你恰当地响应用户或处理异常。

!!! 注意

    输出安全防护措施用于运行在最终的智能体输出上，因此只有当该智能体是“最后一个”智能体时，它的安全防护措施才会运行。与输入安全防护类似，这是因为防护措施往往与具体的智能体相关——不同智能体会运行不同的防护措施，因此将代码就近放置有助于可读性。

    输出安全防护措施总是在智能体完成之后运行，因此不支持`run_in_parallel`参数。

## 工具安全防护措施

工具安全防护措施包装**工具调用**，允许你在执行前后验证或阻止工具调用。它们在工具本身进行配置，并在每次调用该工具时运行。

- 输入工具安全防护措施在工具执行前运行，可跳过调用、用一条消息替换输出，或触发绊线。
- 输出工具安全防护措施在工具执行后运行，可替换输出或触发绊线。
- 工具安全防护措施仅适用于通过[`function_tool`][agents.function_tool]创建的工具调用；托管工具（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）和本地运行时工具（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）不使用此防护流程。

详见下方代码片段。

## 绊线（Tripwires）

若输入或输出未通过安全防护措施，安全防护可通过绊线进行信号通知。一旦检测到某个安全防护触发了绊线，我们会立刻抛出`{Input,Output}GuardrailTripwireTriggered`异常并停止智能体执行。

## 实现安全防护措施

你需要提供一个接收输入并返回[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]的函数。在此示例中，我们将通过在底层运行一个智能体来完成。

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

1. 我们将在防护函数中使用这个智能体。
2. 这是接收智能体输入/上下文并返回结果的防护函数。
3. 我们可以在防护结果中包含额外信息。
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
2. 这是安全防护的输出类型。
3. 这是接收智能体输出并返回结果的防护函数。
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