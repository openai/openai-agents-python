---
search:
  exclude: true
---
# 모델

Agents SDK는 OpenAI 모델을 기본 지원하며, 두 가지 방식이 있습니다:

-   **권장**: [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] — 새로운 [Responses API](https://platform.openai.com/docs/api-reference/responses)를 사용해 OpenAI API를 호출합니다
-   [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] — [Chat Completions API](https://platform.openai.com/docs/api-reference/chat)를 사용해 OpenAI API를 호출합니다

## 모델 설정 선택

설정에 따라 이 페이지를 다음 순서로 사용하세요:

| 목표 | 시작 위치 |
| --- | --- |
| SDK 기본값으로 OpenAI 호스팅 모델 사용 | [OpenAI 모델](#openai-models) |
| websocket 전송으로 OpenAI Responses API 사용 | [Responses WebSocket 전송](#responses-websocket-transport) |
| OpenAI 이외 제공자 사용 | [OpenAI 이외 모델](#non-openai-models) |
| 하나의 워크플로우에서 모델/제공자 혼합 | [고급 모델 선택 및 혼합](#advanced-model-selection-and-mixing) 및 [제공자 간 모델 혼합](#mixing-models-across-providers) |
| 제공자 호환성 문제 디버깅 | [OpenAI 이외 제공자 문제 해결](#troubleshooting-non-openai-providers) |

## OpenAI 모델

`Agent`를 초기화할 때 모델을 지정하지 않으면 기본 모델이 사용됩니다. 현재 기본값은 호환성과 낮은 지연 시간을 위해 [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1)입니다. 접근 권한이 있다면, 명시적인 `model_settings`를 유지하면서 더 높은 품질을 위해 에이전트를 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4)로 설정하는 것을 권장합니다.

[`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) 같은 다른 모델로 전환하려면, 에이전트를 구성하는 방법은 두 가지입니다.

### 기본 모델

첫째, 사용자 지정 모델을 설정하지 않은 모든 에이전트에서 특정 모델을 일관되게 사용하려면, 에이전트 실행 전에 `OPENAI_DEFAULT_MODEL` 환경 변수를 설정하세요.

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
python3 my_awesome_agent.py
```

둘째, `RunConfig`를 통해 실행 단위 기본 모델을 설정할 수 있습니다. 에이전트에 모델을 설정하지 않으면 이 실행의 모델이 사용됩니다.

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

이 방식으로 [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) 같은 GPT-5 모델을 사용하면, SDK가 기본 `ModelSettings`를 적용합니다. 대부분의 사용 사례에서 가장 잘 동작하는 값으로 설정됩니다. 기본 모델의 추론 강도를 조정하려면 사용자 지정 `ModelSettings`를 전달하세요:

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

더 낮은 지연 시간을 위해 `gpt-5.4`에서 `reasoning.effort="none"` 사용을 권장합니다. gpt-4.1 계열(미니 및 나노 변형 포함)도 대화형 에이전트 앱 구축에 여전히 좋은 선택입니다.

#### ComputerTool 모델 선택

에이전트에 [`ComputerTool`][agents.tool.ComputerTool]이 포함되어 있으면, 실제 Responses 요청에서의 유효 모델이 SDK가 전송할 컴퓨터 도구 페이로드를 결정합니다. 명시적 `gpt-5.4` 요청은 GA 내장 `computer` 도구를 사용하고, 명시적 `computer-use-preview` 요청은 기존 `computer_use_preview` 페이로드를 유지합니다.

주요 예외는 프롬프트 관리 호출입니다. 프롬프트 템플릿이 모델을 소유하고 SDK가 요청에서 `model`을 생략하면, SDK는 프롬프트가 어떤 모델에 고정되어 있는지 추측하지 않기 위해 preview 호환 컴퓨터 페이로드를 기본으로 사용합니다. 이 흐름에서 GA 경로를 유지하려면, 요청에 `model="gpt-5.4"`를 명시하거나 `ModelSettings(tool_choice="computer")` 또는 `ModelSettings(tool_choice="computer_use")`로 GA 선택기를 강제하세요.

등록된 [`ComputerTool`][agents.tool.ComputerTool]이 있는 경우 `tool_choice="computer"`, `"computer_use"`, `"computer_use_preview"`는 유효 요청 모델과 일치하는 내장 선택기로 정규화됩니다. `ComputerTool`이 등록되어 있지 않으면 이 문자열들은 일반 함수 이름처럼 계속 동작합니다.

preview 호환 요청은 `environment`와 디스플레이 크기를 사전에 직렬화해야 하므로, [`ComputerProvider`][agents.tool.ComputerProvider] 팩토리를 사용하는 프롬프트 관리 흐름에서는 구체적인 `Computer` 또는 `AsyncComputer` 인스턴스를 전달하거나 요청 전 GA 선택기를 강제해야 합니다. 전체 마이그레이션 세부 내용은 [도구](../tools.md#computertool-and-the-responses-computer-tool)를 참조하세요.

#### GPT-5 이외 모델

사용자 지정 `model_settings` 없이 GPT-5 이외 모델 이름을 전달하면 SDK는 모든 모델과 호환되는 일반 `ModelSettings`로 되돌아갑니다.

### Responses 전용 도구 검색 기능

다음 도구 기능은 OpenAI Responses 모델에서만 지원됩니다:

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 및 기타 지연 로딩 Responses 도구 표면

이 기능들은 Chat Completions 모델과 non-Responses 백엔드에서 거부됩니다. 지연 로딩 도구를 사용할 때는 에이전트에 `ToolSearchTool()`을 추가하고, 이름공간 이름 또는 지연 전용 함수 이름을 강제하지 말고 모델이 `auto` 또는 `required` 도구 선택을 통해 도구를 로드하도록 하세요. 설정 세부사항과 현재 제약은 [도구](../tools.md#hosted-tool-search)를 참조하세요.

### Responses WebSocket 전송

기본적으로 OpenAI Responses API 요청은 HTTP 전송을 사용합니다. OpenAI 기반 모델 사용 시 websocket 전송을 활성화할 수 있습니다.

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

이는 기본 OpenAI 제공자가 해석한 OpenAI Responses 모델(예: `"gpt-5.4"` 같은 문자열 모델 이름 포함)에 영향을 줍니다.

전송 방식 선택은 SDK가 모델 이름을 모델 인스턴스로 해석할 때 이루어집니다. 구체적인 [`Model`][agents.models.interface.Model] 객체를 전달하면 해당 전송은 이미 고정됩니다: [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel]은 websocket, [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]은 HTTP, [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]은 Chat Completions를 사용합니다. `RunConfig(model_provider=...)`를 전달하면 전역 기본값 대신 해당 제공자가 전송 선택을 제어합니다.

제공자별 또는 실행별 websocket 전송 설정도 가능합니다:

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

접두사 기반 모델 라우팅이 필요하면(예: 한 번의 실행에서 `openai/...`와 `litellm/...` 모델 이름 혼합), 대신 [`MultiProvider`][agents.MultiProvider]를 사용하고 `openai_use_responses_websocket=True`를 설정하세요.

`MultiProvider`는 두 가지 기존 기본값을 유지합니다:

-   `openai/...`는 OpenAI 제공자의 별칭으로 처리되므로, `openai/gpt-4.1`은 모델 `gpt-4.1`로 라우팅됩니다
-   알 수 없는 접두사는 그대로 전달되지 않고 `UserError`를 발생시킵니다

OpenAI 제공자를 리터럴 네임스페이스 모델 ID를 기대하는 OpenAI 호환 엔드포인트로 지정하는 경우, 명시적으로 pass-through 동작을 활성화하세요. websocket 활성 구성에서는 `MultiProvider`에도 `openai_use_responses_websocket=True`를 유지하세요:

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

백엔드가 리터럴 `openai/...` 문자열을 기대하면 `openai_prefix_mode="model_id"`를 사용하세요. 백엔드가 `openrouter/openai/gpt-4.1-mini` 같은 다른 네임스페이스 모델 ID를 기대하면 `unknown_prefix_mode="model_id"`를 사용하세요. 이 옵션들은 websocket 전송 외부의 `MultiProvider`에서도 동작합니다. 이 예시는 본 섹션의 전송 설정 일부이므로 websocket을 유지합니다. 동일한 옵션은 [`responses_websocket_session()`][agents.responses_websocket_session]에서도 사용 가능합니다.

사용자 지정 OpenAI 호환 엔드포인트 또는 프록시를 사용하는 경우, websocket 전송에는 호환되는 websocket `/responses` 엔드포인트도 필요합니다. 이러한 구성에서는 `websocket_base_url`을 명시적으로 설정해야 할 수 있습니다.

참고:

-   이는 websocket 전송 위의 Responses API이며, [Realtime API](../realtime/guide.md)가 아닙니다. Chat Completions 또는 Responses websocket `/responses` 엔드포인트를 지원하지 않는 OpenAI 이외 제공자에는 적용되지 않습니다
-   환경에 `websockets` 패키지가 없으면 설치하세요
-   websocket 전송 활성화 후 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]를 직접 사용할 수 있습니다. 여러 턴 워크플로우에서 턴 간(및 중첩된 agent-as-tool 호출 간) 동일한 websocket 연결을 재사용하려면 [`responses_websocket_session()`][agents.responses_websocket_session] 헬퍼를 권장합니다. [에이전트 실행](../running_agents.md) 가이드와 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)를 참조하세요

## OpenAI 이외 모델

대부분의 OpenAI 이외 모델은 [LiteLLM 통합](./litellm.md)을 통해 사용할 수 있습니다. 먼저 litellm 의존성 그룹을 설치하세요:

```bash
pip install "openai-agents[litellm]"
```

그다음 `litellm/` 접두사와 함께 [지원되는 모델](https://docs.litellm.ai/docs/providers)을 사용하세요:

```python
claude_agent = Agent(model="litellm/anthropic/claude-3-5-sonnet-20240620", ...)
gemini_agent = Agent(model="litellm/gemini/gemini-2.5-flash-preview-04-17", ...)
```

### OpenAI 이외 모델을 사용하는 다른 방법

다음 3가지 방식으로 다른 LLM 제공자를 통합할 수 있습니다(예제는 [여기](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)):

1. [`set_default_openai_client`][agents.set_default_openai_client]는 LLM 클라이언트로 `AsyncOpenAI` 인스턴스를 전역 사용하려는 경우 유용합니다. LLM 제공자가 OpenAI 호환 API 엔드포인트를 제공하고 `base_url` 및 `api_key`를 설정할 수 있을 때 사용합니다. 구성 가능한 예제는 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)를 참조하세요
2. [`ModelProvider`][agents.models.interface.ModelProvider]는 `Runner.run` 수준에서 사용됩니다. 이를 통해 "이번 실행의 모든 에이전트에 사용자 지정 모델 제공자를 사용"하도록 설정할 수 있습니다. 구성 가능한 예제는 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)를 참조하세요
3. [`Agent.model`][agents.agent.Agent.model]을 사용하면 특정 Agent 인스턴스에 모델을 지정할 수 있습니다. 이를 통해 에이전트별로 서로 다른 제공자를 혼합해 사용할 수 있습니다. 구성 가능한 예제는 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)를 참조하세요. 사용 가능한 대부분의 모델을 쉽게 사용하는 방법은 [LiteLLM 통합](./litellm.md)입니다

`platform.openai.com`의 API 키가 없는 경우에는 `set_tracing_disabled()`로 트레이싱을 비활성화하거나, [다른 트레이싱 프로세서](../tracing.md)를 설정하는 것을 권장합니다.

!!! note

    이 예제들에서는 대부분의 LLM 제공자가 아직 Responses API를 지원하지 않기 때문에 Chat Completions API/모델을 사용합니다. LLM 제공자가 이를 지원한다면 Responses 사용을 권장합니다

## 고급 모델 선택 및 혼합

하나의 워크플로우 내에서 에이전트별로 다른 모델을 사용하고 싶을 수 있습니다. 예를 들어, 분류에는 더 작고 빠른 모델을 사용하고 복잡한 작업에는 더 크고 성능이 높은 모델을 사용할 수 있습니다. [`Agent`][agents.Agent]를 구성할 때 특정 모델을 선택하는 방법은 다음과 같습니다:

1. 모델 이름 전달
2. 임의의 모델 이름 + 해당 이름을 Model 인스턴스로 매핑할 수 있는 [`ModelProvider`][agents.models.interface.ModelProvider] 전달
3. [`Model`][agents.models.interface.Model] 구현을 직접 전달

!!!note

    SDK는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]과 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 형태를 모두 지원하지만, 두 형태는 지원 기능과 도구 집합이 다르므로 워크플로우별로 하나의 모델 형태를 사용하는 것을 권장합니다. 워크플로우에서 모델 형태를 혼합해야 한다면, 사용하는 모든 기능이 양쪽에서 모두 사용 가능한지 확인하세요

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

에이전트에 사용할 모델을 더 세부적으로 구성하려면 temperature 같은 선택적 모델 구성 매개변수를 제공하는 [`ModelSettings`][agents.models.interface.ModelSettings]를 전달할 수 있습니다.

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

OpenAI Responses API를 사용할 때는 여러 요청 필드가 이미 `ModelSettings`에 직접 매핑되어 있어 `extra_args`가 필요하지 않습니다.

| 필드 | 용도 |
| --- | --- |
| `parallel_tool_calls` | 같은 턴에서 여러 도구 호출을 허용하거나 금지합니다 |
| `truncation` | 컨텍스트 초과 시 실패 대신 가장 오래된 대화 항목을 삭제하도록 Responses API에 `"auto"`를 설정합니다 |
| `store` | 생성된 응답을 이후 조회를 위해 서버 측에 저장할지 제어합니다. 이는 응답 ID에 의존하는 후속 워크플로우와, `store=False`일 때 로컬 입력으로 폴백해야 할 수 있는 세션 압축 흐름에 중요합니다 |
| `prompt_cache_retention` | 예: `"24h"`처럼 캐시된 프롬프트 접두사를 더 오래 유지합니다 |
| `response_include` | `web_search_call.action.sources`, `file_search_call.results`, `reasoning.encrypted_content` 같은 더 풍부한 응답 페이로드를 요청합니다 |
| `top_logprobs` | 출력 텍스트에 대한 상위 토큰 logprobs를 요청합니다. SDK는 `message.output_text.logprobs`도 자동으로 추가합니다 |
| `retry` | 모델 호출에 대해 러너 관리 재시도 설정을 활성화합니다. [Runner 관리 재시도](#runner-managed-retries)를 참조하세요 |

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.4",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

`store=False`를 설정하면 Responses API는 해당 응답을 이후 서버 측 조회용으로 보관하지 않습니다. 이는 무상태 또는 제로 데이터 보존 스타일 흐름에 유용하지만, 일반적으로 응답 ID를 재사용하는 기능은 대신 로컬 관리 상태에 의존해야 함을 의미합니다. 예를 들어 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession]은 마지막 응답이 저장되지 않았을 때 기본 `"auto"` 압축 경로를 입력 기반 압축으로 전환합니다. [세션 가이드](../sessions/index.md#openai-responses-compaction-sessions)를 참조하세요.

#### Runner 관리 재시도

재시도는 런타임 전용이며 opt-in입니다. SDK는 `ModelSettings(retry=...)`를 설정하고 재시도 정책이 재시도를 선택한 경우에만 일반 모델 요청을 재시도합니다.

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.4",
    model_settings=ModelSettings(
        retry=ModelRetrySettings(
            max_retries=4,
            backoff={
                "initial_delay": 0.5,
                "max_delay": 5.0,
                "multiplier": 2.0,
                "jitter": True,
            },
            policy=retry_policies.any(
                retry_policies.provider_suggested(),
                retry_policies.retry_after(),
                retry_policies.network_error(),
                retry_policies.http_status([408, 409, 429, 500, 502, 503, 504]),
            ),
        )
    ),
)
```

`ModelRetrySettings`에는 세 필드가 있습니다:

| 필드 | 타입 | 참고 |
| --- | --- | --- |
| `max_retries` | `int \| None` | 초기 요청 이후 허용되는 재시도 횟수 |
| `backoff` | `ModelRetryBackoffSettings \| dict \| None` | 정책이 명시적 지연 없이 재시도할 때 사용하는 기본 지연 전략 |
| `policy` | `RetryPolicy \| None` | 재시도 여부를 결정하는 콜백입니다. 이 필드는 런타임 전용이며 직렬화되지 않습니다 |

재시도 정책은 [`RetryPolicyContext`][agents.retry.RetryPolicyContext]를 입력받으며 다음을 포함합니다:

- `attempt`와 `max_retries`로 시도 횟수 인지형 판단 가능
- `stream`으로 스트리밍/비스트리밍 분기 가능
- 원문 검사용 `error`
- `status_code`, `retry_after`, `error_code`, `is_network_error`, `is_timeout`, `is_abort` 같은 정규화 정보 `normalized`
- 기본 모델 어댑터가 재시도 가이드를 제공할 수 있을 때의 `provider_advice`

정책은 다음 중 하나를 반환할 수 있습니다:

- 단순 재시도 결정용 `True` / `False`
- 지연 시간을 재정의하거나 진단 사유를 첨부하려는 경우 [`RetryDecision`][agents.retry.RetryDecision]

SDK는 `retry_policies`에 즉시 사용할 수 있는 헬퍼를 제공합니다:

| 헬퍼 | 동작 |
| --- | --- |
| `retry_policies.never()` | 항상 비활성화합니다 |
| `retry_policies.provider_suggested()` | 가능한 경우 제공자 재시도 권고를 따릅니다 |
| `retry_policies.network_error()` | 일시적 전송/타임아웃 실패와 일치합니다 |
| `retry_policies.http_status([...])` | 선택한 HTTP 상태 코드와 일치합니다 |
| `retry_policies.retry_after()` | retry-after 힌트가 있을 때만 해당 지연으로 재시도합니다 |
| `retry_policies.any(...)` | 중첩 정책 중 하나라도 활성화하면 재시도합니다 |
| `retry_policies.all(...)` | 중첩 정책 모두 활성화해야 재시도합니다 |

정책을 조합할 때 `provider_suggested()`는 제공자가 구분 가능한 경우 제공자 veto와 재실행 안전 승인(replay-safety approvals)을 보존하므로 가장 안전한 첫 구성요소입니다.

##### 안전 경계

일부 실패는 자동 재시도되지 않습니다:

- 중단 오류
- 제공자 권고에서 재실행이 안전하지 않다고 표시된 요청
- 출력이 이미 시작되어 재실행이 안전하지 않은 스트리밍 실행

`previous_response_id` 또는 `conversation_id`를 사용하는 상태 기반 후속 요청도 더 보수적으로 처리됩니다. 이러한 요청에서는 `network_error()`나 `http_status([500])` 같은 비제공자 조건만으로는 충분하지 않습니다. 재시도 정책에는 일반적으로 `retry_policies.provider_suggested()`를 통한 제공자 기반 재실행 안전 승인이 포함되어야 합니다.

##### Runner 및 agent 병합 동작

`retry`는 러너 수준과 에이전트 수준 `ModelSettings` 사이에서 깊은 병합(deep-merge)됩니다:

- 에이전트는 `retry.max_retries`만 재정의하고 러너의 `policy`를 상속할 수 있습니다
- 에이전트는 `retry.backoff` 일부만 재정의하고 나머지 backoff 필드를 러너에서 유지할 수 있습니다
- `policy`는 런타임 전용이므로, 직렬화된 `ModelSettings`에는 `max_retries`와 `backoff`만 유지되고 콜백 자체는 제외됩니다

더 완전한 예제는 [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) 및 [`examples/basic/retry_litellm.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)를 참조하세요.

SDK가 아직 최상위로 직접 노출하지 않는 제공자별 또는 최신 요청 필드가 필요할 때는 `extra_args`를 사용하세요.

또한 OpenAI Responses API 사용 시 [다른 선택적 매개변수](https://platform.openai.com/docs/api-reference/responses/create) (`user`, `service_tier` 등)도 있습니다. 최상위에 없으면 `extra_args`로 전달할 수 있습니다.

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

## OpenAI 이외 제공자 문제 해결

### 트레이싱 클라이언트 오류 401

트레이싱 관련 오류가 발생하면, 트레이스가 OpenAI 서버로 업로드되는데 OpenAI API 키가 없기 때문입니다. 해결 방법은 세 가지입니다:

1. 트레이싱 완전 비활성화: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. 트레이싱용 OpenAI 키 설정: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]. 이 API 키는 트레이스 업로드에만 사용되며 [platform.openai.com](https://platform.openai.com/)의 키여야 합니다
3. OpenAI 이외 트레이스 프로세서 사용: [트레이싱 문서](../tracing.md#custom-tracing-processors) 참조

### Responses API 지원

SDK는 기본적으로 Responses API를 사용하지만, 대부분의 다른 LLM 제공자는 아직 이를 지원하지 않습니다. 그 결과 404 또는 유사한 문제가 발생할 수 있습니다. 해결 방법은 두 가지입니다:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] 호출. 환경 변수로 `OPENAI_API_KEY`와 `OPENAI_BASE_URL`을 설정하는 경우 동작합니다
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 사용. 예제는 [여기](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)에 있습니다

### Structured outputs 지원

일부 모델 제공자는 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)를 지원하지 않습니다. 이로 인해 다음과 같은 오류가 발생할 수 있습니다:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

이는 일부 모델 제공자의 한계입니다. JSON 출력은 지원하지만 출력에 사용할 `json_schema` 지정은 허용하지 않습니다. 이에 대한 수정 작업을 진행 중이지만, JSON schema 출력을 지원하는 제공자를 사용하는 것을 권장합니다. 그렇지 않으면 잘못된 JSON으로 인해 앱이 자주 깨질 수 있습니다.

## 제공자 간 모델 혼합

모델 제공자 간 기능 차이를 인지해야 하며, 그렇지 않으면 오류가 발생할 수 있습니다. 예를 들어 OpenAI는 structured outputs, 멀티모달 입력, 호스티드 파일 검색 및 웹 검색을 지원하지만 다른 많은 제공자는 이러한 기능을 지원하지 않습니다. 다음 제한 사항을 유의하세요:

-   지원하지 않는 `tools`를 이해하지 못하는 제공자에게 보내지 마세요
-   텍스트 전용 모델 호출 전에 멀티모달 입력을 필터링하세요
-   structured JSON 출력을 지원하지 않는 제공자는 때때로 유효하지 않은 JSON을 생성할 수 있음을 유의하세요