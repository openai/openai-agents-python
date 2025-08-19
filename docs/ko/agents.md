---
search:
  exclude: true
---
# 에이전트

에이전트는 앱의 핵심 빌딩 블록입니다. 에이전트는 instructions 와 tools 로 구성된 대규모 언어 모델(LLM)입니다.

## 기본 구성

에이전트를 구성할 때 가장 일반적으로 설정하는 속성은 다음과 같습니다:

-   `name`: 에이전트를 식별하는 필수 문자열
-   `instructions`: 개발자 메시지 또는 system prompt 로도 알려져 있음
-   `model`: 사용할 LLM 및 temperature, top_p 등 모델 튜닝 매개변수를 설정하는 선택적 `model_settings`
-   `tools`: 에이전트가 작업을 수행하는 데 사용할 수 있는 도구

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
     """returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="o3-mini",
    tools=[get_weather],
)
```

## 컨텍스트

에이전트는 `context` 타입에 대해 제네릭합니다. 컨텍스트는 의존성 주입 도구로, 여러분이 생성하여 `Runner.run()` 에 전달하는 객체이며, 모든 에이전트, 도구, 핸드오프 등에 전달되고 에이전트 실행을 위한 의존성과 상태를 담는 용도로 사용됩니다. 컨텍스트로는 임의의 Python 객체를 제공할 수 있습니다.

```python
@dataclass
class UserContext:
    name: str
    uid: str
    is_pro_user: bool

    async def fetch_purchases() -> list[Purchase]:
        return ...

agent = Agent[UserContext](
    ...,
)
```

## 출력 타입

기본적으로 에이전트는 일반 텍스트(즉, `str`) 출력을 생성합니다. 에이전트가 특정 타입의 출력을 생성하도록 하려면 `output_type` 매개변수를 사용할 수 있습니다. 일반적인 선택은 [Pydantic](https://docs.pydantic.dev/) 객체를 사용하는 것이지만, Pydantic [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) 로 래핑할 수 있는 모든 타입(데이터클래스, 리스트, TypedDict 등)을 지원합니다.

```python
from pydantic import BaseModel
from agents import Agent


class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

agent = Agent(
    name="Calendar extractor",
    instructions="Extract calendar events from text",
    output_type=CalendarEvent,
)
```

!!! note

    `output_type` 을 전달하면, 모델은 일반 텍스트 응답 대신 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) 를 사용하도록 지시받습니다.

## 핸드오프

핸드오프는 에이전트가 위임할 수 있는 하위 에이전트입니다. 핸드오프 목록을 제공하면, 에이전트는 관련성이 있을 경우 그들에게 위임할 수 있습니다. 이는 단일 작업에 특화된 모듈식 에이전트를 멀티 에이전트 오케스트레이션 하는 강력한 패턴입니다. 자세한 내용은 [핸드오프](handoffs.md) 문서를 참조하세요.

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

triage_agent = Agent(
    name="Triage agent",
    instructions=(
        "Help the user with their questions."
        "If they ask about booking, handoff to the booking agent."
        "If they ask about refunds, handoff to the refund agent."
    ),
    handoffs=[booking_agent, refund_agent],
)
```

## 동적 instructions

대부분의 경우 에이전트를 생성할 때 instructions 를 제공할 수 있습니다. 그러나 함수로 동적 instructions 를 제공할 수도 있습니다. 이 함수는 에이전트와 컨텍스트를 입력으로 받아 프롬프트를 반환해야 합니다. 일반 함수와 `async` 함수 모두 허용됩니다.

```python
def dynamic_instructions(
    context: RunContextWrapper[UserContext], agent: Agent[UserContext]
) -> str:
    return f"The user's name is {context.context.name}. Help them with their questions."


agent = Agent[UserContext](
    name="Triage agent",
    instructions=dynamic_instructions,
)
```

## 라이프사이클 이벤트(hooks)

때로는 에이전트의 라이프사이클을 관찰하고 싶을 수 있습니다. 예를 들어 이벤트를 로깅하거나 특정 이벤트가 발생할 때 데이터를 미리 가져오고 싶을 수 있습니다. `hooks` 속성으로 에이전트 라이프사이클에 훅을 걸 수 있습니다. [`AgentHooks`][agents.lifecycle.AgentHooks] 클래스를 상속하고, 관심 있는 메서드를 오버라이드하세요.

## 가드레일

가드레일을 사용하면 에이전트가 실행되는 동안 사용자 입력에 대한 검사/검증을 병렬로 수행하고, 에이전트 출력이 생성된 후에도 검사할 수 있습니다. 예를 들어 사용자 입력과 에이전트 출력을 관련성 기준으로 스크리닝할 수 있습니다. 자세한 내용은 [가드레일](guardrails.md) 문서를 참조하세요.

## 에이전트 복제/복사

에이전트의 `clone()` 메서드를 사용하면 에이전트를 복제하고, 원하는 속성을 선택적으로 변경할 수 있습니다.

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="o3-mini",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## 도구 사용 강제

도구 목록을 제공한다고 해서 LLM 이 항상 도구를 사용하는 것은 아닙니다. [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] 를 설정하여 도구 사용을 강제할 수 있습니다. 유효한 값은 다음과 같습니다:

1. `auto`: LLM 이 도구 사용 여부를 결정
2. `required`: LLM 이 반드시 도구를 사용하도록 함(어떤 도구를 사용할지는 지능적으로 결정)
3. `none`: LLM 이 도구를 사용하지 않도록 요구
4. 특정 문자열 설정 예: `my_tool` — 해당 특정 도구 사용을 강제

```python
from agents import Agent, Runner, function_tool, ModelSettings

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    model_settings=ModelSettings(tool_choice="get_weather") 
)
```

## 도구 사용 동작

`Agent` 구성의 `tool_use_behavior` 매개변수는 도구 출력 처리 방식을 제어합니다:
- `"run_llm_again"`: 기본값. 도구를 실행한 뒤, LLM 이 결과를 처리하여 최종 응답을 생성
- `"stop_on_first_tool"`: 첫 번째 도구 호출의 출력을 추가 LLM 처리 없이 최종 응답으로 사용

```python
from agents import Agent, Runner, function_tool, ModelSettings

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    tool_use_behavior="stop_on_first_tool"
)
```

- `StopAtTools(stop_at_tool_names=[...])`: 지정된 도구 중 하나가 호출되면 중지하고 그 출력을 최종 응답으로 사용
```python
from agents import Agent, Runner, function_tool
from agents.agent import StopAtTools

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

@function_tool
def sum_numbers(a: int, b: int) -> int:
    """Adds two numbers."""
    return a + b

agent = Agent(
    name="Stop At Stock Agent",
    instructions="Get weather or sum numbers.",
    tools=[get_weather, sum_numbers],
    tool_use_behavior=StopAtTools(stop_at_tool_names=["get_weather"])
)
```
- `ToolsToFinalOutputFunction`: 도구 결과를 처리하고 중지할지 LLM 으로 계속할지 결정하는 커스텀 함수

```python
from agents import Agent, Runner, function_tool, FunctionToolResult, RunContextWrapper
from agents.agent import ToolsToFinalOutputResult
from typing import List, Any

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

def custom_tool_handler(
    context: RunContextWrapper[Any],
    tool_results: List[FunctionToolResult]
) -> ToolsToFinalOutputResult:
    """Processes tool results to decide final output."""
    for result in tool_results:
        if result.output and "sunny" in result.output:
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=f"Final weather: {result.output}"
            )
    return ToolsToFinalOutputResult(
        is_final_output=False,
        final_output=None
    )

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    tool_use_behavior=custom_tool_handler
)
```

!!! note

    무한 루프를 방지하기 위해, 프레임워크는 도구 호출 후 `tool_choice` 를 자동으로 "auto" 로 재설정합니다. 이 동작은 [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] 로 구성할 수 있습니다. 무한 루프는 도구 결과가 LLM 에게 전달되고, `tool_choice` 때문에 LLM 이 또 다른 도구 호출을 생성하는 상황이 반복되며 발생합니다.