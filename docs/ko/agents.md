---
search:
  exclude: true
---
# 에이전트

에이전트는 앱의 핵심 구성 요소입니다. 에이전트는 instructions, tools, 그리고 핸드오프, 가드레일, structured outputs 같은 선택적 런타임 동작으로 구성된 대규모 언어 모델(LLM)입니다.

이 페이지는 단일 에이전트를 정의하거나 사용자 지정하려는 경우에 사용하세요. 여러 에이전트가 어떻게 협업해야 할지 결정 중이라면 [에이전트 오케스트레이션](multi_agent.md)을 읽어보세요.

## 다음 가이드 선택

이 페이지를 에이전트 정의 허브로 사용하세요. 다음에 내려야 할 결정에 맞는 인접 가이드로 이동하세요.

| 원하시는 작업 | 다음에 읽을 문서 |
| --- | --- |
| 모델 또는 provider 설정 선택 | [모델](models/index.md) |
| 에이전트에 기능 추가 | [도구](tools.md) |
| 매니저 스타일 오케스트레이션과 핸드오프 중 선택 | [에이전트 오케스트레이션](multi_agent.md) |
| 핸드오프 동작 구성 | [핸드오프](handoffs.md) |
| 턴 실행, 이벤트 스트리밍, 또는 대화 상태 관리 | [에이전트 실행](running_agents.md) |
| 최종 출력, 실행 항목, 또는 재개 가능한 상태 확인 | [결과](results.md) |
| 로컬 의존성과 런타임 상태 공유 | [컨텍스트 관리](context.md) |

## 기본 구성

에이전트의 가장 일반적인 속성은 다음과 같습니다:

| 속성 | 필수 | 설명 |
| --- | --- | --- |
| `name` | 예 | 사람이 읽기 쉬운 에이전트 이름 |
| `instructions` | 예 | 시스템 프롬프트 또는 동적 instructions 콜백. [동적 instructions](#dynamic-instructions) 참조 |
| `prompt` | 아니요 | OpenAI Responses API 프롬프트 구성. 정적 프롬프트 객체 또는 함수를 허용합니다. [프롬프트 템플릿](#prompt-templates) 참조 |
| `handoff_description` | 아니요 | 이 에이전트가 핸드오프 대상으로 제공될 때 노출되는 짧은 설명 |
| `handoffs` | 아니요 | 대화를 전문 에이전트에 위임합니다. [handoffs](handoffs.md) 참조 |
| `model` | 아니요 | 사용할 LLM. [모델](models/index.md) 참조 |
| `model_settings` | 아니요 | `temperature`, `top_p`, `tool_choice` 같은 모델 튜닝 매개변수 |
| `tools` | 아니요 | 에이전트가 호출할 수 있는 도구. [도구](tools.md) 참조 |
| `mcp_servers` | 아니요 | 에이전트를 위한 MCP 기반 도구. [MCP 가이드](mcp.md) 참조 |
| `input_guardrails` | 아니요 | 이 에이전트 체인의 첫 사용자 입력에서 실행되는 가드레일. [가드레일](guardrails.md) 참조 |
| `output_guardrails` | 아니요 | 이 에이전트의 최종 출력에서 실행되는 가드레일. [가드레일](guardrails.md) 참조 |
| `output_type` | 아니요 | 일반 텍스트 대신 구조화된 출력 타입. [출력 타입](#output-types) 참조 |
| `tool_use_behavior` | 아니요 | 도구 결과를 모델로 다시 전달할지 실행을 종료할지 제어합니다. [도구 사용 동작](#tool-use-behavior) 참조 |
| `reset_tool_choice` | 아니요 | 도구 호출 후 `tool_choice`를 재설정(기본값: `True`)하여 도구 사용 루프를 방지합니다. [도구 사용 강제](#forcing-tool-use) 참조 |

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

`prompt`를 설정하면 OpenAI 플랫폼에서 만든 프롬프트 템플릿을 참조할 수 있습니다. 이는 Responses API를 사용하는 OpenAI 모델에서 동작합니다.

사용 방법은 다음과 같습니다:

1. https://platform.openai.com/playground/prompts 로 이동합니다
2. 새 프롬프트 변수 `poem_style`을 만듭니다
3. 다음 내용을 가진 시스템 프롬프트를 만듭니다:

    ```
    Write a poem in {{poem_style}}
    ```

4. `--prompt-id` 플래그와 함께 예제를 실행합니다

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

에이전트는 `context` 타입에 대해 제네릭입니다. 컨텍스트는 의존성 주입 도구입니다. 즉, 사용자가 생성해 `Runner.run()`에 전달하는 객체이며, 모든 에이전트, 도구, 핸드오프 등에 전달되고 에이전트 실행을 위한 의존성과 상태를 담는 모음으로 사용됩니다. 어떤 Python 객체든 컨텍스트로 제공할 수 있습니다.

전체 `RunContextWrapper` 표면, 공유 사용량 추적, 중첩된 `tool_input`, 직렬화 관련 주의사항은 [컨텍스트 가이드](context.md)를 읽어보세요.

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

기본적으로 에이전트는 일반 텍스트(즉 `str`) 출력을 생성합니다. 에이전트가 특정 타입의 출력을 생성하도록 하려면 `output_type` 매개변수를 사용할 수 있습니다. 일반적인 선택은 [Pydantic](https://docs.pydantic.dev/) 객체이지만, Pydantic [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/)로 래핑할 수 있는 모든 타입을 지원합니다 - dataclasses, lists, TypedDict 등

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

    `output_type`을 전달하면 모델이 일반 일반 텍스트 응답 대신 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)을 사용하도록 지시합니다

## 멀티 에이전트 시스템 설계 패턴

멀티 에이전트 시스템을 설계하는 방법은 많지만, 일반적으로 폭넓게 적용 가능한 두 가지 패턴을 자주 봅니다:

1. 매니저(Agents as tools): 중앙 매니저/오케스트레이터가 전문 하위 에이전트를 도구로 호출하고 대화 제어를 유지합니다
2. 핸드오프: 동등한 에이전트들이 제어를 전문 에이전트로 넘기고, 해당 에이전트가 대화를 이어받습니다. 이는 분산형 방식입니다

자세한 내용은 [에이전트 구축 실전 가이드](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)를 참고하세요.

### 매니저(Agents as tools)

`customer_facing_agent`는 모든 사용자 상호작용을 처리하고 도구로 노출된 전문 하위 에이전트를 호출합니다. 자세한 내용은 [도구](tools.md#agents-as-tools) 문서를 읽어보세요.

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

핸드오프는 에이전트가 위임할 수 있는 하위 에이전트입니다. 핸드오프가 발생하면 위임된 에이전트가 대화 기록을 받아 대화를 이어받습니다. 이 패턴은 단일 작업에 뛰어난 모듈형 전문 에이전트를 가능하게 합니다. 자세한 내용은 [handoffs](handoffs.md) 문서를 읽어보세요.

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

대부분의 경우 에이전트를 생성할 때 instructions를 제공할 수 있습니다. 하지만 함수로 동적 instructions를 제공할 수도 있습니다. 이 함수는 에이전트와 컨텍스트를 받아 프롬프트를 반환해야 합니다. 일반 함수와 `async` 함수 모두 허용됩니다.

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

때로는 에이전트의 라이프사이클을 관찰하고 싶을 수 있습니다. 예를 들어 이벤트를 로깅하거나, 데이터를 미리 가져오거나, 특정 이벤트 발생 시 사용량을 기록하고 싶을 수 있습니다.

hook 범위는 두 가지입니다:

-   [`RunHooks`][agents.lifecycle.RunHooks]는 다른 에이전트로의 핸드오프를 포함해 전체 `Runner.run(...)` 호출을 관찰합니다
-   [`AgentHooks`][agents.lifecycle.AgentHooks]는 `agent.hooks`를 통해 특정 에이전트 인스턴스에 연결됩니다

콜백 컨텍스트도 이벤트에 따라 달라집니다:

-   에이전트 시작/종료 hook은 [`AgentHookContext`][agents.run_context.AgentHookContext]를 받으며, 이는 원래 컨텍스트를 래핑하고 공유 실행 사용량 상태를 포함합니다
-   LLM, 도구, 핸드오프 hook은 [`RunContextWrapper`][agents.run_context.RunContextWrapper]를 받습니다

일반적인 hook 시점:

-   `on_agent_start` / `on_agent_end`: 특정 에이전트가 최종 출력을 생성하기 시작하거나 종료할 때
-   `on_llm_start` / `on_llm_end`: 각 모델 호출 직전/직후
-   `on_tool_start` / `on_tool_end`: 각 로컬 도구 호출 전후
-   `on_handoff`: 제어가 한 에이전트에서 다른 에이전트로 이동할 때

전체 워크플로우에 대한 단일 관찰자가 필요하면 `RunHooks`를, 특정 에이전트에 맞춤 부수 효과가 필요하면 `AgentHooks`를 사용하세요.

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

전체 콜백 표면은 [라이프사이클 API 레퍼런스](ref/lifecycle.md)를 참조하세요.

## 가드레일

가드레일을 사용하면 에이전트 실행과 병렬로 사용자 입력에 대한 검사/검증을 수행하고, 에이전트 출력이 생성된 후 출력에 대해서도 검사/검증을 수행할 수 있습니다. 예를 들어 사용자 입력과 에이전트 출력의 관련성을 검사할 수 있습니다. 자세한 내용은 [가드레일](guardrails.md) 문서를 읽어보세요.

## 에이전트 복제/복사

에이전트의 `clone()` 메서드를 사용하면 Agent를 복제하고, 원하는 속성을 선택적으로 변경할 수 있습니다.

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-5.4",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## 도구 사용 강제

도구 목록을 제공해도 LLM이 항상 도구를 사용하는 것은 아닙니다. [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice]를 설정해 도구 사용을 강제할 수 있습니다. 유효한 값은 다음과 같습니다:

1. `auto`: LLM이 도구 사용 여부를 결정할 수 있습니다
2. `required`: LLM이 도구를 반드시 사용해야 합니다(단, 어떤 도구를 쓸지는 지능적으로 결정할 수 있습니다)
3. `none`: LLM이 도구를 _사용하지 않도록_ 강제합니다
4. 특정 문자열(예: `my_tool`) 설정: LLM이 해당 도구를 반드시 사용해야 합니다

OpenAI Responses 도구 검색을 사용할 때는 이름 지정 도구 선택에 제약이 더 많습니다: `tool_choice`로는 단일 네임스페이스 이름이나 deferred 전용 도구를 대상으로 지정할 수 없고, `tool_choice="tool_search"`는 [`ToolSearchTool`][agents.tool.ToolSearchTool]을 대상으로 하지 않습니다. 이런 경우 `auto` 또는 `required`를 권장합니다. Responses 전용 제약은 [호스티드 툴 검색](tools.md#hosted-tool-search)을 참조하세요.

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

- `"run_llm_again"`: 기본값. 도구를 실행하고, LLM이 결과를 처리해 최종 응답을 생성합니다
- `"stop_on_first_tool"`: 추가 LLM 처리 없이 첫 번째 도구 호출의 출력을 최종 응답으로 사용합니다

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

- `StopAtTools(stop_at_tool_names=[...])`: 지정한 도구 중 하나라도 호출되면 중지하고, 해당 출력을 최종 응답으로 사용합니다

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

- `ToolsToFinalOutputFunction`: 도구 결과를 처리하고 LLM으로 계속할지 중지할지 결정하는 사용자 지정 함수입니다

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

    무한 루프를 방지하기 위해 프레임워크는 도구 호출 후 `tool_choice`를 자동으로 "auto"로 재설정합니다. 이 동작은 [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice]로 구성할 수 있습니다. 무한 루프가 발생하는 이유는 도구 결과가 LLM으로 전송되고, `tool_choice` 때문에 LLM이 또 다른 도구 호출을 생성하는 과정이 무한 반복되기 때문입니다