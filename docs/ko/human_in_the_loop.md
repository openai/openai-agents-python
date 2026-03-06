---
search:
  exclude: true
---
# 휴먼인더루프 (HITL)

human-in-the-loop (HITL) 흐름을 사용하면 민감한 도구 호출을 사람이 승인하거나 거부할 때까지 에이전트 실행을 일시 중지할 수 있습니다. 도구는 승인 필요 여부를 선언하고, 실행 결과의 보류 중 승인 항목은 인터럽션으로 표시되며, `RunState` 를 통해 결정 이후 실행을 직렬화하고 재개할 수 있습니다.

이 승인 표면은 현재 최상위 에이전트에만 제한되지 않고 실행 전체에 적용됩니다. 동일한 패턴은 도구가 현재 에이전트에 속한 경우, 핸드오프를 통해 도달한 에이전트에 속한 경우, 또는 중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에 속한 경우에도 적용됩니다. 중첩 `Agent.as_tool()` 의 경우에도 인터럽션은 외부 실행에 표시되므로, 외부 `RunState` 에서 승인 또는 거부하고 원래 최상위 실행을 재개합니다.

`Agent.as_tool()` 에서는 서로 다른 두 계층에서 승인이 발생할 수 있습니다: 에이전트 도구 자체가 `Agent.as_tool(..., needs_approval=...)` 를 통해 승인을 요구할 수 있고, 중첩 실행이 시작된 뒤 중첩 에이전트 내부 도구가 자체 승인을 추가로 발생시킬 수 있습니다. 둘 다 동일한 외부 실행 인터럽션 흐름으로 처리됩니다.

이 페이지는 `interruptions` 를 통한 수동 승인 흐름에 중점을 둡니다. 앱이 코드에서 결정을 내릴 수 있다면, 일부 도구 유형은 프로그래밍 방식 승인 콜백도 지원하므로 실행을 일시 중지하지 않고 계속 진행할 수 있습니다.

## 승인 필요 도구 표시

`needs_approval` 을 `True` 로 설정하면 항상 승인이 필요하며, 호출별로 결정하는 비동기 함수를 제공할 수도 있습니다. 이 callable 은 실행 컨텍스트, 파싱된 도구 매개변수, 도구 호출 ID 를 받습니다.

```python
from agents import Agent, Runner, function_tool


@function_tool(needs_approval=True)
async def cancel_order(order_id: int) -> str:
    return f"Cancelled order {order_id}"


async def requires_review(_ctx, params, _call_id) -> bool:
    return "refund" in params.get("subject", "").lower()


@function_tool(needs_approval=requires_review)
async def send_email(subject: str, body: str) -> str:
    return f"Sent '{subject}'"


agent = Agent(
    name="Support agent",
    instructions="Handle tickets and ask for approval when needed.",
    tools=[cancel_order, send_email],
)
```

`needs_approval` 은 [`function_tool`][agents.tool.function_tool], [`Agent.as_tool`][agents.agent.Agent.as_tool], [`ShellTool`][agents.tool.ShellTool], [`ApplyPatchTool`][agents.tool.ApplyPatchTool] 에서 사용할 수 있습니다. 로컬 MCP 서버도 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio], [`MCPServerSse`][agents.mcp.server.MCPServerSse], [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 의 `require_approval` 을 통해 승인을 지원합니다. 호스티드 MCP 서버는 [`HostedMCPTool`][agents.tool.HostedMCPTool] 의 `tool_config={"require_approval": "always"}` 와 선택적 `on_approval_request` 콜백을 통해 승인을 지원합니다. Shell 및 apply_patch 도구는 인터럽션을 노출하지 않고 자동 승인 또는 자동 거부하려는 경우 `on_approval` 콜백을 받을 수 있습니다.

## 승인 흐름 작동 방식

1. 모델이 도구 호출을 생성하면 러너가 해당 승인 규칙(`needs_approval`, `require_approval`, 또는 호스티드 MCP 동등 설정)을 평가합니다
2. 해당 도구 호출에 대한 승인 결정이 이미 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 에 저장되어 있다면, 러너는 추가 프롬프트 없이 진행합니다. 호출별 승인은 특정 호출 ID 범위로 제한됩니다. 실행의 남은 기간 동안 해당 도구의 이후 호출에도 동일 결정을 유지하려면 `always_approve=True` 또는 `always_reject=True` 를 전달하세요
3. 그렇지 않으면 실행이 일시 중지되고 `RunResult.interruptions` (또는 `RunResultStreaming.interruptions`) 에 `agent.name`, `tool_name`, `arguments` 같은 세부 정보를 담은 [`ToolApprovalItem`][agents.items.ToolApprovalItem] 항목이 포함됩니다. 여기에는 핸드오프 이후 또는 중첩 `Agent.as_tool()` 실행 내부에서 발생한 승인도 포함됩니다
4. 결과를 `result.to_state()` 로 `RunState` 로 변환하고, `state.approve(...)` 또는 `state.reject(...)` 를 호출한 다음, `Runner.run(agent, state)` 또는 `Runner.run_streamed(agent, state)` 로 재개합니다. 여기서 `agent` 는 해당 실행의 원래 최상위 에이전트입니다
5. 재개된 실행은 중단된 지점부터 계속되며, 새로운 승인이 필요하면 이 흐름으로 다시 진입합니다

`always_approve=True` 또는 `always_reject=True` 로 생성된 고정 결정은 실행 상태에 저장되므로, 나중에 동일한 일시 중지 실행을 재개할 때 `state.to_string()` / `RunState.from_string(...)` 및 `state.to_json()` / `RunState.from_json(...)` 에서도 유지됩니다.

동일한 패스에서 모든 보류 승인을 해결할 필요는 없습니다. `interruptions` 에는 일반 함수 도구, 호스티드 MCP 승인, 중첩 `Agent.as_tool()` 승인이 섞여 있을 수 있습니다. 일부 항목만 승인 또는 거부한 뒤 다시 실행하면, 해결된 호출은 계속 진행되고 미해결 항목은 `interruptions` 에 남아 실행을 다시 일시 중지합니다.

## 자동 승인 결정

수동 `interruptions` 이 가장 일반적인 패턴이지만, 유일한 방식은 아닙니다:

- 로컬 [`ShellTool`][agents.tool.ShellTool] 및 [`ApplyPatchTool`][agents.tool.ApplyPatchTool] 은 `on_approval` 을 사용해 코드에서 즉시 승인 또는 거부할 수 있습니다
- [`HostedMCPTool`][agents.tool.HostedMCPTool] 은 같은 유형의 프로그래밍 방식 결정을 위해 `tool_config={"require_approval": "always"}` 와 `on_approval_request` 를 함께 사용할 수 있습니다
- 일반 [`function_tool`][agents.tool.function_tool] 도구와 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 은 이 페이지의 수동 인터럽션 흐름을 사용합니다

이러한 콜백이 결정을 반환하면, 실행은 사람의 응답을 기다리며 일시 중지하지 않고 계속됩니다. Realtime 및 음성 세션 API 의 경우 [Realtime guide](realtime/guide.md)의 승인 흐름을 참조하세요.

## 스트리밍과 세션

동일한 인터럽션 흐름은 스트리밍 실행에서도 동작합니다. 스트리밍 실행이 일시 중지된 후, 이터레이터가 끝날 때까지 [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] 를 계속 소비하고, [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 를 확인해 해결한 뒤, 재개 출력도 계속 스트리밍하려면 [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] 로 재개하세요. 이 패턴의 스트리밍 버전은 [Streaming](streaming.md)을 참조하세요.

세션도 함께 사용하는 경우 `RunState` 에서 재개할 때 동일한 세션 인스턴스를 계속 전달하거나, 동일한 백킹 스토어를 가리키는 다른 세션 객체를 전달하세요. 그러면 재개된 턴이 동일한 저장 대화 이력에 추가됩니다. 세션 수명주기 상세 내용은 [Sessions](sessions/index.md)를 참조하세요.

## 예시: 일시 중지, 승인, 재개

아래 스니펫은 JavaScript HITL 가이드를 반영합니다: 도구에 승인이 필요하면 일시 중지하고, 상태를 디스크에 저장한 뒤, 다시 로드하고, 결정을 수집한 후 재개합니다.

```python
import asyncio
import json
from pathlib import Path

from agents import Agent, Runner, RunState, function_tool


async def needs_oakland_approval(_ctx, params, _call_id) -> bool:
    return "Oakland" in params.get("city", "")


@function_tool(needs_approval=needs_oakland_approval)
async def get_temperature(city: str) -> str:
    return f"The temperature in {city} is 20° Celsius"


agent = Agent(
    name="Weather assistant",
    instructions="Answer weather questions with the provided tools.",
    tools=[get_temperature],
)

STATE_PATH = Path(".cache/hitl_state.json")


def prompt_approval(tool_name: str, arguments: str | None) -> bool:
    answer = input(f"Approve {tool_name} with {arguments}? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


async def main() -> None:
    result = await Runner.run(agent, "What is the temperature in Oakland?")

    while result.interruptions:
        # Persist the paused state.
        state = result.to_state()
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(state.to_string())

        # Load the state later (could be a different process).
        stored = json.loads(STATE_PATH.read_text())
        state = await RunState.from_json(agent, stored)

        for interruption in result.interruptions:
            approved = await asyncio.get_running_loop().run_in_executor(
                None, prompt_approval, interruption.name or "unknown_tool", interruption.arguments
            )
            if approved:
                state.approve(interruption, always_approve=False)
            else:
                state.reject(interruption)

        result = await Runner.run(agent, state)

    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

이 예시에서 `prompt_approval` 는 `input()` 을 사용하고 `run_in_executor(...)` 로 실행되므로 동기식입니다. 승인 소스가 이미 비동기(예: HTTP 요청 또는 비동기 데이터베이스 쿼리)라면, 대신 `async def` 함수를 사용하고 직접 `await` 할 수 있습니다.

승인을 기다리는 동안 출력을 스트리밍하려면 `Runner.run_streamed` 를 호출하고, 완료될 때까지 `result.stream_events()` 를 소비한 다음, 위에 나온 동일한 `result.to_state()` 및 재개 단계를 따르세요.

## 저장소 패턴 및 코드 예제

- **스트리밍 승인**: `examples/agent_patterns/human_in_the_loop_stream.py` 는 `stream_events()` 를 모두 소비한 뒤 보류 중 도구 호출을 승인하고 `Runner.run_streamed(agent, state)` 로 재개하는 방법을 보여줍니다
- **Agent as tool 승인**: `Agent.as_tool(..., needs_approval=...)` 는 위임된 에이전트 작업에 검토가 필요할 때 동일한 인터럽션 흐름을 적용합니다. 중첩 인터럽션도 외부 실행에 표시되므로, 중첩 에이전트가 아니라 원래 최상위 에이전트를 재개하세요
- **로컬 shell 및 apply_patch 도구**: `ShellTool` 과 `ApplyPatchTool` 도 `needs_approval` 을 지원합니다. 이후 호출에 대한 결정을 캐시하려면 `state.approve(interruption, always_approve=True)` 또는 `state.reject(..., always_reject=True)` 를 사용하세요. 자동 결정을 위해서는 `on_approval` 을 제공하고(`examples/tools/shell.py` 참조), 수동 결정을 위해서는 인터럽션을 처리하세요(`examples/tools/shell_human_in_the_loop.py` 참조). 호스티드 shell 환경은 `needs_approval` 또는 `on_approval` 을 지원하지 않습니다. [tools guide](tools.md)를 참조하세요
- **로컬 MCP 서버**: MCP 도구 호출을 제어하려면 `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` 에 `require_approval` 을 사용하세요(`examples/mcp/get_all_mcp_tools_example/main.py` 및 `examples/mcp/tool_filter_example/main.py` 참조)
- **호스티드 MCP 서버**: HITL 을 강제하려면 `HostedMCPTool` 에서 `require_approval` 을 `"always"` 로 설정하고, 선택적으로 `on_approval_request` 를 제공해 자동 승인 또는 거부할 수 있습니다(`examples/hosted_mcp/human_in_the_loop.py` 및 `examples/hosted_mcp/on_approval.py` 참조). 신뢰할 수 있는 서버에는 `"never"` 를 사용하세요(`examples/hosted_mcp/simple.py`)
- **세션 및 메모리**: 승인과 대화 이력이 여러 턴에 걸쳐 유지되도록 `Runner.run` 에 세션을 전달하세요. SQLite 및 OpenAI Conversations 세션 변형은 `examples/memory/memory_session_hitl_example.py` 및 `examples/memory/openai_session_hitl_example.py` 에 있습니다
- **실시간 에이전트**: realtime 데모는 `RealtimeSession` 의 `approve_tool_call` / `reject_tool_call` 을 통해 도구 호출을 승인 또는 거부하는 WebSocket 메시지를 노출합니다(서버 측 핸들러는 `examples/realtime/app/server.py`, API 표면은 [Realtime guide](realtime/guide.md#tool-approvals) 참조)

## 장기 실행 승인

`RunState` 는 내구성을 고려해 설계되었습니다. 대기 중 작업을 데이터베이스나 큐에 저장하려면 `state.to_json()` 또는 `state.to_string()` 을 사용하고, 나중에 `RunState.from_json(...)` 또는 `RunState.from_string(...)` 으로 다시 생성하세요.

유용한 직렬화 옵션:

- `context_serializer`: 비매핑 컨텍스트 객체를 직렬화하는 방법을 사용자 지정합니다
- `context_deserializer`: `RunState.from_json(...)` 또는 `RunState.from_string(...)` 으로 상태를 로드할 때 비매핑 컨텍스트 객체를 다시 구성합니다
- `strict_context=True`: 컨텍스트가 이미 매핑이거나 적절한 serializer/deserializer 를 제공하지 않으면 직렬화 또는 역직렬화를 실패시킵니다
- `context_override`: 상태 로드 시 직렬화된 컨텍스트를 대체합니다. 원래 컨텍스트 객체를 복원하고 싶지 않을 때 유용하지만, 이미 직렬화된 페이로드에서 해당 컨텍스트를 제거하지는 않습니다
- `include_tracing_api_key=True`: 재개된 작업이 동일한 자격 증명으로 트레이스를 계속 내보내야 할 때 직렬화된 trace 페이로드에 tracing API 키를 포함합니다

직렬화된 실행 상태에는 앱 컨텍스트와 함께, 승인, 사용량, 직렬화된 `tool_input`, 중첩 agent-as-tool 재개, trace 메타데이터, 서버 관리 대화 설정 같은 SDK 관리 런타임 메타데이터가 포함됩니다. 직렬화된 상태를 저장하거나 전송할 계획이라면 `RunContextWrapper.context` 를 영속 데이터로 간주하고, 상태와 함께 이동하도록 의도한 경우가 아니라면 비밀 정보를 넣지 마세요.

## 보류 작업 버전 관리

승인이 한동안 대기될 수 있다면, 직렬화된 상태와 함께 에이전트 정의 또는 SDK 버전 마커를 저장하세요. 그러면 모델, 프롬프트, 도구 정의가 바뀔 때 비호환을 피할 수 있도록 일치하는 코드 경로로 역직렬화를 라우팅할 수 있습니다.