---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (aka MCP)은 LLM에 도구와 컨텍스트를 제공하는 방법입니다. MCP 문서에서 인용:

> MCP는 애플리케이션이 LLM에 컨텍스트를 제공하는 방식을 표준화한 개방형 프로토콜입니다. MCP를 AI 애플리케이션을 위한 USB-C 포트라고 생각해 보세요. USB-C가 다양한 주변기기와 액세서리에 기기를 연결하는 표준화된 방식을 제공하듯, MCP는 AI 모델을 서로 다른 데이터 소스와 도구에 연결하는 표준화된 방식을 제공합니다.

Agents SDK 는 MCP를 지원합니다. 이를 통해 다양한 MCP 서버를 사용하여 에이전트에 도구와 프롬프트를 제공할 수 있습니다.

## MCP 서버

현재 MCP 사양은 사용하는 전송 메커니즘에 따라 세 가지 유형의 서버를 정의합니다:

1. **stdio** 서버는 애플리케이션의 하위 프로세스로 실행됩니다. 로컬에서 실행된다고 생각하시면 됩니다
2. **HTTP over SSE** 서버는 원격으로 실행됩니다. URL을 통해 연결합니다
3. **Streamable HTTP** 서버는 MCP 사양에 정의된 Streamable HTTP 전송을 사용하여 원격으로 실행됩니다

이들 서버에 연결하려면 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio], [`MCPServerSse`][agents.mcp.server.MCPServerSse], [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 클래스를 사용할 수 있습니다.

예를 들어, [공식 MCP filesystem 서버](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem)는 다음과 같이 사용할 수 있습니다.

```python
from agents.run_context import RunContextWrapper

async with MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    }
) as server:
    # Note: In practice, you typically add the server to an Agent
    # and let the framework handle tool listing automatically.
    # Direct calls to list_tools() require run_context and agent parameters.
    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test", instructions="test")
    tools = await server.list_tools(run_context, agent)
```

## MCP 서버 사용

MCP 서버는 에이전트에 추가할 수 있습니다. Agents SDK 는 에이전트가 실행될 때마다 MCP 서버에서 `list_tools()` 를 호출합니다. 이를 통해 LLM이 MCP 서버의 도구를 인지합니다. LLM이 MCP 서버의 도구를 호출하면, SDK가 해당 서버에서 `call_tool()` 을 호출합니다.

```python

agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## 도구 필터링

MCP 서버에서 도구 필터를 구성하여 에이전트에 어떤 도구를 제공할지 필터링할 수 있습니다. SDK는 정적 및 동적 도구 필터링을 모두 지원합니다.

### 정적 도구 필터링

간단한 허용/차단 목록의 경우 정적 필터링을 사용할 수 있습니다:

```python
from agents.mcp import create_static_tool_filter

# Only expose specific tools from this server
server = MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    },
    tool_filter=create_static_tool_filter(
        allowed_tool_names=["read_file", "write_file"]
    )
)

# Exclude specific tools from this server
server = MCPServerStdio(
    params={
        "command": "npx", 
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    },
    tool_filter=create_static_tool_filter(
        blocked_tool_names=["delete_file"]
    )
)

```

**`allowed_tool_names` 와 `blocked_tool_names` 가 모두 구성된 경우 처리 순서는 다음과 같습니다:**
1. 먼저 `allowed_tool_names` (허용 목록)를 적용 - 지정된 도구만 유지
2. 그다음 `blocked_tool_names` (차단 목록)를 적용 - 남은 도구 중 지정된 도구 제외

예를 들어 `allowed_tool_names=["read_file", "write_file", "delete_file"]` 와 `blocked_tool_names=["delete_file"]` 를 구성하면 `read_file` 과 `write_file` 도구만 제공됩니다.

### 동적 도구 필터링

더 복잡한 필터링 로직이 필요하면 함수로 동적 필터를 사용할 수 있습니다:

```python
from agents.mcp import ToolFilterContext

# Simple synchronous filter
def custom_filter(context: ToolFilterContext, tool) -> bool:
    """Example of a custom tool filter."""
    # Filter logic based on tool name patterns
    return tool.name.startswith("allowed_prefix")

# Context-aware filter
def context_aware_filter(context: ToolFilterContext, tool) -> bool:
    """Filter tools based on context information."""
    # Access agent information
    agent_name = context.agent.name

    # Access server information  
    server_name = context.server_name

    # Implement your custom filtering logic here
    return some_filtering_logic(agent_name, server_name, tool)

# Asynchronous filter
async def async_filter(context: ToolFilterContext, tool) -> bool:
    """Example of an asynchronous filter."""
    # Perform async operations if needed
    result = await some_async_check(context, tool)
    return result

server = MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    },
    tool_filter=custom_filter  # or context_aware_filter or async_filter
)
```

`ToolFilterContext` 는 다음에 접근할 수 있습니다:
- `run_context`: 현재 실행 컨텍스트
- `agent`: 도구를 요청한 에이전트
- `server_name`: MCP 서버 이름

## 프롬프트

MCP 서버는 에이전트 instructions 를 동적으로 생성하는 데 사용할 수 있는 프롬프트를 제공할 수도 있습니다. 이를 통해 매개변수로 사용자화할 수 있는 재사용 가능한 instructions 템플릿을 만들 수 있습니다.

### 프롬프트 사용

프롬프트를 지원하는 MCP 서버는 두 가지 핵심 메서드를 제공합니다:

- `list_prompts()`: 서버에서 사용 가능한 모든 프롬프트 나열
- `get_prompt(name, arguments)`: 선택적 매개변수와 함께 특정 프롬프트 가져오기

```python
# List available prompts
prompts_result = await server.list_prompts()
for prompt in prompts_result.prompts:
    print(f"Prompt: {prompt.name} - {prompt.description}")

# Get a specific prompt with parameters
prompt_result = await server.get_prompt(
    "generate_code_review_instructions",
    {"focus": "security vulnerabilities", "language": "python"}
)
instructions = prompt_result.messages[0].content.text

# Use the prompt-generated instructions with an Agent
agent = Agent(
    name="Code Reviewer",
    instructions=instructions,  # Instructions from MCP prompt
    mcp_servers=[server]
)
```

## 캐싱

에이전트가 실행될 때마다 MCP 서버에서 `list_tools()` 를 호출합니다. 특히 서버가 원격인 경우 지연 시간이 늘어날 수 있습니다. 도구 목록을 자동으로 캐시하려면 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio], [`MCPServerSse`][agents.mcp.server.MCPServerSse], [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 에 `cache_tools_list=True` 를 전달하면 됩니다. 도구 목록이 변경되지 않는다고 확신할 때만 사용하세요.

캐시를 무효화하려면 서버에서 `invalidate_tools_cache()` 를 호출하면 됩니다.

## 엔드 투 엔드 code examples

작동하는 전체 code examples 는 [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp)에서 확인하세요.

## 트레이싱

[Tracing](./tracing.md)은 다음을 포함하여 MCP 작업을 자동으로 캡처합니다:

1. 도구 목록을 가져오기 위한 MCP 서버 호출
2. 함수 호출에 대한 MCP 관련 정보

![MCP 트레이싱 스크린샷](../assets/images/mcp-tracing.jpg)