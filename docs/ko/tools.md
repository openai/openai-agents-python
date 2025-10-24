---
search:
  exclude: true
---
# 도구

도구는 에이전트가 데이터를 가져오고, 코드를 실행하고, 외부 API 를 호출하고, 심지어 컴퓨터를 사용하는 등 행동을 취할 수 있게 합니다. Agent SDK 에는 세 가지 종류의 도구가 있습니다:

- 호스티드 툴: AI 모델과 함께 LLM 서버에서 실행됩니다. OpenAI 는 retrieval, 웹 검색 및 컴퓨터 사용을 호스티드 툴로 제공합니다
- 함수 호출: 임의의 Python 함수를 도구로 사용할 수 있게 합니다
- 도구로서의 에이전트: 에이전트를 도구로 사용하여, 핸드오프 없이 에이전트가 다른 에이전트를 호출할 수 있게 합니다

## 호스티드 툴

OpenAI 는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 사용 시 몇 가지 기본 제공 도구를 제공합니다:

- [`WebSearchTool`][agents.tool.WebSearchTool] 은 에이전트가 웹을 검색할 수 있게 합니다
- [`FileSearchTool`][agents.tool.FileSearchTool] 은 OpenAI 벡터 스토어에서 정보를 검색할 수 있게 합니다
- [`ComputerTool`][agents.tool.ComputerTool] 은 컴퓨터 사용 작업을 자동화할 수 있게 합니다
- [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 은 LLM 이 샌드박스 환경에서 코드를 실행할 수 있게 합니다
- [`HostedMCPTool`][agents.tool.HostedMCPTool] 은 원격 MCP 서버의 도구를 모델에 노출합니다
- [`ImageGenerationTool`][agents.tool.ImageGenerationTool] 은 프롬프트로부터 이미지를 생성합니다
- [`LocalShellTool`][agents.tool.LocalShellTool] 은 로컬 머신에서 셸 명령을 실행합니다

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

## 함수 도구

임의의 Python 함수를 도구로 사용할 수 있습니다. Agents SDK 가 도구를 자동으로 설정합니다:

- 도구 이름은 Python 함수 이름이 됩니다(또는 이름을 직접 지정할 수 있음)
- 도구 설명은 함수의 docstring 에서 가져옵니다(또는 설명을 직접 지정할 수 있음)
- 함수 입력에 대한 스키마는 함수의 인자에서 자동으로 생성됨
- 각 입력에 대한 설명은 비활성화하지 않는 한 함수의 docstring 에서 가져옴

Python 의 `inspect` 모듈을 사용해 함수 시그니처를 추출하고, [`griffe`](https://mkdocstrings.github.io/griffe/) 로 docstring 을 파싱하며 스키마 생성에는 `pydantic` 을 사용합니다.

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

1. 함수 인자로는 어떤 Python 타입이든 사용할 수 있으며, 함수는 동기 또는 비동기일 수 있습니다
2. docstring 이 있으면 설명과 인자 설명을 추출하는 데 사용합니다
3. 선택적으로 `context` 를 받을 수 있습니다(첫 번째 인자여야 함). 도구 이름, 설명, 사용할 docstring 스타일 등도 오버라이드할 수 있습니다
4. 데코레이터가 적용된 함수를 도구 목록에 전달하면 됩니다

??? note "Expand to see output"

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

텍스트 출력과 더불어, 함수 도구의 출력으로 하나 이상의 이미지 또는 파일을 반환할 수 있습니다. 이를 위해 다음 중 아무 것이나 반환할 수 있습니다:

- 이미지: [`ToolOutputImage`][agents.tool.ToolOutputImage] (TypedDict 버전: [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict])
- 파일: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent] (TypedDict 버전: [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict])
- 텍스트: 문자열 또는 문자열로 변환 가능한 객체, 혹은 [`ToolOutputText`][agents.tool.ToolOutputText] (TypedDict 버전: [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict])

### 사용자 지정 함수 도구

때로는 Python 함수를 도구로 사용하고 싶지 않을 수 있습니다. 이 경우 [`FunctionTool`][agents.tool.FunctionTool] 을 직접 생성할 수 있습니다. 다음을 제공해야 합니다:

- `name`
- `description`
- `params_json_schema` (인자를 위한 JSON 스키마)
- `on_invoke_tool` (async 함수로, [`ToolContext`][agents.tool_context.ToolContext] 와 JSON 문자열 형태의 인자를 받아야 하며, 도구 출력을 문자열로 반환해야 함)

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

### 인자 및 docstring 자동 파싱

앞서 언급했듯이, 도구의 스키마를 추출하기 위해 함수 시그니처를 자동으로 파싱하고, 도구 및 개별 인자에 대한 설명을 추출하기 위해 docstring 을 파싱합니다. 이에 대한 몇 가지 참고 사항:

1. 시그니처 파싱은 `inspect` 모듈로 수행합니다. 타입 힌트를 사용해 인자 타입을 파악하고, 전체 스키마를 나타내는 Pydantic 모델을 동적으로 빌드합니다. Python 기본형, Pydantic 모델, TypedDict 등 대부분의 타입을 지원합니다
2. docstring 파싱에는 `griffe` 를 사용합니다. 지원하는 docstring 포맷은 `google`, `sphinx`, `numpy` 입니다. 포맷을 자동 감지하려고 시도하지만 최선의 노력일 뿐이므로, `function_tool` 호출 시 명시적으로 설정할 수 있습니다. `use_docstring_info` 를 `False` 로 설정해 docstring 파싱을 비활성화할 수도 있습니다

스키마 추출 코드는 [`agents.function_schema`][] 에 있습니다.

## 도구로서의 에이전트

일부 워크플로에서는 제어를 넘기지 않고 중앙 에이전트가 특화된 에이전트들의 네트워크를 오케스트레이션하기를 원할 수 있습니다. 에이전트를 도구로 모델링하여 이를 수행할 수 있습니다.

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

### 도구형 에이전트 사용자 지정

`agent.as_tool` 함수는 에이전트를 도구로 쉽게 전환하기 위한 편의 메서드입니다. 하지만 모든 구성을 지원하지는 않습니다. 예를 들어 `max_turns` 를 설정할 수 없습니다. 고급 사용 사례에서는 도구 구현 내에서 `Runner.run` 을 직접 사용하세요:

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

### 사용자 지정 출력 추출

특정 경우, 중앙 에이전트로 반환하기 전에 도구형 에이전트의 출력을 수정하고 싶을 수 있습니다. 이는 다음과 같은 경우에 유용합니다:

- 하위 에이전트의 대화 기록에서 특정 정보(예: JSON 페이로드)만 추출
- 에이전트의 최종 답변을 변환 또는 재포맷(예: Markdown 을 일반 텍스트나 CSV 로 변환)
- 에이전트의 응답이 없거나 형식이 잘못된 경우 출력을 검증하거나 대체 값을 제공

이를 위해 `as_tool` 메서드에 `custom_output_extractor` 매개변수를 제공하면 됩니다:

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

### 조건부 도구 활성화

런타임에 `is_enabled` 매개변수를 사용해 에이전트 도구를 조건부로 활성화하거나 비활성화할 수 있습니다. 이를 통해 컨텍스트, 사용자 선호도, 런타임 조건에 따라 LLM 에 제공되는 도구를 동적으로 필터링할 수 있습니다.

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

`is_enabled` 매개변수는 다음을 허용합니다:

- **Boolean values**: `True` (항상 활성) 또는 `False` (항상 비활성)
- **Callable functions**: `(context, agent)` 를 받아 불리언을 반환하는 함수
- **Async functions**: 더 복잡한 조건부 로직을 위한 비동기 함수

비활성화된 도구는 런타임에 LLM 에 완전히 숨겨지므로 다음에 유용합니다:

- 사용자 권한 기반 기능 게이팅
- 환경별 도구 가용성 설정(개발/프로덕션)
- 서로 다른 도구 구성을 A/B 테스트
- 런타임 상태에 따른 동적 도구 필터링

## 함수 도구 오류 처리

`@function_tool` 로 함수 도구를 만들 때 `failure_error_function` 을 전달할 수 있습니다. 이는 도구 호출이 크래시한 경우 LLM 에 오류 응답을 제공하는 함수입니다.

- 기본값(즉, 아무것도 전달하지 않은 경우)으로, 오류가 발생했음을 LLM 에 알리는 `default_tool_error_function` 이 실행됩니다
- 사용자 정의 오류 함수를 전달하면 해당 함수가 대신 실행되어 그 응답이 LLM 에 전송됩니다
- 명시적으로 `None` 을 전달하면, 도구 호출 오류가 다시 발생(raise)하여 직접 처리해야 합니다. 모델이 잘못된 JSON 을 생성한 경우 `ModelBehaviorError`, 코드가 크래시한 경우 `UserError` 등이 될 수 있습니다

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

`FunctionTool` 객체를 수동으로 생성하는 경우, `on_invoke_tool` 함수 내부에서 오류를 직접 처리해야 합니다.