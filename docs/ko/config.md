---
search:
  exclude: true
---
# 구성

이 페이지에서는 애플리케이션 시작 시 일반적으로 한 번 설정하는 SDK 전체 기본값을 다룹니다. 예를 들어 기본 OpenAI 키 또는 클라이언트, 기본 OpenAI API 형태, 트레이싱 내보내기 기본값, 로깅 동작 등이 있습니다.

이러한 기본값은 샌드박스 기반 워크플로에도 계속 적용되지만, 샌드박스 워크스페이스, 샌드박스 클라이언트, 세션 재사용은 별도로 구성합니다.

대신 특정 에이전트 또는 실행을 구성해야 한다면 다음부터 시작하세요:

-   [에이전트](agents.md): 일반 `Agent`의 instructions, tools, 출력 유형, 핸드오프, 가드레일
-   [에이전트 실행](running_agents.md): `RunConfig`, 세션, 대화 상태 옵션
-   [샌드박스 에이전트](sandbox/guide.md): `SandboxRunConfig`, 매니페스트, 기능, 샌드박스 클라이언트별 워크스페이스 설정
-   [모델](models/index.md): 모델 선택 및 제공자 구성
-   [트레이싱](tracing.md): 실행별 트레이싱 메타데이터 및 사용자 지정 트레이스 프로세서

## API 키 및 클라이언트

기본적으로 SDK는 LLM 요청과 트레이싱에 `OPENAI_API_KEY` 환경 변수를 사용합니다. 키는 SDK가 처음으로 OpenAI 클라이언트를 생성할 때 확인됩니다(지연 초기화). 따라서 첫 모델 호출 전에 환경 변수를 설정하세요. 앱 시작 전에 해당 환경 변수를 설정할 수 없다면 [set_default_openai_key()][agents.set_default_openai_key] 함수를 사용해 키를 설정할 수 있습니다.

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

또는 사용할 OpenAI 클라이언트를 구성할 수도 있습니다. 기본적으로 SDK는 환경 변수의 API 키나 위에서 설정한 기본 키를 사용하여 `AsyncOpenAI` 인스턴스를 생성합니다. [set_default_openai_client()][agents.set_default_openai_client] 함수를 사용해 이를 변경할 수 있습니다.

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

환경 변수 기반 엔드포인트 구성을 선호한다면, 기본 OpenAI 제공자는 `OPENAI_BASE_URL`도 읽습니다. Responses WebSocket 전송을 활성화하면 WebSocket `/responses` 엔드포인트용 `OPENAI_WEBSOCKET_BASE_URL`도 읽습니다.

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint.example/v1"
export OPENAI_WEBSOCKET_BASE_URL="wss://your-openai-compatible-endpoint.example/v1"
```

마지막으로 사용할 OpenAI API를 사용자 지정할 수도 있습니다. 기본적으로 OpenAI Responses API를 사용합니다. [set_default_openai_api()][agents.set_default_openai_api] 함수를 사용해 이를 재정의하여 Chat Completions API를 사용할 수 있습니다.

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## OpenAI 제공자 기본값

OpenAI 기반 제공자는 모델 이름을 확인할 때도 SDK 전체 기본값을 읽습니다. OpenAI Responses 모델이 기본적으로 WebSocket 전송을 사용하도록 하려면 [`set_default_openai_responses_transport()`][agents.set_default_openai_responses_transport]를 사용하세요:

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

이는 기본 OpenAI 제공자가 확인한 OpenAI Responses 모델에 영향을 줍니다. 제공자 수준 설정, 연결 재사용, keepalive 옵션, 사용자 지정 WebSocket 엔드포인트는 [Responses WebSocket 전송](models/index.md#responses-websocket-transport)을 참조하세요.

OpenAI 설정에서 제공자 수준 에이전트 등록 메타데이터를 기대하는 경우, 시작 시 기본 harness ID를 한 번 구성하세요:

```python
from agents import set_default_openai_harness

set_default_openai_harness("your-harness-id")
```

전체 등록 객체를 전달할 수도 있습니다:

```python
from agents import OpenAIAgentRegistrationConfig, set_default_openai_agent_registration

set_default_openai_agent_registration(
    OpenAIAgentRegistrationConfig(harness_id="your-harness-id")
)
```

SDK 기본값이 설정되어 있지 않으면 OpenAI 기반 제공자는 `OPENAI_AGENT_HARNESS_ID` 환경 변수로 폴백합니다. harness ID가 구성되어 있으면, SDK는 `RunConfig.trace_metadata`에 해당 키가 이미 있는 경우를 제외하고 이를 `agent_harness_id`로 트레이스 메타데이터에 추가합니다.

## 트레이싱

트레이싱은 기본적으로 활성화되어 있습니다. 기본적으로 위 섹션의 모델 요청과 동일한 OpenAI API 키(즉, 환경 변수 또는 설정한 기본 키)를 사용합니다. [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 함수를 사용하여 트레이싱에 사용할 API 키를 별도로 설정할 수 있습니다.

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

모델 트래픽에는 한 키나 클라이언트를 사용하지만 트레이싱에는 다른 OpenAI 키를 사용해야 한다면, 기본 키나 클라이언트를 설정할 때 `use_for_tracing=False`를 전달한 다음 트레이싱을 별도로 구성하세요. 사용자 지정 클라이언트를 사용하지 않는 경우 [`set_default_openai_key()`][agents.set_default_openai_key]에서도 동일한 패턴을 사용할 수 있습니다.

```python
from openai import AsyncOpenAI
from agents import (
    set_default_openai_client,
    set_tracing_export_api_key,
)

custom_client = AsyncOpenAI(base_url="https://your-openai-compatible-endpoint.example/v1", api_key="provider-key")
set_default_openai_client(custom_client, use_for_tracing=False)

set_tracing_export_api_key("sk-tracing")
```

기본 익스포터를 사용할 때 트레이스를 특정 조직 또는 프로젝트에 귀속해야 한다면 앱 시작 전에 다음 환경 변수를 설정하세요:

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

전역 익스포터를 변경하지 않고 실행별로 트레이싱 API 키를 설정할 수도 있습니다.

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

[`set_tracing_disabled()`][agents.set_tracing_disabled] 함수를 사용하여 트레이싱을 완전히 비활성화할 수도 있습니다.

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

트레이싱은 활성화한 상태로 유지하되 트레이스 페이로드에서 민감할 수 있는 입력/출력을 제외하려면 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]를 `False`로 설정하세요:

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

앱 시작 전에 다음 환경 변수를 설정하면 코드 없이도 기본값을 변경할 수 있습니다:

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

전체 트레이싱 제어 옵션은 [트레이싱 가이드](tracing.md)를 참조하세요.

## 디버그 로깅

SDK는 두 개의 Python 로거(`openai.agents` 및 `openai.agents.tracing`)를 정의하며, 기본적으로 핸들러를 연결하지 않습니다. 로그는 애플리케이션의 Python 로깅 구성을 따릅니다.

상세 로깅을 활성화하려면 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 함수를 사용하세요.

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

또는 핸들러, 필터, 포매터 등을 추가하여 로그를 사용자 지정할 수 있습니다. 자세한 내용은 [Python 로깅 가이드](https://docs.python.org/3/howto/logging.html)를 참조하세요.

```python
import logging

logger = logging.getLogger("openai.agents") # or openai.agents.tracing for the Tracing logger

# To make all logs show up
logger.setLevel(logging.DEBUG)
# To make info and above show up
logger.setLevel(logging.INFO)
# To make warning and above show up
logger.setLevel(logging.WARNING)
# etc

# You can customize this as needed, but this will output to `stderr` by default
logger.addHandler(logging.StreamHandler())
```

### 로그의 민감한 데이터

일부 로그에는 민감한 데이터(예: 사용자 데이터)가 포함될 수 있습니다.

기본적으로 SDK는 LLM 입력/출력이나 도구 입력/출력을 로그로 기록하지 **않습니다**. 이러한 보호 기능은 다음으로 제어됩니다:

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

디버깅을 위해 이 데이터를 일시적으로 포함해야 한다면, 앱 시작 전에 둘 중 하나의 변수를 `0`(또는 `false`)으로 설정하세요:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```