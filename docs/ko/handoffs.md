---
search:
  exclude: true
---
# 핸드오프

핸드오프를 사용하면 한 에이전트가 다른 에이전트에 작업을 위임할 수 있습니다. 이는 서로 다른 에이전트가 각기 다른 영역을 전문으로 다루는 시나리오에서 특히 유용합니다. 예를 들어 고객 지원 앱에서는 주문 상태, 환불, FAQ 등의 작업을 각각 전담하는 에이전트가 있을 수 있습니다

핸드오프는 LLM에 도구로 표현됩니다. 따라서 `Refund Agent`라는 이름의 에이전트로 핸드오프가 있다면, 도구 이름은 `transfer_to_refund_agent`가 됩니다

## 핸드오프 생성

모든 에이전트에는 [`handoffs`][agents.agent.Agent.handoffs] 매개변수가 있으며, 여기에 `Agent`를 직접 전달하거나 핸드오프를 커스터마이즈하는 `Handoff` 객체를 전달할 수 있습니다

일반 `Agent` 인스턴스를 전달하면, 해당 에이전트의 [`handoff_description`][agents.agent.Agent.handoff_description] (설정된 경우)이 기본 도구 설명에 추가됩니다. 전체 `handoff()` 객체를 작성하지 않고도 모델이 해당 핸드오프를 선택해야 하는 시점을 힌트로 제공할 때 사용하세요

Agents SDK에서 제공하는 [`handoff()`][agents.handoffs.handoff] 함수를 사용해 핸드오프를 만들 수 있습니다. 이 함수로 핸드오프 대상 에이전트와 선택적 오버라이드, 입력 필터를 지정할 수 있습니다

### 기본 사용법

다음은 간단한 핸드오프를 만드는 방법입니다

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. 에이전트를 직접 사용할 수 있고(`billing_agent`처럼), 또는 `handoff()` 함수를 사용할 수 있습니다

### `handoff()` 함수를 통한 핸드오프 커스터마이즈

[`handoff()`][agents.handoffs.handoff] 함수로 여러 항목을 커스터마이즈할 수 있습니다

-   `agent`: 핸드오프할 대상 에이전트입니다
-   `tool_name_override`: 기본적으로 `Handoff.default_tool_name()` 함수가 사용되며, 이는 `transfer_to_<agent_name>`으로 해석됩니다. 이를 오버라이드할 수 있습니다
-   `tool_description_override`: `Handoff.default_tool_description()`의 기본 도구 설명을 오버라이드합니다
-   `on_handoff`: 핸드오프가 호출될 때 실행되는 콜백 함수입니다. 핸드오프 호출이 확정되는 즉시 데이터 가져오기를 시작하는 등의 용도에 유용합니다. 이 함수는 에이전트 컨텍스트를 받고, 선택적으로 LLM이 생성한 입력도 받을 수 있습니다. 입력 데이터는 `input_type` 매개변수로 제어됩니다
-   `input_type`: 핸드오프가 기대하는 입력 타입(선택 사항)입니다
-   `input_filter`: 다음 에이전트가 받는 입력을 필터링할 수 있습니다. 자세한 내용은 아래를 참고하세요
-   `is_enabled`: 핸드오프 활성화 여부입니다. 불리언 또는 불리언을 반환하는 함수를 사용할 수 있어 런타임에 동적으로 활성화/비활성화할 수 있습니다
-   `nest_handoff_history`: RunConfig 수준 `nest_handoff_history` 설정에 대한 호출별 선택적 오버라이드입니다. `None`이면 대신 현재 활성 실행 구성에 정의된 값을 사용합니다

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

특정 상황에서는 LLM이 핸드오프를 호출할 때 일부 데이터를 제공하도록 하고 싶을 수 있습니다. 예를 들어 "Escalation agent"로 핸드오프한다고 가정해 보겠습니다. 로깅할 수 있도록 사유를 제공받고 싶을 수 있습니다

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

## 입력 필터

핸드오프가 발생하면 새 에이전트가 대화를 인계받아 이전 대화 기록 전체를 보게 되는 것과 같습니다. 이를 변경하려면 [`input_filter`][agents.handoffs.Handoff.input_filter]를 설정할 수 있습니다. 입력 필터는 [`HandoffInputData`][agents.handoffs.HandoffInputData]를 통해 기존 입력을 받아 새로운 `HandoffInputData`를 반환해야 하는 함수입니다

중첩 핸드오프는 옵트인 베타로 제공되며, 안정화 작업 중이므로 기본적으로 비활성화되어 있습니다. [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]를 활성화하면, 러너는 이전 대화록을 단일 어시스턴트 요약 메시지로 축약하고 `<CONVERSATION HISTORY>` 블록으로 감쌉니다. 같은 실행 중 여러 핸드오프가 발생하면 이 블록에 새 턴이 계속 추가됩니다. 전체 `input_filter`를 작성하지 않고 생성된 메시지를 대체하려면 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]를 통해 자체 매핑 함수를 제공할 수 있습니다. 이 옵트인은 핸드오프와 실행 모두에서 명시적 `input_filter`를 제공하지 않은 경우에만 적용되므로, 이미 페이로드를 커스터마이즈하는 기존 코드(이 저장소의 예제 포함)는 변경 없이 현재 동작을 유지합니다. [`handoff(...)`][agents.handoffs.handoff]에 `nest_handoff_history=True` 또는 `False`를 전달해 단일 핸드오프의 중첩 동작을 오버라이드할 수 있으며, 이는 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]를 설정합니다. 생성된 요약의 래퍼 텍스트만 변경하려면 에이전트를 실행하기 전에 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers](및 선택적으로 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers])를 호출하세요

몇 가지 일반적인 패턴(예: 기록에서 모든 도구 호출 제거)은 [`agents.extensions.handoff_filters`][]에 구현되어 있습니다

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. 이렇게 하면 `FAQ agent`가 호출될 때 기록에서 모든 도구가 자동으로 제거됩니다

## 권장 프롬프트

LLM이 핸드오프를 올바르게 이해하도록 하려면, 에이전트에 핸드오프 관련 정보를 포함하는 것을 권장합니다. [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][]에 권장 접두사가 있으며, [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][]를 호출해 프롬프트에 권장 데이터를 자동으로 추가할 수도 있습니다

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```