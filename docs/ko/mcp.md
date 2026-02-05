---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP)은 애플리케이션이 언어 모델에 도구와 컨텍스트를 노출하는 방식을 표준화합니다. 공식 문서에서 인용하면 다음과 같습니다:

> MCP는 애플리케이션이 LLM 에 컨텍스트를 제공하는 방식을 표준화하는 오픈 프로토콜입니다. MCP 를 AI 애플리케이션을 위한 USB-C 포트처럼 생각해 보세요
> USB-C 가 기기를 다양한 주변기기와 액세서리에 연결하는 표준화된 방법을 제공하듯이, MCP 는 AI 모델을 다양한 데이터 소스와 도구에 연결하는 표준화된 방법을 제공합니다

Agents Python SDK 는 여러 MCP 전송(transport)을 이해합니다. 이를 통해 기존 MCP 서버를 재사용하거나, 파일시스템, HTTP, 또는 커넥터 기반 도구를 에이전트에 노출하기 위한 자체 서버를 구축할 수 있습니다

## MCP 통합 선택

MCP 서버를 에이전트에 연결하기 전에, 도구 호출이 어디에서 실행되어야 하는지와 어떤 전송에 도달할 수 있는지를 결정하세요. 아래 매트릭스는 Python SDK 가 지원하는 옵션을 요약합니다

| What you need                                                                        | Recommended option                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| OpenAI's Responses API 가 모델을 대신해 공개적으로 접근 가능한 MCP 서버를 호출하게 하기| [`HostedMCPTool`][agents.tool.HostedMCPTool] 을 통한 **Hosted MCP server tools** |
| 로컬 또는 원격에서 직접 실행하는 Streamable HTTP 서버에 연결                          | [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 을 통한 **Streamable HTTP MCP servers** |
| Server-Sent Events 를 사용하는 HTTP 를 구현한 서버와 통신                              | [`MCPServerSse`][agents.mcp.server.MCPServerSse] 를 통한 **HTTP with SSE MCP servers** |
| 로컬 프로세스를 실행하고 stdin/stdout 으로 통신                                       | [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 를 통한 **stdio MCP servers** |

아래 섹션에서는 각 옵션, 구성 방법, 그리고 한 전송을 다른 전송보다 선호해야 하는 경우를 안내합니다

## 에이전트 수준 MCP 구성

전송을 선택하는 것 외에도, `Agent.mcp_config` 를 설정하여 MCP 도구가 준비되는 방식을 조정할 수 있습니다

```python
from agents import Agent

agent = Agent(
    name="Assistant",
    mcp_servers=[server],
    mcp_config={
        # Try to convert MCP tool schemas to strict JSON schema.
        "convert_schemas_to_strict": True,
        # If None, MCP tool failures are raised as exceptions instead of
        # returning model-visible error text.
        "failure_error_function": None,
    },
)
```

참고:

- `convert_schemas_to_strict` 는 best-effort 입니다. 스키마를 변환할 수 없는 경우 원래 스키마가 사용됩니다
- `failure_error_function` 은 MCP 도구 호출 실패가 모델에 어떻게 노출되는지 제어합니다
- `failure_error_function` 이 설정되지 않으면, SDK 는 기본 도구 오류 포매터를 사용합니다
- 서버 수준의 `failure_error_function` 은 해당 서버에 대해 `Agent.mcp_config["failure_error_function"]` 을 오버라이드합니다

## 1. Hosted MCP server tools

Hosted tool 은 전체 도구 라운드트립을 OpenAI 인프라로 밀어 넣습니다. 코드에서 도구를 나열하고 호출하는 대신, [`HostedMCPTool`][agents.tool.HostedMCPTool] 이 서버 레이블(및 선택적 커넥터 메타데이터)을 Responses API 로 전달합니다. 모델은 원격 서버의 도구를 나열하고, Python 프로세스에 대한 추가 콜백 없이 이를 호출합니다. Hosted tool 은 현재 Responses API 의 hosted MCP 통합을 지원하는 OpenAI 모델에서 동작합니다

### 기본 hosted MCP tool

에이전트의 `tools` 목록에 [`HostedMCPTool`][agents.tool.HostedMCPTool] 을 추가해 hosted tool 을 생성합니다. `tool_config` dict 는 REST API 로 보낼 JSON 을 그대로 반영합니다:

```python
import asyncio

from agents import Agent, HostedMCPTool, Runner

async def main() -> None:
    agent = Agent(
        name="Assistant",
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "gitmcp",
                    "server_url": "https://gitmcp.io/openai/codex",
                    "require_approval": "never",
                }
            )
        ],
    )

    result = await Runner.run(agent, "Which language is this repository written in?")
    print(result.final_output)

asyncio.run(main())
```

hosted 서버는 도구를 자동으로 노출하므로, `mcp_servers` 에 추가하지 않습니다

### Hosted MCP 결과 스트리밍

Hosted tool 은 함수 도구와 정확히 같은 방식으로 결과 스트리밍을 지원합니다. `Runner.run_streamed` 에 `stream=True` 를 전달하면, 모델이 아직 작업 중인 동안에도 점진적인 MCP 출력을 소비할 수 있습니다:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 선택적 승인 흐름

서버가 민감한 작업을 수행할 수 있다면, 각 도구 실행 전에 사람 또는 프로그램 기반 승인을 요구할 수 있습니다. `tool_config` 에 `require_approval` 을 설정하되, 단일 정책(`"always"`, `"never"`) 또는 도구 이름을 정책에 매핑하는 dict 를 사용할 수 있습니다. Python 내부에서 결정을 내리려면 `on_approval_request` 콜백을 제공하세요

```python
from agents import MCPToolApprovalFunctionResult, MCPToolApprovalRequest

SAFE_TOOLS = {"read_project_metadata"}

def approve_tool(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
    if request.data.name in SAFE_TOOLS:
        return {"approve": True}
    return {"approve": False, "reason": "Escalate to a human reviewer"}

agent = Agent(
    name="Assistant",
    tools=[
        HostedMCPTool(
            tool_config={
                "type": "mcp",
                "server_label": "gitmcp",
                "server_url": "https://gitmcp.io/openai/codex",
                "require_approval": "always",
            },
            on_approval_request=approve_tool,
        )
    ],
)
```

콜백은 동기 또는 비동기일 수 있으며, 모델이 계속 실행하기 위해 승인 데이터가 필요할 때마다 호출됩니다

### 커넥터 기반 hosted 서버

Hosted MCP 는 OpenAI 커넥터도 지원합니다. `server_url` 을 지정하는 대신 `connector_id` 와 액세스 토큰을 제공하세요. Responses API 가 인증을 처리하고, hosted 서버가 커넥터의 도구를 노출합니다

```python
import os

HostedMCPTool(
    tool_config={
        "type": "mcp",
        "server_label": "google_calendar",
        "connector_id": "connector_googlecalendar",
        "authorization": os.environ["GOOGLE_CALENDAR_AUTHORIZATION"],
        "require_approval": "never",
    }
)
```

스트리밍, 승인, 커넥터를 포함한 완전하게 동작하는 hosted tool 샘플은
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) 에 있습니다

## 2. Streamable HTTP MCP servers

네트워크 연결을 직접 관리하고 싶다면 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 를 사용하세요. Streamable HTTP 서버는 전송을 제어하고 싶거나, 지연 시간을 낮게 유지하면서 자체 인프라 내에서 서버를 실행하고자 할 때 이상적입니다

```python
import asyncio
import os

from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp
from agents.model_settings import ModelSettings

async def main() -> None:
    token = os.environ["MCP_SERVER_TOKEN"]
    async with MCPServerStreamableHttp(
        name="Streamable HTTP Python Server",
        params={
            "url": "http://localhost:8000/mcp",
            "headers": {"Authorization": f"Bearer {token}"},
            "timeout": 10,
        },
        cache_tools_list=True,
        max_retry_attempts=3,
    ) as server:
        agent = Agent(
            name="Assistant",
            instructions="Use the MCP tools to answer the questions.",
            mcp_servers=[server],
            model_settings=ModelSettings(tool_choice="required"),
        )

        result = await Runner.run(agent, "Add 7 and 22.")
        print(result.final_output)

asyncio.run(main())
```

생성자는 추가 옵션을 받습니다:

- `client_session_timeout_seconds` 는 HTTP 읽기 타임아웃을 제어합니다
- `use_structured_content` 는 텍스트 출력보다 `tool_result.structured_content` 를 선호할지 여부를 토글합니다
- `max_retry_attempts` 와 `retry_backoff_seconds_base` 는 `list_tools()` 및 `call_tool()` 에 자동 재시도를 추가합니다
- `tool_filter` 는 일부 도구만 노출할 수 있게 합니다([Tool filtering](#tool-filtering) 참조)
- `require_approval` 는 로컬 MCP 도구에 대해 휴먼인더루프 승인 정책을 활성화합니다
- `failure_error_function` 은 모델에 보이는 MCP 도구 실패 메시지를 커스터마이즈합니다. 대신 오류를 raise 하려면 `None` 으로 설정하세요
- `tool_meta_resolver` 는 `call_tool()` 이전에 호출별 MCP `_meta` 페이로드를 주입합니다

### 로컬 MCP 서버의 승인 정책

`MCPServerStdio`, `MCPServerSse`, `MCPServerStreamableHttp` 는 모두 `require_approval` 을 받습니다

지원 형태:

- 모든 도구에 대해 `"always"` 또는 `"never"`
- `True` / `False` (always/never 와 동일)
- 도구별 맵, 예: `{"delete_file": "always", "read_file": "never"}`
- 그룹화된 객체:
  `{"always": {"tool_names": [...]}, "never": {"tool_names": [...]}}`

```python
async with MCPServerStreamableHttp(
    name="Filesystem MCP",
    params={"url": "http://localhost:8000/mcp"},
    require_approval={"always": {"tool_names": ["delete_file"]}},
) as server:
    ...
```

전체 pause/resume 흐름은 [Human-in-the-loop](human_in_the_loop.md) 및 `examples/mcp/get_all_mcp_tools_example/main.py` 를 참고하세요

### `tool_meta_resolver` 로 호출별 메타데이터 전달

MCP 서버가 `_meta` 에 요청 메타데이터(예: 테넌트 ID 또는 트레이스 컨텍스트)를 기대한다면 `tool_meta_resolver` 를 사용하세요. 아래 예시는 `Runner.run(...)` 에 `context` 로 `dict` 를 전달한다고 가정합니다

```python
from agents.mcp import MCPServerStreamableHttp, MCPToolMetaContext


def resolve_meta(context: MCPToolMetaContext) -> dict[str, str] | None:
    run_context_data = context.run_context.context or {}
    tenant_id = run_context_data.get("tenant_id")
    if tenant_id is None:
        return None
    return {"tenant_id": str(tenant_id), "source": "agents-sdk"}


server = MCPServerStreamableHttp(
    name="Metadata-aware MCP",
    params={"url": "http://localhost:8000/mcp"},
    tool_meta_resolver=resolve_meta,
)
```

run context 가 Pydantic 모델, dataclass, 또는 커스텀 클래스라면, 대신 속성 접근으로 테넌트 ID 를 읽으세요

### MCP 도구 출력: 텍스트와 이미지

MCP 도구가 이미지 콘텐츠를 반환하면, SDK 는 이를 이미지 도구 출력 엔트리로 자동 매핑합니다. 텍스트/이미지 혼합 응답은 출력 아이템의 리스트로 전달되므로, 에이전트는 일반 함수 도구의 이미지 출력과 동일한 방식으로 MCP 이미지 결과를 소비할 수 있습니다

## 3. HTTP with SSE MCP servers

!!! warning

    MCP 프로젝트는 Server-Sent Events 전송을 deprecated 했습니다. 새 통합에는 Streamable HTTP 또는 stdio 를 선호하고, SSE 는 레거시 서버에만 유지하세요

MCP 서버가 HTTP with SSE 전송을 구현한다면 [`MCPServerSse`][agents.mcp.server.MCPServerSse] 를 인스턴스화하세요. 전송을 제외하면 API 는 Streamable HTTP 서버와 동일합니다

```python

from agents import Agent, Runner
from agents.model_settings import ModelSettings
from agents.mcp import MCPServerSse

workspace_id = "demo-workspace"

async with MCPServerSse(
    name="SSE Python Server",
    params={
        "url": "http://localhost:8000/sse",
        "headers": {"X-Workspace": workspace_id},
    },
    cache_tools_list=True,
) as server:
    agent = Agent(
        name="Assistant",
        mcp_servers=[server],
        model_settings=ModelSettings(tool_choice="required"),
    )
    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(result.final_output)
```

## 4. stdio MCP servers

로컬 서브프로세스로 실행되는 MCP 서버에는 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 를 사용하세요. SDK 는 프로세스를 스폰하고, 파이프를 열린 상태로 유지하며, 컨텍스트 매니저가 종료될 때 자동으로 닫습니다. 이 옵션은 빠른 PoC 나 서버가 커맨드 라인 엔트리 포인트만 노출하는 경우에 유용합니다

```python
from pathlib import Path
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

current_dir = Path(__file__).parent
samples_dir = current_dir / "sample_files"

async with MCPServerStdio(
    name="Filesystem Server via npx",
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", str(samples_dir)],
    },
) as server:
    agent = Agent(
        name="Assistant",
        instructions="Use the files in the sample directory to answer questions.",
        mcp_servers=[server],
    )
    result = await Runner.run(agent, "List the files available to you.")
    print(result.final_output)
```

## 5. MCP 서버 매니저

여러 MCP 서버가 있을 때는 `MCPServerManager` 를 사용해 미리 연결하고, 연결된 부분집합을 에이전트에 노출하세요

```python
from agents import Agent, Runner
from agents.mcp import MCPServerManager, MCPServerStreamableHttp

servers = [
    MCPServerStreamableHttp(name="calendar", params={"url": "http://localhost:8000/mcp"}),
    MCPServerStreamableHttp(name="docs", params={"url": "http://localhost:8001/mcp"}),
]

async with MCPServerManager(servers) as manager:
    agent = Agent(
        name="Assistant",
        instructions="Use MCP tools when they help.",
        mcp_servers=manager.active_servers,
    )
    result = await Runner.run(agent, "Which MCP tools are available?")
    print(result.final_output)
```

주요 동작:

- `drop_failed_servers=True`(기본값) 인 경우 `active_servers` 는 성공적으로 연결된 서버만 포함합니다
- 실패는 `failed_servers` 와 `errors` 에 추적됩니다
- 첫 연결 실패에서 raise 하려면 `strict=True` 로 설정하세요
- 실패한 서버를 재시도하려면 `reconnect(failed_only=True)` 를, 모든 서버를 재시작하려면 `reconnect(failed_only=False)` 를 호출하세요
- 라이프사이클 동작을 조정하려면 `connect_timeout_seconds`, `cleanup_timeout_seconds`, `connect_in_parallel` 을 사용하세요

## Tool filtering

각 MCP 서버는 도구 필터를 지원하여, 에이전트에 필요한 함수만 노출할 수 있습니다. 필터링은 생성 시점에 수행하거나, 실행(run)마다 동적으로 수행할 수 있습니다

### 정적 도구 필터링

단순한 허용/차단 목록을 구성하려면 [`create_static_tool_filter`][agents.mcp.create_static_tool_filter] 를 사용하세요:

```python
from pathlib import Path

from agents.mcp import MCPServerStdio, create_static_tool_filter

samples_dir = Path("/path/to/files")

filesystem_server = MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", str(samples_dir)],
    },
    tool_filter=create_static_tool_filter(allowed_tool_names=["read_file", "write_file"]),
)
```

`allowed_tool_names` 와 `blocked_tool_names` 를 모두 제공하면, SDK 는 먼저 허용 목록을 적용한 다음 남은 집합에서 차단 도구를 제거합니다

### 동적 도구 필터링

더 정교한 로직을 위해서는 [`ToolFilterContext`][agents.mcp.ToolFilterContext] 를 받는 callable 을 전달하세요. callable 은 동기 또는 비동기일 수 있으며, 도구를 노출해야 할 때 `True` 를 반환합니다

```python
from pathlib import Path

from agents.mcp import MCPServerStdio, ToolFilterContext

samples_dir = Path("/path/to/files")

async def context_aware_filter(context: ToolFilterContext, tool) -> bool:
    if context.agent.name == "Code Reviewer" and tool.name.startswith("danger_"):
        return False
    return True

async with MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", str(samples_dir)],
    },
    tool_filter=context_aware_filter,
) as server:
    ...
```

필터 컨텍스트는 활성 `run_context`, 도구를 요청하는 `agent`, 그리고 `server_name` 을 노출합니다

## 프롬프트

MCP 서버는 에이전트 instructions 를 동적으로 생성하는 프롬프트도 제공할 수 있습니다. 프롬프트를 지원하는 서버는 두 가지 메서드를 노출합니다:

- `list_prompts()` 는 사용 가능한 프롬프트 템플릿을 열거합니다
- `get_prompt(name, arguments)` 는 구체적인 프롬프트를 가져오며, 선택적으로 매개변수를 포함할 수 있습니다

```python
from agents import Agent

prompt_result = await server.get_prompt(
    "generate_code_review_instructions",
    {"focus": "security vulnerabilities", "language": "python"},
)
instructions = prompt_result.messages[0].content.text

agent = Agent(
    name="Code Reviewer",
    instructions=instructions,
    mcp_servers=[server],
)
```

## 캐싱

모든 에이전트 실행은 각 MCP 서버에 대해 `list_tools()` 를 호출합니다. 원격 서버는 눈에 띄는 지연 시간을 유발할 수 있으므로, 모든 MCP 서버 클래스는 `cache_tools_list` 옵션을 노출합니다. 도구 정의가 자주 바뀌지 않는다고 확신할 때만 `True` 로 설정하세요. 이후에 최신 목록을 강제로 가져오려면 서버 인스턴스에서 `invalidate_tools_cache()` 를 호출하세요

## 트레이싱

[Tracing](./tracing.md) 은 다음을 포함해 MCP 활동을 자동으로 캡처합니다:

1. 도구 목록을 가져오기 위해 MCP 서버를 호출하는 동작
2. 도구 호출 시 MCP 관련 정보

![MCP 트레이싱 스크린샷](../assets/images/mcp-tracing.jpg)

## 추가 읽을거리

- [Model Context Protocol](https://modelcontextprotocol.io/) – 명세 및 설계 가이드
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 실행 가능한 stdio, SSE, Streamable HTTP 샘플
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 승인과 커넥터를 포함한 완전한 hosted MCP 데모