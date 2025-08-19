---
search:
  exclude: true
---
# 빠른 시작

## 프로젝트 및 가상 환경 생성

한 번만 수행하면 됩니다.

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 가상 환경 활성화

새 터미널 세션을 시작할 때마다 실행하세요.

```bash
source .venv/bin/activate
```

### Agents SDK 설치

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API 키 설정

키가 없다면 OpenAI API 키를 생성하려면 [이 안내](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)를 따라주세요.

```bash
export OPENAI_API_KEY=sk-...
```

## 첫 에이전트 생성

에이전트는 instructions, 이름, 그리고 선택적 구성(예: `model_config`)으로 정의됩니다

```python
from agents import Agent

agent = Agent(
    name="Math Tutor",
    instructions="You provide help with math problems. Explain your reasoning at each step and include examples",
)
```

## 에이전트 몇 개 더 추가

추가 에이전트도 같은 방식으로 정의할 수 있습니다. `handoff_descriptions` 는 핸드오프 라우팅을 결정하는 데 필요한 추가 컨텍스트를 제공합니다

```python
from agents import Agent

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You provide assistance with historical queries. Explain important events and context clearly.",
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You provide help with math problems. Explain your reasoning at each step and include examples",
)
```

## 핸드오프 정의

각 에이전트에서, 작업을 진행하는 방법을 결정하기 위해 에이전트가 선택할 수 있는 아웃바운드 핸드오프 옵션의 목록을 정의할 수 있습니다.

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="You determine which agent to use based on the user's homework question",
    handoffs=[history_tutor_agent, math_tutor_agent]
)
```

## 에이전트 오케스트레이션 실행

워크플로가 실행되고 분류(triage) 에이전트가 두 전문 에이전트 사이를 올바르게 라우팅하는지 확인해 봅시다

```python
from agents import Runner

async def main():
    result = await Runner.run(triage_agent, "What is the capital of France?")
    print(result.final_output)
```

## 가드레일 추가

입력 또는 출력에 대해 실행할 사용자 지정 가드레일을 정의할 수 있습니다

```python
from agents import GuardrailFunctionOutput, Agent, Runner
from pydantic import BaseModel


class HomeworkOutput(BaseModel):
    is_homework: bool
    reasoning: str

guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the user is asking about homework.",
    output_type=HomeworkOutput,
)

async def homework_guardrail(ctx, agent, input_data):
    result = await Runner.run(guardrail_agent, input_data, context=ctx.context)
    final_output = result.final_output_as(HomeworkOutput)
    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=not final_output.is_homework,
    )
```

## 전체 통합

핸드오프와 입력 가드레일을 사용해 모두 통합한 전체 워크플로를 실행해 봅시다

```python
from agents import Agent, InputGuardrail, GuardrailFunctionOutput, Runner
from agents.exceptions import InputGuardrailTripwireTriggered
from pydantic import BaseModel
import asyncio

class HomeworkOutput(BaseModel):
    is_homework: bool
    reasoning: str

guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the user is asking about homework.",
    output_type=HomeworkOutput,
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You provide help with math problems. Explain your reasoning at each step and include examples",
)

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You provide assistance with historical queries. Explain important events and context clearly.",
)


async def homework_guardrail(ctx, agent, input_data):
    result = await Runner.run(guardrail_agent, input_data, context=ctx.context)
    final_output = result.final_output_as(HomeworkOutput)
    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=not final_output.is_homework,
    )

triage_agent = Agent(
    name="Triage Agent",
    instructions="You determine which agent to use based on the user's homework question",
    handoffs=[history_tutor_agent, math_tutor_agent],
    input_guardrails=[
        InputGuardrail(guardrail_function=homework_guardrail),
    ],
)

async def main():
    # Example 1: History question
    try:
        result = await Runner.run(triage_agent, "who was the first president of the united states?")
        print(result.final_output)
    except InputGuardrailTripwireTriggered as e:
        print("Guardrail blocked this input:", e)

    # Example 2: General/philosophical question
    try:
        result = await Runner.run(triage_agent, "What is the meaning of life?")
        print(result.final_output)
    except InputGuardrailTripwireTriggered as e:
        print("Guardrail blocked this input:", e)

if __name__ == "__main__":
    asyncio.run(main())
```

## 트레이스 보기

에이전트 실행 중에 무슨 일이 일어났는지 검토하려면 [OpenAI 대시보드의 Trace viewer](https://platform.openai.com/traces)로 이동해 실행 트레이스를 확인하세요.

## 다음 단계

더 복잡한 에이전트형 플로우를 만드는 방법:

-   [에이전트](agents.md) 구성 방법 알아보기
-   [에이전트 실행](running_agents.md) 알아보기
-   [도구](tools.md), [가드레일](guardrails.md), [모델](models/index.md) 알아보기