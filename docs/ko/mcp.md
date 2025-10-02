---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP)은 애플리케이션이 도구와 컨텍스트를 언어 모델에 노출하는 방식을 표준화합니다. 공식 문서 중 일부는 다음과 같습니다.

> MCP는 애플리케이션이 LLM에 컨텍스트를 제공하는 방식을 표준화하는 오픈 프로토콜입니다. MCP를 AI 애플리케이션을 위한 USB-C 포트라고 생각해 보세요.
> USB-C가 기기를 다양한 주변 기기 및 액세서리에 표준 방식으로 연결해 주듯이, MCP는 AI 모델을 다양한 데이터 소스와 도구에 표준 방식으로 연결해 줍니다.

Agents Python SDK는 여러 MCP 트랜스포트를 이해합니다. 이를 통해 기존 MCP 서버를 재사용하거나 직접 구축하여 파일 시스템, HTTP, 또는 커넥터 기반 도구를 에이전트에 노출할 수 있습니다.

## MCP 통합 선택

MCP 서버를 에이전트에 연결하기 전에 도구 호출이 어디에서 실행되어야 하는지, 어떤 트랜스포트에 접근할 수 있는지를 결정하세요. 아래 매트릭스는 Python SDK가 지원하는 옵션을 요약합니다.

| 필요한 것                                                                              | 권장 옵션                                              |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| 모델을 대신해 OpenAI의 Responses API가 공개적으로 접근 가능한 MCP 서버를 호출하도록 함 | **Hosted MCP server tools** via [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| 로컬 또는 원격에서 실행 중인 Streamable HTTP 서버에 연결                               | **Streamable HTTP MCP servers** via [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| Server-Sent Events 를 사용하는 HTTP 를 구현한 서버와 통신                                | **HTTP with SSE MCP servers** via [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| 로컬 프로세스를 실행하고 stdin/stdout 으로 통신                                         | **stdio MCP servers** via [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

아래 섹션에서는 각 옵션의 사용 방법, 구성 방법, 그리고 어떤 경우에 어떤 트랜스포트를 선호해야 하는지 안내합니다.

## 1. Hosted MCP server tools

호스티드 툴은 전체 도구 왕복 과정을 OpenAI 인프라로 이전합니다. 코드가 도구를 나열하고 호출하는 대신,
[`HostedMCPTool`][agents.tool.HostedMCPTool] 이 서버 레이블(및 선택적 커넥터 메타데이터)을 Responses API로 전달합니다. 모델은 원격 서버의 도구를 나열하고, Python 프로세스로의 추가 콜백 없이 이를 호출합니다. 호스티드 툴은 현재 Responses API의 호스티드 MCP 통합을 지원하는 OpenAI 모델에서 동작합니다.

### 기본 호스티드 MCP 툴

에이전트의 `tools` 목록에 [`HostedMCPTool`][agents.tool.HostedMCPTool] 을 추가하여 호스티드 툴을 생성합니다. `tool_config`
딕셔너리는 REST API에 전송하는 JSON과 동일한 구조를 따릅니다:

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

### 스트리밍 호스티드 MCP 결과

호스티드 툴은 함수 도구와 동일한 방식으로 스트리밍 결과를 지원합니다. 모델이 여전히 작업 중일 때 점진적인 MCP 출력을 수신하려면 `Runner.run_streamed` 에 `stream=True` 를 전달하세요:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 선택적 승인 플로우

서버가 민감한 작업을 수행할 수 있는 경우 각 도구 실행 전에 사람 또는 프로그램적 승인을 요구할 수 있습니다. `tool_config` 의
`require_approval` 을 단일 정책(`"always"`, `"never"`) 또는 도구 이름을 정책에 매핑하는 딕셔너리로 구성하세요. Python 내부에서 결정을 내리려면 `on_approval_request` 콜백을 제공하세요.

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

콜백은 동기 또는 비동기일 수 있으며, 모델이 실행을 계속하기 위해 승인 데이터가 필요할 때마다 호출됩니다.

### 커넥터 기반 호스티드 서버

호스티드 MCP는 OpenAI 커넥터도 지원합니다. `server_url` 을 지정하는 대신 `connector_id` 와 액세스 토큰을 제공하세요.
Responses API가 인증을 처리하며, 호스티드 서버는 커넥터의 도구를 노출합니다.

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
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) 에서 확인할 수 있습니다.

## 2. Streamable HTTP MCP 서버

네트워크 연결을 직접 관리하려면
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 를 사용하세요. Streamable HTTP 서버는 트랜스포트를 직접 제어하거나, 지연 시간을 낮게 유지하면서 자체 인프라 내에서 서버를 운영하려는 경우에 이상적입니다.

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

생성자는 다음의 추가 옵션을 받습니다.

- `client_session_timeout_seconds` 는 HTTP 읽기 타임아웃을 제어합니다.
- `use_structured_content` 는 텍스트 출력 대신 `tool_result.structured_content` 를 선호할지 여부를 전환합니다.
- `max_retry_attempts` 및 `retry_backoff_seconds_base` 는 `list_tools()` 및 `call_tool()` 에 대한 자동 재시도를 추가합니다.
- `tool_filter` 를 사용하면 도구의 부분 집합만 노출할 수 있습니다([도구 필터링](#tool-filtering) 참조)

## 3. HTTP with SSE MCP 서버

MCP 서버가 HTTP with SSE 트랜스포트를 구현하는 경우
[`MCPServerSse`][agents.mcp.server.MCPServerSse] 를 인스턴스화하세요. 트랜스포트를 제외하면 API는 Streamable HTTP 서버와 동일합니다.

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

## 4. stdio MCP 서버

로컬 하위 프로세스로 실행되는 MCP 서버의 경우 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 를 사용하세요. SDK는 프로세스를 실행하고 파이프를 연 상태로 유지하며, 컨텍스트 매니저가 종료될 때 자동으로 닫습니다. 이 옵션은 빠른 개념 증명이나 서버가 커맨드라인 엔트리 포인트만 노출하는 경우에 유용합니다.

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

## 도구 필터링

각 MCP 서버는 에이전트가 필요로 하는 기능만 노출할 수 있도록 도구 필터를 지원합니다. 필터링은 생성 시점 또는 실행마다 동적으로 수행할 수 있습니다.

### 정적 도구 필터링

간단한 허용/차단 목록을 구성하려면 [`create_static_tool_filter`][agents.mcp.create_static_tool_filter] 를 사용하세요:

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

`allowed_tool_names` 와 `blocked_tool_names` 가 모두 제공되면 SDK는 먼저 허용 목록을 적용한 후, 나머지 집합에서 차단된 도구를 제거합니다.

### 동적 도구 필터링

더 정교한 로직이 필요한 경우 [`ToolFilterContext`][agents.mcp.ToolFilterContext] 를 받는 호출 가능 객체를 전달하세요. 이 콜러블은 동기 또는 비동기로 구현할 수 있으며, 도구를 노출해야 하는 경우 `True` 를 반환합니다.

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

## 프롬프트

MCP 서버는 에이전트 instructions 를 동적으로 생성하는 프롬프트도 제공할 수 있습니다. 프롬프트를 지원하는 서버는 두 가지 메서드를 노출합니다.

- `list_prompts()` 는 사용 가능한 프롬프트 템플릿을 열거합니다.
- `get_prompt(name, arguments)` 는 필요시 매개변수와 함께 구체적인 프롬프트를 가져옵니다.

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

모든 에이전트 실행은 각 MCP 서버에서 `list_tools()` 를 호출합니다. 원격 서버는 눈에 띄는 지연을 유발할 수 있으므로, 모든 MCP 서버 클래스는 `cache_tools_list` 옵션을 제공합니다. 도구 정의가 자주 변경되지 않는다고 확신할 때에만 `True` 로 설정하세요. 나중에 새 목록을 강제로 가져오려면 서버 인스턴스에서 `invalidate_tools_cache()` 를 호출하세요.

## 트레이싱

[트레이싱](./tracing.md)은 다음을 포함하여 MCP 활동을 자동으로 캡처합니다.

1. 도구 목록을 가져오기 위한 MCP 서버 호출
2. 도구 호출에 대한 MCP 관련 정보

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 추가 자료

- [Model Context Protocol](https://modelcontextprotocol.io/) – 명세 및 설계 가이드
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 실행 가능한 stdio, SSE, Streamable HTTP 샘플
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 승인 및 커넥터를 포함한 완전한 호스티드 MCP 데모