---
search:
  exclude: true
---
# 모델

Agents SDK는 두 가지 방식의 OpenAI 모델을 기본 지원합니다:

-   **권장**: 새로운 [Responses API](https://platform.openai.com/docs/api-reference/responses)를 사용해 OpenAI API를 호출하는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat)를 사용해 OpenAI API를 호출하는 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## 모델 설정 선택

설정에 맞는 가장 단순한 경로부터 시작하세요:

| 하려는 작업 | 권장 경로 | 더 읽기 |
| --- | --- | --- |
| OpenAI 모델만 사용 | 기본 OpenAI 제공자를 Responses 모델 경로와 함께 사용 | [OpenAI 모델](#openai-models) |
| 웹소켓 전송을 통해 OpenAI Responses API 사용 | Responses 모델 경로를 유지하고 웹소켓 전송 활성화 | [Responses WebSocket 전송](#responses-websocket-transport) |
| 단일 non-OpenAI 제공자 사용 | 내장 제공자 통합 지점부터 시작 | [non-OpenAI 모델](#non-openai-models) |
| 에이전트 간 모델 또는 제공자 혼합 | 실행별 또는 에이전트별로 제공자를 선택하고 기능 차이 검토 | [단일 워크플로 내 모델 혼합](#mixing-models-in-one-workflow) 및 [제공자 간 모델 혼합](#mixing-models-across-providers) |
| 고급 OpenAI Responses 요청 설정 조정 | OpenAI Responses 경로에서 `ModelSettings` 사용 | [고급 OpenAI Responses 설정](#advanced-openai-responses-settings) |
| non-OpenAI 또는 혼합 제공자 라우팅을 위한 서드 파티 어댑터 사용 | 지원되는 베타 어댑터를 비교하고 출시하려는 제공자 경로 검증 | [서드 파티 어댑터](#third-party-adapters) |

## OpenAI 모델

대부분의 OpenAI 전용 앱에서는 기본 OpenAI 제공자와 문자열 모델 이름을 사용하고 Responses 모델 경로를 유지하는 것이 권장 경로입니다.

`Agent`를 초기화할 때 모델을 지정하지 않으면 기본 모델이 사용됩니다. 현재 기본값은 저지연 에이전트 워크플로를 위해 `reasoning.effort="none"` 및 `verbosity="low"`가 설정된 [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini)입니다. 액세스 권한이 있다면 명시적인 `model_settings`를 유지하면서 더 높은 품질을 위해 에이전트를 [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5)로 설정하는 것을 권장합니다.

[`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) 같은 다른 모델로 전환하려면 에이전트를 구성하는 방법이 두 가지 있습니다.

### 기본 모델

첫째, 사용자 지정 모델을 설정하지 않는 모든 에이전트에 특정 모델을 일관되게 사용하려면 에이전트를 실행하기 전에 `OPENAI_DEFAULT_MODEL` 환경 변수를 설정하세요.

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.5
python3 my_awesome_agent.py
```

둘째, `RunConfig`를 통해 실행의 기본 모델을 설정할 수 있습니다. 에이전트에 모델을 설정하지 않으면 이 실행의 모델이 사용됩니다.

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.5"),
)
```

#### GPT-5 모델

이 방식으로 [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) 같은 GPT-5 모델을 사용하는 경우 SDK는 기본 `ModelSettings`를 적용합니다. 대부분의 사용 사례에 가장 잘 맞는 값으로 설정됩니다. 기본 모델의 reasoning effort를 조정하려면 자체 `ModelSettings`를 전달하세요:

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.5 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.5",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

지연 시간을 줄이려면 GPT-5 모델에서 `reasoning.effort="none"`을 사용하는 것이 권장됩니다.

#### ComputerTool 모델 선택

에이전트에 [`ComputerTool`][agents.tool.ComputerTool]이 포함된 경우, 실제 Responses 요청에서 적용되는 모델이 SDK가 보내는 컴퓨터 도구 페이로드를 결정합니다. 명시적인 `gpt-5.5` 요청은 정식 출시(GA) 내장 `computer` 도구를 사용하고, 명시적인 `computer-use-preview` 요청은 기존 `computer_use_preview` 페이로드를 유지합니다.

프롬프트 관리형 호출이 주요 예외입니다. 프롬프트 템플릿이 모델을 소유하고 SDK가 요청에서 `model`을 생략하는 경우, SDK는 프롬프트가 어떤 모델에 고정되어 있는지 추측하지 않도록 프리뷰 호환 컴퓨터 페이로드를 기본값으로 사용합니다. 해당 흐름에서 GA 경로를 유지하려면 요청에 `model="gpt-5.5"`를 명시하거나 `ModelSettings(tool_choice="computer")` 또는 `ModelSettings(tool_choice="computer_use")`로 GA 선택기를 강제하세요.

등록된 [`ComputerTool`][agents.tool.ComputerTool]이 있는 경우 `tool_choice="computer"`, `"computer_use"`, `"computer_use_preview"`는 실제 요청 모델과 일치하는 내장 선택기로 정규화됩니다. 등록된 `ComputerTool`이 없으면 이러한 문자열은 일반 함수 이름처럼 계속 동작합니다.

프리뷰 호환 요청은 `environment`와 표시 크기를 사전에 직렬화해야 하므로, [`ComputerProvider`][agents.tool.ComputerProvider] 팩터리를 사용하는 프롬프트 관리형 흐름은 구체적인 `Computer` 또는 `AsyncComputer` 인스턴스를 전달하거나 요청을 보내기 전에 GA 선택기를 강제해야 합니다. 전체 마이그레이션 세부 사항은 [도구](../tools.md#computertool-and-the-responses-computer-tool)를 참조하세요.

#### GPT-5가 아닌 모델

사용자 지정 `model_settings` 없이 GPT-5가 아닌 모델 이름을 전달하면 SDK는 모든 모델과 호환되는 일반 `ModelSettings`로 되돌아갑니다.

### Responses 전용 도구 검색 기능

다음 도구 기능은 OpenAI Responses 모델에서만 지원됩니다:

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 및 기타 지연 로딩 Responses 도구 표면

이 기능들은 Chat Completions 모델 및 Responses가 아닌 백엔드에서 거부됩니다. 지연 로딩 도구를 사용할 때는 에이전트에 `ToolSearchTool()`을 추가하고, 네임스페이스 이름만 또는 지연 전용 함수 이름을 강제하는 대신 모델이 `auto` 또는 `required` tool choice를 통해 도구를 로드하도록 하세요. 설정 세부 사항과 현재 제약 사항은 [도구](../tools.md#hosted-tool-search)를 참조하세요.

### Responses WebSocket 전송

기본적으로 OpenAI Responses API 요청은 HTTP 전송을 사용합니다. OpenAI 기반 모델을 사용할 때 웹소켓 전송을 선택할 수 있습니다.

#### 기본 설정

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

이는 기본 OpenAI 제공자가 해석하는 OpenAI Responses 모델(예: `"gpt-5.5"` 같은 문자열 모델 이름)에 영향을 줍니다.

전송 선택은 SDK가 모델 이름을 모델 인스턴스로 해석할 때 이루어집니다. 구체적인 [`Model`][agents.models.interface.Model] 객체를 전달하면 해당 전송은 이미 고정되어 있습니다. [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel]은 웹소켓을 사용하고, [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]은 HTTP를 사용하며, [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]은 Chat Completions에 유지됩니다. `RunConfig(model_provider=...)`를 전달하면 전역 기본값 대신 해당 제공자가 전송 선택을 제어합니다.

#### 제공자 또는 실행 수준 설정

제공자별 또는 실행별로도 웹소켓 전송을 구성할 수 있습니다:

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses_websocket=True,
    # Optional; if omitted, OPENAI_WEBSOCKET_BASE_URL is used when set.
    websocket_base_url="wss://your-proxy.example/v1",
    # Optional low-level websocket keepalive settings.
    responses_websocket_options={"ping_interval": 20.0, "ping_timeout": 60.0},
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

OpenAI 기반 제공자는 선택적 에이전트 등록 구성도 받습니다. 이는 OpenAI 설정이 harness ID 같은 제공자 수준 등록 메타데이터를 기대하는 경우를 위한 고급 옵션입니다.

```python
from agents import (
    Agent,
    OpenAIAgentRegistrationConfig,
    OpenAIProvider,
    RunConfig,
    Runner,
)

provider = OpenAIProvider(
    use_responses_websocket=True,
    agent_registration=OpenAIAgentRegistrationConfig(harness_id="your-harness-id"),
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

#### `MultiProvider`를 사용한 고급 라우팅

접두사 기반 모델 라우팅이 필요한 경우(예: 한 실행에서 `openai/...` 및 `any-llm/...` 모델 이름을 혼합) [`MultiProvider`][agents.MultiProvider]를 사용하고 거기에 `openai_use_responses_websocket=True`를 설정하세요.

`MultiProvider`는 두 가지 기존 기본값을 유지합니다:

-   `openai/...`는 OpenAI 제공자의 별칭으로 처리되므로 `openai/gpt-4.1`은 모델 `gpt-4.1`로 라우팅됩니다.
-   알 수 없는 접두사는 그대로 전달되는 대신 `UserError`를 발생시킵니다.

OpenAI 제공자를 리터럴 네임스페이스 모델 ID를 기대하는 OpenAI 호환 엔드포인트로 지정할 때는 패스스루(pass-through) 동작을 명시적으로 선택하세요. 웹소켓 활성화 설정에서는 `MultiProvider`에도 `openai_use_responses_websocket=True`를 유지하세요:

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

백엔드가 리터럴 `openai/...` 문자열을 기대할 때는 `openai_prefix_mode="model_id"`를 사용하세요. 백엔드가 `openrouter/openai/gpt-4.1-mini` 같은 다른 네임스페이스 모델 ID를 기대할 때는 `unknown_prefix_mode="model_id"`를 사용하세요. 이러한 옵션은 웹소켓 전송 외부의 `MultiProvider`에서도 동작합니다. 이 예제에서는 이 섹션에서 설명하는 전송 설정의 일부이므로 웹소켓을 활성화된 상태로 유지합니다. 동일한 옵션은 [`responses_websocket_session()`][agents.responses_websocket_session]에서도 사용할 수 있습니다.

`MultiProvider`를 통해 라우팅하면서 동일한 제공자 수준 등록 메타데이터가 필요하다면 `openai_agent_registration=OpenAIAgentRegistrationConfig(...)`를 전달하세요. 그러면 기본 OpenAI 제공자로 전달됩니다.

사용자 지정 OpenAI 호환 엔드포인트 또는 프록시를 사용하는 경우, 웹소켓 전송에는 호환되는 웹소켓 `/responses` 엔드포인트도 필요합니다. 이러한 설정에서는 `websocket_base_url`을 명시적으로 설정해야 할 수 있습니다.

#### 참고 사항

-   이는 웹소켓 전송을 통한 Responses API이지 [Realtime API](../realtime/guide.md)가 아닙니다. Responses 웹소켓 `/responses` 엔드포인트를 지원하지 않는 한 Chat Completions 또는 non-OpenAI 제공자에는 적용되지 않습니다.
-   환경에서 아직 사용할 수 없다면 `websockets` 패키지를 설치하세요.
-   웹소켓 전송을 활성화한 후 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]를 직접 사용할 수 있습니다. 동일한 웹소켓 연결을 여러 턴(및 중첩 agent-as-tool 호출)에서 재사용하려는 멀티턴 워크플로에는 [`responses_websocket_session()`][agents.responses_websocket_session] 헬퍼가 권장됩니다. [에이전트 실행](../running_agents.md) 가이드와 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)를 참조하세요.
-   긴 reasoning 턴이나 지연 시간 급증이 있는 네트워크에서는 `responses_websocket_options`로 웹소켓 keepalive 동작을 사용자 지정하세요. 지연된 pong 프레임을 허용하려면 `ping_timeout`을 늘리거나, ping은 활성화한 상태로 하트비트 타임아웃을 비활성화하려면 `ping_timeout=None`을 설정하세요. 신뢰성이 웹소켓 지연 시간보다 중요할 때는 HTTP/SSE 전송을 선호하세요.
-   기본적으로 SDK는 수신 메시지 크기 제한(`max_size=None`)을 비활성화합니다. 프록시 뒤에서 장기간 실행되는 에이전트 프로세스나 메모리 제약이 있는 컨테이너에서는 메시지당 메모리 사용량을 제한하기 위해 `responses_websocket_options={"max_size": 8 * 1024 * 1024}`를 설정하세요.

## non-OpenAI 모델

non-OpenAI 제공자가 필요하다면 SDK의 내장 제공자 통합 지점부터 시작하세요. 많은 설정에서는 서드 파티 어댑터를 추가하지 않아도 이것으로 충분합니다. 각 패턴의 예제는 [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)에 있습니다.

### non-OpenAI 제공자 통합 방식

| 접근 방식 | 사용 시점 | 범위 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 하나의 OpenAI 호환 엔드포인트가 대부분 또는 모든 에이전트의 기본값이어야 할 때 | 전역 기본값 |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 하나의 사용자 지정 제공자를 단일 실행에 적용해야 할 때 | 실행별 |
| [`Agent.model`][agents.agent.Agent.model] | 에이전트마다 서로 다른 제공자 또는 구체적인 모델 객체가 필요할 때 | 에이전트별 |
| 서드 파티 어댑터 | 내장 경로가 제공하지 않는 어댑터 관리형 제공자 커버리지 또는 라우팅이 필요할 때 | [서드 파티 어댑터](#third-party-adapters) 참조 |

다음 내장 경로를 사용해 다른 LLM 제공자를 통합할 수 있습니다:

1. [`set_default_openai_client`][agents.set_default_openai_client]는 `AsyncOpenAI` 인스턴스를 LLM 클라이언트로 전역적으로 사용하려는 경우에 유용합니다. 이는 LLM 제공자가 OpenAI 호환 API 엔드포인트를 가지고 있고 `base_url`과 `api_key`를 설정할 수 있는 경우를 위한 것입니다. 구성 가능한 예제는 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)를 참조하세요.
2. [`ModelProvider`][agents.models.interface.ModelProvider]는 `Runner.run` 수준입니다. 이를 통해 "이 실행의 모든 에이전트에 사용자 지정 모델 제공자를 사용"하도록 지정할 수 있습니다. 구성 가능한 예제는 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)를 참조하세요.
3. [`Agent.model`][agents.agent.Agent.model]을 사용하면 특정 Agent 인스턴스에서 모델을 지정할 수 있습니다. 이를 통해 서로 다른 에이전트에 대해 서로 다른 제공자를 혼합해 사용할 수 있습니다. 구성 가능한 예제는 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)를 참조하세요.

`platform.openai.com`의 API 키가 없는 경우 `set_tracing_disabled()`를 통해 트레이싱을 비활성화하거나 [다른 트레이싱 프로세서](../tracing.md)를 설정하는 것을 권장합니다.

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    이 예제들에서는 Chat Completions API/모델을 사용합니다. 아직 많은 LLM 제공자가 Responses API를 지원하지 않기 때문입니다. LLM 제공자가 이를 지원한다면 Responses를 사용하는 것을 권장합니다.

## 단일 워크플로 내 모델 혼합

단일 워크플로 안에서 각 에이전트마다 서로 다른 모델을 사용하고 싶을 수 있습니다. 예를 들어 더 작고 빠른 모델을 분류에 사용하고, 더 크고 성능이 뛰어난 모델을 복잡한 작업에 사용할 수 있습니다. [`Agent`][agents.Agent]를 구성할 때 다음 중 하나로 특정 모델을 선택할 수 있습니다:

1. 모델 이름 전달
2. 해당 이름을 Model 인스턴스로 매핑할 수 있는 임의의 모델 이름 + [`ModelProvider`][agents.models.interface.ModelProvider] 전달
3. [`Model`][agents.models.interface.Model] 구현 직접 제공

!!! note

    SDK는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 및 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 형태를 모두 지원하지만, 두 형태가 서로 다른 기능과 도구 집합을 지원하므로 각 워크플로에는 단일 모델 형태를 사용하는 것을 권장합니다. 워크플로에서 모델 형태를 섞어야 한다면 사용하는 모든 기능이 양쪽 모두에서 제공되는지 확인하세요.

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
    model="gpt-5.5",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1.  OpenAI 모델 이름을 직접 설정합니다.
2.  [`Model`][agents.models.interface.Model] 구현을 제공합니다.

에이전트에 사용되는 모델을 추가로 구성하려면 [`ModelSettings`][agents.models.interface.ModelSettings]를 전달할 수 있으며, 이는 temperature 같은 선택적 모델 구성 매개변수를 제공합니다.

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

## 고급 OpenAI Responses 설정

OpenAI Responses 경로에서 더 많은 제어가 필요할 때는 `ModelSettings`부터 시작하세요.

### 일반적인 고급 `ModelSettings` 옵션

OpenAI Responses API를 사용할 때 여러 요청 필드는 이미 직접적인 `ModelSettings` 필드가 있으므로, 해당 필드에 `extra_args`를 사용할 필요가 없습니다.

- `parallel_tool_calls`: 같은 턴에서 여러 도구 호출을 허용하거나 금지합니다.
- `truncation`: 컨텍스트가 넘칠 때 실패하는 대신 Responses API가 가장 오래된 대화 항목을 삭제하도록 `"auto"`를 설정합니다.
- `store`: 생성된 응답을 나중에 검색할 수 있도록 서버 측에 저장할지 제어합니다. 이는 응답 ID에 의존하는 후속 워크플로와, `store=False`일 때 로컬 입력으로 폴백해야 할 수 있는 세션 압축 흐름에 중요합니다.
- `context_management`: `compact_threshold`가 있는 Responses 압축 등 서버 측 컨텍스트 처리를 구성합니다.
- `prompt_cache_retention`: 예를 들어 `"24h"`로 캐시된 프롬프트 접두사를 더 오래 유지합니다.
- `response_include`: `web_search_call.action.sources`, `file_search_call.results`, 또는 `reasoning.encrypted_content` 같은 더 풍부한 응답 페이로드를 요청합니다.
- `top_logprobs`: 출력 텍스트에 대한 상위 토큰 logprobs를 요청합니다. SDK는 `message.output_text.logprobs`도 자동으로 추가합니다.
- `retry`: 모델 호출에 runner 관리 재시도 설정을 선택 적용합니다. [Runner 관리 재시도](#runner-managed-retries)를 참조하세요.

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.5",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        context_management=[{"type": "compaction", "compact_threshold": 200000}],
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

`store=False`를 설정하면 Responses API는 해당 응답을 나중에 서버 측에서 검색할 수 있도록 보관하지 않습니다. 이는 상태 비저장 또는 무데이터 보존 방식의 흐름에 유용하지만, 응답 ID를 재사용하던 기능은 대신 로컬에서 관리되는 상태에 의존해야 함을 의미합니다. 예를 들어 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession]은 마지막 응답이 저장되지 않은 경우 기본 `"auto"` 압축 경로를 입력 기반 압축으로 전환합니다. [세션 가이드](../sessions/index.md#openai-responses-compaction-sessions)를 참조하세요.

서버 측 압축은 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession]과 다릅니다. `context_management=[{"type": "compaction", "compact_threshold": ...}]`는 각 Responses API 요청과 함께 전송되며, 렌더링된 컨텍스트가 임계값을 넘을 때 API가 응답의 일부로 압축 항목을 내보낼 수 있습니다. `OpenAIResponsesCompactionSession`은 턴 사이에 독립 실행형 `responses.compact` 엔드포인트를 호출하고 로컬 세션 기록을 다시 작성합니다.

### `extra_args` 전달

SDK가 아직 최상위 수준에서 직접 노출하지 않는 제공자별 요청 필드나 더 새로운 요청 필드가 필요할 때 `extra_args`를 사용하세요.

또한 OpenAI의 Responses API를 사용할 때 [몇 가지 다른 선택적 매개변수](https://platform.openai.com/docs/api-reference/responses/create)(예: `user`, `service_tier` 등)가 있습니다. 최상위 수준에서 사용할 수 없다면 `extra_args`로 전달할 수 있습니다. 동일한 요청 필드를 직접 `ModelSettings` 필드를 통해서도 설정하지 마세요.

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

## Runner 관리 재시도

재시도는 런타임 전용이며 명시적으로 선택해야 합니다. `ModelSettings(retry=...)`를 설정하고 재시도 정책이 재시도를 선택하지 않는 한, SDK는 일반 모델 요청을 재시도하지 않습니다.

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.5",
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

`ModelRetrySettings`에는 세 가지 필드가 있습니다:

<div class="field-table" markdown="1">

| 필드 | 타입 | 참고 사항 |
| --- | --- | --- |
| `max_retries` | `int | None` | 초기 요청 이후 허용되는 재시도 시도 횟수입니다. |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | 정책이 명시적 지연을 반환하지 않고 재시도할 때 사용하는 기본 지연 전략입니다. `backoff.max_delay`는 계산된 이 백오프 지연만 제한합니다. 정책이 반환한 명시적 지연이나 retry-after 힌트는 제한하지 않습니다. |
| `policy` | `RetryPolicy | None` | 재시도 여부를 결정하는 콜백입니다. 이 필드는 런타임 전용이며 직렬화되지 않습니다. |

</div>

재시도 정책은 다음을 포함하는 [`RetryPolicyContext`][agents.retry.RetryPolicyContext]를 받습니다:

- `attempt` 및 `max_retries`: 시도 횟수를 고려한 결정을 내릴 수 있습니다.
- `stream`: 스트리밍 동작과 비스트리밍 동작을 분기할 수 있습니다.
- `error`: 원문 검사에 사용합니다.
- `normalized`: `status_code`, `retry_after`, `error_code`, `is_network_error`, `is_timeout`, `is_abort` 같은 정규화된 정보입니다.
- `provider_advice`: 기본 모델 어댑터가 재시도 지침을 제공할 수 있는 경우 사용됩니다.

정책은 다음 중 하나를 반환할 수 있습니다:

- 간단한 재시도 결정을 위한 `True` / `False`
- 지연을 재정의하거나 진단 사유를 첨부하려는 경우 [`RetryDecision`][agents.retry.RetryDecision]

SDK는 `retry_policies`에 준비된 헬퍼를 내보냅니다:

| 헬퍼 | 동작 |
| --- | --- |
| `retry_policies.never()` | 항상 선택 해제합니다. |
| `retry_policies.provider_suggested()` | 사용 가능한 경우 제공자의 재시도 조언을 따릅니다. |
| `retry_policies.network_error()` | 일시적인 전송 및 타임아웃 실패와 일치합니다. |
| `retry_policies.http_status([...])` | 선택한 HTTP 상태 코드와 일치합니다. |
| `retry_policies.retry_after()` | retry-after 힌트를 사용할 수 있을 때만 해당 지연을 사용해 재시도합니다. 이 헬퍼는 retry-after 값을 명시적 정책 지연으로 처리하므로 `backoff.max_delay`가 이를 제한하지 않습니다. |
| `retry_policies.any(...)` | 중첩된 정책 중 하나라도 선택하면 재시도합니다. |
| `retry_policies.all(...)` | 모든 중첩된 정책이 선택할 때만 재시도합니다. |

정책을 조합할 때 `provider_suggested()`는 가장 안전한 첫 번째 구성 요소입니다. 제공자가 이를 구분할 수 있을 때 제공자 거부와 replay-safety 승인을 보존하기 때문입니다.

##### 안전 경계

일부 실패는 자동으로 재시도되지 않습니다:

- 중단 오류
- 제공자 조언이 replay를 안전하지 않은 것으로 표시한 요청
- replay를 안전하지 않게 만들 방식으로 출력이 이미 시작된 후의 스트리밍 실행

`previous_response_id` 또는 `conversation_id`를 사용하는 상태 저장 후속 요청도 더 보수적으로 처리됩니다. 이러한 요청의 경우 `network_error()` 또는 `http_status([500])` 같은 제공자 외부 조건만으로는 충분하지 않습니다. 재시도 정책에는 일반적으로 `retry_policies.provider_suggested()`를 통해 제공자의 replay-safe 승인이 포함되어야 합니다.

##### Runner 및 에이전트 병합 동작

`retry`는 runner 수준과 에이전트 수준 `ModelSettings` 사이에서 깊게 병합됩니다:

- 에이전트가 `retry.max_retries`만 재정의해도 runner의 `policy`를 계속 상속할 수 있습니다.
- 에이전트가 `retry.backoff`의 일부만 재정의하고 runner의 형제 backoff 필드를 유지할 수 있습니다.
- `policy`는 런타임 전용이므로 직렬화된 `ModelSettings`는 `max_retries`와 `backoff`를 유지하지만 콜백 자체는 생략합니다.

더 완전한 예제는 [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) 및 [어댑터 기반 재시도 예제](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)를 참조하세요.

## non-OpenAI 제공자 문제 해결

### 트레이싱 클라이언트 오류 401

트레이싱 관련 오류가 발생한다면 이는 트레이스가 OpenAI 서버로 업로드되는데 OpenAI API 키가 없기 때문입니다. 이를 해결할 수 있는 옵션은 세 가지입니다:

1. 트레이싱을 완전히 비활성화합니다: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. 트레이싱용 OpenAI 키를 설정합니다: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]. 이 API 키는 트레이스 업로드에만 사용되며 [platform.openai.com](https://platform.openai.com/)에서 발급된 것이어야 합니다.
3. non-OpenAI 트레이스 프로세서를 사용합니다. [트레이싱 문서](../tracing.md#custom-tracing-processors)를 참조하세요.

### Responses API 지원

SDK는 기본적으로 Responses API를 사용하지만, 여전히 많은 다른 LLM 제공자는 이를 지원하지 않습니다. 그 결과 404 또는 유사한 문제가 나타날 수 있습니다. 해결하려면 두 가지 옵션이 있습니다:

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]를 호출합니다. 이는 환경 변수를 통해 `OPENAI_API_KEY`와 `OPENAI_BASE_URL`을 설정하는 경우에 동작합니다.
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]을 사용합니다. 예제는 [여기](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)에 있습니다.

### Chat Completions 호환성 옵션

Chat Completions를 통해 라우팅할 때, SDK는 Chat Completions가 보낼 수 없는 Responses 전용 필드(예: `previous_response_id`, `conversation_id`, 프롬프트, 텍스트 전용이 아닌 도구 출력)를 조용히 삭제하여 호환성을 유지합니다. 개발 중 이런 불일치를 빠르게 실패하게 만들고 싶다면 OpenAI 제공자에서 엄격한 기능 검증을 활성화하세요:

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses=False,
    strict_feature_validation=True,
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

[`MultiProvider`][agents.MultiProvider]를 사용하는 경우 대신 `openai_strict_feature_validation=True`를 전달하세요.

일부 OpenAI 호환 Chat Completions 제공자는 증분 SDK 처리에 충분히 신뢰할 수 없는 청크로 tool-call delta를 스트리밍합니다. 이 경우 SDK가 제공자 스트림이 끝난 후에만 도구 호출을 내보내도록 스트리밍 tool-call 버퍼링을 활성화하세요:

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

[`MultiProvider`][agents.MultiProvider]의 경우 `openai_buffer_streamed_tool_calls=True`를 사용하세요.

### Structured outputs 지원

일부 모델 제공자는 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)를 지원하지 않습니다. 이로 인해 때때로 다음과 유사한 오류가 발생합니다:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

이는 일부 모델 제공자의 한계입니다. JSON 출력은 지원하지만 출력에 사용할 `json_schema`를 지정하도록 허용하지는 않습니다. 이 문제를 수정하기 위해 작업 중이지만, JSON 스키마 출력을 지원하는 제공자에 의존하는 것을 권장합니다. 그렇지 않으면 잘못된 형식의 JSON 때문에 앱이 자주 중단될 수 있습니다.

## 제공자 간 모델 혼합

모델 제공자 간 기능 차이를 알고 있어야 하며, 그렇지 않으면 오류가 발생할 수 있습니다. 예를 들어 OpenAI는 structured outputs, 멀티모달 입력, 호스티드 파일 검색 및 웹 검색을 지원하지만 다른 많은 제공자는 이러한 기능을 지원하지 않습니다. 다음 제한 사항에 유의하세요:

-   지원되지 않는 `tools`를 이해하지 못하는 제공자에 보내지 마세요
-   텍스트 전용 모델을 호출하기 전에 멀티모달 입력을 필터링하세요
-   구조화된 JSON 출력을 지원하지 않는 제공자는 때때로 유효하지 않은 JSON을 생성할 수 있습니다.

## 서드 파티 어댑터

SDK의 내장 제공자 통합 지점만으로 충분하지 않을 때만 서드 파티 어댑터를 사용하세요. 이 SDK로 OpenAI 모델만 사용하는 경우 Any-LLM 또는 LiteLLM 대신 내장 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 경로를 선호하세요. 서드 파티 어댑터는 OpenAI 모델을 non-OpenAI 제공자와 결합해야 하거나, 내장 경로가 제공하지 않는 어댑터 관리형 제공자 커버리지 또는 라우팅이 필요한 경우를 위한 것입니다. 어댑터는 SDK와 업스트림 모델 제공자 사이에 또 다른 호환성 계층을 추가하므로, 기능 지원과 요청 의미 체계는 제공자마다 달라질 수 있습니다. SDK는 현재 최선 노력(best-effort) 방식의 베타 어댑터 통합으로 Any-LLM과 LiteLLM을 포함합니다.

### Any-LLM

Any-LLM 지원은 Any-LLM 관리형 제공자 커버리지 또는 라우팅이 필요한 경우를 위해 최선 노력 기반의 베타로 포함되어 있습니다.

업스트림 제공자 경로에 따라 Any-LLM은 Responses API, Chat Completions 호환 API 또는 제공자별 호환성 계층을 사용할 수 있습니다.

Any-LLM이 필요하다면 `openai-agents[any-llm]`을 설치한 다음 [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) 또는 [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py)부터 시작하세요. [`MultiProvider`][agents.MultiProvider]와 함께 `any-llm/...` 모델 이름을 사용하거나, `AnyLLMModel`을 직접 인스턴스화하거나, 실행 범위에서 `AnyLLMProvider`를 사용할 수 있습니다. 모델 API 표면을 명시적으로 고정해야 한다면 `AnyLLMModel`을 생성할 때 `api="responses"` 또는 `api="chat_completions"`를 전달하세요.

Any-LLM은 여전히 서드 파티 어댑터 계층이므로, 제공자 의존성과 기능 격차는 SDK가 아니라 Any-LLM에 의해 업스트림에서 정의됩니다. 업스트림 제공자가 사용량 지표를 반환하면 해당 지표는 자동으로 전파되지만, 스트리밍 Chat Completions 백엔드는 사용량 청크를 내보내기 전에 `ModelSettings(include_usage=True)`가 필요할 수 있습니다. structured outputs, 도구 호출, 사용량 보고 또는 Responses별 동작에 의존한다면 배포하려는 정확한 제공자 백엔드를 검증하세요.

### LiteLLM

LiteLLM 지원은 LiteLLM별 제공자 커버리지 또는 라우팅이 필요한 경우를 위해 최선 노력 기반의 베타로 포함되어 있습니다.

LiteLLM이 필요하다면 `openai-agents[litellm]`을 설치한 다음 [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) 또는 [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py)부터 시작하세요. `litellm/...` 모델 이름을 사용하거나 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]을 직접 인스턴스화할 수 있습니다.

일부 LiteLLM 기반 제공자는 기본적으로 SDK 사용량 지표를 채우지 않습니다. 사용량 보고가 필요하다면 `ModelSettings(include_usage=True)`를 전달하고, structured outputs, 도구 호출, 사용량 보고 또는 어댑터별 라우팅 동작에 의존한다면 배포하려는 정확한 제공자 백엔드를 검증하세요.