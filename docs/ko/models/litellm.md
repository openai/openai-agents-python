---
search:
  exclude: true
---
# LiteLLM 를 통한 임의 모델 사용

!!! note

    LiteLLM 연동은 베타 단계입니다. 특히 소규모 모델 제공자에서 문제가 발생할 수 있습니다. [Github issues](https://github.com/openai/openai-agents-python/issues)로 문제를 보고해 주세요. 신속히 수정하겠습니다.

[LiteLLM](https://docs.litellm.ai/docs/)은 단일 인터페이스로 100개 이상의 모델을 사용할 수 있게 해주는 라이브러리입니다. Agents SDK 에 LiteLLM 연동을 추가하여, 어떤 AI 모델이든 사용할 수 있습니다.

## 설정

`litellm` 이 사용 가능해야 합니다. 선택적 `litellm` 의존성 그룹을 설치해 준비할 수 있습니다:

```bash
pip install "openai-agents[litellm]"
```

설치가 완료되면, 어떤 에이전트에서도 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] 을 사용할 수 있습니다.

## 예제

다음은 완전히 동작하는 예제입니다. 실행하면 모델 이름과 API 키를 입력하라는 프롬프트가 표시됩니다. 예를 들어 다음과 같이 입력할 수 있습니다:

- `openai/gpt-4.1` 를 모델로, OpenAI API 키
- `anthropic/claude-3-5-sonnet-20240620` 를 모델로, Anthropic API 키
- 등등

LiteLLM 에서 지원하는 전체 모델 목록은 [litellm 제공자 문서](https://docs.litellm.ai/docs/providers)를 참조하세요.

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

LiteLLM 응답이 Agents SDK 사용량 지표에 반영되도록 하려면, 에이전트를 만들 때 `ModelSettings(include_usage=True)` 를 전달하세요.

```python
from agents import Agent, ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)
```

`include_usage=True` 인 경우, LiteLLM 요청은 기본 제공 OpenAI 모델과 마찬가지로 `result.context_wrapper.usage` 를 통해 토큰 및 요청 수를 보고합니다.