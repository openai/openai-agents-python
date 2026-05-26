---
search:
  exclude: true
---
# 컨텍스트 관리

컨텍스트는 여러 의미로 쓰이는 용어입니다. 주로 고려해야 할 컨텍스트에는 두 가지 주요 유형이 있습니다.

1. 코드에서 로컬로 사용할 수 있는 컨텍스트: 도구 함수가 실행될 때, `on_handoff` 같은 콜백 중에, 생명주기 훅 등에서 필요할 수 있는 데이터와 의존성입니다.
2. LLM이 사용할 수 있는 컨텍스트: LLM이 응답을 생성할 때 보는 데이터입니다.

## 로컬 컨텍스트

이는 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 클래스와 그 안의 [`context`][agents.run_context.RunContextWrapper.context] 속성으로 표현됩니다. 작동 방식은 다음과 같습니다.

1. 원하는 Python 객체를 만듭니다. 일반적인 패턴은 dataclass나 Pydantic 객체를 사용하는 것입니다.
2. 해당 객체를 다양한 run 메서드에 전달합니다(예: `Runner.run(..., context=whatever)`).
3. 모든 도구 호출, 생명주기 훅 등에는 래퍼 객체인 `RunContextWrapper[T]`가 전달되며, 여기서 `T`는 `wrapper.context`를 통해 접근할 수 있는 컨텍스트 객체 타입을 나타냅니다.

일부 런타임별 콜백의 경우 SDK가 `RunContextWrapper[T]`의 더 특화된 서브클래스를 전달할 수 있습니다. 예를 들어 함수 도구 생명주기 훅은 일반적으로 `ToolContext`를 받으며, 이는 `tool_call_id`, `tool_name`, `tool_arguments` 같은 도구 호출 메타데이터도 노출합니다.

알아두어야 할 **가장 중요한** 점은 특정 에이전트 실행에 포함되는 모든 에이전트, 도구 함수, 생명주기 등은 동일한 컨텍스트 _타입_을 사용해야 한다는 것입니다.

컨텍스트는 다음과 같은 용도로 사용할 수 있습니다.

-   실행을 위한 컨텍스트 데이터(예: 사용자 이름/uid 또는 사용자에 대한 기타 정보)
-   의존성(예: 로거 객체, 데이터 페처 등)
-   헬퍼 함수

!!! danger "참고"

    컨텍스트 객체는 LLM으로 전송되지 **않습니다**. 이는 오직 로컬 객체이며, 읽고 쓰거나 해당 객체의 메서드를 호출할 수 있습니다.

단일 실행 내에서 파생된 래퍼들은 동일한 기본 앱 컨텍스트, 승인 상태, 사용량 추적을 공유합니다. 중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행은 다른 `tool_input`을 붙일 수 있지만, 기본적으로 앱 상태의 격리된 복사본을 받지는 않습니다.

### `RunContextWrapper`의 노출 항목

[`RunContextWrapper`][agents.run_context.RunContextWrapper]는 앱에서 정의한 컨텍스트 객체를 감싸는 래퍼입니다. 실제로는 대부분 다음을 사용하게 됩니다.

-   [`wrapper.context`][agents.run_context.RunContextWrapper.context]: 직접 사용하는 변경 가능한 앱 상태와 의존성
-   [`wrapper.usage`][agents.run_context.RunContextWrapper.usage]: 현재 실행 전반의 집계된 요청 및 토큰 사용량
-   [`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input]: 현재 실행이 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 안에서 실행 중일 때의 구조화된 입력
-   [`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool]: 승인 상태를 프로그래밍 방식으로 업데이트해야 할 때 사용

`wrapper.context`만 앱에서 정의한 객체입니다. 다른 필드는 SDK가 관리하는 런타임 메타데이터입니다.

나중에 휴먼인더루프 (HITL) 또는 내구성 있는 작업 워크플로를 위해 [`RunState`][agents.run_state.RunState]를 직렬화하면, 해당 런타임 메타데이터가 상태와 함께 저장됩니다. 직렬화된 상태를 영속화하거나 전송할 계획이라면 [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context]에 비밀 정보를 넣지 마세요.

대화 상태는 별개의 문제입니다. 턴을 이어가는 방식에 따라 `result.to_input_list()`, `session`, `conversation_id`, 또는 `previous_response_id`를 사용하세요. 이 결정에 대해서는 [결과](results.md), [에이전트 실행](running_agents.md), [세션](sessions/index.md)을 참고하세요.

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner, function_tool

@dataclass
class UserInfo:  # (1)!
    name: str
    uid: int

@function_tool
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

1. 이것이 컨텍스트 객체입니다. 여기서는 dataclass를 사용했지만, 어떤 타입이든 사용할 수 있습니다.
2. 이것은 도구입니다. `RunContextWrapper[UserInfo]`를 받는 것을 볼 수 있습니다. 도구 구현은 컨텍스트에서 읽습니다.
3. 에이전트에 제네릭 `UserInfo`를 표시하여, 타입 검사기가 오류를 잡을 수 있도록 합니다(예를 들어 다른 컨텍스트 타입을 받는 도구를 전달하려고 한 경우).
4. 컨텍스트가 `run` 함수에 전달됩니다.
5. 에이전트가 도구를 올바르게 호출하고 나이를 가져옵니다.

---

### 고급: `ToolContext`

어떤 경우에는 실행 중인 도구에 대한 추가 메타데이터(예: 이름, 호출 ID, 원문 인수 문자열)에 접근하고 싶을 수 있습니다.  
이를 위해 `RunContextWrapper`를 확장하는 [`ToolContext`][agents.tool_context.ToolContext] 클래스를 사용할 수 있습니다.

```python
from typing import Annotated
from pydantic import BaseModel, Field
from agents import Agent, Runner, function_tool
from agents.tool_context import ToolContext

class WeatherContext(BaseModel):
    user_id: str

class Weather(BaseModel):
    city: str = Field(description="The city name")
    temperature_range: str = Field(description="The temperature range in Celsius")
    conditions: str = Field(description="The weather conditions")

@function_tool
def get_weather(ctx: ToolContext[WeatherContext], city: Annotated[str, "The city to get the weather for"]) -> Weather:
    print(f"[debug] Tool context: (name: {ctx.tool_name}, call_id: {ctx.tool_call_id}, args: {ctx.tool_arguments})")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")

agent = Agent(
    name="Weather Agent",
    instructions="You are a helpful agent that can tell the weather of a given city.",
    tools=[get_weather],
)
```

`ToolContext`는 `RunContextWrapper`와 동일한 `.context` 속성을 제공하며,  
현재 도구 호출에 특화된 추가 필드도 제공합니다.

- `tool_name` – 호출되는 도구의 이름  
- `tool_call_id` – 이 도구 호출의 고유 식별자  
- `tool_arguments` – 도구에 전달된 원문 인수 문자열  
- `tool_namespace` – 도구가 `tool_namespace()` 또는 다른 네임스페이스가 지정된 표면을 통해 로드된 경우, 도구 호출의 Responses 네임스페이스  
- `qualified_tool_name` – 네임스페이스가 있을 때 해당 네임스페이스로 한정된 도구 이름  

실행 중에 도구 수준 메타데이터가 필요할 때 `ToolContext`를 사용하세요.  
에이전트와 도구 간의 일반적인 컨텍스트 공유에는 `RunContextWrapper`로 충분합니다. `ToolContext`는 `RunContextWrapper`를 확장하므로, 중첩된 `Agent.as_tool()` 실행이 구조화된 입력을 제공한 경우 `.tool_input`도 노출할 수 있습니다.

---

## 에이전트/LLM 컨텍스트

LLM이 호출될 때 LLM이 볼 수 있는 **유일한** 데이터는 대화 기록에 있는 데이터입니다. 즉, LLM이 어떤 새 데이터를 사용할 수 있게 하려면 해당 기록에서 사용할 수 있는 방식으로 제공해야 합니다. 이를 수행하는 방법은 몇 가지가 있습니다.

1. Agent `instructions`에 추가할 수 있습니다. 이는 "시스템 프롬프트" 또는 "개발자 메시지"라고도 합니다. 시스템 프롬프트는 정적 문자열일 수도 있고, 컨텍스트를 받아 문자열을 출력하는 동적 함수일 수도 있습니다. 이는 항상 유용한 정보(예: 사용자의 이름 또는 현재 날짜)에 흔히 사용하는 전략입니다.
2. `Runner.run` 함수를 호출할 때 `input`에 추가합니다. 이는 `instructions` 전략과 유사하지만, [명령 체계](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)에서 더 낮은 위치의 메시지를 사용할 수 있게 해줍니다.
3. 함수 도구를 통해 노출합니다. 이는 _온디맨드_ 컨텍스트에 유용합니다. LLM이 어떤 데이터가 필요한 시점을 결정하고, 해당 데이터를 가져오기 위해 도구를 호출할 수 있습니다.
4. 검색 또는 웹 검색을 사용합니다. 이는 파일이나 데이터베이스에서 관련 데이터를 가져올 수 있는 특수 도구(검색) 또는 웹에서 가져올 수 있는 특수 도구(웹 검색)입니다. 이는 응답을 관련 컨텍스트 데이터에 "근거화"하는 데 유용합니다.