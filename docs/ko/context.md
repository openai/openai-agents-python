---
search:
  exclude: true
---
# 컨텍스트 관리

컨텍스트는 여러 의미로 사용되는 용어입니다. 고려해야 할 컨텍스트에는 크게 두 가지 유형이 있습니다.

1. 코드에서 로컬로 사용할 수 있는 컨텍스트: 도구 함수 실행 시, `on_handoff` 같은 콜백 내에서, 수명 주기 훅 등에서 필요할 수 있는 데이터와 종속성입니다.
2. LLM에서 사용할 수 있는 컨텍스트: 응답을 생성할 때 LLM이 확인하는 데이터입니다.

## 로컬 컨텍스트 {#local-context}

이는 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 클래스와 그 안의 [`context`][agents.run_context.RunContextWrapper.context] 속성으로 표현됩니다. 작동 방식은 다음과 같습니다.

1. 원하는 Python 객체를 생성합니다. 일반적으로 데이터 클래스나 Pydantic 객체를 사용합니다.
2. 해당 객체를 다양한 실행 메서드(예: `Runner.run(..., context=whatever)`)에 전달합니다.
3. 모든 도구 호출, 수명 주기 훅 등에는 래퍼 객체 `RunContextWrapper[T]`가 전달됩니다. 여기서 `T`는 컨텍스트 객체의 타입을 나타내며, 객체 자체는 `wrapper.context`을 통해 사용할 수 있습니다.

일부 런타임별 콜백에는 SDK가 `RunContextWrapper[T]`의 보다 특화된 하위 클래스를 전달할 수 있습니다. 예를 들어 `FunctionTool` 인스턴스의 수명 주기 훅은 일반적으로 `ToolContext`를 받으며, 이를 통해 `tool_call_id`, `tool_name`, `tool_arguments` 같은 도구 호출 메타데이터도 사용할 수 있습니다.

알아야 할 **가장 중요한** 사항은 특정 에이전트 실행에 사용되는 모든 에이전트, 도구 함수, 수명 주기 등이 동일한 컨텍스트 _타입_을 사용해야 한다는 것입니다.

컨텍스트는 다음과 같은 용도로 사용할 수 있습니다.

-   실행에 필요한 컨텍스트 데이터(예: 사용자 이름/uid 또는 사용자에 관한 기타 정보)
-   종속성(예: 로거 객체, 데이터 가져오기 도구 등)
-   도우미 함수

!!! danger "참고"

    컨텍스트 객체는 LLM으로 **전송되지 않습니다**. 컨텍스트 객체는 읽고 쓰거나 메서드를 호출할 수 있는 순수한 로컬 객체입니다.

단일 실행 내에서 파생된 래퍼는 동일한 기본 애플리케이션 컨텍스트, 승인 상태, 사용량 추적을 공유합니다. 중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에는 다른 `tool_input`가 연결될 수 있지만, 기본적으로 애플리케이션 상태의 격리된 복사본을 제공하지는 않습니다.

### 기능 표시 여부를 위한 로컬 컨텍스트 사용 {#use-local-context-for-capability-visibility}

함수 도구, MCP 도구, 핸드오프가 동일한 요청 정책에 의존하는 경우 정책 입력이나 도우미를 애플리케이션 컨텍스트에 유지합니다. 각 SDK 표면은 자체 콜백을 통해 현재 실행 컨텍스트를 노출합니다.

-   [`FunctionTool.is_enabled`][agents.tool.FunctionTool.is_enabled]는 `RunContextWrapper`을 받습니다.
-   [`Handoff.is_enabled`][agents.handoffs.Handoff.is_enabled]는 `RunContextWrapper`을 받습니다.
-   MCP [`tool_filter`](mcp.md#dynamic-tool-filtering)는 [`ToolFilterContext`][agents.mcp.ToolFilterContext]을 받으며, 이 객체의 `run_context` 속성에는 현재 `RunContextWrapper`가 포함됩니다.

별도의 기능 목록을 유지하는 대신 공유 애플리케이션 정책을 이러한 콜백에 맞게 적용합니다. 콜백은 현재 실행에서 SDK가 노출하는 기능을 제어하지만, 모델이 생성한 인수나 리소스 선택을 승인할 수는 없습니다. 함수 도구의 경우 도구 구현 내부에서 이러한 결정을 적용하거나, 적절한 경우 [도구 입력 가드레일](guardrails.md#tool-guardrails)과 [승인](human_in_the_loop.md)을 사용합니다. MCP 서버는 자체적으로 보호된 작업을 승인해야 합니다. `input_type`이 있는 핸드오프의 경우 애플리케이션에 부수 효과가 발생하기 전에 `on_handoff` 시작 부분에서 파싱된 입력을 검사하고, 승인에 실패하면 값을 반환하는 대신 예외를 발생시킵니다. 도구 입력 가드레일은 핸드오프에 적용되지 않습니다. 콜백 수명 주기는 [핸드오프 입력](handoffs.md#handoff-inputs)을 참고하세요.

### `RunContextWrapper`에서 제공하는 항목 {#what-runcontextwrapper-exposes}

[`RunContextWrapper`][agents.run_context.RunContextWrapper]은 애플리케이션에서 정의한 컨텍스트 객체를 감싸는 래퍼입니다. 실제로 가장 자주 사용하는 항목은 다음과 같습니다.

-   자체 가변 애플리케이션 상태와 종속성을 위한 [`wrapper.context`][agents.run_context.RunContextWrapper.context]
-   현재 실행 전체에서 집계된 요청 및 토큰 사용량을 위한 [`wrapper.usage`][agents.run_context.RunContextWrapper.usage]
-   현재 실행이 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 내에서 실행 중일 때 구조화된 입력을 위한 [`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input]
-   프로그래밍 방식으로 승인 상태를 업데이트해야 할 때 사용하는 [`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool]

`wrapper.context`만 애플리케이션에서 정의한 객체입니다. 나머지 필드는 SDK가 관리하는 런타임 메타데이터입니다.

나중에 휴먼인더루프 또는 지속성 있는 작업 워크플로를 위해 [`RunState`][agents.run_state.RunState]을 직렬화하면 해당 런타임 메타데이터도 상태와 함께 저장됩니다. 직렬화된 상태를 영구 보관하거나 전송하려는 경우 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]에 비밀 정보를 넣지 마세요.

대화 상태는 별도로 고려해야 합니다. 대화 턴을 이어가는 방식에 따라 `result.to_input_list()`, `session`, `conversation_id`, `previous_response_id` 중 하나를 사용합니다. 이에 관한 결정은 [결과](results.md), [에이전트 실행](running_agents.md), [세션](sessions/index.md)을 참고하세요.

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner
from agents.decorators import tool

@dataclass
class UserInfo:  # (1)!
    name: str
    uid: int

@tool
async def fetch_user_age(wrapper: RunContextWrapper[UserInfo]) -> str:  # (2)!
    """Fetch the age of the user. Call this function to get user's age information."""
    return f"The user {wrapper.context.name} is 47 years old"

async def main():
    user_info = UserInfo(name="John", uid=123)

    agent = Agent[UserInfo](  # (3)!
        name="Assistant",
        tools=[fetch_user_age],
    )

    result = await Runner.run(  # (4)!
        starting_agent=agent,
        input="What is the age of the user?",
        context=user_info,
    )

    print(result.final_output)  # (5)!
    # The user John is 47 years old.

if __name__ == "__main__":
    asyncio.run(main())
```

1. 컨텍스트 객체입니다. 여기서는 데이터 클래스를 사용했지만, 어떤 타입이든 사용할 수 있습니다.
2. 도구입니다. 이 도구가 `RunContextWrapper[UserInfo]`을 받는 것을 확인할 수 있습니다. 도구 구현은 컨텍스트에서 데이터를 읽습니다.
3. 타입 검사기가 오류를 포착할 수 있도록 에이전트에 제네릭 `UserInfo`을 지정합니다. 예를 들어 다른 컨텍스트 타입을 받는 도구를 전달하려 하면 오류를 포착할 수 있습니다.
4. 컨텍스트가 `run` 함수에 전달됩니다.
5. 에이전트가 도구를 올바르게 호출하여 나이를 가져옵니다.

---

### 고급: `ToolContext` {#advanced-toolcontext}

경우에 따라 실행 중인 도구의 이름, 호출 ID 또는 raw 인수 문자열 같은 추가 메타데이터에 접근해야 할 수 있습니다.  
이때 `RunContextWrapper`를 확장한 [`ToolContext`][agents.tool_context.ToolContext] 클래스를 사용할 수 있습니다.

```python
from typing import Annotated
from pydantic import BaseModel, Field
from agents import Agent
from agents.decorators import tool
from agents.tool_context import ToolContext

class WeatherContext(BaseModel):
    user_id: str

class Weather(BaseModel):
    city: str = Field(description="The city name")
    temperature_range: str = Field(description="The temperature range in Celsius")
    conditions: str = Field(description="The weather conditions")

@tool
def get_weather(ctx: ToolContext[WeatherContext], city: Annotated[str, "The city to get the weather for"]) -> Weather:
    print(f"[debug] Tool context: (name: {ctx.tool_name}, call_id: {ctx.tool_call_id}, args: {ctx.tool_arguments})")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")

agent = Agent(
    name="Weather Agent",
    instructions="You are a helpful agent that can tell the weather of a given city.",
    tools=[get_weather],
)
```

`ToolContext`은 `RunContextWrapper`과 동일한 `.context` 속성을 제공하며,  
현재 도구 호출에 해당하는 다음과 같은 추가 필드도 제공합니다.

- `tool_name` – 호출되는 도구의 이름  
- `tool_call_id` – 이 도구 호출의 고유 식별자  
- `tool_arguments` – 도구에 전달된 raw 인수 문자열  
- `tool_namespace` – 도구가 `tool_namespace()` 또는 네임스페이스가 있는 다른 표면을 통해 로드된 경우 도구 호출의 Responses 네임스페이스  
- `qualified_tool_name` – 네임스페이스를 사용할 수 있는 경우 네임스페이스로 한정된 도구 이름  

실행 중 도구 수준 메타데이터가 필요하면 `ToolContext`를 사용합니다.  
에이전트와 도구 간의 일반적인 컨텍스트 공유에는 `RunContextWrapper`만으로 충분합니다. `ToolContext`은 `RunContextWrapper`을 확장하므로, 중첩된 `Agent.as_tool()` 실행에서 구조화된 입력이 제공된 경우 `.tool_input`도 노출할 수 있습니다.

---

## 에이전트/LLM 컨텍스트 {#agentllm-context}

LLM이 호출될 때 확인할 수 있는 **유일한** 데이터는 대화 기록에 있는 데이터입니다. 따라서 LLM에서 새로운 데이터를 사용할 수 있게 하려면 해당 데이터를 대화 기록에서 사용할 수 있는 방식으로 제공해야 합니다. 이를 위한 방법은 몇 가지가 있습니다.

1. 에이전트의 `instructions`에 추가할 수 있습니다. 이는 "시스템 프롬프트" 또는 "개발자 메시지"라고도 합니다. 시스템 프롬프트는 정적 문자열일 수도 있고, 컨텍스트를 받아 문자열을 출력하는 동적 함수일 수도 있습니다. 항상 유용한 정보(예: 사용자의 이름 또는 현재 날짜)를 제공하는 일반적인 방법입니다.
2. `Runner.run` 함수를 호출할 때 `input`에 추가합니다. 이는 `instructions` 방식과 유사하지만, [명령 체계](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)에서 우선순위가 더 낮은 메시지를 사용할 수 있습니다.
3. `FunctionTool` 인스턴스를 통해 노출합니다. 이는 _필요할 때만_ 제공되는 컨텍스트에 유용합니다. LLM이 특정 데이터가 필요한 시점을 판단하고 도구를 호출하여 해당 데이터를 가져올 수 있습니다.
4. 검색 또는 웹 검색을 사용합니다. 이러한 특수 도구는 파일이나 데이터베이스(검색) 또는 웹(웹 검색)에서 관련 데이터를 가져올 수 있습니다. 이는 관련 컨텍스트 데이터를 기반으로 응답의 근거를 마련하는 데 유용합니다.