---
search:
  exclude: true
---
# 컨텍스트 관리

컨텍스트는 다양한 의미로 사용됩니다. 여기서 중요한 컨텍스트는 두 가지 범주가 있습니다:

1. 코드에서 로컬로 사용할 수 있는 컨텍스트: 도구 함수가 실행될 때, `on_handoff` 같은 콜백이나 라이프사이클 훅에서 필요할 수 있는 데이터와 의존성입니다
2. LLM 이 사용할 수 있는 컨텍스트: LLM 이 응답을 생성할 때 볼 수 있는 데이터입니다

## 로컬 컨텍스트

이는 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 클래스와 그 안의 [`context`][agents.run_context.RunContextWrapper.context] 속성을 통해 표현됩니다. 동작 방식은 다음과 같습니다:

1. 원하는 Python 객체를 만듭니다. 일반적으로 dataclass 또는 Pydantic 객체를 사용합니다
2. 해당 객체를 다양한 실행 메서드에 전달합니다(예: `Runner.run(..., **context=whatever**))`)
3. 모든 도구 호출, 라이프사이클 훅 등은 래퍼 객체 `RunContextWrapper[T]`를 전달받으며, 여기서 `T` 는 `wrapper.context` 를 통해 접근할 수 있는 컨텍스트 객체 타입을 나타냅니다

**가장 중요한 점**: 특정 에이전트 실행에 대한 모든 에이전트, 도구 함수, 라이프사이클 등은 동일한 _타입_ 의 컨텍스트를 사용해야 합니다.

컨텍스트는 다음과 같은 용도로 사용할 수 있습니다:

-   실행을 위한 컨텍스트 데이터(예: 사용자 이름/uid 같은 값 또는 사용자에 대한 기타 정보)
-   의존성(예: 로거 객체, 데이터 페처 등)
-   헬퍼 함수

!!! danger "주의"

    컨텍스트 객체는 LLM 으로 **전송되지 않습니다**. 읽고 쓰고 메서드를 호출할 수 있는 순수한 로컬 객체입니다.

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

1. 이것이 컨텍스트 객체입니다. 여기서는 dataclass 를 사용했지만, 어떤 타입이든 사용할 수 있습니다
2. 이것은 도구입니다. `RunContextWrapper[UserInfo]` 를 받는 것을 볼 수 있습니다. 도구 구현은 컨텍스트에서 값을 읽습니다
3. 타입체커가 오류를 잡을 수 있도록 에이전트에 제네릭 `UserInfo` 를 지정합니다(예를 들어, 다른 컨텍스트 타입을 받는 도구를 전달하려 하면 잡아냄)
4. 컨텍스트는 `run` 함수로 전달됩니다
5. 에이전트는 도구를 올바르게 호출하여 age 를 얻습니다

## 에이전트/LLM 컨텍스트

LLM 이 호출될 때, 볼 수 있는 **유일한** 데이터는 대화 히스토리에서 옵니다. 즉, LLM 에게 새로운 데이터를 제공하려면 그 히스토리에 포함되도록 해야 합니다. 이를 위한 방법은 다음과 같습니다:

1. Agent `instructions` 에 추가할 수 있습니다. 이는 "system prompt" 또는 "developer message" 로도 알려져 있습니다. System prompts 는 정적 문자열일 수도, 컨텍스트를 받아 문자열을 출력하는 동적 함수일 수도 있습니다. 항상 유용한 정보(예: 사용자 이름이나 현재 날짜)에는 흔히 사용하는 기법입니다
2. `Runner.run` 함수를 호출할 때 `input` 에 추가할 수 있습니다. 이는 `instructions` 기법과 유사하지만, [명령 체계](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)에서 더 하위 위치의 메시지를 사용할 수 있습니다
3. 함수 도구를 통해 노출합니다. 이는 _온디맨드_ 컨텍스트에 유용합니다. LLM 이 언제 데이터가 필요한지 결정하고, 그 데이터를 가져오기 위해 도구를 호출할 수 있습니다
4. retrieval 또는 웹 검색을 사용합니다. 이는 파일이나 데이터베이스(retrieval) 또는 웹(웹 검색)에서 관련 데이터를 가져올 수 있는 특수 도구입니다. 이는 응답을 관련 컨텍스트 데이터에 "그라운딩" 하는 데 유용합니다