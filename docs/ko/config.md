---
search:
  exclude: true
---
# 구성

이 페이지에서는 기본 OpenAI 키 또는 클라이언트, 기본 OpenAI API 형식, 트레이싱 내보내기 기본값, 로깅 동작처럼 애플리케이션 시작 시 일반적으로 한 번 설정하는 SDK 전역 기본값을 다룹니다.

이러한 기본값은 샌드박스 기반 워크플로에도 적용되지만, 샌드박스 워크스페이스, 샌드박스 클라이언트, 세션 재사용은 별도로 구성합니다.

대신 특정 에이전트나 실행을 구성해야 한다면 다음 문서부터 확인하세요.

- [에이전트](agents.md): 일반 `Agent`의 instructions, tools, 출력 유형, 핸드오프, 가드레일
- [에이전트 실행](running_agents.md): `RunConfig`, 세션, 대화 상태 옵션
- [샌드박스 에이전트](sandbox/guide.md): `SandboxRunConfig`, 매니페스트, 기능, 샌드박스 클라이언트별 워크스페이스 설정
- [모델](models/index.md): 모델 선택 및 공급자 구성
- [트레이싱](tracing.md): 실행별 트레이싱 메타데이터 및 맞춤형 트레이스 프로세서

## 구성 객체와 딕셔너리

SDK에서 정의한 구성 매개변수는 일반적으로 형식이 지정된 설정 객체 또는 동일한 필드를 포함하는 딕셔너리를 허용합니다. 이는 형식 어노테이션에 딕셔너리가 포함된 에이전트, 실행, 모델, 세션, 샌드박스, 음성 구성 경계 전반에 적용됩니다. SDK에서 정의한 중첩 설정 유형에도 딕셔너리를 사용할 수 있습니다.

```python
from agents import Agent

agent = Agent(
    name="Assistant",
    model="gpt-5.6-sol",
    model_settings={
        "reasoning": {"effort": "high"},
        "verbosity": "low",
    },
)
```

SDK는 이러한 딕셔너리를 해당 설정 객체로 정규화합니다. SDK에서 정의한 데이터 클래스 구성 유형에 알 수 없는 필드가 있으면 `TypeError`가 발생하므로, 옵션 이름의 오타를 조기에 발견하는 데 도움이 됩니다. 특정 경계에서 딕셔너리를 허용하는지 확인하려면 해당 매개변수의 형식 어노테이션 또는 API 레퍼런스를 확인하세요.

## API 키와 클라이언트

기본적으로 SDK는 LLM 요청과 트레이싱에 `OPENAI_API_KEY` 환경 변수를 사용합니다. SDK가 OpenAI 클라이언트를 처음 생성할 때 키가 확인되므로(지연 초기화), 첫 모델 호출 전에 환경 변수를 설정하세요. 앱 시작 전에 해당 환경 변수를 설정할 수 없다면 [set_default_openai_key()][agents.set_default_openai_key] 함수를 사용하여 키를 설정할 수 있습니다.

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

또는 사용할 OpenAI 클라이언트를 구성할 수도 있습니다. 기본적으로 SDK는 환경 변수의 API 키 또는 위에서 설정한 기본 키를 사용하여 `AsyncOpenAI` 인스턴스를 생성합니다. [set_default_openai_client()][agents.set_default_openai_client] 함수를 사용하여 이를 변경할 수 있습니다.

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

### `openai` v3 기반 맞춤형 HTTP 클라이언트

버전 0.21.0에는 `openai>=3.0.0,<4`이 필요합니다. 기본 OpenAI 공급자는 HTTPX2를 사용하므로 대부분의 애플리케이션에서는 HTTP 클라이언트를 직접 구성할 필요가 없습니다. 애플리케이션이 `AsyncOpenAI`에 `http_client=`을 전달하는 경우, 맞춤형 클라이언트와 전송 관련 옵션에 HTTPX2 유형을 사용하세요.

```python
import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpx2Client

from agents import set_default_openai_client

http_client = DefaultAsyncHttpx2Client(
    timeout=httpx2.Timeout(30.0, connect=5.0),
)
custom_client = AsyncOpenAI(
    api_key="...",
    http_client=http_client,
)
set_default_openai_client(custom_client)
```

동일한 마이그레이션이 맞춤형 전송, 인증, 이벤트 훅, 모의 전송, URL, 요청, 응답, 전송 예외 처리에도 적용됩니다. 각각에 해당하는 `httpx2`을 사용하세요. Agents SDK는 임의의 레거시 `httpx` 객체를 HTTPX2로 변환하지 않습니다. 애플리케이션에서 `httpx`을 명시적으로 설치하면 OpenAI Python SDK가 레거시 클라이언트를 위한 임시 호환성 경로를 제공하지만, 새 코드와 마이그레이션된 코드는 HTTPX2를 사용해야 합니다.

이 OpenAI 클라이언트 경계는 로컬 MCP 전송 맞춤 설정과 별개입니다. MCP Python SDK v1은 자체 레거시 `httpx` 종속성을 사용하고, MCP Python SDK v2는 `httpx2`을 사용합니다. 자세한 내용은 [MCP Python SDK v1 및 v2](mcp.md#mcp-python-sdk-v1-and-v2)를 참조하세요.

환경 기반 엔드포인트 구성을 선호하는 경우 기본 OpenAI 공급자는 `OPENAI_BASE_URL`도 읽습니다. Responses 웹소켓 전송을 활성화하면 웹소켓 `/responses` 엔드포인트에 사용할 `OPENAI_WEBSOCKET_BASE_URL`도 읽습니다.

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint.example/v1"
export OPENAI_WEBSOCKET_BASE_URL="wss://your-openai-compatible-endpoint.example/v1"
```

마지막으로 사용할 OpenAI API도 맞춤 설정할 수 있습니다. 기본적으로 OpenAI Responses API를 사용합니다. [set_default_openai_api()][agents.set_default_openai_api] 함수를 사용하여 Chat Completions API를 사용하도록 재정의할 수 있습니다.

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## OpenAI 공급자 기본값

SDK의 OpenAI 백엔드를 사용하는 공급자는 모델 이름 문자열을 모델에 매핑할 때 SDK 전역 기본값도 읽습니다. OpenAI Responses 모델에서 기본적으로 웹소켓 전송을 사용하도록 하려면 [`set_default_openai_responses_transport()`][agents.set_default_openai_responses_transport]을 사용하세요.

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

이는 기본 OpenAI 공급자가 모델 이름을 확인하여 생성한 OpenAI Responses 모델에 영향을 줍니다. 공급자 수준 설정, 연결 재사용, keepalive 옵션, 맞춤형 웹소켓 엔드포인트에 관한 자세한 내용은 [Responses WebSocket 전송](models/index.md#responses-websocket-transport)을 참조하세요.

OpenAI 설정에서 공급자 수준 에이전트 등록 메타데이터가 필요한 경우 시작 시 기본 하네스 ID를 한 번 구성하세요.

```python
from agents import set_default_openai_harness

set_default_openai_harness("your-harness-id")
```

전체 등록 객체를 전달할 수도 있습니다.

```python
from agents import OpenAIAgentRegistrationConfig, set_default_openai_agent_registration

set_default_openai_agent_registration(
    OpenAIAgentRegistrationConfig(harness_id="your-harness-id")
)
```

SDK 기본값을 설정하지 않으면 SDK의 OpenAI 백엔드를 사용하는 공급자는 `OPENAI_AGENT_HARNESS_ID` 환경 변수를 대신 사용합니다. 하네스 ID가 구성된 경우 `RunConfig.trace_metadata`에 해당 키가 아직 없으면 SDK가 이를 `agent_harness_id`으로 트레이스 메타데이터에 추가합니다.

## 트레이싱

트레이싱은 기본적으로 활성화되어 있습니다. 기본적으로 위 섹션의 모델 요청과 동일한 OpenAI API 키, 즉 환경 변수 또는 설정한 기본 키를 사용합니다. 트레이싱에 사용할 API 키를 별도로 설정하려면 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 함수를 사용하세요.

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

모델 트래픽에는 한 키 또는 클라이언트를 사용하지만 트레이싱에는 다른 OpenAI 키를 사용해야 하는 경우, 기본 키 또는 클라이언트를 설정할 때 `use_for_tracing=False`을 전달한 다음 트레이싱을 별도로 구성하세요. 맞춤형 클라이언트를 사용하지 않는다면 [`set_default_openai_key()`][agents.set_default_openai_key]에도 같은 방식을 사용할 수 있습니다.

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

기본 내보내기를 사용할 때 트레이스를 특정 조직이나 프로젝트에 귀속해야 한다면 앱 시작 전에 다음 환경 변수를 설정하세요.

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

전역 내보내기를 변경하지 않고 실행별 트레이싱 API 키를 설정할 수도 있습니다.

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

트레이싱을 활성화된 상태로 유지하되 민감할 수 있는 입력과 출력을 트레이스 페이로드에서 제외하려면 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]을 `False`로 설정하세요.

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

앱 시작 전에 다음 환경 변수를 설정하여 코드 없이 기본값을 변경할 수도 있습니다.

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

전체 트레이싱 제어 옵션은 [트레이싱 가이드](tracing.md)를 참조하세요.

## 디버그 로깅

SDK는 두 개의 Python 로거(`openai.agents` 및 `openai.agents.tracing`)를 정의하며 기본적으로 핸들러를 연결하지 않습니다. 로그는 애플리케이션의 Python 로깅 구성을 따릅니다.

상세 로깅을 활성화하려면 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 함수를 사용하세요.

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

또는 핸들러, 필터, 포매터 등을 추가하여 로그를 맞춤 설정할 수 있습니다. 자세한 내용은 [Python 로깅 가이드](https://docs.python.org/3/howto/logging.html)를 참조하세요.

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

### 로그 및 진단의 민감한 데이터

일부 로그와 진단 예외에는 민감한 데이터가 포함될 수 있습니다(예: 모델 또는 도구의 입력과 출력).

기본적으로 SDK는 LLM 입력/출력이나 도구 입력/출력을 로그에 기록하지 **않습니다**. 이러한 보호 기능은 다음 항목으로 제어합니다.

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

디버깅을 위해 이 데이터를 일시적으로 포함해야 한다면 앱 시작 전에 두 변수 중 하나를 `0`(또는 `false`)로 설정하세요.

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```

이 플래그는 영향을 받는 실패가 페이로드를 포함한 진단 세부 정보를 유지할지 여부도 제어합니다. 예를 들어 도구 데이터 마스킹이 활성화된 경우 `FunctionTool`에 대한 잘못된 인수는 내부 검증 오류를 예외 체인으로 연결하지 않고 일반적인 `ModelBehaviorError`을 발생시킵니다. 두 변수 중 하나를 `0`로 설정하면 로그, 예외 메시지, 예외 체인, 기타 진단 컨텍스트에 가공되지 않은 모델 또는 도구 데이터가 노출될 수 있으므로 통제된 개발 환경에서만 활성화하세요.