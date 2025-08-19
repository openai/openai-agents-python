---
search:
  exclude: true
---
# SDK 구성

## API 키와 클라이언트

기본적으로 SDK 는 가져오자마자 LLM 요청과 트레이싱을 위해 `OPENAI_API_KEY` 환경 변수를 찾습니다. 앱 시작 전에 해당 환경 변수를 설정할 수 없다면 [set_default_openai_key()][agents.set_default_openai_key] 함수를 사용해 키를 설정할 수 있습니다.

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

또는 사용할 OpenAI 클라이언트를 구성할 수도 있습니다. 기본적으로 SDK 는 환경 변수의 API 키 또는 위에서 설정한 기본 키를 사용하여 `AsyncOpenAI` 인스턴스를 생성합니다. [set_default_openai_client()][agents.set_default_openai_client] 함수를 사용해 이를 변경할 수 있습니다.

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

마지막으로, 사용되는 OpenAI API 를 커스터마이즈할 수도 있습니다. 기본값으로는 OpenAI Responses API 를 사용합니다. [set_default_openai_api()][agents.set_default_openai_api] 함수를 사용하여 Chat Completions API 를 사용하도록 재정의할 수 있습니다.

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## 트레이싱

트레이싱은 기본적으로 활성화되어 있습니다. 기본적으로 위 섹션의 OpenAI API 키(즉, 환경 변수 또는 설정한 기본 키)를 사용합니다. [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 함수를 사용하여 트레이싱에 사용되는 API 키를 별도로 설정할 수 있습니다.

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

[`set_tracing_disabled()`][agents.set_tracing_disabled] 함수를 사용해 트레이싱을 완전히 비활성화할 수도 있습니다.

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## 디버그 로깅

SDK 에는 핸들러가 설정되지 않은 두 개의 Python 로거가 있습니다. 기본적으로 이는 경고와 오류는 `stdout` 으로 전송되지만, 그 외 로그는 억제된다는 의미입니다.

자세한 로깅을 활성화하려면 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 함수를 사용하세요.

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

또는 핸들러, 필터, 포매터 등을 추가하여 로그를 커스터마이즈할 수 있습니다. 자세한 내용은 [Python logging guide](https://docs.python.org/3/howto/logging.html)에서 확인하세요.

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