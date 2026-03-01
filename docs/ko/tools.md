---
search:
  exclude: true
---
# 도구

도구는 에이전트가 동작을 수행하게 합니다: 데이터 가져오기, 코드 실행, 외부 API 호출, 심지어 컴퓨터 사용 같은 작업입니다. SDK 는 다섯 가지 카테고리를 지원합니다:

-   OpenAI 호스팅 도구: OpenAI 서버에서 모델과 함께 실행됩니다
-   로컬/런타임 실행 도구: `ComputerTool` 및 `ApplyPatchTool` 는 항상 사용자 환경에서 실행되며, `ShellTool` 은 로컬 또는 호스팅 컨테이너에서 실행될 수 있습니다
-   함수 호출: 어떤 Python 함수든 도구로 래핑합니다
-   Agents as tools: 전체 핸드오프 없이 에이전트를 호출 가능한 도구로 노출합니다
-   실험적: Codex 도구: 도구 호출에서 워크스페이스 범위 Codex 작업을 실행합니다

## 도구 유형 선택

이 페이지를 카탈로그로 사용한 다음, 제어하는 런타임에 맞는 섹션으로 이동하세요.

| 원하는 작업 | 시작 위치 |
| --- | --- |
| OpenAI 관리형 도구 사용 (웹 검색, 파일 검색, 코드 인터프리터, 호스티드 MCP, 이미지 생성) | [호스티드 도구](#hosted-tools) |
| 사용자 프로세스 또는 환경에서 도구 실행 | [로컬 런타임 도구](#local-runtime-tools) |
| Python 함수를 도구로 래핑 | [함수 도구](#function-tools) |
| 핸드오프 없이 한 에이전트가 다른 에이전트를 호출 | [Agents as tools](#agents-as-tools) |
| 에이전트에서 워크스페이스 범위 Codex 작업 실행 | [실험적: Codex 도구](#experimental-codex-tool) |

## 호스티드 도구

OpenAI 는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 사용 시 몇 가지 내장 도구를 제공합니다:

-   [`WebSearchTool`][agents.tool.WebSearchTool] 은 에이전트가 웹을 검색할 수 있게 합니다
-   [`FileSearchTool`][agents.tool.FileSearchTool] 은 OpenAI 벡터 스토어에서 정보를 검색할 수 있게 합니다
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 은 LLM 이 샌드박스 환경에서 코드를 실행할 수 있게 합니다
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 은 원격 MCP 서버의 도구를 모델에 노출합니다
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] 은 프롬프트로 이미지를 생성합니다

고급 호스티드 검색 옵션:

-   `FileSearchTool` 은 `vector_store_ids` 및 `max_num_results` 외에 `filters`, `ranking_options`, `include_search_results` 를 지원합니다
-   `WebSearchTool` 은 `filters`, `user_location`, `search_context_size` 를 지원합니다

```python
from agents import Agent, FileSearchTool, Runner, WebSearchTool

agent = Agent(
    name="Assistant",
    tools=[
        WebSearchTool(),
        FileSearchTool(
            max_num_results=3,
            vector_store_ids=["VECTOR_STORE_ID"],
        ),
    ],
)

async def main():
    result = await Runner.run(agent, "Which coffee shop should I go to, taking into account my preferences and the weather today in SF?")
    print(result.final_output)
```

### 호스티드 컨테이너 셸 + 스킬

`ShellTool` 은 OpenAI 호스팅 컨테이너 실행도 지원합니다. 로컬 런타임 대신 관리형 컨테이너에서 모델이 셸 명령을 실행하도록 하려면 이 모드를 사용하세요.

```python
from agents import Agent, Runner, ShellTool, ShellToolSkillReference

csv_skill: ShellToolSkillReference = {
    "type": "skill_reference",
    "skill_id": "skill_698bbe879adc81918725cbc69dcae7960bc5613dadaed377",
    "version": "1",
}

agent = Agent(
    name="Container shell agent",
    model="gpt-5.2",
    instructions="Use the mounted skill when helpful.",
    tools=[
        ShellTool(
            environment={
                "type": "container_auto",
                "network_policy": {"type": "disabled"},
                "skills": [csv_skill],
            }
        )
    ],
)

result = await Runner.run(
    agent,
    "Use the configured skill to analyze CSV files in /mnt/data and summarize totals by region.",
)
print(result.final_output)
```

이후 실행에서 기존 컨테이너를 재사용하려면 `environment={"type": "container_reference", "container_id": "cntr_..."}` 를 설정하세요.

알아둘 점:

-   호스티드 셸은 Responses API 셸 도구를 통해 사용할 수 있습니다
-   `container_auto` 는 요청을 위해 컨테이너를 프로비저닝하고, `container_reference` 는 기존 컨테이너를 재사용합니다
-   `container_auto` 에는 `file_ids` 와 `memory_limit` 도 포함할 수 있습니다
-   `environment.skills` 는 스킬 참조와 인라인 스킬 번들을 받습니다
-   호스티드 환경에서는 `ShellTool` 에 `executor`, `needs_approval`, `on_approval` 를 설정하지 마세요
-   `network_policy` 는 `disabled` 및 `allowlist` 모드를 지원합니다
-   allowlist 모드에서는 `network_policy.domain_secrets` 로 도메인 범위 시크릿을 이름으로 주입할 수 있습니다
-   전체 코드 예제는 `examples/tools/container_shell_skill_reference.py` 및 `examples/tools/container_shell_inline_skill.py` 를 참고하세요
-   OpenAI 플랫폼 가이드: [Shell](https://platform.openai.com/docs/guides/tools-shell) 및 [Skills](https://platform.openai.com/docs/guides/tools-skills)

## 로컬 런타임 도구

로컬 런타임 도구는 모델 응답 자체 밖에서 실행됩니다. 모델이 호출 시점을 결정하는 것은 동일하지만, 실제 작업은 사용자 애플리케이션 또는 구성된 실행 환경이 수행합니다.

`ComputerTool` 및 `ApplyPatchTool` 는 항상 사용자가 제공하는 로컬 구현이 필요합니다. `ShellTool` 은 두 모드를 모두 포괄합니다: 관리형 실행을 원하면 위의 호스티드 컨테이너 구성을, 사용자 프로세스에서 명령을 실행하려면 아래 로컬 런타임 구성을 사용하세요.

로컬 런타임 도구는 구현을 제공해야 합니다:

-   [`ComputerTool`][agents.tool.ComputerTool]: GUI/브라우저 자동화를 위해 [`Computer`][agents.computer.Computer] 또는 [`AsyncComputer`][agents.computer.AsyncComputer] 인터페이스를 구현합니다
-   [`ShellTool`][agents.tool.ShellTool]: 로컬 실행과 호스티드 컨테이너 실행 모두를 위한 최신 셸 도구입니다
-   [`LocalShellTool`][agents.tool.LocalShellTool]: 레거시 로컬 셸 통합입니다
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]: 로컬에서 diff 를 적용하려면 [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] 를 구현합니다
-   로컬 셸 스킬은 `ShellTool(environment={"type": "local", "skills": [...]})` 로 사용할 수 있습니다

```python
from agents import Agent, ApplyPatchTool, ShellTool
from agents.computer import AsyncComputer
from agents.editor import ApplyPatchResult, ApplyPatchOperation, ApplyPatchEditor


class NoopComputer(AsyncComputer):
    environment = "browser"
    dimensions = (1024, 768)
    async def screenshot(self): return ""
    async def click(self, x, y, button): ...
    async def double_click(self, x, y): ...
    async def scroll(self, x, y, scroll_x, scroll_y): ...
    async def type(self, text): ...
    async def wait(self): ...
    async def move(self, x, y): ...
    async def keypress(self, keys): ...
    async def drag(self, path): ...


class NoopEditor(ApplyPatchEditor):
    async def create_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")
    async def update_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")
    async def delete_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")


async def run_shell(request):
    return "shell output"


agent = Agent(
    name="Local tools agent",
    tools=[
        ShellTool(executor=run_shell),
        ApplyPatchTool(editor=NoopEditor()),
        # ComputerTool expects a Computer/AsyncComputer implementation; omitted here for brevity.
    ],
)
```

## 함수 도구

어떤 Python 함수든 도구로 사용할 수 있습니다. Agents SDK 가 도구를 자동으로 설정합니다:

-   도구 이름은 Python 함수 이름이 됩니다(또는 이름을 제공할 수 있습니다)
-   도구 설명은 함수의 docstring 에서 가져옵니다(또는 설명을 제공할 수 있습니다)
-   함수 입력용 스키마는 함수 인자에서 자동 생성됩니다
-   각 입력의 설명은 비활성화하지 않는 한 함수의 docstring 에서 가져옵니다

함수 시그니처 추출에는 Python 의 `inspect` 모듈을 사용하고, docstring 파싱에는 [`griffe`](https://mkdocstrings.github.io/griffe/), 스키마 생성에는 `pydantic` 을 사용합니다.

```python
import json

from typing_extensions import TypedDict, Any

from agents import Agent, FunctionTool, RunContextWrapper, function_tool


class Location(TypedDict):
    lat: float
    long: float

@function_tool  # (1)!
async def fetch_weather(location: Location) -> str:
    # (2)!
    """Fetch the weather for a given location.

    Args:
        location: The location to fetch the weather for.
    """
    # In real life, we'd fetch the weather from a weather API
    return "sunny"


@function_tool(name_override="fetch_data")  # (3)!
def read_file(ctx: RunContextWrapper[Any], path: str, directory: str | None = None) -> str:
    """Read the contents of a file.

    Args:
        path: The path to the file to read.
        directory: The directory to read the file from.
    """
    # In real life, we'd read the file from the file system
    return "<file contents>"


agent = Agent(
    name="Assistant",
    tools=[fetch_weather, read_file],  # (4)!
)

for tool in agent.tools:
    if isinstance(tool, FunctionTool):
        print(tool.name)
        print(tool.description)
        print(json.dumps(tool.params_json_schema, indent=2))
        print()

```

1.  함수 인자로 어떤 Python 타입이든 사용할 수 있으며, 함수는 sync 또는 async 일 수 있습니다
2.  docstring 이 있으면 설명과 인자 설명을 추출하는 데 사용됩니다
3.  함수는 선택적으로 `context` 를 받을 수 있습니다(첫 번째 인자여야 함). 도구 이름, 설명, docstring 스타일 등 오버라이드도 설정할 수 있습니다
4.  데코레이터가 적용된 함수를 도구 목록에 전달할 수 있습니다

??? note "출력 보기 확장"

    ```
    fetch_weather
    Fetch the weather for a given location.
    {
    "$defs": {
      "Location": {
        "properties": {
          "lat": {
            "title": "Lat",
            "type": "number"
          },
          "long": {
            "title": "Long",
            "type": "number"
          }
        },
        "required": [
          "lat",
          "long"
        ],
        "title": "Location",
        "type": "object"
      }
    },
    "properties": {
      "location": {
        "$ref": "#/$defs/Location",
        "description": "The location to fetch the weather for."
      }
    },
    "required": [
      "location"
    ],
    "title": "fetch_weather_args",
    "type": "object"
    }

    fetch_data
    Read the contents of a file.
    {
    "properties": {
      "path": {
        "description": "The path to the file to read.",
        "title": "Path",
        "type": "string"
      },
      "directory": {
        "anyOf": [
          {
            "type": "string"
          },
          {
            "type": "null"
          }
        ],
        "default": null,
        "description": "The directory to read the file from.",
        "title": "Directory"
      }
    },
    "required": [
      "path"
    ],
    "title": "fetch_data_args",
    "type": "object"
    }
    ```

### 함수 도구에서 이미지 또는 파일 반환

텍스트 출력 반환 외에도, 함수 도구 출력으로 하나 이상의 이미지 또는 파일을 반환할 수 있습니다. 이를 위해 다음 중 아무거나 반환할 수 있습니다:

-   이미지: [`ToolOutputImage`][agents.tool.ToolOutputImage] (또는 TypedDict 버전인 [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict])
-   파일: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent] (또는 TypedDict 버전인 [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict])
-   텍스트: 문자열 또는 문자열로 변환 가능한 객체, 혹은 [`ToolOutputText`][agents.tool.ToolOutputText] (또는 TypedDict 버전인 [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict])

### 사용자 정의 함수 도구

때로는 Python 함수를 도구로 사용하고 싶지 않을 수 있습니다. 원한다면 [`FunctionTool`][agents.tool.FunctionTool] 을 직접 만들 수 있습니다. 다음을 제공해야 합니다:

-   `name`
-   `description`
-   `params_json_schema`: 인자를 위한 JSON 스키마
-   `on_invoke_tool`: [`ToolContext`][agents.tool_context.ToolContext] 와 JSON 문자열 형태의 인자를 받아 도구 출력(예: 텍스트, 구조화된 도구 출력 객체, 또는 출력 목록)을 반환하는 async 함수

```python
from typing import Any

from pydantic import BaseModel

from agents import RunContextWrapper, FunctionTool



def do_some_work(data: str) -> str:
    return "done"


class FunctionArgs(BaseModel):
    username: str
    age: int


async def run_function(ctx: RunContextWrapper[Any], args: str) -> str:
    parsed = FunctionArgs.model_validate_json(args)
    return do_some_work(data=f"{parsed.username} is {parsed.age} years old")


tool = FunctionTool(
    name="process_user",
    description="Processes extracted user data",
    params_json_schema=FunctionArgs.model_json_schema(),
    on_invoke_tool=run_function,
)
```

### 자동 인자 및 docstring 파싱

앞서 언급했듯이, 도구용 스키마 추출을 위해 함수 시그니처를 자동 파싱하고, 도구 및 개별 인자 설명 추출을 위해 docstring 을 파싱합니다. 관련 참고 사항은 다음과 같습니다:

1. 시그니처 파싱은 `inspect` 모듈로 수행됩니다. 인자 타입을 이해하기 위해 타입 어노테이션을 사용하고, 전체 스키마를 표현하는 Pydantic 모델을 동적으로 구성합니다. Python 기본 타입, Pydantic 모델, TypedDict 등 대부분의 타입을 지원합니다
2. docstring 파싱에는 `griffe` 를 사용합니다. 지원되는 docstring 형식은 `google`, `sphinx`, `numpy` 입니다. docstring 형식을 자동 감지하려고 시도하지만 최선 시도이며, `function_tool` 호출 시 명시적으로 설정할 수 있습니다. `use_docstring_info` 를 `False` 로 설정해 docstring 파싱을 비활성화할 수도 있습니다

스키마 추출 코드는 [`agents.function_schema`][] 에 있습니다.

### Pydantic Field 로 인자 제약 및 설명 지정

Pydantic 의 [`Field`](https://docs.pydantic.dev/latest/concepts/fields/) 를 사용해 도구 인자에 제약(예: 숫자 최소/최대, 문자열 길이 또는 패턴)과 설명을 추가할 수 있습니다. Pydantic 과 마찬가지로 두 형태를 모두 지원합니다: 기본값 기반(`arg: int = Field(..., ge=1)`)과 `Annotated`(`arg: Annotated[int, Field(..., ge=1)]`). 생성된 JSON 스키마와 검증에는 이러한 제약이 포함됩니다.

```python
from typing import Annotated
from pydantic import Field
from agents import function_tool

# Default-based form
@function_tool
def score_a(score: int = Field(..., ge=0, le=100, description="Score from 0 to 100")) -> str:
    return f"Score recorded: {score}"

# Annotated form
@function_tool
def score_b(score: Annotated[int, Field(..., ge=0, le=100, description="Score from 0 to 100")]) -> str:
    return f"Score recorded: {score}"
```

### 함수 도구 타임아웃

async 함수 도구에는 `@function_tool(timeout=...)` 로 호출별 타임아웃을 설정할 수 있습니다.

```python
import asyncio
from agents import Agent, Runner, function_tool


@function_tool(timeout=2.0)
async def slow_lookup(query: str) -> str:
    await asyncio.sleep(10)
    return f"Result for {query}"


agent = Agent(
    name="Timeout demo",
    instructions="Use tools when helpful.",
    tools=[slow_lookup],
)
```

타임아웃에 도달하면 기본 동작은 `timeout_behavior="error_as_result"` 이며, 모델에 보이는 타임아웃 메시지(예: `Tool 'slow_lookup' timed out after 2 seconds.`)를 보냅니다.

타임아웃 처리를 제어할 수 있습니다:

-   `timeout_behavior="error_as_result"` (기본값): 모델이 복구할 수 있도록 타임아웃 메시지를 반환합니다
-   `timeout_behavior="raise_exception"`: [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError] 를 발생시키고 실행을 실패 처리합니다
-   `timeout_error_function=...`: `error_as_result` 사용 시 타임아웃 메시지를 사용자 정의합니다

```python
import asyncio
from agents import Agent, Runner, ToolTimeoutError, function_tool


@function_tool(timeout=1.5, timeout_behavior="raise_exception")
async def slow_tool() -> str:
    await asyncio.sleep(5)
    return "done"


agent = Agent(name="Timeout hard-fail", tools=[slow_tool])

try:
    await Runner.run(agent, "Run the tool")
except ToolTimeoutError as e:
    print(f"{e.tool_name} timed out in {e.timeout_seconds} seconds")
```

!!! note

    타임아웃 구성은 async `@function_tool` 핸들러에서만 지원됩니다

### 함수 도구의 오류 처리

`@function_tool` 로 함수 도구를 만들 때 `failure_error_function` 을 전달할 수 있습니다. 이 함수는 도구 호출이 비정상 종료될 경우 LLM 에 오류 응답을 제공합니다.

-   기본값(즉, 아무것도 전달하지 않는 경우)으로는 오류가 발생했음을 LLM 에 알리는 `default_tool_error_function` 이 실행됩니다
-   사용자 정의 오류 함수를 전달하면 그것이 대신 실행되고, 그 응답이 LLM 으로 전송됩니다
-   명시적으로 `None` 을 전달하면 도구 호출 오류가 다시 발생되어 사용자가 처리하게 됩니다. 이는 모델이 잘못된 JSON 을 생성했을 때의 `ModelBehaviorError`, 코드가 비정상 종료되었을 때의 `UserError` 등일 수 있습니다

```python
from agents import function_tool, RunContextWrapper
from typing import Any

def my_custom_error_function(context: RunContextWrapper[Any], error: Exception) -> str:
    """A custom function to provide a user-friendly error message."""
    print(f"A tool call failed with the following error: {error}")
    return "An internal server error occurred. Please try again later."

@function_tool(failure_error_function=my_custom_error_function)
def get_user_profile(user_id: str) -> str:
    """Fetches a user profile from a mock API.
     This function demonstrates a 'flaky' or failing API call.
    """
    if user_id == "user_123":
        return "User profile for user_123 successfully retrieved."
    else:
        raise ValueError(f"Could not retrieve profile for user_id: {user_id}. API returned an error.")

```

`FunctionTool` 객체를 수동으로 만드는 경우에는 `on_invoke_tool` 함수 내부에서 오류를 처리해야 합니다.

## Agents as tools

일부 워크플로에서는 제어를 핸드오프하는 대신, 중앙 에이전트가 전문화된 에이전트 네트워크를 에이전트 오케스트레이션 하도록 하고 싶을 수 있습니다. 이를 위해 에이전트를 도구로 모델링할 수 있습니다.

```python
from agents import Agent, Runner
import asyncio

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You translate the user's message to Spanish",
)

french_agent = Agent(
    name="French agent",
    instructions="You translate the user's message to French",
)

orchestrator_agent = Agent(
    name="orchestrator_agent",
    instructions=(
        "You are a translation agent. You use the tools given to you to translate."
        "If asked for multiple translations, you call the relevant tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="translate_to_spanish",
            tool_description="Translate the user's message to Spanish",
        ),
        french_agent.as_tool(
            tool_name="translate_to_french",
            tool_description="Translate the user's message to French",
        ),
    ],
)

async def main():
    result = await Runner.run(orchestrator_agent, input="Say 'Hello, how are you?' in Spanish.")
    print(result.final_output)
```

### 도구-에이전트 사용자 정의

`agent.as_tool` 함수는 에이전트를 도구로 쉽게 전환할 수 있도록 하는 편의 메서드입니다. `max_turns`, `run_config`, `hooks`, `previous_response_id`, `conversation_id`, `session`, `needs_approval` 같은 일반적인 런타임 옵션을 지원합니다. 또한 `parameters`, `input_builder`, `include_input_schema` 를 통한 구조화된 입력도 지원합니다. 고급 오케스트레이션(예: 조건부 재시도, 폴백 동작, 여러 에이전트 호출 체이닝)의 경우 도구 구현에서 `Runner.run` 을 직접 사용하세요:

```python
@function_tool
async def run_my_agent() -> str:
    """A tool that runs the agent with custom configs"""

    agent = Agent(name="My agent", instructions="...")

    result = await Runner.run(
        agent,
        input="...",
        max_turns=5,
        run_config=...
    )

    return str(result.final_output)
```

### 도구-에이전트용 구조화된 입력

기본적으로 `Agent.as_tool()` 은 단일 문자열 입력(`{"input": "..."}`)을 기대하지만, `parameters`(Pydantic 모델 또는 dataclass 타입)를 전달해 구조화된 스키마를 노출할 수 있습니다.

추가 옵션:

- `include_input_schema=True` 는 생성된 중첩 입력에 전체 JSON Schema 를 포함합니다
- `input_builder=...` 는 구조화된 도구 인자가 중첩 에이전트 입력으로 변환되는 방식을 완전히 사용자 정의할 수 있게 합니다
- `RunContextWrapper.tool_input` 은 중첩 실행 컨텍스트 내부에서 파싱된 구조화 페이로드를 포함합니다

```python
from pydantic import BaseModel, Field


class TranslationInput(BaseModel):
    text: str = Field(description="Text to translate.")
    source: str = Field(description="Source language.")
    target: str = Field(description="Target language.")


translator_tool = translator_agent.as_tool(
    tool_name="translate_text",
    tool_description="Translate text between languages.",
    parameters=TranslationInput,
    include_input_schema=True,
)
```

전체 실행 가능한 예제는 `examples/agent_patterns/agents_as_tools_structured.py` 를 참고하세요.

### 도구-에이전트 승인 게이트

`Agent.as_tool(..., needs_approval=...)` 는 `function_tool` 과 동일한 승인 흐름을 사용합니다. 승인이 필요하면 실행이 일시 중지되고 보류 항목이 `result.interruptions` 에 나타납니다. 그런 다음 `result.to_state()` 를 사용하고 `state.approve(...)` 또는 `state.reject(...)` 호출 후 재개하세요. 전체 일시중지/재개 패턴은 [휴먼인더루프 (HITL) 가이드](human_in_the_loop.md)를 참고하세요.

### 사용자 정의 출력 추출

특정 경우에는 도구-에이전트의 출력을 중앙 에이전트에 반환하기 전에 수정하고 싶을 수 있습니다. 다음과 같은 경우에 유용합니다:

-   서브 에이전트의 채팅 기록에서 특정 정보(예: JSON 페이로드) 추출
-   에이전트의 최종 답변 변환 또는 재포맷(예: Markdown 을 일반 텍스트 또는 CSV 로 변환)
-   출력 검증 또는 에이전트 응답이 누락되거나 잘못된 형식일 때 폴백 값 제공

이를 위해 `as_tool` 메서드에 `custom_output_extractor` 인자를 제공할 수 있습니다:

```python
async def extract_json_payload(run_result: RunResult) -> str:
    # Scan the agent’s outputs in reverse order until we find a JSON-like message from a tool call.
    for item in reversed(run_result.new_items):
        if isinstance(item, ToolCallOutputItem) and item.output.strip().startswith("{"):
            return item.output.strip()
    # Fallback to an empty JSON object if nothing was found
    return "{}"


json_tool = data_agent.as_tool(
    tool_name="get_data_json",
    tool_description="Run the data agent and return only its JSON payload",
    custom_output_extractor=extract_json_payload,
)
```

### 중첩 에이전트 실행 스트리밍

`as_tool` 에 `on_stream` 콜백을 전달하면, 스트림이 완료된 뒤 최종 출력을 반환하면서도 중첩 에이전트가 내보내는 스트리밍 이벤트를 수신할 수 있습니다.

```python
from agents import AgentToolStreamEvent


async def handle_stream(event: AgentToolStreamEvent) -> None:
    # Inspect the underlying StreamEvent along with agent metadata.
    print(f"[stream] {event['agent'].name} :: {event['event'].type}")


billing_agent_tool = billing_agent.as_tool(
    tool_name="billing_helper",
    tool_description="Answer billing questions.",
    on_stream=handle_stream,  # Can be sync or async.
)
```

예상 동작:

- 이벤트 유형은 `StreamEvent["type"]` 과 동일합니다: `raw_response_event`, `run_item_stream_event`, `agent_updated_stream_event`
- `on_stream` 을 제공하면 중첩 에이전트가 자동으로 스트리밍 모드로 실행되고, 최종 출력을 반환하기 전에 스트림을 모두 소비합니다
- 핸들러는 동기 또는 비동기일 수 있으며, 각 이벤트는 도착 순서대로 전달됩니다
- `tool_call` 은 모델 도구 호출을 통해 도구가 호출될 때 존재하며, 직접 호출에서는 `None` 일 수 있습니다
- 전체 실행 가능한 샘플은 `examples/agent_patterns/agents_as_tools_streaming.py` 를 참고하세요

### 조건부 도구 활성화

`is_enabled` 매개변수를 사용해 런타임에 에이전트 도구를 조건부로 활성화 또는 비활성화할 수 있습니다. 이를 통해 컨텍스트, 사용자 선호, 런타임 조건에 따라 LLM 에 사용 가능한 도구를 동적으로 필터링할 수 있습니다.

```python
import asyncio
from agents import Agent, AgentBase, Runner, RunContextWrapper
from pydantic import BaseModel

class LanguageContext(BaseModel):
    language_preference: str = "french_spanish"

def french_enabled(ctx: RunContextWrapper[LanguageContext], agent: AgentBase) -> bool:
    """Enable French for French+Spanish preference."""
    return ctx.context.language_preference == "french_spanish"

# Create specialized agents
spanish_agent = Agent(
    name="spanish_agent",
    instructions="You respond in Spanish. Always reply to the user's question in Spanish.",
)

french_agent = Agent(
    name="french_agent",
    instructions="You respond in French. Always reply to the user's question in French.",
)

# Create orchestrator with conditional tools
orchestrator = Agent(
    name="orchestrator",
    instructions=(
        "You are a multilingual assistant. You use the tools given to you to respond to users. "
        "You must call ALL available tools to provide responses in different languages. "
        "You never respond in languages yourself, you always use the provided tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="respond_spanish",
            tool_description="Respond to the user's question in Spanish",
            is_enabled=True,  # Always enabled
        ),
        french_agent.as_tool(
            tool_name="respond_french",
            tool_description="Respond to the user's question in French",
            is_enabled=french_enabled,
        ),
    ],
)

async def main():
    context = RunContextWrapper(LanguageContext(language_preference="french_spanish"))
    result = await Runner.run(orchestrator, "How are you?", context=context.context)
    print(result.final_output)

asyncio.run(main())
```

`is_enabled` 매개변수는 다음을 받습니다:

-   **불리언 값**: `True` (항상 활성화) 또는 `False` (항상 비활성화)
-   **호출 가능한 함수**: `(context, agent)` 를 받아 불리언을 반환하는 함수
-   **비동기 함수**: 복잡한 조건 로직을 위한 async 함수

비활성화된 도구는 런타임에 LLM 에 완전히 숨겨지므로 다음에 유용합니다:

-   사용자 권한 기반 기능 게이팅
-   환경별 도구 가용성(dev vs prod)
-   다양한 도구 구성 A/B 테스트
-   런타임 상태 기반 동적 도구 필터링

## 실험적: Codex 도구

`codex_tool` 은 Codex CLI 를 래핑하여, 도구 호출 중 에이전트가 워크스페이스 범위 작업(셸, 파일 편집, MCP 도구)을 실행할 수 있게 합니다. 이 표면은 실험적이며 변경될 수 있습니다.

현재 실행을 벗어나지 않고 메인 에이전트가 제한된 워크스페이스 작업을 Codex 에 위임하도록 하려면 사용하세요. 기본 도구 이름은 `codex` 입니다. 사용자 정의 이름을 설정하면 `codex` 이거나 `codex_` 로 시작해야 합니다. 에이전트에 여러 Codex 도구가 포함될 때 각각 고유한 이름을 사용해야 합니다.

```python
from agents import Agent
from agents.extensions.experimental.codex import ThreadOptions, TurnOptions, codex_tool

agent = Agent(
    name="Codex Agent",
    instructions="Use the codex tool to inspect the workspace and answer the question.",
    tools=[
        codex_tool(
            sandbox_mode="workspace-write",
            working_directory="/path/to/repo",
            default_thread_options=ThreadOptions(
                model="gpt-5.2-codex",
                model_reasoning_effort="low",
                network_access_enabled=True,
                web_search_mode="disabled",
                approval_policy="never",
            ),
            default_turn_options=TurnOptions(
                idle_timeout_seconds=60,
            ),
            persist_session=True,
        )
    ],
)
```

다음 옵션 그룹부터 시작하세요:

-   실행 표면: `sandbox_mode` 와 `working_directory` 는 Codex 가 동작할 수 있는 위치를 정의합니다. 함께 설정하고, 작업 디렉터리가 Git 저장소 내부가 아니면 `skip_git_repo_check=True` 를 설정하세요
-   스레드 기본값: `default_thread_options=ThreadOptions(...)` 는 모델, 추론 노력, 승인 정책, 추가 디렉터리, 네트워크 접근, 웹 검색 모드를 구성합니다. 레거시 `web_search_enabled` 대신 `web_search_mode` 를 선호하세요
-   턴 기본값: `default_turn_options=TurnOptions(...)` 는 `idle_timeout_seconds` 와 선택적 취소 `signal` 같은 턴별 동작을 구성합니다
-   도구 I/O: 도구 호출에는 `{ "type": "text", "text": ... }` 또는 `{ "type": "local_image", "path": ... }` 를 가진 `inputs` 항목이 최소 하나 포함되어야 합니다. `output_schema` 로 구조화된 Codex 응답을 요구할 수 있습니다

스레드 재사용과 지속성은 별도 제어입니다:

-   `persist_session=True` 는 동일한 도구 인스턴스에 대한 반복 호출에서 하나의 Codex 스레드를 재사용합니다
-   `use_run_context_thread_id=True` 는 동일한 가변 컨텍스트 객체를 공유하는 실행 간에 실행 컨텍스트에 스레드 ID 를 저장하고 재사용합니다
-   스레드 ID 우선순위는 다음과 같습니다: 호출별 `thread_id`, 그다음 (활성화된 경우) 실행 컨텍스트 스레드 ID, 그다음 구성된 `thread_id` 옵션
-   기본 실행 컨텍스트 키는 `name="codex"` 인 경우 `codex_thread_id`, `name="codex_<suffix>"` 인 경우 `codex_thread_id_<suffix>` 입니다. `run_context_thread_id_key` 로 재정의할 수 있습니다

런타임 구성:

-   인증: `CODEX_API_KEY` (권장) 또는 `OPENAI_API_KEY` 를 설정하거나, `codex_options={"api_key": "..."}` 를 전달하세요
-   런타임: `codex_options.base_url` 이 CLI base URL 을 재정의합니다
-   바이너리 확인: CLI 경로를 고정하려면 `codex_options.codex_path_override` (또는 `CODEX_PATH`) 를 설정하세요. 그렇지 않으면 SDK 는 `PATH` 에서 `codex` 를 확인한 뒤, 번들된 vendor 바이너리로 폴백합니다
-   환경: `codex_options.env` 는 하위 프로세스 환경을 완전히 제어합니다. 제공되면 하위 프로세스는 `os.environ` 을 상속하지 않습니다
-   스트림 제한: `codex_options.codex_subprocess_stream_limit_bytes` (또는 `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`) 는 stdout/stderr 리더 제한을 제어합니다. 유효 범위는 `65536` ~ `67108864` 이며, 기본값은 `8388608` 입니다
-   스트리밍: `on_stream` 은 스레드/턴 라이프사이클 이벤트와 항목 이벤트(`reasoning`, `command_execution`, `mcp_tool_call`, `file_change`, `web_search`, `todo_list`, `error` 항목 업데이트)를 수신합니다
-   출력: 결과에는 `response`, `usage`, `thread_id` 가 포함되며, usage 는 `RunContextWrapper.usage` 에 추가됩니다

참고 자료:

-   [Codex 도구 API 레퍼런스](ref/extensions/experimental/codex/codex_tool.md)
-   [ThreadOptions 레퍼런스](ref/extensions/experimental/codex/thread_options.md)
-   [TurnOptions 레퍼런스](ref/extensions/experimental/codex/turn_options.md)
-   전체 실행 가능한 샘플은 `examples/tools/codex.py` 및 `examples/tools/codex_same_thread.py` 를 참고하세요