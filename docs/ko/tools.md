---
search:
  exclude: true
---
# 도구

도구는 에이전트가 데이터를 가져오고, 코드를 실행하고, 외부 API 를 호출하고, 심지어 컴퓨터를 사용하는 등의 작업을 수행하도록 합니다. Agents SDK 에는 세 가지 클래스의 도구가 있습니다:

- 호스티드 툴: 이는 AI 모델과 함께 LLM 서버에서 실행됩니다. OpenAI 는 retrieval, 웹 검색 및 컴퓨터 사용을 호스티드 툴로 제공합니다
- 함수 호출: 임의의 Python 함수를 도구로 사용할 수 있게 합니다
- 도구로서의 에이전트: 에이전트를 도구처럼 사용하여, 핸드오프 없이 에이전트가 다른 에이전트를 호출할 수 있게 합니다

## 호스티드 툴

OpenAI 는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 사용 시 몇 가지 내장 도구를 제공합니다:

- [`WebSearchTool`][agents.tool.WebSearchTool]: 에이전트가 웹을 검색할 수 있게 합니다
- [`FileSearchTool`][agents.tool.FileSearchTool]: OpenAI 벡터 스토어에서 정보를 검색할 수 있게 합니다
- [`ComputerTool`][agents.tool.ComputerTool]: 컴퓨터 사용 작업을 자동화할 수 있게 합니다
- [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool]: LLM 이 샌드박스 환경에서 코드를 실행할 수 있게 합니다
- [`HostedMCPTool`][agents.tool.HostedMCPTool]: 원격 MCP 서버의 도구를 모델에 노출합니다
- [`ImageGenerationTool`][agents.tool.ImageGenerationTool]: 프롬프트로부터 이미지를 생성합니다
- [`LocalShellTool`][agents.tool.LocalShellTool]: 로컬 머신에서 셸 명령을 실행합니다

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

- 도구 이름은 Python 함수 이름이 됩니다(또는 직접 이름을 제공할 수 있음)
- 도구 설명은 함수의 도크스트링에서 가져옵니다(또는 직접 설명을 제공할 수 있음)
- 함수 입력에 대한 스키마는 함수의 인자에서 자동으로 생성됩니다
- 각 입력의 설명은 비활성화하지 않는 한 함수의 도크스트링에서 가져옵니다

Python 의 `inspect` 모듈로 함수 시그니처를 추출하고, 도크스트링 파싱에는 [`griffe`](https://mkdocstrings.github.io/griffe/) 를, 스키마 생성에는 `pydantic` 을 사용합니다.

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

1. 함수의 인자로 임의의 Python 타입을 사용할 수 있으며, 함수는 동기 또는 비동기로 작성할 수 있습니다
2. 도크스트링이 있으면 설명과 인자 설명을 캡처하는 데 사용됩니다
3. 함수는 선택적으로 `context` 를 받을 수 있습니다(반드시 첫 번째 인자). 툴의 이름, 설명, 사용할 도크스트링 스타일 등 오버라이드를 설정할 수도 있습니다
4. 데코레이터가 적용된 함수를 도구 목록에 전달할 수 있습니다

??? note "결과 펼쳐서 보기"

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

### 사용자 지정 함수 도구

때로는 Python 함수를 도구로 사용하고 싶지 않을 수 있습니다. 원한다면 직접 [`FunctionTool`][agents.tool.FunctionTool] 을 생성할 수 있습니다. 다음을 제공해야 합니다:

- `name`
- `description`
- `params_json_schema` (인자에 대한 JSON 스키마)
- `on_invoke_tool` (비동기 함수로, [`ToolContext`][agents.tool_context.ToolContext] 와 인자(JSON 문자열)를 받아 도구의 출력 문자열을 반환해야 함)

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

### 인자 및 도크스트링 자동 파싱

앞서 언급했듯이, 도구의 스키마를 추출하기 위해 함수 시그니처를 자동으로 파싱하고, 도구 및 개별 인자에 대한 설명을 추출하기 위해 도크스트링을 파싱합니다. 참고 사항은 다음과 같습니다:

1. 시그니처 파싱은 `inspect` 모듈을 통해 수행됩니다. 인자의 타입을 이해하기 위해 타입 주석을 사용하고, 전체 스키마를 표현하는 Pydantic 모델을 동적으로 생성합니다. Python 기본형, Pydantic 모델, TypedDict 등 대부분의 타입을 지원합니다
2. 도크스트링 파싱에는 `griffe` 를 사용합니다. 지원되는 도크스트링 형식은 `google`, `sphinx`, `numpy` 입니다. 도크스트링 형식은 자동 감지를 시도하지만 최선의 노력일 뿐이므로, `function_tool` 호출 시 명시적으로 설정할 수 있습니다. `use_docstring_info` 를 `False` 로 설정하면 도크스트링 파싱을 비활성화할 수 있습니다

스키마 추출을 위한 코드는 [`agents.function_schema`][] 에 있습니다.

## 도구로서의 에이전트

일부 워크플로에서는 제어권을 넘기지 않고, 중앙 에이전트가 특화된 에이전트 네트워크를 멀티 에이전트 오케스트레이션 하도록 원할 수 있습니다. 에이전트를 도구로 모델링하여 이를 수행할 수 있습니다.

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

### 도구-에이전트 커스터마이징

`agent.as_tool` 함수는 에이전트를 도구로 쉽게 전환하기 위한 편의 메서드입니다. 그러나 모든 구성을 지원하지는 않습니다. 예를 들어 `max_turns` 를 설정할 수 없습니다. 고급 사용 사례의 경우, 도구 구현 내에서 `Runner.run` 을 직접 사용하세요:

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

특정 상황에서는 중앙 에이전트에 반환하기 전에 도구-에이전트의 출력을 수정하고 싶을 수 있습니다. 다음과 같은 경우에 유용합니다:

- 하위 에이전트의 대화 기록에서 특정 정보(예: JSON 페이로드)를 추출
- 에이전트의 최종 답변을 변환하거나 재포맷(예: Markdown 을 일반 텍스트 또는 CSV 로 변환)
- 에이전트의 응답이 누락되었거나 형식이 올바르지 않을 때 출력을 검증하거나 폴백 값을 제공

이는 `as_tool` 메서드에 `custom_output_extractor` 인자를 제공하여 수행할 수 있습니다:

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

런타임에 `is_enabled` 매개변수를 사용해 에이전트 도구를 조건부로 활성화하거나 비활성화할 수 있습니다. 이를 통해 컨텍스트, 사용자 선호도, 런타임 조건에 따라 LLM 에게 제공되는 도구를 동적으로 필터링할 수 있습니다.

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

- **불리언 값**: `True`(항상 활성) 또는 `False`(항상 비활성)
- **호출 가능한 함수**: `(context, agent)` 를 받아 불리언을 반환하는 함수
- **비동기 함수**: 복잡한 조건부 로직을 위한 async 함수

비활성화된 도구는 런타임에 LLM 에서 완전히 숨겨지므로 다음과 같은 경우에 유용합니다:

- 사용자 권한에 따른 기능 게이팅
- 환경별 도구 가용성(개발 vs 운영)
- 서로 다른 도구 구성을 A/B 테스트
- 런타임 상태에 따른 동적 도구 필터링

## 함수 도구에서의 오류 처리

`@function_tool` 로 함수 도구를 생성할 때 `failure_error_function` 을 전달할 수 있습니다. 이는 도구 호출이 실패했을 때 LLM 에 오류 응답을 제공하는 함수입니다.

- 기본적으로(아무 것도 전달하지 않으면) 오류 발생을 LLM 에 알리는 `default_tool_error_function` 을 실행합니다
- 사용자 정의 오류 함수를 전달하면 해당 함수가 실행되고, 그 응답이 LLM 에 전송됩니다
- 명시적으로 `None` 을 전달하면, 도구 호출 오류가 다시 발생하여 사용자가 처리해야 합니다. 모델이 잘못된 JSON 을 생성한 경우 `ModelBehaviorError`, 코드가 크래시한 경우 `UserError` 등이 될 수 있습니다

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

`FunctionTool` 객체를 수동으로 생성하는 경우, 오류는 `on_invoke_tool` 함수 내부에서 직접 처리해야 합니다.