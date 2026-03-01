---
search:
  exclude: true
---
# 휴먼인더루프 (HITL)

휴먼인더루프 (HITL) 흐름을 사용하면 사람이 민감한 도구 호출을 승인하거나 거절할 때까지 에이전트 실행을 일시 중지할 수 있습니다. 도구는 승인 필요 여부를 선언하고, 실행 결과는 대기 중인 승인을 인터럽션으로 표시하며, `RunState`를 통해 결정 이후 실행을 직렬화하고 재개할 수 있습니다.

## 승인이 필요한 도구 표시

항상 승인이 필요하도록 `needs_approval`을 `True`로 설정하거나, 호출별로 결정하는 비동기 함수를 제공할 수 있습니다. 이 callable은 실행 컨텍스트, 파싱된 도구 매개변수, 도구 호출 ID를 받습니다.

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

`needs_approval`은 [`function_tool`][agents.tool.function_tool], [`Agent.as_tool`][agents.agent.Agent.as_tool], [`ShellTool`][agents.tool.ShellTool], [`ApplyPatchTool`][agents.tool.ApplyPatchTool]에서 사용할 수 있습니다. 로컬 MCP 서버도 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio], [`MCPServerSse`][agents.mcp.server.MCPServerSse], [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]의 `require_approval`을 통해 승인을 지원합니다. 호스티드 MCP 서버는 [`HostedMCPTool`][agents.tool.HostedMCPTool]에서 `tool_config={"require_approval": "always"}`로 승인 기능을 지원하며, 선택적으로 `on_approval_request` 콜백을 사용할 수 있습니다. Shell 및 apply_patch 도구는 인터럽션을 노출하지 않고 자동 승인 또는 자동 거절하려는 경우 `on_approval` 콜백을 받을 수 있습니다.

## 승인 흐름 작동 방식

1. 모델이 도구 호출을 생성하면 runner가 `needs_approval`을 평가합니다
2. 해당 도구 호출에 대한 승인 결정이 이미 [`RunContextWrapper`][agents.run_context.RunContextWrapper]에 저장되어 있으면(예: `always_approve=True`), runner는 추가 확인 없이 진행합니다. 호출별 승인은 특정 호출 ID 범위로 제한됩니다. 이후 호출도 자동 허용하려면 `always_approve=True`를 사용하세요
3. 그렇지 않으면 실행이 일시 중지되고 `RunResult.interruptions`(또는 `RunResultStreaming.interruptions`)에 `agent.name`, `name`, `arguments` 같은 상세 정보를 포함한 `ToolApprovalItem` 항목이 들어갑니다
4. `result.to_state()`로 결과를 `RunState`로 변환하고, `state.approve(...)` 또는 `state.reject(...)`를 호출한 뒤(`always_approve` 또는 `always_reject`를 선택적으로 전달), `Runner.run(agent, state)` 또는 `Runner.run_streamed(agent, state)`로 재개합니다
5. 재개된 실행은 중단된 지점부터 계속되며, 새 승인이 필요하면 이 흐름으로 다시 진입합니다

## 예시: 일시 중지, 승인, 재개

아래 스니펫은 JavaScript HITL 가이드와 동일한 흐름입니다. 도구에 승인이 필요할 때 일시 중지하고, 상태를 디스크에 저장한 뒤, 다시 불러와 결정을 수집한 후 재개합니다.

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

이 예시에서 `prompt_approval`은 `input()`을 사용하고 `run_in_executor(...)`로 실행되므로 동기 함수입니다. 승인 소스가 이미 비동기라면(예: HTTP 요청 또는 비동기 데이터베이스 쿼리) `async def` 함수를 사용해 직접 `await`할 수 있습니다.

승인 대기 중에도 출력을 스트리밍하려면 `Runner.run_streamed`를 호출하고, 완료될 때까지 `result.stream_events()`를 소비한 뒤, 위에 나온 동일한 `result.to_state()` 및 재개 단계를 따르세요.

## 리포지토리 패턴 및 코드 예제

- **스트리밍 승인**: `examples/agent_patterns/human_in_the_loop_stream.py`는 `stream_events()`를 소진한 뒤 대기 중인 도구 호출을 승인하고 `Runner.run_streamed(agent, state)`로 재개하는 방법을 보여줍니다
- **에이전트를 도구로 사용하는 승인**: `Agent.as_tool(..., needs_approval=...)`는 위임된 에이전트 작업에 검토가 필요할 때 동일한 인터럽션 흐름을 적용합니다
- **Shell 및 apply_patch 도구**: `ShellTool`과 `ApplyPatchTool`도 `needs_approval`을 지원합니다. 이후 호출에 대한 결정을 캐시하려면 `state.approve(interruption, always_approve=True)` 또는 `state.reject(..., always_reject=True)`를 사용하세요. 자동 결정을 위해서는 `on_approval`을 제공하고(`examples/tools/shell.py` 참고), 수동 결정을 위해서는 인터럽션을 처리하세요(`examples/tools/shell_human_in_the_loop.py` 참고)
- **로컬 MCP 서버**: `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp`의 `require_approval`을 사용해 MCP 도구 호출을 게이트할 수 있습니다(`examples/mcp/get_all_mcp_tools_example/main.py` 및 `examples/mcp/tool_filter_example/main.py` 참고)
- **호스티드 MCP 서버**: HITL을 강제하려면 `HostedMCPTool`에서 `require_approval`을 `"always"`로 설정하세요. 선택적으로 `on_approval_request`를 제공해 자동 승인 또는 거절할 수 있습니다(`examples/hosted_mcp/human_in_the_loop.py` 및 `examples/hosted_mcp/on_approval.py` 참고). 신뢰하는 서버에는 `"never"`를 사용하세요(`examples/hosted_mcp/simple.py`)
- **세션 및 메모리**: 여러 턴에 걸쳐 승인과 대화 기록을 유지하려면 `Runner.run`에 세션을 전달하세요. SQLite 및 OpenAI Conversations 세션 변형은 `examples/memory/memory_session_hitl_example.py`와 `examples/memory/openai_session_hitl_example.py`에 있습니다
- **실시간 에이전트**: 실시간 데모는 `RealtimeSession`의 `approve_tool_call` / `reject_tool_call`을 통해 도구 호출을 승인 또는 거절하는 WebSocket 메시지를 노출합니다(서버 측 핸들러는 `examples/realtime/app/server.py` 참고)

## 장기 실행 승인

`RunState`는 내구성을 고려해 설계되었습니다. `state.to_json()` 또는 `state.to_string()`을 사용해 대기 중인 작업을 데이터베이스나 큐에 저장하고, 이후 `RunState.from_json(...)` 또는 `RunState.from_string(...)`으로 다시 생성할 수 있습니다.

유용한 직렬화 옵션:

-   `context_serializer`: 매핑이 아닌 컨텍스트 객체를 직렬화하는 방식을 사용자 지정합니다
-   `strict_context=True`: 컨텍스트가 이미 매핑이거나 적절한 serializer/deserializer를 제공하지 않으면 직렬화 또는 역직렬화를 실패시킵니다
-   `context_override`: 상태를 로드할 때 직렬화된 컨텍스트를 교체합니다. 원래 컨텍스트 객체를 복원하고 싶지 않을 때 유용하지만, 이미 직렬화된 페이로드에서 해당 컨텍스트를 제거하지는 않습니다
-   `include_tracing_api_key=True`: 재개된 작업이 동일한 자격 증명으로 트레이스를 계속 내보내야 할 때 직렬화된 트레이스 페이로드에 트레이싱 API 키를 포함합니다

`RunState`는 트레이스 메타데이터와 서버 관리 대화 설정도 보존하므로, 재개된 실행이 동일한 트레이스와 동일한 `conversation_id` / `previous_response_id` 체인을 이어갈 수 있습니다.

## 대기 중 작업 버전 관리

승인이 한동안 대기할 수 있다면, 직렬화된 상태와 함께 에이전트 정의 또는 SDK 버전 마커를 저장하세요. 그러면 모델, 프롬프트, 도구 정의가 변경될 때 비호환성을 피하기 위해 역직렬화를 일치하는 코드 경로로 라우팅할 수 있습니다.