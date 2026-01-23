---
search:
  exclude: true
---
# 에이전트

에이전트는 앱의 핵심 구성 요소입니다. 에이전트는 instructions 및 tools로 구성된 대규모 언어 모델(LLM)입니다.

## 기본 구성

에이전트를 구성할 때 가장 흔히 설정하는 속성은 다음과 같습니다:

-   `name`: 에이전트를 식별하는 필수 문자열입니다
-   `instructions`: 개발자 메시지 또는 시스템 프롬프트로도 알려져 있습니다
-   `model`: 사용할 LLM, 그리고 temperature, top_p 등과 같은 모델 튜닝 매개변수를 구성하기 위한 선택적 `model_settings`
-   `prompt`: OpenAI의 Responses API를 사용할 때 id(및 변수)로 프롬프트 템플릿을 참조합니다
-   `tools`: 작업을 달성하기 위해 에이전트가 사용할 수 있는 도구입니다
-   `mcp_servers`: 에이전트에 도구를 제공하는 MCP 서버입니다. [MCP 가이드](mcp.md)를 참고하세요
-   `reset_tool_choice`: 도구 호출 이후 `tool_choice`를 재설정할지 여부(기본값: `True`)로, 도구 사용 루프를 방지합니다. [도구 사용 강제](#forcing-tool-use)를 참고하세요

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
    """returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="gpt-5-nano",
    tools=[get_weather],
)
```

## 프롬프트 템플릿

`prompt`를 설정하면 OpenAI 플랫폼에서 생성한 프롬프트 템플릿을 참조할 수 있습니다. 이는 Responses API를 사용하는 OpenAI 모델에서 동작합니다.

사용하려면 다음을 수행하세요:

1. https://platform.openai.com/playground/prompts 로 이동합니다
2. 새 프롬프트 변수 `poem_style`을 생성합니다
3. 다음 내용을 가진 시스템 프롬프트를 생성합니다:

    ```
    Write a poem in {{poem_style}}
    ```

4. `--prompt-id` 플래그로 예제를 실행합니다

```python
from agents import Agent

agent = Agent(
    name="Prompted assistant",
    prompt={
        "id": "pmpt_123",
        "version": "1",
        "variables": {"poem_style": "haiku"},
    },
)
```

실행 시점에 프롬프트를 동적으로 생성할 수도 있습니다:

```python
from dataclasses import dataclass

from agents import Agent, GenerateDynamicPromptData, Runner

@dataclass
class PromptContext:
    prompt_id: str
    poem_style: str


async def build_prompt(data: GenerateDynamicPromptData):
    ctx: PromptContext = data.context.context
    return {
        "id": ctx.prompt_id,
        "version": "1",
        "variables": {"poem_style": ctx.poem_style},
    }


agent = Agent(name="Prompted assistant", prompt=build_prompt)
result = await Runner.run(
    agent,
    "Say hello",
    context=PromptContext(prompt_id="pmpt_123", poem_style="limerick"),
)
```

## 컨텍스트

에이전트는 `context` 타입에 대해 제네릭합니다. 컨텍스트는 의존성 주입 도구입니다. 즉, `Runner.run()`에 생성해 전달하는 객체이며, 모든 에이전트, 도구, 핸드오프 등에 전달되고, 에이전트 실행을 위한 의존성과 상태를 담는 잡동사니 가방 역할을 합니다. 컨텍스트로는 어떤 Python 객체든 제공할 수 있습니다.

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

기본적으로 에이전트는 일반 텍스트(즉 `str`) 출력을 생성합니다. 에이전트가 특정 타입의 출력을 생성하게 하려면 `output_type` 매개변수를 사용할 수 있습니다. 흔한 선택은 [Pydantic](https://docs.pydantic.dev/) 객체이지만, Pydantic의 [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/)로 감쌀 수 있는 타입이라면 무엇이든 지원합니다. 예: dataclasses, lists, TypedDict 등

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

    `output_type`을 전달하면, 모델이 일반적인 평문 응답 대신 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)를 사용하도록 지시하게 됩니다.

## 멀티 에이전트 시스템 설계 패턴

멀티‑에이전트 시스템을 설계하는 방법은 많지만, 일반적으로 다음의 두 가지 폭넓게 적용 가능한 패턴을 자주 봅니다:

1. Manager (agents as tools): 중앙 관리자/오케스트레이터가 전문화된 하위 에이전트를 도구로 호출하고 대화의 제어권을 유지합니다
2. handoffs: 동등한 에이전트들이 전문화된 에이전트에게 제어권을 핸드오프하여, 그 에이전트가 대화를 이어받습니다. 이는 분산형입니다

자세한 내용은 [에이전트 구축 실전 가이드](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)를 참고하세요.

### Manager (agents as tools)

`customer_facing_agent`는 모든 사용자 상호작용을 처리하고, 도구로 노출된 전문화된 하위 에이전트를 호출합니다. 자세한 내용은 [tools](tools.md#agents-as-tools) 문서를 참고하세요.

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

customer_facing_agent = Agent(
    name="Customer-facing agent",
    instructions=(
        "Handle all direct user communication. "
        "Call the relevant tools when specialized expertise is needed."
    ),
    tools=[
        booking_agent.as_tool(
            tool_name="booking_expert",
            tool_description="Handles booking questions and requests.",
        ),
        refund_agent.as_tool(
            tool_name="refund_expert",
            tool_description="Handles refund questions and requests.",
        )
    ],
)
```

### handoffs

핸드오프는 에이전트가 위임할 수 있는 하위 에이전트입니다. 핸드오프가 발생하면, 위임된 에이전트가 대화 기록을 받고 대화를 이어받습니다. 이 패턴은 단일 작업에 뛰어난 모듈식 전문 에이전트를 가능하게 합니다. 자세한 내용은 [handoffs](handoffs.md) 문서를 참고하세요.

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

triage_agent = Agent(
    name="Triage agent",
    instructions=(
        "Help the user with their questions. "
        "If they ask about booking, hand off to the booking agent. "
        "If they ask about refunds, hand off to the refund agent."
    ),
    handoffs=[booking_agent, refund_agent],
)
```

## 동적 instructions

대부분의 경우 에이전트를 생성할 때 instructions를 제공할 수 있습니다. 하지만 함수로 동적 instructions를 제공할 수도 있습니다. 이 함수는 에이전트와 컨텍스트를 입력으로 받아 프롬프트를 반환해야 합니다. 일반 함수와 `async` 함수 모두 허용됩니다.

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

때로는 에이전트의 라이프사이클을 관찰하고 싶을 수 있습니다. 예를 들어 이벤트를 로깅하거나, 특정 이벤트가 발생했을 때 데이터를 미리 가져오고 싶을 수 있습니다. `hooks` 속성으로 에이전트 라이프사이클에 후킹할 수 있습니다. [`AgentHooks`][agents.lifecycle.AgentHooks] 클래스를 서브클래싱하고, 관심 있는 메서드를 오버라이드하세요.

## 가드레일

가드레일은 에이전트가 실행되는 동안 사용자 입력에 대한 검사/검증을 병렬로 수행하고, 출력이 생성된 후에는 에이전트 출력에 대해서도 검사/검증을 수행할 수 있게 합니다. 예를 들어 사용자의 입력과 에이전트의 출력이 관련성이 있는지 스크리닝할 수 있습니다. 자세한 내용은 [guardrails](guardrails.md) 문서를 참고하세요.

## 에이전트 복제/복사

에이전트의 `clone()` 메서드를 사용하면 Agent를 복제할 수 있으며, 원하는 속성을 선택적으로 변경할 수도 있습니다.

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-5.2",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## 도구 사용 강제

tools 목록을 제공한다고 해서 LLM이 항상 도구를 사용한다는 뜻은 아닙니다. [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice]를 설정하여 도구 사용을 강제할 수 있습니다. 유효한 값은 다음과 같습니다:

1. `auto`: LLM이 도구 사용 여부를 결정하도록 허용합니다
2. `required`: LLM이 도구를 사용하도록 요구합니다(하지만 어떤 도구를 사용할지는 지능적으로 결정할 수 있습니다)
3. `none`: LLM이 도구를 _사용하지 않도록_ 요구합니다
4. 예: `my_tool`처럼 특정 문자열을 설정: LLM이 해당 특정 도구를 사용하도록 요구합니다

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

`Agent` 구성의 `tool_use_behavior` 매개변수는 도구 출력이 처리되는 방식을 제어합니다:

- `"run_llm_again"`: 기본값입니다. 도구를 실행하고, LLM이 결과를 처리해 최종 응답을 생성합니다
- `"stop_on_first_tool"`: 첫 번째 도구 호출의 출력을 추가적인 LLM 처리 없이 최종 응답으로 사용합니다

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

- `StopAtTools(stop_at_tool_names=[...])`: 지정된 도구 중 하나라도 호출되면, 해당 출력으로 최종 응답을 사용하고 중지합니다

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

- `ToolsToFinalOutputFunction`: 도구 결과를 처리하고 LLM을 중지할지 계속할지 결정하는 사용자 정의 함수입니다

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

    무한 루프를 방지하기 위해, 프레임워크는 도구 호출 이후 `tool_choice`를 자동으로 "auto"로 재설정합니다. 이 동작은 [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice]를 통해 구성할 수 있습니다. 무한 루프는 도구 결과가 LLM에 전송되고, LLM이 `tool_choice` 때문에 다시 도구 호출을 생성하는 일이 무한히 반복되면서 발생합니다.