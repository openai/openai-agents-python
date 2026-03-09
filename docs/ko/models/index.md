---
search:
  exclude: true
---
# 모델

Agents SDK는 OpenAI 모델을 두 가지 방식으로 즉시 사용할 수 있도록 지원합니다

-   **권장**: 새로운 [Responses API](https://platform.openai.com/docs/api-reference/responses)를 사용해 OpenAI API를 호출하는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat)를 사용해 OpenAI API를 호출하는 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## 모델 설정 선택

설정에 따라 다음 순서로 이 페이지를 사용하세요

| 목표 | 시작 위치 |
| --- | --- |
| SDK 기본값으로 OpenAI 호스팅 모델 사용 | [OpenAI 모델](#openai-models) |
| websocket 전송을 통해 OpenAI Responses API 사용 | [Responses WebSocket 전송](#responses-websocket-transport) |
| OpenAI 이외 공급자 사용 | [비 OpenAI 모델](#non-openai-models) |
| 하나의 워크플로에서 모델/공급자 혼합 | [고급 모델 선택 및 혼합](#advanced-model-selection-and-mixing) 및 [공급자 간 모델 혼합](#mixing-models-across-providers) |
| 공급자 호환성 문제 디버깅 | [비 OpenAI 공급자 문제 해결](#troubleshooting-non-openai-providers) |

## OpenAI 모델

`Agent`를 초기화할 때 모델을 지정하지 않으면 기본 모델이 사용됩니다. 현재 기본값은 호환성과 낮은 지연 시간을 위해 [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1)입니다. 접근 권한이 있다면, 명시적인 `model_settings`를 유지하면서 더 높은 품질을 위해 에이전트를 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4)로 설정하는 것을 권장합니다.

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) 같은 다른 모델로 전환하려면 에이전트를 구성하는 방법이 두 가지 있습니다.

### 기본 모델

첫째, 커스텀 모델을 설정하지 않은 모든 에이전트에서 특정 모델을 일관되게 사용하려면, 에이전트를 실행하기 전에 `OPENAI_DEFAULT_MODEL` 환경 변수를 설정하세요.

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

둘째, `RunConfig`를 통해 실행(run) 단위 기본 모델을 설정할 수 있습니다. 에이전트에 모델을 설정하지 않으면 이 실행의 모델이 사용됩니다.

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.4"),
)
```

#### GPT-5 모델

이 방식으로 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) 같은 GPT-5 모델을 사용하면 SDK가 기본 `ModelSettings`를 적용합니다. 대부분의 사용 사례에서 가장 잘 동작하는 값으로 설정됩니다. 기본 모델의 추론 강도를 조정하려면 사용자 정의 `ModelSettings`를 전달하세요

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.4 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.4",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

더 낮은 지연 시간을 위해 `gpt-5.4`와 함께 `reasoning.effort="none"` 사용을 권장합니다. gpt-4.1 계열(미니 및 나노 변형 포함)도 인터랙티브 에이전트 앱 구축에 여전히 좋은 선택입니다.

#### ComputerTool 모델 선택

에이전트에 [`ComputerTool`][agents.tool.ComputerTool]이 포함된 경우, 실제 Responses 요청에서의 유효 모델이 SDK가 전송하는 컴퓨터 도구 페이로드를 결정합니다. 명시적 `gpt-5.4` 요청은 GA 내장 `computer` 도구를 사용하고, 명시적 `computer-use-preview` 요청은 기존 `computer_use_preview` 페이로드를 유지합니다.

주요 예외는 프롬프트 관리 호출입니다. 프롬프트 템플릿이 모델을 소유하고 SDK가 요청에서 `model`을 생략하면, 프롬프트가 어떤 모델에 고정되어 있는지 추측하지 않도록 SDK는 preview 호환 컴퓨터 페이로드를 기본으로 사용합니다. 이 흐름에서 GA 경로를 유지하려면 요청에 `model="gpt-5.4"`를 명시하거나 `ModelSettings(tool_choice="computer")` 또는 `ModelSettings(tool_choice="computer_use")`로 GA 선택기를 강제하세요.

등록된 [`ComputerTool`][agents.tool.ComputerTool]이 있는 경우 `tool_choice="computer"`, `"computer_use"`, `"computer_use_preview"`는 유효 요청 모델과 일치하는 내장 선택기로 정규화됩니다. `ComputerTool`이 등록되지 않은 경우, 이 문자열들은 일반 함수 이름처럼 계속 동작합니다.

preview 호환 요청은 `environment`와 디스플레이 크기를 미리 직렬화해야 하므로, [`ComputerProvider`][agents.tool.ComputerProvider] 팩토리를 사용하는 프롬프트 관리 흐름에서는 구체적인 `Computer` 또는 `AsyncComputer` 인스턴스를 전달하거나 요청 전송 전에 GA 선택기를 강제해야 합니다. 전체 마이그레이션 세부 사항은 [도구](../tools.md#computertool-and-the-responses-computer-tool)를 참조하세요.

#### 비 GPT-5 모델

사용자 정의 `model_settings` 없이 비 GPT-5 모델 이름을 전달하면 SDK는 모든 모델과 호환되는 일반 `ModelSettings`로 되돌아갑니다.

### Responses 전용 도구 검색 기능

다음 도구 기능은 OpenAI Responses 모델에서만 지원됩니다

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 및 기타 지연 로딩 Responses 도구 표면

이 기능들은 Chat Completions 모델과 non-Responses 백엔드에서 거부됩니다. 지연 로딩 도구를 사용할 때는 에이전트에 `ToolSearchTool()`을 추가하고, 네임스페이스 이름만 또는 지연 전용 함수 이름을 강제하는 대신 `auto` 또는 `required` 도구 선택을 통해 모델이 도구를 로드하도록 하세요. 설정 세부 사항과 현재 제약은 [도구](../tools.md#hosted-tool-search)를 참조하세요.

### Responses WebSocket 전송

기본적으로 OpenAI Responses API 요청은 HTTP 전송을 사용합니다. OpenAI 기반 모델을 사용할 때 websocket 전송을 선택할 수 있습니다.

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

이는 기본 OpenAI 공급자가 해석하는 OpenAI Responses 모델(예: `"gpt-5.4"` 같은 문자열 모델 이름 포함)에 영향을 줍니다.

전송 방식 선택은 SDK가 모델 이름을 모델 인스턴스로 해석할 때 발생합니다. 구체적인 [`Model`][agents.models.interface.Model] 객체를 전달하면 해당 전송은 이미 고정되어 있습니다: [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel]은 websocket을 사용하고, [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]은 HTTP를 사용하며, [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]은 Chat Completions에 머뭅니다. `RunConfig(model_provider=...)`를 전달하면 전역 기본값 대신 해당 공급자가 전송 선택을 제어합니다.

공급자별 또는 실행별로 websocket 전송을 구성할 수도 있습니다

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses_websocket=True,
    # Optional; if omitted, OPENAI_WEBSOCKET_BASE_URL is used when set.
    websocket_base_url="wss://your-proxy.example/v1",
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

접두사 기반 모델 라우팅이 필요하다면(예: 하나의 실행에서 `openai/...`와 `litellm/...` 모델 이름 혼합), 대신 [`MultiProvider`][agents.MultiProvider]를 사용하고 거기서 `openai_use_responses_websocket=True`를 설정하세요.

`MultiProvider`는 두 가지 기존 기본값을 유지합니다

-   `openai/...`는 OpenAI 공급자의 별칭으로 처리되므로 `openai/gpt-4.1`은 모델 `gpt-4.1`로 라우팅됩니다
-   알 수 없는 접두사는 그대로 전달되지 않고 `UserError`를 발생시킵니다

리터럴 네임스페이스 모델 ID를 기대하는 OpenAI 호환 엔드포인트에 OpenAI 공급자를 연결하는 경우, 명시적으로 pass-through 동작을 활성화하세요. websocket이 활성화된 설정에서는 `MultiProvider`에서도 `openai_use_responses_websocket=True`를 유지하세요

```python
from agents import Agent, MultiProvider, RunConfig, Runner

provider = MultiProvider(
    openai_base_url="https://openrouter.ai/api/v1",
    openai_api_key="...",
    openai_use_responses_websocket=True,
    openai_prefix_mode="model_id",
    unknown_prefix_mode="model_id",
)

agent = Agent(
    name="Assistant",
    instructions="Be concise.",
    model="openai/gpt-4.1",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

백엔드가 리터럴 `openai/...` 문자열을 기대하면 `openai_prefix_mode="model_id"`를 사용하세요. 백엔드가 `openrouter/openai/gpt-4.1-mini` 같은 다른 네임스페이스 모델 ID를 기대하면 `unknown_prefix_mode="model_id"`를 사용하세요. 이 옵션들은 websocket 전송 외부의 `MultiProvider`에서도 동작합니다. 이 예제는 이 섹션에서 설명한 전송 설정의 일부이기 때문에 websocket을 활성화한 상태를 유지합니다. 동일한 옵션은 [`responses_websocket_session()`][agents.responses_websocket_session]에서도 사용할 수 있습니다.

커스텀 OpenAI 호환 엔드포인트나 프록시를 사용하는 경우, websocket 전송에도 호환되는 websocket `/responses` 엔드포인트가 필요합니다. 이러한 설정에서는 `websocket_base_url`을 명시적으로 설정해야 할 수 있습니다.

참고

-   이것은 websocket 전송을 통한 Responses API이며, [Realtime API](../realtime/guide.md)가 아닙니다. Chat Completions 또는 Responses websocket `/responses` 엔드포인트를 지원하지 않는 비 OpenAI 공급자에는 적용되지 않습니다
-   환경에 아직 없다면 `websockets` 패키지를 설치하세요
-   websocket 전송 활성화 후 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]를 바로 사용할 수 있습니다. 여러 턴 워크플로에서 동일한 websocket 연결을 턴 간(및 중첩된 agent-as-tool 호출 간) 재사용하려면 [`responses_websocket_session()`][agents.responses_websocket_session] 헬퍼를 권장합니다. [에이전트 실행](../running_agents.md) 가이드와 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)를 참조하세요

## 비 OpenAI 모델

대부분의 다른 비 OpenAI 모델은 [LiteLLM 통합](./litellm.md)을 통해 사용할 수 있습니다. 먼저 litellm 의존성 그룹을 설치하세요

```bash
pip install "openai-agents[litellm]"
```

그런 다음 `litellm/` 접두사로 [지원되는 모델](https://docs.litellm.ai/docs/providers)을 사용할 수 있습니다

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### 비 OpenAI 모델을 사용하는 다른 방법

다른 LLM 공급자는 3가지 방법으로 추가 통합할 수 있습니다(예제는 [여기](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/))

1. [`set_default_openai_client`][agents.set_default_openai_client]는 `AsyncOpenAI` 인스턴스를 LLM 클라이언트로 전역 사용하려는 경우에 유용합니다. LLM 공급자가 OpenAI 호환 API 엔드포인트를 제공하고 `base_url` 및 `api_key`를 설정할 수 있는 경우를 위한 방식입니다. 구성 가능한 예제는 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)를 참조하세요
2. [`ModelProvider`][agents.models.interface.ModelProvider]는 `Runner.run` 수준에서 적용됩니다. 이를 통해 "이 실행의 모든 에이전트에 커스텀 모델 공급자를 사용"하도록 지정할 수 있습니다. 구성 가능한 예제는 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)를 참조하세요
3. [`Agent.model`][agents.agent.Agent.model]을 사용하면 특정 Agent 인스턴스에서 모델을 지정할 수 있습니다. 이를 통해 서로 다른 에이전트에 서로 다른 공급자를 혼합할 수 있습니다. 구성 가능한 예제는 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)를 참조하세요. 대부분의 사용 가능한 모델을 쉽게 사용하는 방법은 [LiteLLM 통합](./litellm.md)을 이용하는 것입니다

`platform.openai.com`의 API 키가 없는 경우에는 `set_tracing_disabled()`로 트레이싱을 비활성화하거나 [다른 트레이싱 프로세서](../tracing.md)를 설정하는 것을 권장합니다.

!!! note

    이 예제들에서는 대부분의 LLM 공급자가 아직 Responses API를 지원하지 않기 때문에 Chat Completions API/모델을 사용합니다. LLM 공급자가 이를 지원한다면 Responses 사용을 권장합니다

## 고급 모델 선택 및 혼합

하나의 워크플로 내에서 각 에이전트에 서로 다른 모델을 사용하고 싶을 수 있습니다. 예를 들어 분류에는 더 작고 빠른 모델을 쓰고, 복잡한 작업에는 더 크고 성능이 높은 모델을 사용할 수 있습니다. [`Agent`][agents.Agent]를 구성할 때는 다음 중 하나로 특정 모델을 선택할 수 있습니다

1. 모델 이름 전달
2. 임의의 모델 이름 + 해당 이름을 Model 인스턴스로 매핑할 수 있는 [`ModelProvider`][agents.models.interface.ModelProvider] 전달
3. [`Model`][agents.models.interface.Model] 구현을 직접 제공

!!!note

    SDK는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]과 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 형태를 모두 지원하지만, 두 형태는 지원 기능과 도구 집합이 다르므로 워크플로마다 단일 모델 형태를 사용하는 것을 권장합니다. 워크플로에서 모델 형태를 혼합해야 한다면 사용 중인 모든 기능이 양쪽 모두에서 사용 가능한지 확인하세요

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
    model="gpt-5.4",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1.  OpenAI 모델 이름을 직접 설정합니다
2.  [`Model`][agents.models.interface.Model] 구현을 제공합니다

에이전트에서 사용할 모델을 더 세부적으로 구성하려면 temperature 같은 선택적 모델 구성 매개변수를 제공하는 [`ModelSettings`][agents.models.interface.ModelSettings]를 전달할 수 있습니다.

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

#### 일반적인 고급 `ModelSettings` 옵션

OpenAI Responses API를 사용할 때는 여러 요청 필드가 이미 직접적인 `ModelSettings` 필드를 가지므로, 이를 위해 `extra_args`를 사용할 필요가 없습니다.

| 필드 | 용도 |
| --- | --- |
| `parallel_tool_calls` | 같은 턴에서 여러 도구 호출을 허용하거나 금지 |
| `truncation` | 컨텍스트가 넘칠 때 실패하는 대신 Responses API가 가장 오래된 대화 항목을 삭제하도록 `"auto"` 설정 |
| `prompt_cache_retention` | 예를 들어 `"24h"`로 캐시된 프롬프트 접두사를 더 오래 유지 |
| `response_include` | `web_search_call.action.sources`, `file_search_call.results`, `reasoning.encrypted_content` 같은 더 풍부한 응답 페이로드 요청 |
| `top_logprobs` | 출력 텍스트의 상위 토큰 logprobs 요청. SDK는 `message.output_text.logprobs`도 자동 추가 |

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.4",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

SDK가 아직 최상위로 직접 노출하지 않은 공급자별 또는 최신 요청 필드가 필요하면 `extra_args`를 사용하세요.

또한 OpenAI의 Responses API를 사용할 때 [다른 선택적 매개변수](https://platform.openai.com/docs/api-reference/responses/create)(예: `user`, `service_tier` 등)가 몇 가지 있습니다. 최상위에서 사용할 수 없다면 `extra_args`로 함께 전달할 수 있습니다.

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

## 비 OpenAI 공급자 문제 해결

### 트레이싱 클라이언트 오류 401

트레이싱 관련 오류가 발생한다면, 트레이스가 OpenAI 서버로 업로드되는데 OpenAI API 키가 없기 때문입니다. 해결 방법은 세 가지입니다

1. 트레이싱 완전 비활성화: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. 트레이싱용 OpenAI 키 설정: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]. 이 API 키는 트레이스 업로드에만 사용되며 [platform.openai.com](https://platform.openai.com/) 키여야 합니다
3. 비 OpenAI 트레이스 프로세서 사용. [트레이싱 문서](../tracing.md#custom-tracing-processors) 참조

### Responses API 지원

SDK는 기본적으로 Responses API를 사용하지만, 대부분의 다른 LLM 공급자는 아직 이를 지원하지 않습니다. 그 결과 404 또는 유사한 문제가 발생할 수 있습니다. 해결 옵션은 두 가지입니다

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] 호출. 환경 변수로 `OPENAI_API_KEY`와 `OPENAI_BASE_URL`을 설정하는 경우 동작합니다
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 사용. 예제는 [여기](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)에 있습니다

### structured outputs 지원

일부 모델 공급자는 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)를 지원하지 않습니다. 이 경우 때때로 다음과 같은 오류가 발생합니다

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

이는 일부 모델 공급자의 한계입니다. JSON 출력은 지원하지만 출력에 사용할 `json_schema`를 지정할 수는 없습니다. 이 문제는 수정 중이지만, 그렇지 않으면 앱이 잘못된 JSON 때문에 자주 깨질 수 있으므로 JSON schema 출력을 지원하는 공급자를 사용하는 것을 권장합니다.

## 공급자 간 모델 혼합

모델 공급자 간 기능 차이를 인지해야 하며, 그렇지 않으면 오류가 발생할 수 있습니다. 예를 들어 OpenAI는 structured outputs, 멀티모달 입력, 호스티드 file search 및 웹 검색을 지원하지만 많은 다른 공급자는 이러한 기능을 지원하지 않습니다. 다음 제한 사항에 유의하세요

-   지원하지 않는 공급자에 이해할 수 없는 `tools`를 보내지 마세요
-   텍스트 전용 모델을 호출하기 전에 멀티모달 입력을 필터링하세요
-   structured JSON 출력을 지원하지 않는 공급자는 때때로 유효하지 않은 JSON을 생성할 수 있다는 점에 유의하세요