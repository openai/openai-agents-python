---
search:
  exclude: true
---
# 모델

Agents SDK 는 OpenAI 모델을 두 가지 형태로 기본 지원합니다:

-   **권장**: 새로운 [Responses API](https://platform.openai.com/docs/api-reference/responses) 를 사용해 OpenAI API 를 호출하는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) 를 사용해 OpenAI API 를 호출하는 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## OpenAI 모델

`Agent` 를 초기화할 때 모델을 지정하지 않으면 기본 모델이 사용됩니다. 현재 기본값은 호환성과 낮은 지연 시간을 위해 [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1) 입니다. 접근 권한이 있다면, 명시적인 `model_settings` 를 유지하면서 더 높은 품질을 위해 에이전트를 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) 로 설정하는 것을 권장합니다.

[`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) 같은 다른 모델로 전환하려면, 에이전트를 구성하는 방법이 두 가지 있습니다.

### 기본 모델

첫째, 커스텀 모델을 설정하지 않은 모든 에이전트에서 특정 모델을 일관되게 사용하려면, 에이전트를 실행하기 전에 `OPENAI_DEFAULT_MODEL` 환경 변수를 설정하세요.

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.2
python3 my_awesome_agent.py
```

둘째, `RunConfig` 를 통해 실행(run) 단위의 기본 모델을 설정할 수 있습니다. 에이전트에 모델을 설정하지 않으면 이 실행의 모델이 사용됩니다.

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.2"),
)
```

#### GPT-5.x 모델

이 방식으로 [`gpt-5.2`](https://platform.openai.com/docs/models/gpt-5.2) 같은 GPT-5.x 모델을 사용하면, SDK 가 기본 `ModelSettings` 를 적용합니다. 대부분의 사용 사례에서 가장 잘 동작하는 설정을 사용합니다. 기본 모델의 추론 노력(reasoning effort)을 조정하려면, 자체 `ModelSettings` 를 전달하세요:

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.2 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5.x model name explicitly:
    model="gpt-5.2",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

더 낮은 지연 시간을 위해 `gpt-5.2` 와 함께 `reasoning.effort="none"` 을 사용하는 것을 권장합니다. gpt-4.1 계열( mini 및 nano 변형 포함)도 인터랙티브 에이전트 앱을 구축하는 데 여전히 견고한 선택지입니다.

#### 비 GPT-5 모델

커스텀 `model_settings` 없이 비 GPT-5 모델 이름을 전달하면, SDK 는 어떤 모델과도 호환되는 일반 `ModelSettings` 로 되돌아갑니다.

## 비 OpenAI 모델

[LiteLLM 통합](./litellm.md) 을 통해 대부분의 다른 비 OpenAI 모델을 사용할 수 있습니다. 먼저 litellm 의존성 그룹을 설치하세요:

```bash
pip install "openai-agents[litellm]"
```

그다음 `litellm/` 접두사를 사용해 [지원되는 모델](https://docs.litellm.ai/docs/providers) 중 아무 것이나 사용하세요:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 비 OpenAI 모델을 사용하는 다른 방법

다른 LLM 제공자를 3가지 추가 방식으로 통합할 수 있습니다(예시는 [여기](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) 참고):

1. [`set_default_openai_client`][agents.set_default_openai_client] 는 `AsyncOpenAI` 인스턴스를 LLM 클라이언트로 전역에서 사용하려는 경우에 유용합니다. 이는 LLM 제공자가 OpenAI 호환 API 엔드포인트를 제공하고, `base_url` 과 `api_key` 를 설정할 수 있는 경우를 위한 것입니다. [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) 에서 구성 가능한 예시를 참고하세요.
2. [`ModelProvider`][agents.models.interface.ModelProvider] 는 `Runner.run` 레벨에 있습니다. 이를 통해 “이 실행에서 모든 에이전트에 대해 커스텀 모델 제공자를 사용”이라고 지정할 수 있습니다. [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) 에서 구성 가능한 예시를 참고하세요.
3. [`Agent.model`][agents.agent.Agent.model] 은 특정 Agent 인스턴스에서 모델을 지정할 수 있게 해줍니다. 이를 통해 서로 다른 에이전트에 대해 서로 다른 제공자를 혼합해 사용할 수 있습니다. [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) 에서 구성 가능한 예시를 참고하세요. 대부분의 사용 가능한 모델을 사용하는 쉬운 방법은 [LiteLLM 통합](./litellm.md) 입니다.

`platform.openai.com` 의 API 키가 없는 경우, `set_tracing_disabled()` 로 트레이싱을 비활성화하거나, [다른 트레이싱 프로세서](../tracing.md) 를 설정하는 것을 권장합니다.

!!! note

    이 예시들에서는 Chat Completions API/모델을 사용합니다. 대부분의 LLM 제공자가 아직 Responses API 를 지원하지 않기 때문입니다. LLM 제공자가 이를 지원한다면 Responses 를 사용하는 것을 권장합니다.

## 모델 혼합 사용

단일 워크플로 내에서 각 에이전트에 서로 다른 모델을 사용하고 싶을 수 있습니다. 예를 들어 트리아지에는 더 작고 빠른 모델을, 복잡한 작업에는 더 크고 더 유능한 모델을 사용할 수 있습니다. [`Agent`][agents.Agent] 를 구성할 때, 다음 중 하나로 특정 모델을 선택할 수 있습니다:

1. 모델 이름을 전달
2. 모델 이름 + 해당 이름을 Model 인스턴스로 매핑할 수 있는 [`ModelProvider`][agents.models.interface.ModelProvider] 를 전달
3. [`Model`][agents.models.interface.Model] 구현을 직접 제공

!!!note

    SDK 는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 과 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 형태를 모두 지원하지만, 두 형태는 지원하는 기능과 도구 집합이 서로 다르므로 각 워크플로에서는 하나의 모델 형태만 사용하는 것을 권장합니다. 워크플로에서 모델 형태를 혼합해야 한다면, 사용 중인 모든 기능이 양쪽 모두에서 사용 가능한지 확인하세요.

```python
from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel
import asyncio

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
    model="gpt-5-mini", # (1)!
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model=OpenAIChatCompletionsModel( # (2)!
        model="gpt-5-nano",
        openai_client=AsyncOpenAI()
    ),
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    model="gpt-5",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1.  OpenAI 모델 이름을 직접 설정합니다.
2.  [`Model`][agents.models.interface.Model] 구현을 제공합니다.

에이전트에 사용되는 모델을 추가로 구성하려면, temperature 같은 선택적 모델 구성 매개변수를 제공하는 [`ModelSettings`][agents.models.interface.ModelSettings] 를 전달할 수 있습니다.

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

또한 OpenAI Responses API 를 사용할 때, [몇 가지 추가 선택 매개변수](https://platform.openai.com/docs/api-reference/responses/create) (예: `user`, `service_tier` 등)도 있습니다. 최상위 레벨에서 사용할 수 없다면 `extra_args` 를 사용해 함께 전달할 수 있습니다.

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(
        temperature=0.1,
        extra_args={"service_tier": "flex", "user": "user_12345"},
    ),
)
```

## 다른 LLM 제공자 사용 시 흔한 문제

### 트레이싱 클라이언트 오류 401

트레이싱 관련 오류가 발생한다면, 트레이스가 OpenAI 서버로 업로드되는데 OpenAI API 키가 없기 때문입니다. 이를 해결하는 방법은 세 가지입니다:

1. 트레이싱을 완전히 비활성화: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. 트레이싱용 OpenAI 키 설정: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]. 이 API 키는 트레이스 업로드에만 사용되며, 반드시 [platform.openai.com](https://platform.openai.com/) 발급 키여야 합니다.
3. 비 OpenAI 트레이스 프로세서 사용. [트레이싱 문서](../tracing.md#custom-tracing-processors) 를 참고하세요.

### Responses API 지원

SDK 는 기본적으로 Responses API 를 사용하지만, 대부분의 다른 LLM 제공자는 아직 이를 지원하지 않습니다. 그 결과 404 같은 문제가 발생할 수 있습니다. 해결 방법은 두 가지입니다:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] 를 호출합니다. 이는 환경 변수로 `OPENAI_API_KEY` 와 `OPENAI_BASE_URL` 을 설정하는 경우에 동작합니다.
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 을 사용합니다. 예시는 [여기](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) 에 있습니다.

### structured outputs 지원

일부 모델 제공자는 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) 를 지원하지 않습니다. 이 경우 때때로 다음과 비슷한 오류가 발생합니다:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

이는 일부 모델 제공자의 한계입니다. JSON 출력은 지원하지만, 출력에 사용할 `json_schema` 를 지정하는 것을 허용하지 않습니다. 이에 대한 수정 작업을 진행 중이지만, JSON schema 출력을 지원하는 제공자에 의존하는 것을 권장합니다. 그렇지 않으면 잘못된 형식의 JSON 때문에 앱이 자주 깨질 수 있습니다.

## 제공자 간 모델 혼합

모델 제공자 간 기능 차이를 인지해야 하며, 그렇지 않으면 오류가 발생할 수 있습니다. 예를 들어 OpenAI 는 structured outputs, 멀티모달 입력, 호스티드 파일 검색 및 웹 검색을 지원하지만, 많은 다른 제공자는 이러한 기능을 지원하지 않습니다. 다음 제한을 유의하세요:

-   이해하지 못하는 제공자에게 지원되지 않는 `tools` 를 보내지 마세요
-   텍스트 전용 모델을 호출하기 전에 멀티모달 입력을 필터링하세요
-   structured JSON 출력을 지원하지 않는 제공자는 때때로 유효하지 않은 JSON 을 생성할 수 있음을 유의하세요