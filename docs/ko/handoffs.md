---
search:
  exclude: true
---
# 핸드오프

핸드오프는 한 에이전트가 다른 에이전트에게 작업을 위임할 수 있게 합니다. 이는 서로 다른 영역을 전문으로 하는 에이전트들이 있는 시나리오에서 특히 유용합니다. 예를 들어, 고객 지원 앱에는 주문 상태, 환불, FAQ 등과 같은 작업을 각각 전담하는 에이전트들이 있을 수 있습니다.

핸드오프는 LLM 에게 도구로 표현됩니다. 따라서 `Refund Agent` 라는 에이전트로의 핸드오프가 있다면, 해당 도구의 이름은 `transfer_to_refund_agent` 가 됩니다.

## 핸드오프 생성

모든 에이전트에는 [`handoffs`][agents.agent.Agent.handoffs] 매개변수가 있으며, 이는 `Agent` 를 직접 전달하거나 핸드오프를 커스터마이즈하는 `Handoff` 객체를 받을 수 있습니다.

Agents SDK 에서 제공하는 [`handoff()`][agents.handoffs.handoff] 함수를 사용해 핸드오프를 생성할 수 있습니다. 이 함수로 핸드오프 대상 에이전트를 지정하고, 선택적으로 override 와 입력 필터를 설정할 수 있습니다.

### 기본 사용법

간단한 핸드오프를 만드는 방법은 다음과 같습니다:

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` 처럼 에이전트를 직접 사용할 수도 있고, `handoff()` 함수를 사용할 수도 있습니다.

### `handoff()` 함수를 통한 핸드오프 커스터마이징

[`handoff()`][agents.handoffs.handoff] 함수로 다양한 항목을 커스터마이즈할 수 있습니다.

- `agent`: 핸드오프 대상 에이전트
- `tool_name_override`: 기본적으로 `Handoff.default_tool_name()` 함수가 사용되며, 이는 `transfer_to_<agent_name>` 으로 결정됩니다. 이 값을 override 할 수 있습니다.
- `tool_description_override`: `Handoff.default_tool_description()` 의 기본 도구 설명을 override
- `on_handoff`: 핸드오프가 호출될 때 실행되는 콜백 함수입니다. 핸드오프가 호출되었음을 인지하자마자 데이터 페칭을 시작하는 등의 작업에 유용합니다. 이 함수는 에이전트 컨텍스트를 받으며, 선택적으로 LLM 이 생성한 입력도 받을 수 있습니다. 입력 데이터는 `input_type` 매개변수로 제어됩니다.
- `input_type`: 핸드오프에서 기대하는 입력의 타입(선택 사항)
- `input_filter`: 다음 에이전트가 받는 입력을 필터링할 수 있습니다. 아래 내용을 참고하세요.
- `is_enabled`: 핸드오프 활성화 여부입니다. 불리언 또는 불리언을 반환하는 함수가 될 수 있어 런타임에 동적으로 핸드오프를 활성/비활성화할 수 있습니다.

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

특정 상황에서는 LLM 이 핸드오프를 호출할 때 일부 데이터를 제공하길 원할 수 있습니다. 예를 들어, "에스컬레이션 에이전트" 로의 핸드오프를 상상해 보세요. 기록을 위해 사유를 제공받고 싶을 수 있습니다.

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

핸드오프가 발생하면, 마치 새 에이전트가 대화를 인계받아 이전의 전체 대화 히스토리를 보게 되는 것과 같습니다. 이를 변경하고 싶다면 [`input_filter`][agents.handoffs.Handoff.input_filter] 를 설정할 수 있습니다. 입력 필터는 [`HandoffInputData`][agents.handoffs.HandoffInputData] 를 통해 기존 입력을 받고, 새로운 `HandoffInputData` 를 반환해야 하는 함수입니다.

일반적인 패턴들이 일부 존재하며(예: 히스토리에서 모든 도구 호출 제거), 이는 [`agents.extensions.handoff_filters`][] 에 구현되어 있습니다.

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. 이는 `FAQ 에이전트` 가 호출될 때 히스토리에서 모든 도구를 자동으로 제거합니다.

## 추천 프롬프트

LLM 이 핸드오프를 올바르게 이해하도록 하려면, 에이전트에 핸드오프에 대한 정보를 포함하는 것을 권장합니다. [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] 의 권장 접두사를 사용하거나, [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] 를 호출하여 권장 데이터를 자동으로 프롬프트에 추가할 수 있습니다.

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```