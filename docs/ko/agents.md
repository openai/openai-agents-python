---
search:
  exclude: true
---
# 에이전트

에이전트는 앱의 핵심 구성 요소입니다. 에이전트는 instructions와 tools로 구성된 대규모 언어 모델(LLM)입니다

## 기본 구성

에이전트에서 가장 일반적으로 구성하는 속성은 다음과 같습니다:

-   `name`: 에이전트를 식별하는 필수 문자열
-   `instructions`: developer message 또는 시스템 프롬프트라고도 합니다
-   `model`: 사용할 LLM, 그리고 temperature, top_p 등과 같은 모델 튜닝 매개변수를 구성하기 위한 선택적 `model_settings`
-   `prompt`: OpenAI의 Responses API를 사용할 때 id(및 변수)로 프롬프트 템플릿을 참조합니다
-   `tools`: 에이전트가 작업을 수행하기 위해 사용할 수 있는 도구
-   `mcp_servers`: 에이전트에 도구를 제공하는 MCP 서버. [MCP 가이드](mcp.md)를 참고하세요
-   `reset_tool_choice`: 도구 호출 이후 `tool_choice`를 재설정할지 여부(기본값: `True`). 도구 사용 루프를 방지합니다. [도구 사용 강제](#forcing-tool-use)를 참고하세요

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

`prompt`를 설정하면 OpenAI 플랫폼에서 생성한 프롬프트 템플릿을 참조할 수 있습니다. 이는 Responses API를 사용하는 OpenAI 모델에서 동작합니다

사용하려면 다음을 진행하세요:

1. https://platform.openai.com/playground/prompts 로 이동합니다
2. 새 프롬프트 변수 `poem_style`를 생성합니다
3. 다음 내용으로 시스템 프롬프트를 생성합니다:

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

런타임에 프롬프트를 동적으로 생성할 수도 있습니다:

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

에이전트는 `context` 타입에 대해 범용적입니다. 컨텍스트는 의존성 주입 도구입니다: 사용자가 생성하여 `Runner.run()`에 전달하는 객체이며, 모든 에이전트, 도구, 핸드오프 등에 전달됩니다. 또한 에이전트 실행을 위한 의존성과 상태를 담는 저장소 역할을 합니다. 컨텍스트로는 어떤 Python 객체든 제공할 수 있습니다

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

기본적으로 에이전트는 일반 텍스트(즉, `str`) 출력을 생성합니다. 에이전트가 특정 출력 타입을 생성하도록 하려면 `output_type` 매개변수를 사용할 수 있습니다. 일반적인 선택지는 [Pydantic](https://docs.pydantic.dev/) 객체이며, Pydantic의 [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/)로 감쌀 수 있는 모든 타입을 지원합니다 - dataclass, list, TypedDict 등

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

    `output_type`을 전달하면, 일반 일반 텍스트 응답 대신 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)을 사용하도록 모델에 지시합니다

## 멀티 에이전트 시스템 설계 패턴

멀티 에이전트 시스템을 설계하는 방법은 다양하지만, 일반적으로 폭넓게 적용 가능한 두 가지 패턴이 자주 사용됩니다:

1. 매니저(Agents as tools): 중앙 매니저/오케스트레이터가 전문화된 하위 에이전트를 도구로 호출하고 대화 제어권을 유지합니다
2. 핸드오프: 동등한 에이전트들이 전문화된 에이전트에게 제어권을 넘기고, 해당 에이전트가 대화를 이어받습니다. 이는 분산형 방식입니다

자세한 내용은 [에이전트 구축 실전 가이드](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)를 참고하세요

### 매니저(Agents as tools)

`customer_facing_agent`는 모든 사용자 상호작용을 처리하고, 도구로 노출된 전문화된 하위 에이전트를 호출합니다. 자세한 내용은 [tools](tools.md#agents-as-tools) 문서를 참고하세요

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

### 핸드오프

핸드오프는 에이전트가 위임할 수 있는 하위 에이전트입니다. 핸드오프가 발생하면 위임된 에이전트가 대화 기록을 전달받고 대화를 이어받습니다. 이 패턴은 단일 작업에 뛰어난 모듈식 전문 에이전트를 가능하게 합니다. 자세한 내용은 [handoffs](handoffs.md) 문서를 참고하세요

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

대부분의 경우 에이전트를 생성할 때 instructions를 제공할 수 있습니다. 하지만 함수로 동적 instructions를 제공할 수도 있습니다. 함수는 에이전트와 컨텍스트를 입력받아 프롬프트를 반환해야 합니다. 일반 함수와 `async` 함수 모두 허용됩니다

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

## 생명주기 이벤트(hooks)

때로는 에이전트의 생명주기를 관찰하고 싶을 수 있습니다. 예를 들어 이벤트 로깅, 데이터 사전 로드, 특정 이벤트 발생 시 사용량 기록 등을 할 수 있습니다

hook 범위는 두 가지입니다:

-   [`RunHooks`][agents.lifecycle.RunHooks]는 다른 에이전트로의 핸드오프를 포함해 전체 `Runner.run(...)` 호출을 관찰합니다
-   [`AgentHooks`][agents.lifecycle.AgentHooks]는 `agent.hooks`를 통해 특정 에이전트 인스턴스에 연결됩니다

콜백 컨텍스트도 이벤트에 따라 달라집니다:

-   에이전트 시작/종료 hook은 [`AgentHookContext`][agents.run_context.AgentHookContext]를 받으며, 이는 원래 컨텍스트를 래핑하고 공유 실행 사용량 상태를 포함합니다
-   LLM, 도구, 핸드오프 hook은 [`RunContextWrapper`][agents.run_context.RunContextWrapper]를 받습니다

일반적인 hook 시점:

-   `on_agent_start` / `on_agent_end`: 특정 에이전트가 최종 출력을 생성하기 시작하거나 마칠 때
-   `on_llm_start` / `on_llm_end`: 각 모델 호출의 직전/직후
-   `on_tool_start` / `on_tool_end`: 각 로컬 도구 호출의 전후
-   `on_handoff`: 제어권이 한 에이전트에서 다른 에이전트로 이동할 때

전체 워크플로에 대한 단일 관찰자가 필요하면 `RunHooks`를, 특정 에이전트에 사용자 지정 부수 효과가 필요하면 `AgentHooks`를 사용하세요

```python
from agents import Agent, RunHooks, Runner


class LoggingHooks(RunHooks):
    async def on_agent_start(self, context, agent):
        print(f"Starting {agent.name}")

    async def on_llm_end(self, context, agent, response):
        print(f"{agent.name} produced {len(response.output)} output items")

    async def on_agent_end(self, context, agent, output):
        print(f"{agent.name} finished with usage: {context.usage}")


agent = Agent(name="Assistant", instructions="Be concise.")
result = await Runner.run(agent, "Explain quines", hooks=LoggingHooks())
print(result.final_output)
```

전체 콜백 표면은 [Lifecycle API 레퍼런스](ref/lifecycle.md)를 참고하세요

## 가드레일

가드레일을 사용하면 에이전트가 실행되는 동안 사용자 입력에 대한 검사/검증을 병렬로 실행하고, 에이전트 출력이 생성된 후에도 검사할 수 있습니다. 예를 들어 사용자 입력과 에이전트 출력의 관련성을 검사할 수 있습니다. 자세한 내용은 [guardrails](guardrails.md) 문서를 참고하세요

## 에이전트 복제/복사

에이전트에서 `clone()` 메서드를 사용하면 Agent를 복제하고, 원하는 속성을 선택적으로 변경할 수 있습니다

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

도구 목록을 제공했다고 해서 LLM이 항상 도구를 사용하는 것은 아닙니다. [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice]를 설정해 도구 사용을 강제할 수 있습니다. 유효한 값은 다음과 같습니다:

1. `auto`: 도구 사용 여부를 LLM이 결정할 수 있습니다
2. `required`: LLM이 도구를 반드시 사용해야 합니다(어떤 도구를 사용할지는 지능적으로 결정할 수 있습니다)
3. `none`: LLM이 도구를 사용하지 않아야 합니다
4. 예: `my_tool` 같은 특정 문자열 설정: LLM이 해당 특정 도구를 사용해야 합니다

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
- `"stop_on_first_tool"`: 첫 번째 도구 호출의 출력을 추가 LLM 처리 없이 최종 응답으로 사용합니다

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

- `StopAtTools(stop_at_tool_names=[...])`: 지정된 도구 중 하나가 호출되면 중단하고, 해당 출력을 최종 응답으로 사용합니다

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

- `ToolsToFinalOutputFunction`: 도구 결과를 처리하고 LLM으로 계속 진행할지 중단할지를 결정하는 사용자 지정 함수입니다

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

    무한 루프를 방지하기 위해 프레임워크는 도구 호출 후 `tool_choice`를 자동으로 "auto"로 재설정합니다. 이 동작은 [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice]로 구성할 수 있습니다. 무한 루프는 도구 결과가 LLM으로 전송되고, `tool_choice` 때문에 LLM이 다시 도구 호출을 생성하는 과정이 무한 반복되기 때문에 발생합니다