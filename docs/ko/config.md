---
search:
  exclude: true
---
# SDK 구성

## API 키 및 클라이언트

기본적으로 SDK 는 가져오는 즉시 LLM 요청과 트레이싱을 위해 `OPENAI_API_KEY` 환경 변수를 찾습니다. 앱이 시작되기 전에 해당 환경 변수를 설정할 수 없는 경우, [set_default_openai_key()][agents.set_default_openai_key] 함수를 사용해 키를 설정할 수 있습니다.

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

또는 사용할 OpenAI 클라이언트를 구성할 수도 있습니다. 기본적으로 SDK 는 환경 변수의 API 키 또는 위에서 설정한 기본 키를 사용해 `AsyncOpenAI` 인스턴스를 생성합니다. [set_default_openai_client()][agents.set_default_openai_client] 함수를 사용해 이를 변경할 수 있습니다.

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

마지막으로, 사용되는 OpenAI API 도 커스터마이즈할 수 있습니다. 기본적으로는 OpenAI Responses API 를 사용합니다. [set_default_openai_api()][agents.set_default_openai_api] 함수를 사용해 이를 재정의하여 Chat Completions API 를 사용하도록 할 수 있습니다.

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## 트레이싱

트레이싱은 기본적으로 활성화되어 있습니다. 기본적으로 위 섹션의 OpenAI API 키(즉, 환경 변수 또는 설정한 기본 키)를 사용합니다. [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 함수를 사용해 트레이싱에 사용되는 API 키를 별도로 설정할 수 있습니다.

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

기본 exporter 를 사용할 때 트레이스를 특정 조직 또는 프로젝트로 귀속시키려면, 앱이 시작되기 전에 다음 환경 변수를 설정하세요.

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

전역 exporter 를 변경하지 않고도 실행(run) 단위로 트레이싱 API 키를 설정할 수도 있습니다.

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

## 디버그 로깅

SDK 에는 핸들러가 설정되지 않은 두 개의 Python 로거가 있습니다. 기본적으로 경고와 오류는 `stdout` 으로 전송되지만, 다른 로그는 억제됩니다.

자세한 로깅을 활성화하려면 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 함수를 사용하세요.

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

또는 핸들러, 필터, 포매터 등을 추가해 로그를 커스터마이즈할 수 있습니다. 자세한 내용은 [Python 로깅 가이드](https://docs.python.org/3/howto/logging.html)를 참고하세요.

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

일부 로그에는 민감한 데이터(예: 사용자 데이터)가 포함될 수 있습니다. 이 데이터가 로깅되지 않도록 하려면 다음 환경 변수를 설정하세요.

LLM 입력과 출력을 로깅하지 않으려면:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

도구 입력과 출력을 로깅하지 않으려면:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```