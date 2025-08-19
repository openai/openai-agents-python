---
search:
  exclude: true
---
# 가드레일

가드레일은 에이전트와 _병렬로_ 실행되어 사용자 입력에 대한 점검과 유효성 검사를 수행할 수 있게 합니다. 예를 들어, 고객 요청을 처리하기 위해 매우 똑똑한(따라서 느리고/비용이 높은) 모델을 사용하는 에이전트가 있다고 가정해 보겠습니다. 악의적인 사용자가 수학 숙제를 도와 달라고 모델에 요청하는 것은 원치 않을 것입니다. 이때 빠르고/저렴한 모델로 가드레일을 실행할 수 있습니다. 가드레일이 악의적 사용을 감지하면 즉시 오류를 발생시켜, 비용이 큰 모델 실행을 중단하고 시간과 비용을 절약할 수 있습니다.

가드레일에는 두 가지 종류가 있습니다:

1. 입력 가드레일은 초기 사용자 입력에서 실행됨
2. 출력 가드레일은 최종 에이전트 출력에서 실행됨

## 입력 가드레일

입력 가드레일은 다음 3단계로 실행됩니다:

1. 먼저, 가드레일은 에이전트에 전달된 동일한 입력을 받습니다
2. 다음으로, 가드레일 함수가 실행되어 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]을 생성하고, 이는 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]로 래핑됩니다
3. 마지막으로 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]가 true 인지 확인합니다. true 인 경우, [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 예외가 발생하며, 이를 통해 사용자에게 적절히 응답하거나 예외를 처리할 수 있습니다

!!! Note

    입력 가드레일은 사용자 입력에서 실행되도록 설계되었으므로, 에이전트의 가드레일은 해당 에이전트가 *첫 번째* 에이전트일 때만 실행됩니다. `guardrails` 속성이 왜 에이전트에 있고 `Runner.run`에 전달되지 않는지 궁금할 수 있습니다. 이는 가드레일이 실제 에이전트와 연관되는 경향이 있기 때문입니다. 에이전트마다 서로 다른 가드레일을 실행하므로 코드를 같은 위치에 두는 것이 가독성에 유리합니다.

## 출력 가드레일

출력 가드레일은 다음 3단계로 실행됩니다:

1. 먼저, 가드레일은 에이전트가 생성한 출력을 받습니다
2. 다음으로, 가드레일 함수가 실행되어 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]을 생성하고, 이는 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]로 래핑됩니다
3. 마지막으로 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]가 true 인지 확인합니다. true 인 경우, [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 예외가 발생하며, 이를 통해 사용자에게 적절히 응답하거나 예외를 처리할 수 있습니다

!!! Note

    출력 가드레일은 최종 에이전트 출력에서 실행되도록 설계되었으므로, 에이전트의 가드레일은 해당 에이전트가 *마지막* 에이전트일 때만 실행됩니다. 입력 가드레일과 유사하게, 가드레일은 실제 에이전트와 연관되는 경향이 있으므로 코드를 같은 위치에 두는 것이 가독성에 유리합니다.

## 트립와이어(tripwire)

입력 또는 출력이 가드레일을 통과하지 못하는 경우, 가드레일은 트립와이어(tripwire)로 이를 신호할 수 있습니다. 트립와이어가 트리거된 가드레일을 확인하는 즉시 `{Input,Output}GuardrailTripwireTriggered` 예외를 발생시키고 에이전트 실행을 중단합니다.

## 가드레일 구현

입력을 받아 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]을 반환하는 함수를 제공해야 합니다. 이 예제에서는 내부적으로 에이전트를 실행하여 이를 수행합니다.

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

1. 가드레일 함수에서 이 에이전트를 사용합니다
2. 이는 에이전트의 입력/컨텍스트를 받아 결과를 반환하는 가드레일 함수입니다
3. 가드레일 결과에 추가 정보를 포함할 수 있습니다
4. 이는 워크플로를 정의하는 실제 에이전트입니다

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

1. 이는 실제 에이전트의 출력 타입입니다
2. 이는 가드레일의 출력 타입입니다
3. 이는 에이전트의 출력을 받아 결과를 반환하는 가드레일 함수입니다
4. 이는 워크플로를 정의하는 실제 에이전트입니다