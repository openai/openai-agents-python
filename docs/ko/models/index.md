---
search:
  exclude: true
---
# 모델

Agents SDK는 OpenAI 모델을 다음 두 가지 방식으로 기본 지원합니다:

-   **추천**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] — 새로운 [Responses API](https://platform.openai.com/docs/api-reference/responses)를 사용해 OpenAI API를 호출합니다
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] — [Chat Completions API](https://platform.openai.com/docs/api-reference/chat)를 사용해 OpenAI API를 호출합니다

## OpenAI 모델

`Agent`를 초기화할 때 모델을 지정하지 않으면 기본 모델이 사용됩니다. 현재 기본값은 [`gpt-4.1`](https://platform.openai.com/docs/models/gpt-4.1)이며, 에이전트 워크플로에 적합한 예측 가능성과 낮은 지연 시간의 균형이 우수합니다.

[`gpt-5`](https://platform.openai.com/docs/models/gpt-5) 같은 다른 모델로 전환하려면 다음 섹션의 단계를 따르세요.

### 기본 OpenAI 모델

사용자 지정 모델을 설정하지 않은 모든 에이전트에 대해 특정 모델을 일관되게 사용하려면, 에이전트를 실행하기 전에 `OPENAI_DEFAULT_MODEL` 환경 변수를 설정하세요.

```bash
export OPENAI_DEFAULT_MODEL=gpt-5
python3 my_awesome_agent.py
```

#### GPT-5 모델

GPT-5의 reasoning 모델([`gpt-5`](https://platform.openai.com/docs/models/gpt-5), [`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini), 또는 [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano))을 이 방식으로 사용하면, SDK가 기본적으로 합리적인 `ModelSettings`를 적용합니다. 구체적으로 `reasoning.effort`와 `verbosity`를 모두 `"low"`로 설정합니다. 이러한 설정을 직접 구성하려면 `agents.models.get_default_model_settings("gpt-5")`를 호출하세요.

지연 시간을 더 낮추거나 특정 요구 사항이 있는 경우, 다른 모델과 설정을 선택할 수 있습니다. 기본 모델의 reasoning effort를 조정하려면, 사용자 지정 `ModelSettings`를 전달하세요:

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    model_settings=ModelSettings(reasoning=Reasoning(effort="minimal"), verbosity="low")
    # If OPENAI_DEFAULT_MODEL=gpt-5 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    # model="gpt-5",
)
```

특히 낮은 지연 시간을 위해서는 [`gpt-5-mini`](https://platform.openai.com/docs/models/gpt-5-mini) 또는 [`gpt-5-nano`](https://platform.openai.com/docs/models/gpt-5-nano) 모델에 `reasoning.effort="minimal"`을 사용하면 기본 설정보다 더 빠르게 응답하는 경우가 많습니다. 다만 Responses API의 일부 내장 도구(예: 파일 검색과 이미지 생성)는 `"minimal"` reasoning effort를 지원하지 않으므로, 본 Agents SDK는 기본값을 `"low"`로 설정합니다.

#### 비 GPT-5 모델

사용자 지정 `model_settings` 없이 비 GPT-5 모델 이름을 전달하는 경우, SDK는 모든 모델과 호환되는 일반적인 `ModelSettings`로 되돌립니다.

## 비 OpenAI 모델

대부분의 다른 비 OpenAI 모델은 [LiteLLM 통합](./litellm.md)을 통해 사용할 수 있습니다. 먼저 litellm 의존성 그룹을 설치하세요:

```bash
pip install "openai-agents[litellm]"
```

그런 다음 `litellm/` 접두사와 함께 [지원되는 모델](https://docs.litellm.ai/docs/providers) 중 아무 것이나 사용하세요:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 비 OpenAI 모델을 사용하는 다른 방법

다른 LLM 제공자를 통합하는 방법은 추가로 3가지가 있습니다(예시는 [여기](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)에 있습니다):

1. [`set_default_openai_client`][agents.set_default_openai_client]는 전역적으로 `AsyncOpenAI` 인스턴스를 LLM 클라이언트로 사용하려는 경우에 유용합니다. 이는 LLM 제공자가 OpenAI 호환 API 엔드포인트를 제공하고, `base_url`과 `api_key`를 설정할 수 있을 때 해당됩니다. 구성 가능한 예시는 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)를 참고하세요.
2. [`ModelProvider`][agents.models.interface.ModelProvider]는 `Runner.run` 수준에서 사용합니다. 이를 통해 "이 실행(run)의 모든 에이전트에 대해 사용자 지정 모델 제공자를 사용"할 수 있습니다. 구성 가능한 예시는 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)를 참고하세요.
3. [`Agent.model`][agents.agent.Agent.model]을 사용하면 특정 Agent 인스턴스에 대해 모델을 지정할 수 있습니다. 이를 통해 서로 다른 에이전트에 대해 서로 다른 제공자를 혼합하여 사용할 수 있습니다. 구성 가능한 예시는 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)를 참고하세요. 대부분의 사용 가능한 모델을 손쉽게 사용하는 방법은 [LiteLLM 통합](./litellm.md)을 활용하는 것입니다.

`platform.openai.com`의 API 키가 없는 경우, `set_tracing_disabled()`로 트레이싱을 비활성화하거나, [다른 트레이싱 프로세서](../tracing.md)를 설정하는 것을 권장합니다.

!!! note

    이 예시들에서는 대부분의 LLM 제공자가 아직 Responses API를 지원하지 않기 때문에 Chat Completions API/모델을 사용합니다. 사용 중인 LLM 제공자가 이를 지원한다면 Responses 사용을 권장합니다.

## 모델 혼합 사용

하나의 워크플로 내에서 에이전트마다 다른 모델을 사용하고 싶을 수 있습니다. 예를 들어, 분류(트리아지)에는 더 작고 빠른 모델을, 복잡한 작업에는 더 크고 성능이 높은 모델을 사용할 수 있습니다. [`Agent`][agents.Agent]를 구성할 때 다음 중 하나의 방법으로 특정 모델을 선택할 수 있습니다:

1. 모델 이름을 전달
2. 모델 이름과 해당 이름을 Model 인스턴스로 매핑할 수 있는 [`ModelProvider`][agents.models.interface.ModelProvider]를 함께 전달
3. [`Model`][agents.models.interface.Model] 구현체를 직접 제공

!!!note

    우리 SDK는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]과 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 두 가지 모델 형태를 모두 지원하지만, 각 워크플로에 하나의 모델 형태만 사용하는 것을 권장합니다. 두 형태는 지원하는 기능과 도구 집합이 다릅니다. 워크플로가 서로 다른 모델 형태의 혼합을 요구한다면, 사용하는 모든 기능이 두 형태 모두에서 제공되는지 확인하세요.

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

1.  OpenAI 모델의 이름을 직접 설정합니다
2.  [`Model`][agents.models.interface.Model] 구현체를 제공합니다

에이전트에 사용되는 모델을 더 세부적으로 구성하려면, temperature 같은 선택적 모델 구성 매개변수를 제공하는 [`ModelSettings`][agents.models.interface.ModelSettings]를 전달할 수 있습니다.

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

또한 OpenAI의 Responses API를 사용할 때는 [몇 가지 다른 선택적 매개변수](https://platform.openai.com/docs/api-reference/responses/create)(예: `user`, `service_tier` 등)가 있습니다. 최상위에서 제공되지 않는 경우, `extra_args`를 사용해 함께 전달할 수 있습니다.

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

## 다른 LLM 제공자 사용 시 일반적인 문제

### Tracing 클라이언트 오류 401

트레이싱 관련 오류가 발생한다면, 이는 트레이스가 OpenAI 서버로 업로드되는데 OpenAI API 키가 없기 때문입니다. 해결 방법은 다음 세 가지입니다:

1. 트레이싱을 완전히 비활성화: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. 트레이싱을 위한 OpenAI 키 설정: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]. 이 API 키는 트레이스 업로드에만 사용되며, [platform.openai.com](https://platform.openai.com/)의 키여야 합니다
3. 비 OpenAI 트레이스 프로세서를 사용. [트레이싱 문서](../tracing.md#custom-tracing-processors)를 참고하세요

### Responses API 지원

SDK는 기본적으로 Responses API를 사용하지만, 대부분의 다른 LLM 제공자는 아직 이를 지원하지 않습니다. 이로 인해 404 같은 문제가 발생할 수 있습니다. 해결하려면 다음 두 가지 중 하나를 사용하세요:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]를 호출하세요. 이는 환경 변수로 `OPENAI_API_KEY`와 `OPENAI_BASE_URL`을 설정하는 경우에 동작합니다
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]을 사용하세요. 예시는 [여기](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)에 있습니다

### structured outputs 지원

일부 모델 제공자는 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)을 지원하지 않습니다. 이로 인해 다음과 유사한 오류가 발생할 수 있습니다:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

이는 일부 모델 제공자의 한계로, JSON 출력을 지원하더라도 출력에 사용할 `json_schema`를 지정하도록 허용하지 않습니다. 이에 대한 해결책을 마련 중이지만, JSON 스키마 출력을 지원하는 제공자를 사용하는 것을 권장합니다. 그렇지 않으면 잘못된 JSON 때문에 앱이 자주 실패할 수 있습니다.

## 제공자 간 모델 혼합

모델 제공자 간 기능 차이를 인지하지 못하면 오류가 발생할 수 있습니다. 예를 들어 OpenAI는 structured outputs, 멀티모달 입력, 호스팅되는 파일 검색과 웹 검색을 지원하지만, 많은 다른 제공자들은 이러한 기능을 지원하지 않습니다. 다음 제한 사항에 유의하세요:

-   지원되지 않는 `tools`를 이해하지 못하는 제공자에게 보내지 않기
-   텍스트 전용 모델을 호출하기 전에 멀티모달 입력을 필터링하기
-   structured JSON 출력을 지원하지 않는 제공자는 때때로 잘못된 JSON을 생성할 수 있음을 인지하기