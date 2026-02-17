---
search:
  exclude: true
---
# SDK 구성

## API 키 및 클라이언트

기본적으로 SDK는 LLM 요청과 트레이싱에 `OPENAI_API_KEY` 환경 변수를 사용합니다. 키는 SDK가 처음 OpenAI 클라이언트를 생성할 때(지연 초기화) 해석되므로, 첫 모델 호출 전에 환경 변수를 설정하세요. 앱 시작 전에 해당 환경 변수를 설정할 수 없는 경우, [set_default_openai_key()][agents.set_default_openai_key] 함수를 사용해 키를 설정할 수 있습니다.

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

대안으로, 사용할 OpenAI 클라이언트를 구성할 수도 있습니다. 기본적으로 SDK는 환경 변수의 API 키 또는 위에서 설정한 기본 키를 사용해 `AsyncOpenAI` 인스턴스를 생성합니다. [set_default_openai_client()][agents.set_default_openai_client] 함수를 사용해 이를 변경할 수 있습니다.

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

마지막으로, 사용되는 OpenAI API도 커스터마이즈할 수 있습니다. 기본적으로 OpenAI Responses API를 사용합니다. [set_default_openai_api()][agents.set_default_openai_api] 함수를 사용해 이를 Chat Completions API로 재정의할 수 있습니다.

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## 트레이싱

트레이싱은 기본적으로 활성화되어 있습니다. 기본적으로 위 섹션의 OpenAI API 키(즉, 환경 변수 또는 설정한 기본 키)를 사용합니다. [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 함수를 사용해 트레이싱에 사용되는 API 키를 명시적으로 설정할 수 있습니다.

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

기본 exporter를 사용할 때 트레이스를 특정 조직 또는 프로젝트에 귀속시켜야 한다면, 앱 시작 전에 다음 환경 변수를 설정하세요:

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

전역 exporter를 변경하지 않고도 실행(run)별로 트레이싱 API 키를 설정할 수 있습니다.

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

[`set_tracing_disabled()`][agents.set_tracing_disabled] 함수를 사용해 트레이싱을 완전히 비활성화할 수도 있습니다.

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

트레이싱은 활성화된 상태로 유지하되 잠재적으로 민감한 입력/출력을 트레이스 페이로드에서 제외하려면, [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]를 `False`로 설정하세요:

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

전체 트레이싱 제어에 대해서는 [tracing 가이드](tracing.md)를 참고하세요.

## 디버그 로깅

SDK는 두 개의 Python 로거(`openai.agents`, `openai.agents.tracing`)를 정의하며 기본적으로 핸들러를 연결하지 않습니다. 로그는 애플리케이션의 Python 로깅 구성 설정을 따릅니다.

상세(Verbose) 로깅을 활성화하려면 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 함수를 사용하세요.

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

대안으로, 핸들러, 필터, 포매터 등을 추가해 로그를 커스터마이즈할 수도 있습니다. 자세한 내용은 [Python 로깅 가이드](https://docs.python.org/3/howto/logging.html)를 참고하세요.

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

### 로그의 민감 데이터

일부 로그에는 민감한 데이터(예: 사용자 데이터)가 포함될 수 있습니다.

기본적으로 SDK는 LLM 입력/출력 또는 도구 입력/출력을 **로깅하지 않습니다**. 이러한 보호는 다음으로 제어됩니다:

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

디버깅을 위해 일시적으로 이 데이터를 포함해야 한다면, 앱 시작 전에 두 변수 중 하나를 `0`(또는 `false`)으로 설정하세요:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```