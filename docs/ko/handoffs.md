---
search:
  exclude: true
---
# 핸드오프

핸드오프를 사용하면 한 에이전트가 다른 에이전트에 작업을 위임할 수 있습니다. 이는 서로 다른 에이전트가 각기 다른 영역을 전문으로 하는 시나리오에서 특히 유용합니다. 예를 들어 고객 지원 앱에는 주문 상태, 환불, FAQ 등의 작업을 각각 전담하는 에이전트가 있을 수 있습니다.

핸드오프는 LLM에 도구로 표현됩니다. 따라서 `Refund Agent`라는 이름의 에이전트로 핸드오프가 있으면 도구 이름은 `transfer_to_refund_agent`가 됩니다.

## 핸드오프 생성

모든 에이전트에는 [`handoffs`][agents.agent.Agent.handoffs] 매개변수가 있으며, 여기에 `Agent`를 직접 전달하거나 핸드오프를 사용자 지정하는 `Handoff` 객체를 전달할 수 있습니다.

일반 `Agent` 인스턴스를 전달하면 해당 [`handoff_description`][agents.agent.Agent.handoff_description] (설정된 경우)이 기본 도구 설명에 추가됩니다. 전체 `handoff()` 객체를 작성하지 않고도 모델이 해당 핸드오프를 선택해야 하는 시점을 힌트로 제공할 때 사용하세요.

Agents SDK가 제공하는 [`handoff()`][agents.handoffs.handoff] 함수를 사용해 핸드오프를 만들 수 있습니다. 이 함수로 핸드오프 대상 에이전트와 선택적 재정의 및 입력 필터를 지정할 수 있습니다.

### 기본 사용법

간단한 핸드오프를 만드는 방법은 다음과 같습니다:

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. 에이전트를 직접 사용할 수 있고(`billing_agent`처럼), 또는 `handoff()` 함수를 사용할 수 있습니다.

### `handoff()` 함수로 핸드오프 사용자 지정

[`handoff()`][agents.handoffs.handoff] 함수로 여러 항목을 사용자 지정할 수 있습니다.

-   `agent`: 핸드오프 대상 에이전트입니다.
-   `tool_name_override`: 기본적으로 `Handoff.default_tool_name()` 함수가 사용되며, `transfer_to_<agent_name>`으로 해석됩니다. 이를 재정의할 수 있습니다.
-   `tool_description_override`: `Handoff.default_tool_description()`의 기본 도구 설명을 재정의합니다
-   `on_handoff`: 핸드오프가 호출될 때 실행되는 콜백 함수입니다. 핸드오프 호출이 확정되는 즉시 데이터 페칭을 시작하는 등의 용도에 유용합니다. 이 함수는 에이전트 컨텍스트를 받으며, 선택적으로 LLM이 생성한 입력도 받을 수 있습니다. 입력 데이터는 `input_type` 매개변수로 제어됩니다.
-   `input_type`: 핸드오프 도구 호출 인자의 스키마입니다. 설정하면 파싱된 페이로드가 `on_handoff`로 전달됩니다.
-   `input_filter`: 다음 에이전트가 받는 입력을 필터링할 수 있습니다. 자세한 내용은 아래를 참고하세요.
-   `is_enabled`: 핸드오프 활성화 여부입니다. 불리언 또는 불리언을 반환하는 함수가 될 수 있어 런타임에 동적으로 핸드오프를 활성화/비활성화할 수 있습니다.
-   `nest_handoff_history`: RunConfig 수준의 `nest_handoff_history` 설정에 대한 선택적 호출별 재정의입니다. `None`이면 활성 run 설정에 정의된 값을 대신 사용합니다.

[`handoff()`][agents.handoffs.handoff] 헬퍼는 항상 전달한 특정 `agent`로 제어를 넘깁니다. 가능한 대상이 여러 개라면 대상마다 하나의 핸드오프를 등록하고 모델이 그중에서 선택하게 하세요. 호출 시점에 어떤 에이전트를 반환할지 직접 핸드오프 코드에서 결정해야 할 때만 사용자 지정 [`Handoff`][agents.handoffs.Handoff]를 사용하세요.

```python
from agents import Agent, handoff, RunContextWrapper

def on_handoff(ctx: RunContextWrapper[None]):
    print("Handoff called")

agent = Agent(name="My agent")

handoff_obj = handoff(
    agent=agent,
    on_handoff=on_handoff,
    tool_name_override="custom_handoff_tool",
    tool_description_override="Custom description",
)
```

## 핸드오프 입력

특정 상황에서는 핸드오프를 호출할 때 LLM이 일부 데이터를 제공하도록 하고 싶을 수 있습니다. 예를 들어 "Escalation agent"로 핸드오프한다고 가정해 보겠습니다. 이때 기록을 남기기 위해 사유를 함께 받도록 할 수 있습니다.

```python
from pydantic import BaseModel

from agents import Agent, handoff, RunContextWrapper

class EscalationData(BaseModel):
    reason: str

async def on_handoff(ctx: RunContextWrapper[None], input_data: EscalationData):
    print(f"Escalation agent called with reason: {input_data.reason}")

agent = Agent(name="Escalation agent")

handoff_obj = handoff(
    agent=agent,
    on_handoff=on_handoff,
    input_type=EscalationData,
)
```

`input_type`은 핸드오프 도구 호출 자체의 인자를 설명합니다. SDK는 그 스키마를 핸드오프 도구의 `parameters`로 모델에 노출하고, 반환된 JSON을 로컬에서 검증한 뒤, 파싱된 값을 `on_handoff`에 전달합니다.

이는 다음 에이전트의 기본 입력을 대체하지 않으며, 다른 목적지를 선택하지도 않습니다. [`handoff()`][agents.handoffs.handoff] 헬퍼는 여전히 래핑한 특정 에이전트로 전송하며, 수신 에이전트는 [`input_filter`][agents.handoffs.Handoff.input_filter] 또는 중첩 핸드오프 기록 설정으로 변경하지 않는 한 대화 기록을 계속 확인합니다.

`input_type`은 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]와도 별개입니다. 이미 로컬에 있는 애플리케이션 상태나 의존성이 아니라, 모델이 핸드오프 시점에 결정하는 메타데이터에 `input_type`을 사용하세요.

### `input_type` 사용 시점

핸드오프에 `reason`, `language`, `priority`, `summary` 같은 모델 생성 메타데이터의 작은 조각이 필요할 때 `input_type`을 사용하세요. 예를 들어 트리아지 에이전트는 `{ "reason": "duplicate_charge", "priority": "high" }`와 함께 환불 에이전트로 핸드오프할 수 있으며, `on_handoff`는 환불 에이전트가 이어받기 전에 해당 메타데이터를 기록하거나 저장할 수 있습니다.

목적이 다르면 다른 메커니즘을 선택하세요:

-   기존 애플리케이션 상태와 의존성은 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]에 넣으세요. [컨텍스트 가이드](context.md)를 참고하세요.
-   수신 에이전트가 보는 기록을 바꾸려면 [`input_filter`][agents.handoffs.Handoff.input_filter], [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history], 또는 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]를 사용하세요.
-   가능한 전문 에이전트 대상이 여러 개라면 대상마다 하나의 핸드오프를 등록하세요. `input_type`은 선택된 핸드오프에 메타데이터를 추가할 수는 있지만, 대상 간 디스패치를 수행하지는 않습니다.
-   대화를 전송하지 않고 중첩 전문 에이전트에 구조화된 입력을 주고 싶다면 [`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool]을 우선 사용하세요. [도구](tools.md#structured-input-for-tool-agents)를 참고하세요.

## 입력 필터

핸드오프가 발생하면 새 에이전트가 대화를 이어받아 이전 전체 대화 기록을 보는 것과 같습니다. 이를 변경하려면 [`input_filter`][agents.handoffs.Handoff.input_filter]를 설정할 수 있습니다. 입력 필터는 [`HandoffInputData`][agents.handoffs.HandoffInputData]를 통해 기존 입력을 받고, 새로운 `HandoffInputData`를 반환해야 하는 함수입니다.

[`HandoffInputData`][agents.handoffs.HandoffInputData]에는 다음이 포함됩니다:

-   `input_history`: `Runner.run(...)` 시작 전의 입력 기록
-   `pre_handoff_items`: 핸드오프가 호출된 에이전트 턴 이전에 생성된 항목
-   `new_items`: 핸드오프 호출 및 핸드오프 출력 항목을 포함해 현재 턴에서 생성된 항목
-   `input_items`: `new_items` 대신 다음 에이전트로 전달할 선택적 항목으로, 세션 기록용 `new_items`는 유지하면서 모델 입력을 필터링할 수 있게 해줍니다
-   `run_context`: 핸드오프 호출 시점의 활성 [`RunContextWrapper`][agents.run_context.RunContextWrapper]

중첩 핸드오프는 옵트인 베타로 제공되며 안정화 중이므로 기본적으로 비활성화되어 있습니다. [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]를 활성화하면 러너는 이전 전사를 단일 어시스턴트 요약 메시지로 축약하고, 동일 run에서 여러 핸드오프가 발생할 때 새 턴이 계속 추가되도록 `<CONVERSATION HISTORY>` 블록으로 감쌉니다. 전체 `input_filter`를 작성하지 않고 생성된 메시지를 대체하려면 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]를 통해 자체 매핑 함수를 제공할 수 있습니다. 이 옵트인은 핸드오프와 run 어느 쪽에서도 명시적 `input_filter`를 제공하지 않을 때만 적용되므로, 이미 페이로드를 사용자 지정하는 기존 코드(이 저장소의 예제 포함)는 변경 없이 현재 동작을 유지합니다. [`handoff(...)`][agents.handoffs.handoff]에 `nest_handoff_history=True` 또는 `False`를 전달해 단일 핸드오프의 중첩 동작을 재정의할 수 있으며, 이는 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]를 설정합니다. 생성된 요약의 래퍼 텍스트만 바꾸면 된다면 에이전트를 실행하기 전에 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] (및 선택적으로 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers])를 호출하세요.

핸드오프와 활성 [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] 양쪽 모두 필터를 정의한 경우, 해당 핸드오프에는 핸드오프별 [`input_filter`][agents.handoffs.Handoff.input_filter]가 우선 적용됩니다.

!!! note

    핸드오프는 단일 run 내에서만 유지됩니다. 입력 가드레일은 체인의 첫 번째 에이전트에만 계속 적용되고, 출력 가드레일은 최종 출력을 생성하는 에이전트에만 적용됩니다. 워크플로 내 각 사용자 지정 함수 도구 호출 주변에서 검사가 필요하다면 도구 가드레일을 사용하세요.

일부 일반 패턴(예: 기록에서 모든 도구 호출 제거)은 [`agents.extensions.handoff_filters`][]에 구현되어 있습니다

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. 이렇게 하면 `FAQ agent`가 호출될 때 기록에서 모든 도구가 자동으로 제거됩니다.

## 권장 프롬프트

LLM이 핸드오프를 올바르게 이해하도록 하려면, 에이전트에 핸드오프 관련 정보를 포함할 것을 권장합니다. [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][]에 권장 접두사가 있으며, [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][]를 호출해 프롬프트에 권장 데이터를 자동으로 추가할 수도 있습니다.

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```