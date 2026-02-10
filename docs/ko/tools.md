---
search:
  exclude: true
---
# 도구

도구는 에이전트가 작업을 수행할 수 있게 해줍니다. 예를 들어 데이터 가져오기, 코드 실행, 외부 API 호출, 심지어 컴퓨터 사용까지 가능합니다. SDK 는 다섯 가지 카테고리를 지원합니다:

-   OpenAI 호스트하는 도구: OpenAI 서버에서 모델과 함께 실행됩니다
-   로컬 런타임 도구: 사용자의 환경에서 실행됩니다 (컴퓨터 사용, 셸, 패치 적용)
-   함수 호출: 어떤 Python 함수든 도구로 감쌉니다
-   Agents as tools: 전체 핸드오프 없이 에이전트를 호출 가능한 도구로 노출합니다
-   실험적: Codex 도구: 도구 호출로 워크스페이스 범위의 Codex 작업을 실행합니다

## 호스티드 툴

OpenAI 는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 을 사용할 때 몇 가지 내장 도구를 제공합니다:

-   [`WebSearchTool`][agents.tool.WebSearchTool] 은 에이전트가 웹을 검색할 수 있게 해줍니다
-   [`FileSearchTool`][agents.tool.FileSearchTool] 은 OpenAI Vector Stores 에서 정보를 가져올 수 있게 해줍니다
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] 은 LLM 이 샌드박스 환경에서 코드를 실행할 수 있게 해줍니다
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 은 원격 MCP 서버의 도구를 모델에 노출합니다
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] 은 프롬프트로부터 이미지를 생성합니다

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

## 로컬 런타임 도구

로컬 런타임 도구는 사용자의 환경에서 실행되며, 사용자가 구현을 제공해야 합니다:

-   [`ComputerTool`][agents.tool.ComputerTool]: [`Computer`][agents.computer.Computer] 또는 [`AsyncComputer`][agents.computer.AsyncComputer] 인터페이스를 구현해 GUI/브라우저 자동화를 활성화합니다
-   [`ShellTool`][agents.tool.ShellTool] 또는 [`LocalShellTool`][agents.tool.LocalShellTool]: 명령을 실행할 셸 실행기를 제공합니다
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]: [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] 를 구현해 로컬에서 diff 를 적용합니다

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

어떤 Python 함수든 도구로 사용할 수 있습니다. Agents SDK 가 자동으로 도구를 설정합니다:

-   도구의 이름은 Python 함수의 이름입니다(또는 이름을 제공할 수 있습니다)
-   도구 설명은 함수의 docstring 에서 가져옵니다(또는 설명을 제공할 수 있습니다)
-   함수 입력에 대한 스키마는 함수의 인자에서 자동으로 생성됩니다
-   각 입력에 대한 설명은 비활성화하지 않는 한 함수의 docstring 에서 가져옵니다

Python 의 `inspect` 모듈을 사용해 함수 시그니처를 추출하고, [`griffe`](https://mkdocstrings.github.io/griffe/) 로 docstring 을 파싱하며, 스키마 생성을 위해 `pydantic` 을 사용합니다.

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

1.  함수 인자로 어떤 Python 타입이든 사용할 수 있으며, 함수는 동기 또는 비동기일 수 있습니다
2.  docstring 이 있으면 설명과 인자 설명을 캡처하는 데 사용됩니다
3.  함수는 선택적으로 `context` 를 받을 수 있습니다(첫 번째 인자여야 합니다). 또한 도구 이름, 설명, 사용할 docstring 스타일 등과 같은 오버라이드를 설정할 수 있습니다
4.  데코레이트된 함수를 tools 리스트에 전달할 수 있습니다

??? note "출력을 보려면 펼치기"

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

텍스트 출력 외에도, 함수 도구의 출력으로 하나 또는 여러 개의 이미지나 파일을 반환할 수 있습니다. 이를 위해 다음 중 아무 것이나 반환할 수 있습니다:

-   이미지: [`ToolOutputImage`][agents.tool.ToolOutputImage] (또는 TypedDict 버전인 [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict])
-   파일: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent] (또는 TypedDict 버전인 [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict])
-   텍스트: 문자열 또는 문자열로 변환 가능한 객체, 또는 [`ToolOutputText`][agents.tool.ToolOutputText] (또는 TypedDict 버전인 [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict])

### 사용자 정의 함수 도구

때로는 Python 함수를 도구로 사용하고 싶지 않을 수 있습니다. 원한다면 [`FunctionTool`][agents.tool.FunctionTool] 을 직접 만들 수 있습니다. 다음을 제공해야 합니다:

-   `name`
-   `description`
-   `params_json_schema`: 인자에 대한 JSON 스키마
-   `on_invoke_tool`: [`ToolContext`][agents.tool_context.ToolContext] 와 JSON 문자열로 된 인자를 받고, 도구 출력을 문자열로 반환해야 하는 async 함수

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

앞서 언급했듯이, 도구의 스키마를 추출하기 위해 함수 시그니처를 자동으로 파싱하고, 도구 및 개별 인자에 대한 설명을 추출하기 위해 docstring 을 파싱합니다. 이에 대한 참고 사항은 다음과 같습니다:

1. 시그니처 파싱은 `inspect` 모듈을 통해 수행됩니다. 인자의 타입을 이해하기 위해 타입 어노테이션을 사용하고, 전체 스키마를 나타내는 Pydantic 모델을 동적으로 빌드합니다. Python 기본 타입, Pydantic 모델, TypedDict 등 대부분의 타입을 지원합니다
2. docstring 파싱에는 `griffe` 를 사용합니다. 지원되는 docstring 형식은 `google`, `sphinx`, `numpy` 입니다. docstring 형식을 자동으로 감지하려고 시도하지만 이는 best-effort 이며, `function_tool` 호출 시 명시적으로 설정할 수 있습니다. 또한 `use_docstring_info` 를 `False` 로 설정해 docstring 파싱을 비활성화할 수 있습니다

스키마 추출 코드는 [`agents.function_schema`][] 에 있습니다.

### Pydantic Field 로 인자 제약 및 설명 추가

Pydantic 의 [`Field`](https://docs.pydantic.dev/latest/concepts/fields/) 를 사용해 도구 인자에 제약(예: 숫자의 최소/최대, 문자열의 길이 또는 패턴)과 설명을 추가할 수 있습니다. Pydantic 과 동일하게 두 형태를 모두 지원합니다: 기본값 기반(`arg: int = Field(..., ge=1)`)과 `Annotated` (`arg: Annotated[int, Field(..., ge=1)]`). 생성된 JSON 스키마와 검증에는 이러한 제약이 포함됩니다.

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

## Agents as tools

일부 워크플로에서는 제어를 핸드오프하기보다, 중앙 에이전트가 전문화된 에이전트 네트워크를 멀티 에이전트 오케스트레이션 하도록 만들고 싶을 수 있습니다. 에이전트를 도구로 모델링하여 이를 구현할 수 있습니다.

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

### 도구 에이전트 커스터마이징

`agent.as_tool` 함수는 에이전트를 도구로 쉽게 전환할 수 있는 편의 메서드입니다. `max_turns`, `run_config`, `hooks`, `previous_response_id`, `conversation_id`, `session`, `needs_approval` 같은 일반적인 런타임 옵션을 지원합니다. 또한 `parameters`, `input_builder`, `include_input_schema` 로 structured 입력도 지원합니다. 고급 오케스트레이션(예: 조건부 재시도, 폴백 동작, 여러 에이전트 호출 체이닝)을 위해서는, 도구 구현에서 `Runner.run` 을 직접 사용하세요:

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

### 도구 에이전트의 structured 입력

기본적으로 `Agent.as_tool()` 은 단일 문자열 입력(`{"input": "..."}`)을 기대하지만, `parameters`(Pydantic 모델 또는 dataclass 타입)를 전달해 structured 스키마를 노출할 수 있습니다.

추가 옵션:

- `include_input_schema=True` 는 생성된 중첩 입력에 전체 JSON Schema 를 포함합니다
- `input_builder=...` 는 structured 도구 인자가 중첩된 에이전트 입력으로 변환되는 방식을 완전히 커스터마이즈할 수 있게 해줍니다
- `RunContextWrapper.tool_input` 에는 중첩 실행 컨텍스트 내에서 파싱된 structured 페이로드가 들어 있습니다

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

완전한 실행 가능한 예제는 `examples/agent_patterns/agents_as_tools_structured.py` 를 참고하세요.

### 도구 에이전트의 승인 게이트

`Agent.as_tool(..., needs_approval=...)` 는 `function_tool` 과 동일한 승인 플로우를 사용합니다. 승인이 필요하면 실행이 일시정지되고 보류 항목이 `result.interruptions` 에 나타납니다. 그런 다음 `result.to_state()` 를 사용하고 `state.approve(...)` 또는 `state.reject(...)` 호출 후 재개하세요. 전체 일시정지/재개 패턴은 [Human-in-the-loop guide](human_in_the_loop.md) 를 참고하세요.

### 사용자 정의 출력 추출

특정 경우에는, 중앙 에이전트로 반환하기 전에 도구 에이전트의 출력을 수정하고 싶을 수 있습니다. 이는 다음과 같은 경우에 유용할 수 있습니다:

-   서브 에이전트의 채팅 히스토리에서 특정 정보(예: JSON 페이로드)를 추출하려는 경우
-   에이전트의 최종 답변을 변환하거나 재포맷하려는 경우(예: Markdown 을 일반 텍스트 또는 CSV 로 변환)
-   출력 값을 검증하거나, 에이전트 응답이 누락되었거나 형식이 올바르지 않을 때 폴백 값을 제공하려는 경우

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

### 중첩된 에이전트 실행 스트리밍

`as_tool` 에 `on_stream` 콜백을 전달하면, 스트림이 완료된 뒤 최종 출력을 반환하면서도 중첩된 에이전트가 내보내는 streaming 이벤트를 수신할 수 있습니다.

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

기대할 수 있는 사항:

- 이벤트 타입은 `StreamEvent["type"]` 과 동일합니다: `raw_response_event`, `run_item_stream_event`, `agent_updated_stream_event`
- `on_stream` 을 제공하면 중첩된 에이전트는 자동으로 streaming 모드로 실행되며, 최종 출력을 반환하기 전에 스트림을 모두 소비합니다
- 핸들러는 동기 또는 비동기일 수 있으며, 각 이벤트는 도착하는 순서대로 전달됩니다
- 도구가 모델 도구 호출을 통해 호출된 경우 `tool_call` 이 존재합니다. 직접 호출의 경우 `None` 일 수 있습니다
- 완전한 실행 가능한 샘플은 `examples/agent_patterns/agents_as_tools_streaming.py` 를 참고하세요

### 조건부 도구 활성화

`is_enabled` 매개변수를 사용하면 런타임에 에이전트 도구를 조건부로 활성화하거나 비활성화할 수 있습니다. 이를 통해 컨텍스트, 사용자 선호, 런타임 조건에 따라 LLM 이 사용할 수 있는 도구를 동적으로 필터링할 수 있습니다.

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

-   **불리언 값**: `True`(항상 활성화) 또는 `False`(항상 비활성화)
-   **호출 가능한 함수**: `(context, agent)` 를 받아 불리언을 반환하는 함수
-   **비동기 함수**: 복잡한 조건 로직을 위한 async 함수

비활성화된 도구는 런타임에 LLM 에서 완전히 숨겨지므로, 다음에 유용합니다:

-   사용자 권한에 따른 기능 게이팅
-   환경별 도구 가용성(dev vs prod)
-   서로 다른 도구 구성에 대한 A/B 테스트
-   런타임 상태에 따른 동적 도구 필터링

## 실험적: Codex 도구

`codex_tool` 은 Codex CLI 를 감싸서, 에이전트가 도구 호출 중에 워크스페이스 범위의 작업(셸, 파일 편집, MCP 도구)을 실행할 수 있게 합니다.
이 표면은 실험적이며 변경될 수 있습니다.
기본적으로 도구 이름은 `codex` 입니다. 사용자 정의 이름을 설정하는 경우, `codex` 이거나 `codex_` 로 시작해야 합니다.
에이전트에 여러 Codex 도구가 포함될 때는, 각각이 고유한 이름을 사용해야 합니다(Codex 도구와 비 Codex 도구를 포함).

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

알아둘 점:

-   인증: `CODEX_API_KEY`(권장) 또는 `OPENAI_API_KEY` 를 설정하거나, `codex_options={"api_key": "..."}` 를 전달하세요
-   런타임: `codex_options.base_url` 은 CLI base URL 을 오버라이드합니다
-   바이너리 해석: `codex_options.codex_path_override`(또는 `CODEX_PATH`)를 설정해 CLI 경로를 고정할 수 있습니다. 그렇지 않으면 SDK 는 `PATH` 에서 `codex` 를 찾고, 그 다음 번들된 vendor 바이너리로 폴백합니다
-   환경: `codex_options.env` 는 서브프로세스 환경을 완전히 제어합니다. 제공되면 서브프로세스는 `os.environ` 을 상속하지 않습니다
-   스트림 제한: `codex_options.codex_subprocess_stream_limit_bytes`(또는 `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`)가 stdout/stderr 리더 제한을 제어합니다. 유효 범위는 `65536` ~ `67108864` 이며 기본값은 `8388608` 입니다
-   입력: 도구 호출은 `inputs` 에 최소 1개 항목을 포함해야 하며, `{ "type": "text", "text": ... }` 또는 `{ "type": "local_image", "path": ... }` 여야 합니다
-   스레드 기본값: `model_reasoning_effort`, `web_search_mode`(레거시 `web_search_enabled` 보다 권장), `approval_policy`, `additional_directories` 에 대해 `default_thread_options` 를 구성하세요
-   턴 기본값: `idle_timeout_seconds` 와 취소 `signal` 에 대해 `default_turn_options` 를 구성하세요
-   안전: `sandbox_mode` 를 `working_directory` 와 함께 사용하세요. Git 저장소 밖에서는 `skip_git_repo_check=True` 를 설정하세요
-   실행 컨텍스트 스레드 지속성: `use_run_context_thread_id=True` 는 해당 컨텍스트를 공유하는 실행들 전반에서 run context 에 `thread_id` 를 저장하고 재사용합니다. 이를 위해서는 변경 가능한 run context(예: `dict` 또는 쓰기 가능한 객체 필드)가 필요합니다
-   실행 컨텍스트 키 기본값: 저장되는 키는 `name="codex"` 인 경우 기본적으로 `codex_thread_id` 이고, `name="codex_<suffix>"` 인 경우 `codex_thread_id_<suffix>` 입니다. 오버라이드하려면 `run_context_thread_id_key` 를 설정하세요
-   스레드 ID 우선순위: 호출별 `thread_id` 입력이 최우선이며, 그 다음은(활성화된 경우) run-context `thread_id`, 그 다음은 구성된 `thread_id` 옵션입니다
-   스트리밍: `on_stream` 은 스레드/턴 라이프사이클 이벤트와 아이템 이벤트(`reasoning`, `command_execution`, `mcp_tool_call`, `file_change`, `web_search`, `todo_list`, `error` 아이템 업데이트)를 수신합니다
-   출력: 결과에는 `response`, `usage`, `thread_id` 가 포함되며, usage 는 `RunContextWrapper.usage` 에 추가됩니다
-   구조: `output_schema` 는 typed 출력이 필요할 때 structured Codex 응답을 강제합니다
-   완전한 실행 가능한 샘플은 `examples/tools/codex.py` 와 `examples/tools/codex_same_thread.py` 를 참고하세요

## 함수 도구에서 오류 처리

`@function_tool` 로 함수 도구를 만들 때 `failure_error_function` 을 전달할 수 있습니다. 이는 도구 호출이 크래시할 경우 LLM 에 오류 응답을 제공하는 함수입니다.

-   기본적으로(즉, 아무것도 전달하지 않으면) 오류가 발생했음을 LLM 에 알려주는 `default_tool_error_function` 이 실행됩니다
-   자체 오류 함수를 전달하면, 대신 그 함수가 실행되고 그 응답이 LLM 에 전송됩니다
-   명시적으로 `None` 을 전달하면, 모든 도구 호출 오류가 사용자가 처리할 수 있도록 다시 raise 됩니다. 이는 모델이 잘못된 JSON 을 생성한 경우 `ModelBehaviorError` 일 수도 있고, 코드가 크래시한 경우 `UserError` 일 수도 있습니다

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

`FunctionTool` 객체를 수동으로 생성하는 경우에는, `on_invoke_tool` 함수 내부에서 오류를 처리해야 합니다.