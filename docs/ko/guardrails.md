---
search:
  exclude: true
---
# 가드레일

가드레일은 사용자 입력과 에이전트 출력에 대한 확인 및 검증을 가능하게 합니다. 예를 들어, 매우 똑똑한(따라서 느리고/비싼) 모델을 사용해 고객 요청을 돕는 에이전트가 있다고 가정해 보겠습니다. 악의적인 사용자가 수학 숙제를 도와 달라고 모델에 요청하는 것을 원치 않을 것입니다. 이때 빠르고/저렴한 모델로 가드레일을 실행할 수 있습니다. 가드레일이 악성 사용을 감지하면 즉시 오류를 발생시켜 비싼 모델 실행을 막아 시간과 비용을 절약할 수 있습니다(**차단형 가드레일 사용 시에 한함. 병렬 가드레일의 경우, 가드레일이 완료되기 전에 비싼 모델이 이미 실행을 시작했을 수 있습니다. 자세한 내용은 아래의 "실행 모드"를 참고하세요**).

가드레일에는 두 가지 종류가 있습니다:

1. 입력 가드레일은 최초 사용자 입력에서 실행됨
2. 출력 가드레일은 최종 에이전트 출력에서 실행됨

## 입력 가드레일

입력 가드레일은 3단계로 실행됩니다:

1. 먼저, 가드레일이 에이전트에 전달된 것과 동일한 입력을 받습니다.
2. 다음으로, 가드레일 함수가 실행되어 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]을 생성하고, 이는 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]로 래핑됩니다
3. 마지막으로 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]가 true인지 확인합니다. true이면 [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 예외가 발생하며, 이를 통해 사용자에게 적절히 응답하거나 예외를 처리할 수 있습니다.

!!! Note

    입력 가드레일은 사용자 입력에서 실행되도록 설계되었으므로, 해당 에이전트가 *첫 번째* 에이전트일 때만 에이전트의 가드레일이 실행됩니다. 왜 `guardrails` 속성이 에이전트에 있고 `Runner.run`에 전달하지 않는지 궁금할 수 있습니다. 가드레일은 실제 에이전트와 밀접하게 연관되는 경향이 있기 때문입니다. 에이전트마다 서로 다른 가드레일을 실행하게 되므로, 코드를 같은 위치에 두면 가독성에 도움이 됩니다.

### 실행 모드

입력 가드레일은 두 가지 실행 모드를 지원합니다:

- **병렬 실행**(기본값, `run_in_parallel=True`): 가드레일이 에이전트 실행과 동시에 실행됩니다. 둘이 동시에 시작되므로 지연 시간이 가장 좋습니다. 다만, 가드레일이 실패할 경우, 에이전트가 취소되기 전에 이미 토큰을 소비하고 도구를 실행했을 수 있습니다.

- **차단 실행**(`run_in_parallel=False`): 가드레일이 에이전트 시작 *이전* 에 실행되어 완료됩니다. 가드레일 트립와이어가 트리거되면 에이전트는 절대 실행되지 않으므로 토큰 소비와 도구 실행을 방지할 수 있습니다. 비용 최적화와 도구 호출에 따른 잠재적 부작용을 피하고 싶을 때 이상적입니다.

## 출력 가드레일

출력 가드레일은 3단계로 실행됩니다:

1. 먼저, 가드레일이 에이전트가 생성한 출력을 받습니다.
2. 다음으로, 가드레일 함수가 실행되어 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]을 생성하고, 이는 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]로 래핑됩니다
3. 마지막으로 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]가 true인지 확인합니다. true이면 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 예외가 발생하며, 이를 통해 사용자에게 적절히 응답하거나 예외를 처리할 수 있습니다.

!!! Note

    출력 가드레일은 최종 에이전트 출력에서 실행되도록 설계되었으므로, 해당 에이전트가 *마지막* 에이전트일 때만 에이전트의 가드레일이 실행됩니다. 입력 가드레일과 마찬가지로, 가드레일은 실제 에이전트와 밀접하게 연관되므로 코드를 같은 위치에 두면 가독성에 도움이 됩니다.

    출력 가드레일은 항상 에이전트 완료 후에 실행되므로 `run_in_parallel` 매개변수를 지원하지 않습니다.

## 도구 가드레일

도구 가드레일은 **함수 도구** 를 래핑하고, 실행 전후로 도구 호출을 검증하거나 차단할 수 있게 해줍니다. 이는 도구 자체에서 구성되며 해당 도구가 호출될 때마다 실행됩니다.

- 입력 도구 가드레일은 도구가 실행되기 전에 동작하며, 호출을 건너뛰거나, 메시지로 출력을 대체하거나, 트립와이어를 발생시킬 수 있습니다
- 출력 도구 가드레일은 도구가 실행된 후 동작하며, 출력을 대체하거나 트립와이어를 발생시킬 수 있습니다
- 도구 가드레일은 [`function_tool`][agents.function_tool]로 생성된 함수 도구에만 적용됩니다. 호스티드 툴(`WebSearchTool`, `FileSearchTool`, `HostedMCPTool`, `CodeInterpreterTool`, `ImageGenerationTool`)과 로컬 런타임 도구(`ComputerTool`, `ShellTool`, `ApplyPatchTool`, `LocalShellTool`)는 이 가드레일 파이프라인을 사용하지 않습니다

자세한 내용은 아래 코드 스니펫을 참고하세요.

## 트립와이어

입력 또는 출력이 가드레일을 통과하지 못하면, 가드레일은 트립와이어로 이를 신호할 수 있습니다. 트립와이어가 트리거된 가드레일을 확인하는 즉시 `{Input,Output}GuardrailTripwireTriggered` 예외를 발생시키고 에이전트 실행을 중단합니다.

## 가드레일 구현

입력을 받아 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]을 반환하는 함수를 제공해야 합니다. 이 예시에서는 내부적으로 에이전트를 실행하여 이를 수행합니다.

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

1. 이 에이전트를 가드레일 함수에서 사용합니다
2. 이것이 에이전트의 입력/컨텍스트를 받아 결과를 반환하는 가드레일 함수입니다
3. 가드레일 결과에 추가 정보를 포함할 수 있습니다
4. 이것이 워크플로를 정의하는 실제 에이전트입니다

출력 가드레일도 유사합니다.

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

1. 이것이 실제 에이전트의 출력 타입입니다
2. 이것이 가드레일의 출력 타입입니다
3. 이것이 에이전트의 출력을 받아 결과를 반환하는 가드레일 함수입니다
4. 이것이 워크플로를 정의하는 실제 에이전트입니다

마지막으로, 도구 가드레일의 예시입니다.

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