---
search:
  exclude: true
---
# 安全防护措施

安全防护措施可用于对用户输入和智能体输出进行检查与验证。比如，你有一个使用非常智能（因此较慢/昂贵）模型来处理客户请求的智能体。你不希望恶意用户要求该模型帮他们做数学作业。因此，你可以使用快速/廉价的模型运行一道安全防护措施。如果该措施检测到恶意使用，它可以立即抛出错误并阻止昂贵模型运行，从而节省时间和金钱（**在使用阻塞式安全防护措施时；对于并行安全防护措施，可能在防护完成前昂贵模型已开始运行。详见下文“执行模式”**）。

安全防护措施有两种类型：

1. 输入安全防护措施运行于初始用户输入
2. 输出安全防护措施运行于最终智能体输出

## 输入安全防护措施

输入安全防护措施分三步运行：

1. 首先，安全防护措施接收与智能体相同的输入。
2. 接着，运行防护函数以产生一个[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后将其包装为[`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]
3. 最后，我们检查[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]是否为 true。若为 true，将抛出[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]异常，以便你适当响应用户或处理异常。

!!! Note

    输入安全防护措施旨在运行于用户输入上，因此仅当该智能体是*第一个*智能体时，其安全防护措施才会运行。你或许会疑惑，为何 `guardrails` 属性在智能体上，而不是传给 `Runner.run`？这是因为安全防护措施通常与具体的智能体相关——你会为不同的智能体运行不同的防护，因此将代码放在一起有助于可读性。

### 执行模式

输入安全防护措施支持两种执行模式：

- **并行执行**（默认，`run_in_parallel=True`）：安全防护措施与智能体的执行并发运行。因为两者同时启动，这能提供最佳的时延表现。但是，如果安全防护失败，智能体在被取消前可能已消耗了 tokens 并执行了工具。

- **阻塞执行**（`run_in_parallel=False`）：安全防护措施在智能体开始之前运行并完成。如果触发了防护的触发线（tripwire），智能体将不会执行，从而避免 token 消耗和工具执行。这在成本优化以及你想避免工具调用潜在副作用时非常理想。

## 输出安全防护措施

输出安全防护措施分三步运行：

1. 首先，安全防护措施接收由智能体产生的输出。
2. 接着，运行防护函数以产生一个[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]，随后将其包装为[`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]
3. 最后，我们检查[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]是否为 true。若为 true，将抛出[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]异常，以便你适当响应用户或处理异常。

!!! Note

    输出安全防护措施旨在运行于最终的智能体输出上，因此仅当该智能体是*最后一个*智能体时，其安全防护措施才会运行。与输入安全防护措施类似，我们这样做是因为安全防护通常与具体的智能体相关——你会为不同的智能体运行不同的防护，因此将代码放在一起有助于可读性。

    输出安全防护措施总是在智能体完成后运行，因此不支持 `run_in_parallel` 参数。

## 触发线

如果输入或输出未通过安全防护措施，防护可通过触发线（tripwire）发出信号。一旦发现某个安全防护触发了触发线，我们会立即抛出 `{Input,Output}GuardrailTripwireTriggered` 异常并终止智能体执行。

## 实现安全防护措施

你需要提供一个接收输入并返回[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]的函数。在此示例中，我们将通过在幕后运行一个智能体来完成此操作。

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

输出安全防护措施也类似。

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
3. 这是接收智能体输出并返回结果的防护函数。
4. 这是定义工作流的实际智能体。