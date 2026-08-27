---
search:
  exclude: true
---
# 가드레일

가드레일을 사용하면 사용자 입력과 에이전트 출력을 확인하고 검증할 수 있습니다. 예를 들어 고객 요청을 지원하기 위해 매우 지능적이고 그만큼 느리며 비용이 많이 드는 모델을 사용하는 에이전트가 있다고 가정해 보겠습니다. 악의적인 사용자가 모델에 수학 숙제를 도와달라고 요청하는 것은 원하지 않을 것입니다. 이 경우 빠르고 저렴한 모델로 가드레일을 실행할 수 있습니다. 가드레일이 악의적인 사용을 감지하면 즉시 오류를 발생시켜 시간과 비용을 절약할 수 있습니다. 차단 실행은 고비용 모델이 시작되지 않도록 보장합니다. 병렬 실행에서는 가드레일이 완료되기 전에 고비용 모델이 이미 시작되었을 수 있습니다. 자세한 내용은 아래의 "실행 모드"를 참조하세요.

가드레일에는 두 가지 종류가 있습니다.

1. 입력 가드레일은 최초 사용자 입력에 대해 실행됩니다
2. 출력 가드레일은 최종 에이전트 출력에 대해 실행됩니다

## 워크플로 경계 {#workflow-boundaries}

가드레일은 에이전트와 도구에 연결되지만, 워크플로에서 모두 같은 시점에 실행되는 것은 아닙니다.

- **입력 가드레일**은 체인의 첫 번째 에이전트에 대해서만 실행됩니다.
- **출력 가드레일**은 최종 출력을 생성하는 에이전트에 대해서만 실행됩니다.
- **도구 가드레일**은 사용자 정의 함수 도구를 호출할 때마다 실행되며, 입력 가드레일은 실행 전에, 출력 가드레일은 실행 후에 실행됩니다.

매니저, 핸드오프 또는 위임된 전문 에이전트가 포함된 워크플로에서 각 사용자 정의 함수 도구 호출 전후에 검사가 필요하다면, 에이전트 수준의 입력/출력 가드레일에만 의존하지 말고 도구 가드레일을 사용하세요.

## 입력 가드레일 {#input-guardrails}

입력 가드레일은 다음 3단계로 실행됩니다.

1. 먼저 가드레일은 에이전트에 전달된 것과 동일한 입력을 받습니다.
2. 다음으로 가드레일 함수가 실행되어 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]을 생성하고, 이 값은 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]로 래핑됩니다
3. 마지막으로 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]이 참인지 확인합니다. 참이면 [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 예외가 발생하므로 사용자에게 적절히 응답하거나 예외를 처리할 수 있습니다.

!!! 참고

    입력 가드레일은 사용자 입력에 대해 실행되도록 설계되었으므로 에이전트가 *첫 번째* 에이전트인 경우에만 해당 에이전트의 가드레일이 실행됩니다. 그렇다면 왜 `guardrails` 속성을 `Runner.run`에 전달하지 않고 에이전트에 지정하는지 궁금할 수 있습니다. 이는 가드레일이 실제 에이전트와 관련되는 경우가 많기 때문입니다. 에이전트마다 서로 다른 가드레일을 실행하므로 코드를 함께 배치하면 가독성이 향상됩니다.

### 실행 모드 {#execution-modes}

입력 가드레일은 두 가지 실행 모드를 지원합니다.

- **병렬 실행** (기본값, `run_in_parallel=True`): 가드레일이 에이전트 실행과 동시에 실행됩니다. 둘 다 동시에 시작하므로 지연 시간이 가장 짧습니다. 그러나 가드레일의 트립와이어가 작동하면 에이전트가 취소되기 전에 이미 토큰을 소비하고 도구를 실행했을 수 있습니다.

- **차단 실행** (`run_in_parallel=False`): 가드레일이 에이전트보다 *먼저* 실행되어 완료됩니다. 가드레일 트립와이어가 작동하면 에이전트가 실행되지 않으므로 토큰 소비와 도구 실행을 방지할 수 있습니다. 비용을 최적화하거나 도구 호출로 발생할 수 있는 부작용을 방지하려는 경우에 적합합니다.

## 출력 가드레일 {#output-guardrails}

출력 가드레일은 다음 3단계로 실행됩니다.

1. 먼저 가드레일은 에이전트가 생성한 출력을 받습니다.
2. 다음으로 가드레일 함수가 실행되어 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]을 생성하고, 이 값은 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]로 래핑됩니다
3. 마지막으로 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]이 참인지 확인합니다. 참이면 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 예외가 발생하므로 사용자에게 적절히 응답하거나 예외를 처리할 수 있습니다.

!!! 참고

    출력 가드레일은 최종 에이전트 출력에 대해 실행되도록 설계되었으므로 에이전트가 *마지막* 에이전트인 경우에만 해당 에이전트의 가드레일이 실행됩니다. 입력 가드레일과 마찬가지로, 가드레일이 실제 에이전트와 관련되는 경우가 많기 때문에 이렇게 동작합니다. 에이전트마다 서로 다른 가드레일을 실행하므로 코드를 함께 배치하면 가독성이 향상됩니다.

    출력 가드레일은 항상 에이전트가 완료된 후에 실행되므로 `run_in_parallel` 매개변수를 지원하지 않습니다.

출력 트립와이어와 가드레일 함수에서 발생한 예외는 세션에서 서로 다르게 처리됩니다. 트립와이어는 후보 최종 출력을 거부합니다. 트립와이어가 작동하면 러너는 구성된 세션에 이미 완료된 도구 호출 및 도구 출력 항목과 해당 호출을 재현하는 데 필요한 모든 추론 컨텍스트를 저장하되, 거부된 후보 최종 출력은 제외하도록 요청합니다. 러너는 스트리밍 실행과 비스트리밍 실행 모두에 이 트립와이어 규칙을 적용합니다. 가드레일 함수가 트립와이어 결과를 반환하는 대신 예외를 발생시키면 러너는 판정을 알 수 없는 것으로 간주하고, 가드레일 예외를 표면화하기 전에 완료된 최종 턴 항목을 저장하도록 구성된 세션에 요청합니다. 해당 세션 쓰기까지 실패하면 세션 쓰기 오류가 우선합니다. 스트리밍 실행은 비스트리밍 실행과 동일한 저장 순서를 사용하며 `stream_events()`에서 최종 예외를 발생시킵니다. 출력 가드레일이 실행되는 동안 [`RunResultStreaming.cancel()`][agents.result.RunResultStreaming.cancel]을 즉시 호출하면 진행 중인 가드레일이 취소되고 최종 턴 세션 쓰기는 시작되지 않습니다.

터미널 함수 도구 출력은 에이전트 수준 출력 가드레일이 값을 검사하기 전에 도구가 이미 실행되었으므로 추가 처리가 필요합니다. [`Agent.tool_use_behavior`][agents.agent.Agent.tool_use_behavior]에 따라 해당 도구 결과가 최종 출력이 되고 출력 트립와이어가 이를 거부하는 경우, SDK는 검증된 필드로 함수 호출/출력 쌍을 다시 구성할 수 있을 때만 재현 가능한 유효한 쌍을 유지합니다. 유지되는 `function_call_output` 페이로드는 고정 텍스트 `"Output withheld by an output guardrail."`로 대체됩니다. 원래 도구 출력 페이로드는 세션, `RunState`, 스트리밍 결과 상태 또는 샌드박스 메모리 입력에 유지되지 않습니다. SDK는 함수 인수를 포함하여 재현에 필요한 검증된 함수 호출 메타데이터를 유지하므로, 해당 메타데이터에는 거부된 출력에도 나타난 데이터가 포함될 수 있습니다. 현재 응답의 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] 객체도 `agent_output`을 고정 텍스트로 대체하고 `output_info`을 비웁니다. 현재 응답의 [`ToolOutputGuardrailResult`][agents.tool_guardrails.ToolOutputGuardrailResult] 객체는 허용/거부 동작 유형을 유지하지만, 페이로드를 포함하는 `output_info`과 거부 메시지를 동일한 텍스트로 대체합니다. 이전에 수락된 턴과 가드레일 결과는 변경되지 않습니다. 응답에 추론 또는 SDK가 안전하게 정리할 수 없는 다른 형식이 포함된 경우, SDK는 거부된 출력 페이로드를 유지하는 대신 현재 응답의 전체 후행 부분을 폐기합니다. 예외를 발생시킨 가드레일 함수는 거부 판정을 반환하지 않은 것이므로, 완료된 터미널 도구 턴에는 위에서 설명한 예외 저장 동작이 적용됩니다.

## 도구 가드레일 {#tool-guardrails}

도구 가드레일은 **`FunctionTool` 인스턴스**를 래핑하며, 해당 도구의 실행 전후에 호출을 검증하거나 차단할 수 있게 합니다. 도구 자체에 구성되며 해당 도구가 호출될 때마다 실행됩니다.

- 입력 도구 가드레일은 도구 실행 전에 실행되며, 호출을 건너뛰거나 출력을 메시지로 대체하거나 트립와이어를 발생시킬 수 있습니다.
- 출력 도구 가드레일은 도구 실행 후에 실행되며, 출력을 대체하거나 트립와이어를 발생시킬 수 있습니다.
- 함수 도구에 승인이 필요한 경우 입력 도구 가드레일은 일반적으로 승인 후, 실행 직전에 실행됩니다. 승인 대기 인터럽션(중단 처리)이 발생하기 전에 이러한 입력 검사를 실행하려면 [`RunConfig.tool_execution`][agents.run.RunConfig.tool_execution]을 [`ToolExecutionConfig(pre_approval_tool_input_guardrails=True)`][agents.run.ToolExecutionConfig]으로 설정하세요. 이 사전 승인 검사를 통과한 호출도 승인 후 도구가 실행되기 전에 다시 검사됩니다.
- 도구 가드레일은 [`function_tool`][agents.tool.function_tool]로 생성된 함수 도구에만 적용됩니다. 핸드오프는 일반 함수 도구 파이프라인이 아니라 SDK의 핸드오프 파이프라인을 통해 실행되므로 도구 가드레일은 핸드오프 호출 자체에 적용되지 않습니다. 호스티드 툴(`WebSearchTool`, `FileSearchTool`, `HostedMCPTool`, `CodeInterpreterTool`, `ImageGenerationTool`)과 기본 제공 실행 도구(`ComputerTool`, `ShellTool`, `ApplyPatchTool`, `LocalShellTool`)도 이 가드레일 파이프라인을 사용하지 않으며, [`Agent.as_tool()`][agents.agent.Agent.as_tool]은 현재 도구 가드레일 옵션을 직접 제공하지 않습니다.

자세한 내용은 아래 코드 스니펫을 참조하세요.

## 트립와이어 {#tripwires}

에이전트 입력이나 출력이 가드레일을 통과하지 못하면 가드레일은 트립와이어로 이를 알릴 수 있습니다. 러너는 즉시 `InputGuardrailTripwireTriggered` 또는 `OutputGuardrailTripwireTriggered` 예외를 발생시키고 에이전트 실행을 중단합니다. 도구 가드레일은 이에 대응하는 `ToolInputGuardrailTripwireTriggered` 및 `ToolOutputGuardrailTripwireTriggered` 예외를 사용합니다.

에이전트 수준 트립와이어의 경우 예외의 `guardrail_result`는 트립와이어를 작동시킨 가드레일을 식별합니다. 러너가 입력 트립와이어를 발생시키면 `exception.run_data.input_guardrail_results`에는 실행이 중단되기 전에 완료된 모든 입력 가드레일 결과가 포함되며, 여기에는 트립와이어를 작동시킨 결과도 포함됩니다. 출력 트립와이어는 이에 상응하는 누적 결과를 `exception.run_data.output_guardrail_results`를 통해 제공합니다.

반면 도구 트립와이어 예외는 트립와이어를 작동시킨 `guardrail`와 `output`을 직접 노출합니다. 해당 예외의 `run_data.tool_input_guardrail_results` 및 `run_data.tool_output_guardrail_results` 목록에는 실패 전에 완료된 턴에서 누적된 결과가 유지되며, 트립와이어를 작동시킨 결과는 예외의 `output`를 통해 확인할 수 있습니다. `MaxTurnsExceeded`과 같이 러너가 관리하는 다른 실패도 완료된 도구 가드레일 결과를 이 목록에 유지합니다. `stream_events()`에서 예외가 발생한 후 스트리밍 결과는 동일하게 누적된 에이전트 및 도구 가드레일 결과 목록을 노출합니다. 러너가 관리하는 실행 경로 외부에서 예외가 발생하면 `run_data`는 `None`일 수 있습니다.

## 가드레일 구현 {#implementing-a-guardrail}

입력을 받아 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]을 반환하는 함수를 제공해야 합니다. 이 예제에서는 내부적으로 에이전트를 실행하여 이를 구현합니다.

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

1. 가드레일 함수에서 이 에이전트를 사용합니다.
2. 에이전트의 입력/컨텍스트를 받아 결과를 반환하는 가드레일 함수입니다.
3. 가드레일 결과에 추가 정보를 포함할 수 있습니다.
4. 워크플로를 정의하는 실제 에이전트입니다.

출력 가드레일도 이와 유사합니다.

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

1. 실제 에이전트의 출력 타입입니다.
2. 가드레일의 출력 타입입니다.
3. 에이전트의 출력을 받아 결과를 반환하는 가드레일 함수입니다.
4. 워크플로를 정의하는 실제 에이전트입니다.

마지막으로 도구 가드레일의 예제는 다음과 같습니다.

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