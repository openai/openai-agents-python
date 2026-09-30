---
search:
  exclude: true
---
# 모델

Agents SDK는 기본적으로 다음 두 가지 방식의 OpenAI 모델을 지원합니다.

-   **권장**: 새로운 [Responses API](https://platform.openai.com/docs/api-reference/responses)를 사용하여 OpenAI API를 호출하는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]
-   [Chat Completions API](https://platform.openai.com/docs/api-reference/chat)를 사용하여 OpenAI API를 호출하는 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]

## 모델 설정 선택 {#choosing-a-model-setup}

설정에 적합한 가장 간단한 방법부터 시작하세요.

| 원하는 작업 | 권장 방법 | 자세히 보기 |
| --- | --- | --- |
| OpenAI 모델만 사용 | Responses 모델 경로와 함께 기본 OpenAI 프로바이더 사용 | [OpenAI 모델](#openai-models) |
| WebSocket 전송을 통해 OpenAI Responses API 사용 | Responses 모델 경로를 유지하고 WebSocket 전송 활성화 | [Responses WebSocket 전송](#responses-websocket-transport) |
| OpenAI 호스트 서브에이전트 사용 | 실험적 호스티드 멀티 에이전트 모델 사용 | [호스티드 멀티 에이전트](#hosted-multi-agent-experimental) |
| 하나의 비 OpenAI 프로바이더 사용 | 기본 제공 프로바이더 통합 지점으로 시작 | [비 OpenAI 모델](#non-openai-models) |
| 에이전트 간에 모델 또는 프로바이더 혼합 | 실행별 또는 에이전트별로 프로바이더를 선택하고 기능 차이 검토 | [하나의 워크플로에서 모델 혼합](#mixing-models-in-one-workflow) 및 [프로바이더 간 모델 혼합](#mixing-models-across-providers) |
| 고급 OpenAI Responses 요청 설정 조정 | OpenAI Responses 경로에서 `ModelSettings` 사용 | [고급 OpenAI Responses 설정](#advanced-openai-responses-settings) |
| 비 OpenAI 또는 혼합 프로바이더 라우팅에 서드 파티 어댑터 사용 | 지원되는 베타 어댑터를 비교하고 배포하려는 프로바이더 경로 검증 | [서드 파티 어댑터](#third-party-adapters) |

## OpenAI 모델 {#openai-models}

OpenAI만 사용하는 대부분의 앱에는 기본 OpenAI 프로바이더와 문자열 모델 이름을 사용하고 Responses 모델 경로를 유지하는 방법을 권장합니다.

[`Agent`][agents.agent.Agent]가 모델을 지정하지 않으면 Agents SDK는 비용에 민감한 대규모 에이전트 워크플로를 위해 기본적으로 `reasoning.effort="none"` 및 `verbosity="low"`과 함께 [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna)를 사용합니다. 최첨단 기능이 필요한 애플리케이션은 `model="gpt-5.6-sol"`을 명시적으로 설정하고 워크로드에 적합한 `model_settings`을 선택할 수 있습니다.

`gpt-5.6-sol` 같은 다른 모델로 전환하려면 에이전트를 구성하는 두 가지 방법이 있습니다.

### 기본 모델 {#default-model}

첫째, 사용자 지정 모델을 설정하지 않은 모든 에이전트에서 특정 모델을 일관되게 사용하려면 에이전트를 실행하기 전에 `OPENAI_DEFAULT_MODEL` 환경 변수를 설정하세요.

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.6-sol
python3 my_awesome_agent.py
```

둘째, `RunConfig`을 통해 실행의 기본 모델을 설정할 수 있습니다. 에이전트에 모델을 설정하지 않으면 이 실행의 모델이 사용됩니다.

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.6-sol"),
)
```

#### GPT-5 모델 {#gpt-5-models}

이 방식으로 `gpt-5.6-sol` 같은 GPT-5 모델을 사용하면 SDK가 기본 `ModelSettings`을 적용합니다. 대부분의 사용 사례에 가장 적합한 값이 설정됩니다. 기본 모델의 추론 강도를 조정하려면 자체 `ModelSettings`를 전달하세요.

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.6-sol is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.6-sol",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

지연 시간을 줄이려면 GPT-5 모델에 `reasoning.effort="none"`를 사용하는 것이 좋습니다.

GPT-5.6은 기존 `reasoning` 설정을 통해 추론 모드, 대화 턴 간에 유지되는 추론 컨텍스트, `"max"` 강도 수준도 지원합니다. 이러한 제어 기능은 Responses API 경로에서 사용할 수 있습니다.

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

agent = Agent(
    name="Deep research agent",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        reasoning=Reasoning(
            mode="pro",
            effort="max",
            context="all_turns",
        ),
    ),
)
```

`reasoning.mode` 및 `reasoning.context`는 Responses 전용 설정입니다. Chat Completions는 `reasoning.effort`만 사용하며, 지원되는 강도 수준은 모델과 API 인터페이스에 따라 달라집니다. GPT-5.6의 `"max"` 강도를 사용하려면 Responses API를 사용하세요. Chat Completions 어댑터는 경고와 함께 모드 및 컨텍스트를 무시합니다. 해당 경고를 오류로 전환하려면 OpenAI 프로바이더에서 `strict_feature_validation=True`를 설정하세요.

`context="all_turns"`을 사용할 때는 `previous_response_id`, 서버 측 Responses API 대화 또는 다음 요청에 이전 추론 항목을 포함하는 방식으로 대화를 유지하세요. 상태 비저장 `store=False` 호출의 경우 응답에서 `reasoning.encrypted_content`을 요청한 다음 해당 추론 항목을 다음 요청의 입력으로 포함하세요.

#### ComputerTool 모델 선택 {#computertool-model-selection}

에이전트에 [`ComputerTool`][agents.tool.ComputerTool]이 포함된 경우 실제 Responses 요청의 유효 모델에 따라 SDK가 전송할 컴퓨터 도구 페이로드가 결정됩니다. 에이전트가 `model`을 설정하지 않으면 일반적인 SDK 모델 선택 우선순위가 적용됩니다. 현재 [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna)인 SDK 기본 모델은 GA 기본 제공 `computer` 도구를 지원합니다. `OPENAI_DEFAULT_MODEL` 또는 `RunConfig.model`가 해당 기본값을 재정의하는 경우 컴퓨터 사용을 지원하는 모델을 선택하세요. 컴퓨터 사용 워크로드에 다른 기능 및 비용 프로필을 선택하려면 에이전트에서 `model`을 설정하세요. 예를 들어 `model="gpt-5.6"`는 OpenAI가 GPT-5.6 Sol로 라우팅하는 별칭을 사용합니다. 명시적인 `computer-use-preview` 요청은 이전 `computer_use_preview` 페이로드를 유지합니다.

프롬프트로 관리되는 호출은 주요 예외입니다. 프롬프트 템플릿에서 모델을 지정하고 SDK가 요청에서 `model`을 생략하면, SDK는 프롬프트에 고정된 모델을 추측하지 않도록 프리뷰 호환 컴퓨터 페이로드를 기본값으로 사용합니다. 해당 흐름에서 GA 경로를 유지하려면 `model="gpt-5.6"` 같은 지원되는 GA 모델을 요청에 명시하거나 `ModelSettings(tool_choice="computer")` 또는 `ModelSettings(tool_choice="computer_use")`으로 GA 선택기를 강제하세요.

[`ComputerTool`][agents.tool.ComputerTool]이 등록되어 있으면 `tool_choice="computer"`, `"computer_use"`, `"computer_use_preview"`는 유효 요청 모델에 맞는 기본 제공 선택기로 정규화됩니다. `ComputerTool`가 등록되지 않은 경우 이러한 문자열은 계속 일반 함수 이름처럼 동작합니다.

프리뷰 호환 요청은 `environment`과 디스플레이 크기를 미리 직렬화해야 합니다. 따라서 [`ComputerProvider`][agents.tool.ComputerProvider] 팩토리를 사용하는 프롬프트 관리 흐름에서는 구체적인 `Computer` 또는 `AsyncComputer` 인스턴스를 전달하거나 요청을 전송하기 전에 GA 선택기를 강제해야 합니다. 전체 마이그레이션 세부 정보는 [도구](../tools.md#computertool-and-the-responses-computer-tool)를 참조하세요.

#### 비 GPT-5 모델 {#non-gpt-5-models}

사용자 지정 `model_settings` 없이 비 GPT-5 모델 이름을 전달하면 SDK는 모든 모델과 호환되는 일반 `ModelSettings`로 되돌아갑니다.

### Responses 전용 도구 기능 {#responses-only-tool-features}

다음 도구 기능은 OpenAI Responses 모델에서만 지원됩니다.

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` 및 지연 로딩을 지원하는 기타 Responses 도구 인터페이스
-   [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool], `allowed_callers`, `tool_choice="programmatic_tool_calling"`

이러한 기능은 Chat Completions 모델과 Responses가 아닌 백엔드에서 거부됩니다. 지연 로딩 도구를 사용할 때는 에이전트에 `ToolSearchTool()`을 추가하고, 단순 네임스페이스 이름이나 지연 전용 함수 이름을 강제하는 대신 모델이 `auto` 또는 `required` 도구 선택을 통해 도구를 로드하도록 하세요. 설정 세부 정보와 현재 제약 조건은 [호스티드 도구 검색](../tools.md#hosted-tool-search) 및 [프로그래매틱 도구 호출](../tools.md#programmatic-tool-calling)을 참조하세요.

### Responses WebSocket 전송 {#responses-websocket-transport}

기본적으로 OpenAI Responses API 요청은 HTTP 전송을 사용합니다. OpenAI Responses 프로바이더 경로를 사용할 때 WebSocket 전송을 활성화할 수 있습니다.

#### 기본 설정 {#basic-setup}

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

이는 기본 OpenAI 프로바이더가 모델 이름을 확인할 때 생성되는 OpenAI Responses 모델에 영향을 줍니다. 여기에는 `"gpt-5.6-sol"` 같은 문자열 모델 이름도 포함됩니다.

SDK가 모델 이름을 모델 인스턴스로 확인할 때 전송 방식이 선택됩니다. 구체적인 [`Model`][agents.models.interface.Model] 객체를 전달하면 해당 객체의 전송 방식은 이미 고정되어 있습니다. [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel]은 WebSocket을 사용하고, [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]은 HTTP를 사용하며, [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]은 Chat Completions를 계속 사용합니다. `RunConfig(model_provider=...)`을 전달하면 전역 기본값 대신 해당 프로바이더가 전송 방식 선택을 제어합니다.

#### 프로바이더 또는 실행 수준 설정 {#provider-or-run-level-setup}

프로바이더별 또는 실행별로 WebSocket 전송을 구성할 수도 있습니다.

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

SDK의 OpenAI 통합을 통해 라우팅하는 프로바이더는 선택적 에이전트 등록 구성도 허용합니다. 이는 OpenAI 설정에서 하네스 ID 같은 프로바이더 수준 등록 메타데이터를 요구하는 경우를 위한 고급 옵션입니다.

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

#### `MultiProvider`을 사용한 고급 라우팅 {#advanced-routing-with-multiprovider}

접두사 기반 모델 라우팅이 필요한 경우(예: 한 실행에서 `openai/...` 및 `any-llm/...` 모델 이름 혼합) [`MultiProvider`][agents.MultiProvider]을 사용하고 그곳에서 `openai_use_responses_websocket=True`을 설정하세요.

`MultiProvider`는 두 가지 기존 기본 동작을 유지합니다.

-   `openai/...`은 OpenAI 프로바이더의 별칭으로 취급되므로 `openai/gpt-4.1`는 모델 `gpt-4.1`로 라우팅됩니다.
-   알 수 없는 접두사는 그대로 전달되지 않고 `UserError`을 발생시킵니다.

OpenAI 프로바이더가 리터럴 네임스페이스 모델 ID를 요구하는 OpenAI 호환 엔드포인트를 가리키도록 설정한 경우 명시적으로 통과 동작을 활성화하세요. WebSocket이 활성화된 설정에서는 `MultiProvider`에도 `openai_use_responses_websocket=True`을 유지하세요.

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

백엔드가 리터럴 `openai/...` 문자열을 요구할 때는 `openai_prefix_mode="model_id"`를 사용하세요. 백엔드가 `openrouter/openai/gpt-4.1-mini` 같은 다른 네임스페이스 모델 ID를 요구할 때는 `unknown_prefix_mode="model_id"`을 사용하세요. 이러한 옵션은 WebSocket 전송 외부의 `MultiProvider`에서도 동작합니다. 이 예제에서는 이 섹션에서 설명한 전송 설정의 일부이므로 WebSocket을 계속 활성화합니다. 같은 옵션은 [`responses_websocket_session()`][agents.responses_websocket_session]에서도 사용할 수 있습니다.

`MultiProvider`를 통해 라우팅하면서 동일한 프로바이더 수준 등록 메타데이터가 필요한 경우 `openai_agent_registration=OpenAIAgentRegistrationConfig(...)`을 전달하면 내부 OpenAI 프로바이더로 전달됩니다.

사용자 지정 OpenAI 호환 엔드포인트 또는 프록시를 사용하는 경우 WebSocket 전송에는 호환되는 WebSocket `/responses` 엔드포인트도 필요합니다. 이러한 설정에서는 `websocket_base_url`을 명시적으로 설정해야 할 수 있습니다.

#### 참고 사항 {#notes}

-   이는 WebSocket 전송을 통한 Responses API이며 [Realtime API](../realtime/guide.md)가 아닙니다. Chat Completions에는 적용되지 않습니다. 비 OpenAI 프로바이더의 경우 Responses WebSocket `/responses` 엔드포인트를 지원할 때만 적용됩니다.
-   환경에 아직 없다면 `websockets` 패키지를 설치하세요.
-   WebSocket 전송을 활성화한 후 [`Runner.run_streamed()`][agents.run.Runner.run_streamed]을 직접 사용할 수 있습니다. 여러 턴 및 중첩된 에이전트 도구 호출에서 동일한 WebSocket 연결을 재사용하려는 멀티턴 워크플로에는 [`responses_websocket_session()`][agents.responses_websocket_session] 헬퍼를 권장합니다. [에이전트 실행](../running_agents.md) 가이드와 [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py)을 참조하세요.
-   긴 추론 턴 또는 지연 시간이 급증하는 네트워크에서는 `responses_websocket_options`로 WebSocket 연결 유지 동작을 사용자 지정하세요. 지연된 pong 프레임을 허용하려면 `ping_timeout`를 늘리고, ping은 활성화한 채 하트비트 시간 초과를 비활성화하려면 `ping_timeout=None`을 설정하세요. WebSocket 지연 시간보다 안정성이 중요한 경우 HTTP/SSE 전송을 권장합니다.
-   기본적으로 SDK는 수신 메시지 크기 제한(`max_size=None`)을 비활성화합니다. 프록시 뒤에서 실행되는 장기 에이전트 프로세스나 메모리가 제한된 컨테이너에서는 메시지별 메모리 사용량을 제한하도록 `responses_websocket_options={"max_size": 8 * 1024 * 1024}`을 설정하세요.
-   [Responses API WebSocket 서비스](https://developers.openai.com/api/docs/guides/websocket-mode)는 각 연결에서 한 번에 하나의 응답을 처리하며 각 연결을 60분으로 제한합니다. 이 제한 이후에는 새 연결을 여세요. 병렬 실행이 필요하면 여러 연결을 사용하세요.
-   서비스는 연결 로컬 메모리에 가장 최근 응답만 유지합니다. 실패한 `4xx` 또는 `5xx` 턴은 `previous_response_id`이 참조하는 응답을 해당 메모리에서 제거합니다. 다시 연결한 후에도 저장된 응답이 있으면 이어갈 수 있지만, `store=False` 및 ZDR 흐름에는 영구 저장된 대체 데이터가 없습니다. `previous_response_id=None`으로 새 체인을 시작하고 전체 입력 컨텍스트를 전송하거나 로컬에서 관리하는 세션 상태로 해당 컨텍스트를 재구성하세요.

### 호스티드 멀티 에이전트(실험적) {#hosted-multi-agent-experimental}

OpenAI Responses API 호스티드 멀티 에이전트 베타를 사용하면 GPT-5.6 루트 모델이 서버에서 호스트되는 서브에이전트를 생성하고 조정할 수 있습니다. Agents SDK는 일반적인 `Runner`를 계속 사용할 수 있습니다. 호스티드 오케스트레이션은 서비스에서 유지되며 개발자가 정의한 함수 도구는 애플리케이션에서 실행됩니다.

이 통합은 실험적이며 로컬 함수 출력을 `response.inject`를 통해 활성 호스티드 에이전트로 반환할 수 있도록 Responses WebSocket 전송을 사용합니다. `client.beta.responses.connect`을 노출하는 `openai[realtime]` 버전 2.45.0 이상의 빌드가 필요합니다. 인터페이스와 베타 항목 스키마는 정식 출시 전에 변경될 수 있습니다.

#### 모델 구성 {#configure-the-model}

실험적 모듈에서 모델을 가져와 SDK `Agent`에 할당하세요.

```python
from agents import Agent
from agents.extensions.experimental.hosted_multi_agent import OpenAIHostedMultiAgentModel

agent = Agent(
    name="Research coordinator",
    instructions="Delegate independent research tasks, then synthesize the findings.",
    model=OpenAIHostedMultiAgentModel(model="gpt-5.6-sol", config={"max_concurrent_subagents": 3}),
)
```

`OpenAIHostedMultiAgentModel`을 생성하면 `multi_agent.enabled`이 활성화되고 `OpenAI-Beta: responses_multi_agent=v1` WebSocket 헤더가 전송됩니다. `openai_client`가 제공되지 않으면 모델은 기본 OpenAI 클라이언트를 사용합니다. `max_concurrent_subagents`을 생략하면 서비스 기본값이 사용됩니다.

#### 로컬 함수 도구 {#local-function-tools}

모든 호스티드 에이전트는 요청에 구성된 모델과 도구를 공유합니다. Responses API는 어떤 호스티드 에이전트가 함수를 호출할지 결정합니다. 일반 SDK Runner는 함수를 로컬에서 실행하고 동일한 호출 ID를 사용하는 `function_call_output`를 활성 WebSocket 응답에 주입합니다. 이를 통해 서비스는 원래 호스티드 호출자를 재개할 수 있습니다. 함수 실행에는 여전히 Runner의 일반 가드레일, 훅, 실패 변환이 적용됩니다. SDK 도구 승인 인터럽션(중단 처리)은 지원되지 않습니다. `needs_approval` 설정이 `False`이 아닌 함수 도구는 요청을 전송하기 전에 거부됩니다.

도구에 호출자 인식 로깅 또는 권한 부여가 필요할 때는 `get_hosted_agent_metadata()`을 사용하세요.

```python
from typing import Any

from agents.decorators import tool
from agents.extensions.experimental.hosted_multi_agent import get_hosted_agent_metadata
from agents.tool_context import ToolContext

@tool
def lookup_document(ctx: ToolContext[Any], section: str) -> str:
    metadata = get_hosted_agent_metadata(ctx)
    caller = metadata.agent_name if metadata else "unknown"
    print(f"tool caller: {caller}; call ID: {ctx.tool_call_id}")
    return f"Contents for {section}"
```

호스티드 에이전트 이름은 관찰용 메타데이터이며 로컬 라우팅 메커니즘이 아닙니다. SDK에서 제공하는 호출 ID로 출력을 라우팅하세요. 부작용이 있는 도구에서는 해당 호출 ID를 멱등성 키로 사용하고, 도구 실행 전이나 실행 중에 애플리케이션 코드에서 필요한 권한 부여를 적용하세요. 이 모델에는 `needs_approval`을 사용하지 마세요. 도구 인수와 출력은 Responses API 경계를 통과합니다.

#### 출력 및 스트리밍 동작 {#output-and-streaming-behavior}

단계가 `final_answer`인 `/root`에 귀속된 메시지만 일반 최종 메시지가 됩니다. 실험적 어댑터는 높은 수준의 `RunResult`에서 서브에이전트 메시지와 호스티드 오케스트레이션 레코드를 필터링합니다. SDK는 이러한 레코드를 로컬 함수로 실행하지 않습니다.

raw 스트리밍은 호스티드 출력 항목과 `response.inject.created` 확인을 포함한 베타 Responses 이벤트를 계속 노출합니다. 어댑터는 함수 호출이 준비되면 하나의 활성 프로바이더 응답을 SDK에 표시되는 논리적 모델 턴으로 분할한 다음, Runner가 출력을 생성한 후 동일한 프로바이더 응답을 재개합니다. 항목 또는 도구 호출이 어떤 호스티드 에이전트에 귀속되는지 식별하려면 raw 호스티드 항목 또는 `ToolContext`와 함께 `get_hosted_agent_metadata()`을 사용하세요.

#### SDK 오케스트레이션과의 관계 {#relationship-to-sdk-orchestration}

호스티드 멀티 에이전트는 SDK 핸드오프 및 Agents-as-tools와 별개입니다.

-   호스티드 멀티 에이전트는 OpenAI 서비스에서 서브에이전트를 생성합니다. 애플리케이션은 이러한 서브에이전트를 생성하거나 예약하지 않습니다.
-   SDK 핸드오프는 활성 로컬 SDK `Agent`를 변경합니다. 모든 호스티드 에이전트가 동일한 핸드오프 도구를 받아 소유권 충돌이 발생하므로 이 실험적 모델을 사용할 때는 거부됩니다.
-   Agents-as-tools는 계속 사용할 수 있지만, 이를 사용하면 클라이언트 측 및 서버 측 오케스트레이션이 중첩됩니다. 추가 지연 시간, 비용, 도구 노출을 신중히 평가하세요.

#### 현재 제한 사항 {#current-limitations}

실험적 모델은 `reasoning.summary`, `max_tool_calls`, 호출자가 제공한 `multi_agent` 또는 `betas` 재정의를 거부합니다. 베타에서는 Responses `/compact` 엔드포인트가 지원되지 않지만, 서비스가 각 호스티드 에이전트 컨텍스트를 독립적으로 자동 압축하므로 명시적인 `context_management.compact_threshold`을 사용할 수 있습니다.

하나의 `OpenAIHostedMultiAgentModel` 인스턴스는 한 번에 최대 하나의 활성 호스티드 응답을 소유합니다. 로컬 함수 출력을 기다리는 동안 실행이 중단된 경우 `await model.close()`을 호출하여 WebSocket을 해제하세요. 진행 중인 호스티드 응답을 다른 프로세스나 이벤트 루프에서 복원하는 기능은 현재 지원되지 않습니다.

기본 Responses API 베타 동작은 [OpenAI 멀티 에이전트 가이드](https://developers.openai.com/api/docs/guides/tools-multi-agent)를 참조하세요. 비스트리밍 및 스트리밍 SDK 사용법은 [`examples/agent_patterns/hosted_multi_agent_beta.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/hosted_multi_agent_beta.py)를 참조하세요.

## 비 OpenAI 모델 {#non-openai-models}

비 OpenAI 프로바이더가 필요한 경우 SDK의 기본 제공 프로바이더 통합 지점부터 시작하세요. 많은 설정에서는 서드 파티 어댑터를 추가하지 않아도 충분합니다. 각 패턴의 예제는 [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)에 있습니다.

### 비 OpenAI 프로바이더 통합 방법 {#ways-to-integrate-non-openai-providers}

| 접근 방식 | 사용 시점 | 범위 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 하나의 OpenAI 호환 엔드포인트를 대부분 또는 모든 에이전트의 기본값으로 사용해야 할 때 | 전역 기본값 |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 하나의 사용자 지정 프로바이더를 단일 실행에 적용해야 할 때 | 실행별 |
| [`Agent.model`][agents.agent.Agent.model] | 에이전트마다 다른 프로바이더 또는 구체적인 모델 객체가 필요할 때 | 에이전트별 |
| 서드 파티 어댑터 | 기본 제공 경로가 제공하지 않는 프로바이더 지원 범위나 라우팅이 필요할 때 | [서드 파티 어댑터](#third-party-adapters) 참조 |

다음 기본 제공 경로를 사용하여 다른 LLM 프로바이더를 통합할 수 있습니다.

1. [`set_default_openai_client`][agents.set_default_openai_client]은 `AsyncOpenAI` 인스턴스를 LLM 클라이언트로 전역에서 사용하려는 경우 유용합니다. LLM 프로바이더에 OpenAI 호환 API 엔드포인트가 있어 `base_url` 및 `api_key`을 설정할 수 있는 경우에 사용합니다. 구성 가능한 예제는 [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py)를 참조하세요.
2. [`ModelProvider`][agents.models.interface.ModelProvider]는 `Runner.run` 수준에서 사용합니다. 이를 통해 "이 실행의 모든 에이전트에 사용자 지정 모델 프로바이더를 사용"하도록 지정할 수 있습니다. 구성 가능한 예제는 [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py)를 참조하세요.
3. [`Agent.model`][agents.agent.Agent.model]를 사용하면 특정 Agent 인스턴스에 모델을 지정할 수 있습니다. 이를 통해 에이전트마다 서로 다른 프로바이더를 조합할 수 있습니다. 구성 가능한 예제는 [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py)를 참조하세요.

`platform.openai.com`의 API 키가 없는 경우 `set_tracing_disabled()`으로 트레이싱을 비활성화하거나 [다른 트레이싱 프로세서](../tracing.md)를 설정하는 것이 좋습니다.

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    이 예제에서는 많은 LLM 프로바이더가 아직 Responses API를 지원하지 않으므로 Chat Completions API/모델을 사용합니다. LLM 프로바이더가 Responses를 지원한다면 Responses 사용을 권장합니다.

## 하나의 워크플로에서 모델 혼합 {#mixing-models-in-one-workflow}

단일 워크플로 내에서 에이전트마다 서로 다른 모델을 사용할 수 있습니다. 예를 들어 분류에는 더 작고 빠른 모델을 사용하고 복잡한 작업에는 더 크고 성능이 우수한 모델을 사용할 수 있습니다. [`Agent`][agents.Agent]를 구성할 때 다음 중 하나로 특정 모델을 선택할 수 있습니다.

1. 모델 이름 전달
2. 임의의 모델 이름과 해당 이름을 Model 인스턴스에 매핑할 수 있는 [`ModelProvider`][agents.models.interface.ModelProvider] 전달
3. [`Model`][agents.models.interface.Model] 구현 직접 제공

!!! note

    SDK는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 및 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] 형식을 모두 지원하지만, 두 형식에서 지원하는 기능과 도구 집합이 서로 다르므로 각 워크플로에서는 하나의 모델 형식을 사용하는 것이 좋습니다. 워크플로에서 모델 형식을 혼합해야 한다면 사용하는 모든 기능을 양쪽 모두에서 사용할 수 있는지 확인하세요.

```python
import asyncio

from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel

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
    model="gpt-5.6-sol",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

1.  OpenAI 모델 이름을 직접 설정합니다.
2.  [`Model`][agents.models.interface.Model] 구현을 제공합니다.

에이전트에서 사용하는 모델을 추가로 구성하려면 temperature 같은 선택적 모델 구성 매개변수를 제공하는 [`ModelSettings`][agents.model_settings.ModelSettings]를 전달할 수 있습니다.

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

## 고급 OpenAI Responses 설정 {#advanced-openai-responses-settings}

OpenAI Responses 경로에서 더 세밀한 제어가 필요하면 `ModelSettings`부터 사용하세요.

### 일반적인 고급 `ModelSettings` 옵션 {#common-advanced-modelsettings-options}

OpenAI Responses API를 사용할 때 여러 요청 필드에는 이미 대응하는 `ModelSettings` 필드가 있으므로 해당 필드에 `extra_args`을 사용할 필요가 없습니다.

- `parallel_tool_calls`: 동일한 턴에서 여러 도구 호출 허용 또는 금지
- `truncation`: 컨텍스트가 초과될 때 실패하는 대신 Responses API가 가장 오래된 대화 항목을 삭제하도록 `"auto"` 설정
- `store`: 생성된 응답을 나중에 조회할 수 있도록 서버 측에 저장할지 제어합니다. 이는 응답 ID를 사용하는 후속 워크플로와 `store=False`일 때 로컬 입력으로 대체해야 할 수 있는 세션 압축 흐름에 중요합니다.
- `context_management`: `compact_threshold`를 사용한 Responses 압축 같은 서버 측 컨텍스트 처리 구성
- `prompt_cache_retention`: 이전 모델 제품군의 연장 보존 구성. 예:
  `"24h"`
- `prompt_cache_options`: 암시적 또는 명시적 프롬프트 캐싱을 선택하고, GPT-5.6에서는 `"30m"` 캐시 TTL 구성
- `response_include`: `web_search_call.action.sources`, `file_search_call.results`, `reasoning.encrypted_content` 같은 더 풍부한 응답 페이로드 요청
- `top_logprobs`: 출력 텍스트에 대한 상위 토큰 logprobs 요청. SDK는 `message.output_text.logprobs`도 자동으로 추가합니다.
- `retry`: 모델 호출에 Runner가 관리하는 재시도 설정 활성화. [Runner 관리형 재시도](#runner-managed-retries)를 참조하세요.

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        context_management=[{"type": "compaction", "compact_threshold": 200000}],
        prompt_cache_options={"mode": "explicit", "ttl": "30m"},
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

명시적 프롬프트 캐싱에서는 재사용 가능한 접두사가 끝나는 콘텐츠 부분에 중단점을 추가하세요. 동일한 `ModelSettings.prompt_cache_options` 필드가 Responses 및 Chat Completions 요청에 전달되며, Chat Completions 변환기는 텍스트, 이미지, 오디오, 파일 콘텐츠 부분의 중단점을 유지합니다.

```python
from agents import Runner

result = await Runner.run(
    research_agent,
    [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Reusable background material...",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {
                    "type": "input_text",
                    "text": "Analyze the latest question.",
                },
            ],
        }
    ],
)
```

`prompt_cache_retention`은 레거시
보존 제어를 사용하는 이전 모델 제품군에서 계속 사용할 수 있습니다. 직접 지정한 `ModelSettings` 필드와
`extra_args`의 동일한 키를 함께 사용하지 마세요.

`store=False`을 설정하면 Responses API는 해당 응답을 나중에 서버 측에서 조회할 수 있도록 보관하지 않습니다. 이는 상태 비저장 또는 제로 데이터 보존 방식의 흐름에 유용하지만, 그렇지 않았다면 응답 ID를 재사용할 기능이 로컬에서 관리하는 상태에 의존해야 한다는 의미이기도 합니다. 예를 들어 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession]은 마지막 응답이 저장되지 않은 경우 기본 `"auto"` 압축 경로를 입력 기반 압축으로 전환합니다. [세션 가이드](../sessions/index.md#openai-responses-compaction-sessions)를 참조하세요.

서버 측 압축은 [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession]과 다릅니다. `context_management=[{"type": "compaction", "compact_threshold": ...}]`는 각 Responses API 요청과 함께 전송되며, 렌더링된 컨텍스트가 임계값을 넘으면 API가 응답의 일부로 압축 항목을 생성할 수 있습니다. `OpenAIResponsesCompactionSession`는 턴 사이에 독립 실행형 `responses.compact` 엔드포인트를 호출하고 로컬 세션 기록을 다시 작성합니다.

### `extra_args` 전달 {#passing-extra_args}

SDK가 아직 최상위 수준에서 직접 노출하지 않는 프로바이더별 요청 필드나 최신 요청 필드가 필요한 경우 `extra_args`을 사용하세요.

OpenAI 모델을 사용할 때 `extra_args`는 Responses API와 Chat Completions API 모두에 선택적 매개변수(예: `user` 및 `service_tier`)를 전달할 수 있습니다. 지원되는 모델에서는 [고속 모드](https://developers.openai.com/api/docs/guides/fast-mode)를 사용하도록 `extra_args={"service_tier": "fast"}`를 설정하세요. `"priority"`도 동일하게 동작합니다. 직접 지정한 `ModelSettings` 필드를 통해 동일한 요청 필드를 함께 설정하지 마세요.

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

## 모델 호출 시간 초과 {#model-call-timeouts}

각 모델 호출 시도의 시간을 제한하려면 [`ModelSettings.timeout`][agents.model_settings.ModelSettings.timeout]를 양의 초 단위 값으로 설정하세요. 시간 초과는 스트리밍 및 비스트리밍 호출에 적용되며 전송 대기를 포함한 전체 시도를 포괄합니다. 전체 에이전트 실행, 함수 도구 실행 또는 재시도 백오프는 제한하지 않습니다.

```python
from agents import Agent, ModelSettings

agent = Agent(
    name="Assistant",
    model_settings=ModelSettings(timeout=30.0),
)
```

시도가 제한 시간을 초과하면 SDK는 시도를 취소하고 정리가 완료될 때까지 기다린 후 [`ModelTimeoutError`][agents.exceptions.ModelTimeoutError]를 발생시킵니다. Runner가 관리하는 재시도를 활성화한 경우 SDK는 `context.normalized.is_timeout`이 `True`로 설정된 시간 초과 실패를 재시도 정책에 전달합니다. 예를 들어 `retry_policies.network_error()`는 해당 분류와 일치합니다. 허용된 각 재시도에는 새로운 시도별 시간 초과가 적용됩니다. SDK는 재시도 전에 일반적인 [재실행 안전 규칙](#safety-boundaries)을 계속 적용합니다.

## Runner 관리형 재시도 {#runner-managed-retries}

재시도는 런타임 전용이며 명시적으로 활성화해야 합니다. `ModelSettings(retry=...)`을 설정하고 재시도 정책이 재시도를 선택하지 않는 한 SDK는 일반 모델 요청을 재시도하지 않습니다.

Responses WebSocket 전송에서 `retry_policies.provider_suggested()`은 응답 전 과부하 프레임과 코드가 없는 `server_error` 프레임을 재시도 제안으로 인식합니다. 이것만으로 재시도가 활성화되지는 않습니다. 여전히 `ModelRetrySettings`이 필요하며 일반적인 재실행 안전 검사가 적용됩니다. 응답 이벤트가 하나라도 이미 도착했다면 SDK는 요청을 재실행하지 않습니다.

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.6-sol",
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

`ModelRetrySettings`에는 세 가지 필드가 있습니다.

<div class="field-table" markdown="1">

| 필드 | 유형 | 참고 |
| --- | --- | --- |
| `max_retries` | `int | None` | 최초 요청 후 허용되는 재시도 횟수 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | 정책이 명시적 지연 시간을 반환하지 않고 재시도할 때의 기본 지연 전략입니다. `backoff.max_delay`는 이렇게 계산된 백오프 지연만 제한합니다. 정책 또는 retry-after 힌트가 반환한 명시적 지연은 제한하지 않습니다. |
| `policy` | `RetryPolicy | None` | 재시도 여부를 결정하는 콜백입니다. 이 필드는 런타임 전용이며 직렬화되지 않습니다. |

</div>

재시도 정책은 다음 항목이 포함된 [`RetryPolicyContext`][agents.retry.RetryPolicyContext]를 받습니다.

- `attempt` 및 `max_retries`: 시도를 고려한 결정에 사용
- `stream`: 스트리밍 및 비스트리밍 동작 분기에 사용
- `error`: raw 검사에 사용
- `normalized`: `status_code`, `retry_after`, `error_code`, `is_network_error`, `is_timeout`, `is_abort` 같은 정보
- `provider_advice`: 내부 모델 어댑터가 재시도 지침을 제공할 수 있는 경우 사용
- `response_started`, `replay_safety`, `stateful_request`: 정책 실행 전에 캡처되는 안정적인 재실행 안전 정보입니다. `replay_safety`은 `"safe"`, `"unsafe"`, `"unknown"` 중 하나이며, 요청이 `previous_response_id` 또는 `conversation_id`를 사용하면 `stateful_request`가 참입니다.

정책은 다음 중 하나를 반환할 수 있습니다.

- 간단한 재시도 결정을 위한 `True` / `False`
- 지연 시간을 재정의하거나 진단 사유를 첨부하거나 범위가 제한된 안전하지 않은 재실행을 명시적으로 승인하려는 경우 [`RetryDecision`][agents.retry.RetryDecision]

SDK는 `retry_policies`에 즉시 사용할 수 있는 헬퍼를 제공합니다.

| 헬퍼 | 동작 |
| --- | --- |
| `retry_policies.never()` | 항상 재시도하지 않습니다. |
| `retry_policies.provider_suggested()` | 가능한 경우 프로바이더의 재시도 권고를 따릅니다. |
| `retry_policies.network_error()` | 일시적인 전송 및 시간 초과 실패와 일치합니다. |
| `retry_policies.http_status([...])` | 선택된 HTTP 상태 코드와 일치합니다. |
| `retry_policies.retry_after()` | retry-after 힌트를 사용할 수 있을 때만 해당 지연 시간을 사용하여 재시도합니다. 이 헬퍼는 retry-after 값을 명시적 정책 지연으로 취급하므로 `backoff.max_delay`가 이를 제한하지 않습니다. |
| `retry_policies.any(...)` | 중첩된 정책 중 하나라도 재시도를 선택하면 재시도합니다. |
| `retry_policies.all(...)` | 모든 중첩 정책이 재시도를 선택할 때만 재시도합니다. |

정책을 조합할 때 `provider_suggested()`은 프로바이더가 거부 및 재실행 안전 승인을 구분할 수 있는 경우 이를 유지하므로 가장 안전한 첫 번째 기본 구성 요소입니다.

##### 안전 경계 {#safety-boundaries}

다음과 같은 일부 실패는 재시도되지 않습니다.

- 중단 오류
- 재실행이 안전하지 않을 수 있는 방식으로 출력이 이미 시작된 스트리밍 실행
- 프로바이더가 별도로 재실행을 안전하다고 표시하지 않은 한, 프로그래매틱 도구 호출 요청을 포함하여 별도의 로컬 부작용 재실행 거부가 있는 요청

프로바이더가 안전하지 않다고 표시한 실패도 기본적으로 차단됩니다. 별도의 로컬 부작용 거부가 없는 비스트리밍 요청에서는 애플리케이션이 `RetryDecision(retry=True, approve_unsafe_replay=True)`을 반환하여 프로바이더 측 재실행 위험을 수용할 수 있습니다. 이 승인을 부여하기 전에 `context.response_started`, `context.replay_safety`, `context.stateful_request`을 확인하고 프로바이더 측 작업을 반복해도 되는 경우에만 승인하세요. 일반적인 `RetryDecision(retry=True)`는 재실행 보호를 우회하지 않으며, `approve_unsafe_replay=True`은 스트리밍 재시도나 로컬 부작용을 승인할 수 없습니다.

`previous_response_id` 또는 `conversation_id`를 사용하는 상태 저장 후속 요청은 재실행 안전성이 불확실하면 차단됩니다. 이러한 요청에서는 `network_error()` 또는 `http_status([500])` 같은 비 프로바이더 조건만으로는 충분하지 않습니다. 일반적으로 `retry_policies.provider_suggested()`을 통해 프로바이더의 재실행 안전 승인을 포함하거나, 위에서 설명한 대로 프로바이더가 안전하지 않다고 표시한 비스트리밍 실패를 명시적으로 승인하세요.

##### Runner 및 에이전트 병합 동작 {#runner-and-agent-merge-behavior}

`retry`은 Runner 수준 및 에이전트 수준 `ModelSettings` 간에 깊은 병합됩니다.

- 에이전트는 `retry.max_retries`만 재정의하면서 Runner의 `policy`를 계속 상속할 수 있습니다.
- 에이전트는 `retry.backoff`의 일부만 재정의하고 Runner의 다른 백오프 필드를 유지할 수 있습니다.
- `policy`는 런타임 전용이므로 직렬화된 `ModelSettings`는 `max_retries` 및 `backoff`을 유지하지만 콜백 자체는 생략합니다.

더 자세한 예제는 [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) 및 [어댑터 기반 재시도 예제](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)를 참조하세요.

## 비 OpenAI 프로바이더 문제 해결 {#troubleshooting-non-openai-providers}

### 트레이싱 클라이언트 오류 401 {#tracing-client-error-401}

트레이싱 관련 오류가 발생하는 이유는 트레이스가 OpenAI 서버에 업로드되는데 OpenAI API 키가 없기 때문입니다. 이 문제를 해결하는 세 가지 방법이 있습니다.

1. 트레이싱 완전히 비활성화: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]
2. 트레이싱용 OpenAI 키 설정: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]. 이 API 키는 트레이스 업로드에만 사용되며 [platform.openai.com](https://platform.openai.com/)에서 발급된 키여야 합니다.
3. 비 OpenAI 트레이스 프로세서 사용. [트레이싱 문서](../tracing.md#custom-tracing-processors)를 참조하세요.

### Responses API 지원 {#responses-api-support}

SDK는 기본적으로 Responses API를 사용하지만 많은 다른 LLM 프로바이더가 아직 이를 지원하지 않습니다. 그 결과 404 또는 유사한 문제가 발생할 수 있습니다. 해결 방법은 두 가지입니다.

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]을 호출합니다. 환경 변수를 통해 `OPENAI_API_KEY` 및 `OPENAI_BASE_URL`을 설정하는 경우 사용할 수 있습니다.
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]을 사용합니다. 예제는 [여기](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)에서 확인할 수 있습니다.

### Chat Completions 호환성 옵션 {#chat-completions-compatibility-options}

Chat Completions를 통해 라우팅할 때 SDK는 `previous_response_id`, `conversation_id`, Responses API의 `prompt` 필드 또는 텍스트 전용이 아닌 도구 출력처럼 Chat Completions가 전송할 수 없는 Responses 전용 필드를 자동으로 삭제하여 호환성을 유지합니다. 개발 중에 이러한 불일치를 즉시 실패로 처리하려면 OpenAI 프로바이더에서 엄격한 기능 검증을 활성화하세요.

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

[`MultiProvider`][agents.MultiProvider]을 사용하는 경우 대신 `openai_strict_feature_validation=True`를 전달하세요.

OpenAI Chat Completions API는 오디오 출력을 반환할 수 있지만 [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]은 현재 오디오 출력을 Agents SDK 실행 항목으로 변환하지 않습니다. 비스트리밍 메시지나 스트리밍 델타에 오디오 출력이 포함된 경우 어댑터는 일부 또는 빈 결과를 반환하는 대신 `AgentsException("Audio is not currently supported")`을 발생시킵니다. SDK가 관리하는 오디오 워크플로에는 [실시간 에이전트](../realtime/guide.md) 또는 [음성 에이전트](../voice/quickstart.md)를 사용하세요.

스트리밍 또는 비스트리밍 Chat Completions 응답이 어시스턴트 텍스트, 도구 호출 또는 거부를 생성하기 전에 `finish_reason="length"`로 종료되면 어댑터는 [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]을 발생시킵니다. SDK는 이 빈 결과를 콘텐츠 정책 거부가 아니라 토큰 또는 추론 예산 소진으로 취급하므로 모델 거부 핸들러가 실행되지 않습니다.

일부 OpenAI 호환 Chat Completions 프로바이더는 증분 SDK 처리에 충분히 안정적이지 않은 청크 단위로 도구 호출 델타를 스트리밍합니다. 이 경우 스트리밍 도구 호출 버퍼링을 활성화하여 프로바이더 스트림이 끝난 후에만 SDK가 도구 호출을 생성하도록 하세요.

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

[`MultiProvider`][agents.MultiProvider]에는 `openai_buffer_streamed_tool_calls=True`를 사용하세요.

### structured outputs 지원 {#structured-outputs-support}

일부 모델 프로바이더는 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs)를 지원하지 않습니다. 이로 인해 다음과 유사한 오류가 발생하기도 합니다.

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

이는 일부 모델 프로바이더의 한계입니다. JSON 출력은 지원하지만 출력에 사용할 `json_schema`을 지정할 수 없습니다. 이 문제를 해결하기 위해 작업 중이지만, JSON 스키마 출력을 지원하는 프로바이더를 사용하는 것이 좋습니다. 그렇지 않으면 잘못된 형식의 JSON 때문에 앱이 자주 중단될 수 있습니다.

## 프로바이더 간 모델 혼합 {#mixing-models-across-providers}

모델 프로바이더 간 기능 차이를 알고 있어야 합니다. 그렇지 않으면 오류가 발생할 수 있습니다. 예를 들어 OpenAI는 structured outputs, 멀티모달 입력, 호스티드 파일 검색 및 웹 검색을 지원하지만 다른 많은 프로바이더는 이러한 기능을 지원하지 않습니다. 다음 제한 사항에 유의하세요.

-   이해하지 못하는 프로바이더에 지원되지 않는 `tools`을 전송하지 마세요.
-   텍스트 전용 모델을 호출하기 전에 멀티모달 입력을 필터링하세요.
-   structured JSON 출력을 지원하지 않는 프로바이더는 가끔 유효하지 않은 JSON을 생성할 수 있다는 점에 유의하세요.

## 서드 파티 어댑터 {#third-party-adapters}

SDK의 기본 제공 프로바이더 통합 지점만으로 충분하지 않을 때만 서드 파티 어댑터를 사용하세요. 이 SDK에서 OpenAI 모델만 사용하는 경우 Any-LLM 또는 LiteLLM 대신 기본 제공 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 경로를 권장합니다. 서드 파티 어댑터는 OpenAI 모델과 비 OpenAI 프로바이더를 결합하거나 어댑터만 제공하는 프로바이더 지원 범위 또는 라우팅이 필요한 경우에 사용합니다. 어댑터는 SDK와 상위 모델 프로바이더 사이에 또 하나의 호환성 계층을 추가하므로 기능 지원과 요청 의미 체계가 프로바이더마다 다를 수 있습니다. SDK에는 현재 Any-LLM 및 LiteLLM이 최선형 베타 어댑터 통합으로 포함되어 있습니다.

### Any-LLM {#any-llm}

Any-LLM 지원은 Any-LLM이 관리하는 프로바이더 지원 범위 또는 라우팅이 필요한 경우를 위해 최선형 베타로 제공됩니다.

상위 프로바이더 경로에 따라 Any-LLM은 Responses API, Chat Completions 호환 API 또는 프로바이더별 호환성 계층을 사용할 수 있습니다.

Any-LLM이 필요한 경우 `openai-agents[any-llm]`를 설치한 다음 [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) 또는 [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py)에서 시작하세요. [`MultiProvider`][agents.MultiProvider]과 함께 `any-llm/...` 모델 이름을 사용하거나, `AnyLLMModel`를 직접 인스턴스화하거나, 실행 범위에서 `AnyLLMProvider`를 사용할 수 있습니다. 모델 인터페이스를 명시적으로 고정해야 하는 경우 `AnyLLMModel`을 생성할 때 `api="responses"` 또는 `api="chat_completions"`을 전달하세요.

Any-LLM Chat Completions 경로에서 [`ModelSettings.extra_body`][agents.model_settings.ModelSettings.extra_body]는 중첩된 `extra_body` 인수로 유지됩니다. Agents SDK는 해당 매핑을 Any-LLM의 최상위 호출 인수에 병합하지 않으므로 프로바이더별 요청 본문 필드는 `extra_body` 매핑 내부에 유지하세요.

Any-LLM은 여전히 서드 파티 어댑터 계층이므로 프로바이더 종속성과 기능 격차는 SDK가 아니라 상위 Any-LLM에서 정의합니다. 상위 프로바이더가 사용량 메트릭을 반환하면 자동으로 전달되지만, 스트리밍 Chat Completions 백엔드에서는 사용량 청크를 생성하기 전에 `ModelSettings(include_usage=True)`가 필요할 수 있습니다. structured outputs, 도구 호출, 사용량 보고 또는 Responses별 동작에 의존한다면 배포하려는 정확한 프로바이더 백엔드를 검증하세요.

### LiteLLM {#litellm}

LiteLLM 지원은 LiteLLM별 프로바이더 지원 범위 또는 라우팅이 필요한 경우를 위해 최선형 베타로 제공됩니다.

LiteLLM이 필요한 경우 `openai-agents[litellm]`을 설치한 다음 [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) 또는 [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py)에서 시작하세요. `litellm/...` 모델 이름을 사용하거나 [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel]을 직접 인스턴스화할 수 있습니다.

LiteLLM 어댑터를 통해 액세스하는 일부 프로바이더는 기본적으로 SDK 사용량 메트릭을 채우지 않습니다. 사용량 보고가 필요한 경우 `ModelSettings(include_usage=True)`을 전달하고, structured outputs, 도구 호출, 사용량 보고 또는 어댑터별 라우팅 동작에 의존한다면 배포하려는 정확한 프로바이더 백엔드를 검증하세요.

LiteLLM이 응답 객체에 대해 Pydantic 직렬화 경고를 생성하는 경우 LiteLLM 어댑터를 가져오기 전에 SDK의 호환성 패치를 활성화할 수 있습니다.

```bash
export OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true
```

이 패치는 기본적으로 비활성화되어 있으며 `1` 또는 `true` 값에 대해서만 활성화됩니다. 비공개 LiteLLM 로깅 헬퍼를 래핑하여 특정 종류의 LiteLLM 응답 직렬화 경고를 억제하므로 일반적인 직렬화 설정이 아니라 제한된 우회 방법으로 취급하세요. 비공개 LiteLLM API에 의존하므로 LiteLLM을 업그레이드할 때 다시 검증하고 상위 라이브러리에서 경고가 더 이상 발생하지 않으면 환경 변수를 제거하세요.