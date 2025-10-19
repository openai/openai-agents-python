---
search:
  exclude: true
---
# LiteLLM 를 통한 임의 모델 사용

!!! note

    LiteLLM 통합은 베타 상태입니다. 특히 규모가 작은 일부 모델 제공자에서 문제가 발생할 수 있습니다. [Github issues](https://github.com/openai/openai-agents-python/issues)를 통해 이슈를 보고해 주세요. 신속히 수정하겠습니다.

[LiteLLM](https://docs.litellm.ai/docs/) 은 단일 인터페이스로 100개 이상의 모델을 사용할 수 있게 해주는 라이브러리입니다. Agents SDK 에 LiteLLM 통합을 추가하여 어떤 AI 모델이든 사용할 수 있도록 했습니다.

## 설정

`litellm` 이 사용 가능해야 합니다. 선택적 `litellm` 의존성 그룹을 설치하면 됩니다:

```bash
pip install "openai-agents[litellm]"
```

설치가 완료되면, 어떤 에이전트에서든 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] 을 사용할 수 있습니다.

## 예제

완전히 동작하는 예제입니다. 실행하면 모델 이름과 API 키를 입력하라는 프롬프트가 표시됩니다. 예를 들어 다음과 같이 입력할 수 있습니다:

- `openai/gpt-4.1` 를 모델로, OpenAI API 키
- `anthropic/claude-3-5-sonnet-20240620` 를 모델로, Anthropic API 키
- 등

LiteLLM 이 지원하는 전체 모델 목록은 [litellm providers docs](https://docs.litellm.ai/docs/providers) 를 참고하세요.

```python
from __future__ import annotations

import asyncio

from agents import Agent, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel

@function_tool
def get_weather(city: str):
    print(f"[debug] getting weather for {city}")
    return f"The weather in {city} is sunny."


async def main(model: str, api_key: str):
    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        model=LitellmModel(model=model, api_key=api_key),
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(result.final_output)


if __name__ == "__main__":
    # First try to get model/api key from args
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, required=False)
    parser.add_argument("--api-key", type=str, required=False)
    args = parser.parse_args()

    model = args.model
    if not model:
        model = input("Enter a model name for Litellm: ")

    api_key = args.api_key
    if not api_key:
        api_key = input("Enter an API key for Litellm: ")

    asyncio.run(main(model, api_key))
```

## 사용량 데이터 추적

LiteLLM 응답으로 Agents SDK 사용량 메트릭을 채우고 싶다면, 에이전트를 생성할 때 `ModelSettings(include_usage=True)` 를 전달하세요.

```python
from agents import Agent, ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)
```

`include_usage=True` 를 사용하면, LiteLLM 요청은 기본 제공 OpenAI 모델과 마찬가지로 `result.context_wrapper.usage` 를 통해 토큰 및 요청 수를 보고합니다.