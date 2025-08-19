---
search:
  exclude: true
---
# LiteLLM을 통한 임의 모델 사용

!!! note

    LiteLLM 통합은 베타 단계입니다. 특히 소규모 모델 제공업체와 함께 사용할 때 문제가 발생할 수 있습니다. 문제를 발견하시면 [Github issues](https://github.com/openai/openai-agents-python/issues)를 통해 보고해 주세요. 신속하게 수정하겠습니다.

[LiteLLM](https://docs.litellm.ai/docs/)은 단일 인터페이스로 100개 이상의 모델을 사용할 수 있게 해주는 라이브러리입니다. Agents SDK에 LiteLLM 통합을 추가하여, 원하는 어떤 AI 모델이든 사용할 수 있습니다.

## 설정

`litellm`이 사용 가능한지 확인해야 합니다. 선택적 `litellm` 종속성 그룹을 설치하면 됩니다:

```bash
pip install "openai-agents[litellm]"
```

설치가 완료되면, 어떤 에이전트에서든 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]을 사용할 수 있습니다.

## 예시

다음은 완전한 동작 예시입니다. 실행하면 모델 이름과 API 키 입력을 요청합니다. 예를 들어 다음과 같이 입력할 수 있습니다:

-   모델에 `openai/gpt-4.1`, 그리고 OpenAI API 키
-   모델에 `anthropic/claude-3-5-sonnet-20240620`, 그리고 Anthropic API 키
-   기타

LiteLLM에서 지원하는 모델 전체 목록은 [litellm providers 문서](https://docs.litellm.ai/docs/providers)를 참고하세요.

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