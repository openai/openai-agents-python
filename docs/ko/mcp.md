---
search:
  exclude: true
---
# Model context protocol (MCP)

The [Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) standardises how applications expose tools and
context to language models. From the official documentation:

> MCP is an open protocol that standardizes how applications provide context to LLMs. Think of MCP like a USB-C port for AI
> applications. Just as USB-C provides a standardized way to connect your devices to various peripherals and accessories, MCP
> provides a standardized way to connect AI models to different data sources and tools.

Agents Python SDK 는 여러 MCP 전송 방식을 이해합니다. 이를 통해 기존 MCP 서버를 재사용하거나, 자체 서버를 구축해 파일시스템, HTTP, 혹은 커넥터 기반 도구를 에이전트에 노출할 수 있습니다.

## Choosing an MCP integration

에이전트에 MCP 서버를 연결하기 전에, 도구 호출을 어디에서 실행할지와 접근 가능한 전송 방식을 결정하세요. 아래 매트릭스는 Python SDK 가 지원하는 옵션을 요약합니다.

| 필요한 것                                                                            | 권장 옵션                                              |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| 모델을 대신해 OpenAI 의 Responses API 가 공용 MCP 서버를 호출하도록 하기              | **호스티드 MCP 서버 도구** via [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| 로컬 또는 원격에서 실행하는 Streamable HTTP 서버에 연결                               | **Streamable HTTP MCP 서버** via [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| Server-Sent Events 를 사용하는 HTTP 구현 서버와 통신                                   | **HTTP with SSE MCP 서버** via [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| 로컬 프로세스를 실행하고 stdin/stdout 으로 통신                                       | **stdio MCP 서버** via [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

아래 섹션에서는 각 옵션에 대해 구성 방법과 언제 어떤 전송 방식을 선호해야 하는지를 설명합니다.

## 1. Hosted MCP server tools

호스티드 툴은 전체 도구 왕복 과정을 OpenAI 인프라에서 처리합니다. 코드에서 도구를 나열하고 호출하는 대신,
[`HostedMCPTool`][agents.tool.HostedMCPTool] 이 서버 레이블(및 선택적 커넥터 메타데이터)을 Responses API 로 전달합니다. 모델은 원격 서버의 도구를 나열하고 Python 프로세스에 추가 콜백 없이 이를 호출합니다. 호스티드 툴은 현재 Responses API 의 호스티드 MCP 통합을 지원하는 OpenAI 모델과 함께 동작합니다.

### Basic hosted MCP tool

에이전트의 `tools` 리스트에 [`HostedMCPTool`][agents.tool.HostedMCPTool] 을 추가하여 호스티드 툴을 생성합니다. `tool_config`
dict 는 REST API 에 전송할 JSON 과 동일한 구조를 따릅니다:

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

호스티드 서버는 도구를 자동으로 노출합니다. `mcp_servers` 에 추가할 필요가 없습니다.

### Streaming hosted MCP results

호스티드 툴은 함수 도구와 정확히 같은 방식으로 스트리밍 결과를 지원합니다. `Runner.run_streamed` 에 `stream=True` 를 전달하여
모델이 작업 중인 동안 증분 MCP 출력을 소비할 수 있습니다:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### Optional approval flows

서버가 민감한 작업을 수행할 수 있는 경우 각 도구 실행 전에 사람 또는 프로그램적 승인을 요구할 수 있습니다. `tool_config` 의
`require_approval` 을 단일 정책(`"always"`, `"never"`) 또는 도구 이름을 정책에 매핑한 dict 로 구성하세요. 의사결정을 Python 내부에서 수행하려면 `on_approval_request` 콜백을 제공하세요.

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

콜백은 동기 또는 비동기일 수 있으며, 모델이 계속 실행되기 위해 승인 데이터가 필요할 때마다 호출됩니다.

### Connector-backed hosted servers

호스티드 MCP 는 OpenAI 커넥터도 지원합니다. `server_url` 을 지정하는 대신 `connector_id` 와 액세스 토큰을 제공하세요.
Responses API 가 인증을 처리하고, 호스티드 서버가 커넥터의 도구를 노출합니다.

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

스트리밍, 승인, 커넥터를 포함한 완전한 호스티드 툴 샘플은
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) 에 있습니다.

## 2. Streamable HTTP MCP servers

네트워크 연결을 직접 관리하려면
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 를 사용하세요. Streamable HTTP 서버는 전송을 직접 제어하거나, 지연 시간을 낮게 유지하면서 자체 인프라 내에서 서버를 실행하고자 할 때 이상적입니다.

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

생성자는 다음 추가 옵션을 허용합니다:

- `client_session_timeout_seconds` 는 HTTP 읽기 타임아웃을 제어합니다
- `use_structured_content` 는 `tool_result.structured_content` 를 텍스트 출력보다 선호할지 여부를 토글합니다
- `max_retry_attempts` 및 `retry_backoff_seconds_base` 는 `list_tools()` 및 `call_tool()` 에 자동 재시도를 추가합니다
- `tool_filter` 를 사용하면 노출할 도구의 서브셋만 선택할 수 있습니다(see [도구 필터링](#tool-filtering))

## 3. HTTP with SSE MCP servers

MCP 서버가 HTTP with SSE 전송을 구현하는 경우,
[`MCPServerSse`][agents.mcp.server.MCPServerSse] 를 인스턴스화하세요. 전송 방식을 제외하면 API 는 Streamable HTTP 서버와 동일합니다.

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

로컬 하위 프로세스로 실행되는 MCP 서버에는 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 를 사용하세요. SDK 가 프로세스를 시작하고 파이프를 열어두며 컨텍스트 매니저가 종료될 때 자동으로 닫습니다. 이 옵션은 빠른 개념 증명이나 서버가 커맨드라인 엔트리 포인트만 노출하는 경우에 유용합니다.

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

## Tool filtering

각 MCP 서버는 에이전트에 필요한 기능만 노출할 수 있도록 도구 필터를 지원합니다. 필터링은 생성 시점 또는 실행 단위로 동적으로 수행할 수 있습니다.

### Static tool filtering

[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] 를 사용하여 간단한 허용/차단 리스트를 구성하세요:

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

`allowed_tool_names` 와 `blocked_tool_names` 가 모두 제공되면 SDK 는 먼저 허용 리스트를 적용한 뒤, 남은 집합에서 차단된 도구를 제거합니다.

### Dynamic tool filtering

더 정교한 로직을 위해 [`ToolFilterContext`][agents.mcp.ToolFilterContext] 를 받는 호출 가능 객체를 전달하세요. 이 호출체는 동기 또는 비동기일 수 있으며, 도구를 노출해야 할 때 `True` 를 반환합니다.

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

필터 컨텍스트는 활성 `run_context`, 도구를 요청하는 `agent`, 그리고 `server_name` 을 제공합니다.

## Prompts

MCP 서버는 에이전트 instructions 를 동적으로 생성하는 프롬프트도 제공할 수 있습니다. 프롬프트를 지원하는 서버는 두 가지
메서드를 노출합니다:

- `list_prompts()` 는 사용 가능한 프롬프트 템플릿을 열거합니다
- `get_prompt(name, arguments)` 는 필요하면 매개변수와 함께 구체적인 프롬프트를 가져옵니다

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

## Caching

모든 에이전트 실행은 각 MCP 서버에서 `list_tools()` 를 호출합니다. 원격 서버는 눈에 띄는 지연을 유발할 수 있으므로, 모든 MCP
서버 클래스는 `cache_tools_list` 옵션을 노출합니다. 도구 정의가 자주 변경되지 않는다고 확신하는 경우에만 `True` 로 설정하세요. 나중에 목록을 새로 강제하려면 서버 인스턴스에서 `invalidate_tools_cache()` 를 호출하세요.

## Tracing

[Tracing](./tracing.md) 은 다음을 포함해 MCP 활동을 자동으로 캡처합니다:

1. MCP 서버에 도구 목록을 요청하는 호출
2. 도구 호출의 MCP 관련 정보

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## Further reading

- [Model Context Protocol](https://modelcontextprotocol.io/) – 사양과 설계 가이드
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 실행 가능한 stdio, SSE, Streamable HTTP 샘플
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 승인 및 커넥터를 포함한 완전한 호스티드 MCP 데모