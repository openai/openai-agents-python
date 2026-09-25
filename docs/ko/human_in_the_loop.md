---
search:
  exclude: true
---
# 휴먼인더루프 (HITL)

휴먼인더루프 (HITL) 흐름을 사용하면 사람이 민감한 도구 호출을 승인하거나 거부할 때까지 에이전트 실행을 일시 중지할 수 있습니다. 도구는 승인이 필요한 시점을 선언하고, 실행 결과에는 보류 중인 승인이 인터럽션으로 노출되며, `RunState` 기능을 사용하면 일시 중지된 실행을 직렬화하고 결정이 내려진 후 재개할 수 있습니다.

이 승인 범위는 현재 최상위 에이전트로 제한되지 않고 실행 전체에 적용됩니다. 도구가 현재 에이전트에 속한 경우, 핸드오프를 통해 도달한 에이전트에 속한 경우, 중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에 속한 경우에도 같은 패턴이 적용됩니다. 중첩된 `Agent.as_tool()` 사례에서도 인터럽션은 외부 실행에 노출되므로, 외부 `RunState` 항목에서 승인하거나 거부한 후 원래 최상위 실행을 재개합니다.

`Agent.as_tool()` 기능을 사용하면 두 계층에서 승인이 발생할 수 있습니다. 에이전트 도구 자체가 `Agent.as_tool(..., needs_approval=...)` 설정을 통해 승인을 요구할 수 있고, 중첩 실행이 시작된 후 중첩된 에이전트 내부의 도구가 자체 승인을 요청할 수도 있습니다. 두 경우 모두 동일한 외부 실행 인터럽션 흐름을 통해 처리됩니다.

이 페이지에서는 `interruptions` 기능을 통한 수동 승인 흐름을 중점적으로 설명합니다. 애플리케이션이 코드에서 결정을 내릴 수 있다면 일부 도구 유형은 프로그래밍 방식의 승인 콜백도 지원하므로 실행을 일시 중지하지 않고 계속할 수 있습니다.

## 승인이 필요한 도구 표시 {#marking-tools-that-need-approval}

항상 승인을 요구하려면 `needs_approval` 값을 `True` 값으로 설정하고, 호출별로 결정하려면 비동기 함수를 제공합니다. 호출 가능 객체는 실행 컨텍스트, 파싱된 도구 매개변수, 도구 호출 ID를 받습니다.

SDK가 인수를 안전하게 검사할 수 없으면 호출 가능한 승인 규칙은 안전을 위해 승인이 필요한 것으로 처리됩니다. 인수가 없거나 비어 있는 경우, 공백만 포함된 경우, 잘못된 형식의 JSON인 경우, 유효한 JSON이지만 객체가 아닌 경우(예: `null` 또는 목록), `NaN`, `Infinity`, `-Infinity` 같은 비표준 상수를 포함하는 경우에는 호출 가능 객체가 실행되지 않으며 해당 호출에 수동 승인이 필요합니다. 이 동작은 Runner와 Realtime 도구 호출에서 동일합니다.

```python
from agents import Agent
from agents.decorators import tool


@tool(needs_approval=True)
async def cancel_order(order_id: int) -> str:
    return f"Cancelled order {order_id}"


async def requires_review(_ctx, params, _call_id) -> bool:
    return "refund" in params.get("subject", "").lower()


@tool(needs_approval=requires_review)
async def send_email(subject: str, body: str) -> str:
    return f"Sent '{subject}'"


agent = Agent(
    name="Support agent",
    instructions="Handle tickets and ask for approval when needed.",
    tools=[cancel_order, send_email],
)
```

`needs_approval` 기능은 [`function_tool`][agents.tool.function_tool], [`Agent.as_tool`][agents.agent.Agent.as_tool], [`ShellTool`][agents.tool.ShellTool], [`ApplyPatchTool`][agents.tool.ApplyPatchTool]에서 사용할 수 있습니다. 로컬 MCP 서버도 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio], [`MCPServerSse`][agents.mcp.server.MCPServerSse], [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]의 `require_approval` 설정을 통해 승인을 지원합니다. 호스티드 MCP 서버는 `tool_config={"require_approval": "always"}` 설정과 선택적 `on_approval_request` 콜백이 포함된 [`HostedMCPTool`][agents.tool.HostedMCPTool] 기능을 통해 승인을 지원합니다. 셸 및 apply_patch 도구는 인터럽션을 노출하지 않고 자동으로 승인하거나 거부하려는 경우 `on_approval` 콜백을 받습니다.

## 승인 흐름의 작동 방식 {#how-the-approval-flow-works}

1. 모델이 도구 호출을 생성하면 러너가 해당 승인 규칙(`needs_approval`, `require_approval` 또는 호스티드 MCP의 동등한 설정)을 평가합니다.
2. 해당 도구 호출에 대한 승인 결정이 이미 [`RunContextWrapper`][agents.run_context.RunContextWrapper] 객체에 저장되어 있다면 러너는 확인을 요청하지 않고 계속 진행합니다. 호출별 승인은 특정 호출 ID에만 적용됩니다. 실행의 남은 기간 동안 동일한 도구 식별자를 사용하는 이후 호출에도 같은 결정을 유지하려면 `always_approve=True` 또는 `always_reject=True` 값을 전달합니다.
3. 승인 규칙에 따라 승인이 필요하지만 해당 도구 호출에 대한 결정이 저장되어 있지 않으면 실행이 일시 중지되고, `RunResult.interruptions` 또는 `RunResultStreaming.interruptions` 결과에 `agent.name`, `tool_name`, `arguments` 같은 세부 정보가 포함된 [`ToolApprovalItem`][agents.items.ToolApprovalItem] 항목이 들어갑니다. 여기에는 핸드오프 후 또는 중첩된 `Agent.as_tool()` 실행 내부에서 발생한 승인도 포함됩니다.
4. `result.to_state()` 기능을 사용해 결과를 `RunState` 객체로 변환하고, `state.approve(...)` 또는 `state.reject(...)` 기능을 호출한 다음, `Runner.run(agent, state)` 또는 `Runner.run_streamed(agent, state)` 기능으로 재개합니다. 이때 `agent` 항목은 해당 실행의 원래 최상위 에이전트입니다.
5. 재개된 실행은 중단된 지점부터 계속되며, 새로운 승인이 필요하면 이 흐름으로 다시 진입합니다.

`always_approve=True` 또는 `always_reject=True` 기능으로 생성한 고정 결정은 실행 상태에 저장되므로, 나중에 동일한 일시 중지 실행을 재개할 때 `state.to_string()` / `RunState.from_string(...)` 및 `state.to_json()` / `RunState.from_json(...)` 과정을 거쳐도 유지됩니다.

[`HostedMCPTool`][agents.tool.HostedMCPTool] 기능의 승인 요청에서는 Agents SDK가 `server_label` 값과 도구 이름의 조합으로 고정 도구 결정을 식별합니다. 한 호스티드 MCP 서버의 `lookup_account` 도구를 항상 승인하도록 결정해도 다른 서버에서 이름이 같은 도구까지 승인되지는 않습니다. Agents SDK는 호스티드 MCP 승인 요청에 비어 있지 않은 두 식별 필드가 모두 포함된 경우에만 항상 승인 또는 항상 거부 결정을 유지합니다.

보류 중인 모든 승인을 한 번의 처리에서 해결할 필요는 없습니다. `interruptions` 목록에는 일반 함수 도구, 호스티드 MCP 승인, 중첩된 `Agent.as_tool()` 승인이 함께 포함될 수 있습니다. 일부 항목만 승인하거나 거부한 후 다시 실행하면 해결된 호출은 계속 진행되고, 해결되지 않은 호출은 `interruptions` 목록에 남아 실행을 다시 일시 중지합니다.

## 사용자 지정 거부 메시지 {#custom-rejection-messages}

기본적으로 거부된 도구 호출은 SDK의 표준 거부 텍스트를 실행에 다시 반환합니다. 다음 두 계층에서 이 메시지를 사용자 지정할 수 있습니다.

-   실행 전체 대체 설정: 전체 실행에서 승인 거부 시 모델에 표시되는 기본 메시지를 제어하려면 [`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter] 값을 설정합니다.
-   호출별 재정의: 특정 도구 호출 하나가 거부될 때 다른 메시지를 표시하려면 `state.reject(...)` 기능에 `rejection_message=...` 값을 전달합니다.

둘 다 제공하면 호출별 `rejection_message` 값이 실행 전체 포매터보다 우선합니다.

```python
from agents import RunConfig, ToolErrorFormatterArgs


def format_rejection(args: ToolErrorFormatterArgs[None]) -> str | None:
    if args.kind != "approval_rejected":
        return None
    return "Publish action was canceled because approval was rejected."


run_config = RunConfig(tool_error_formatter=format_rejection)

# Later, while resolving a specific interruption:
state.reject(
    interruption,
    rejection_message="Publish action was canceled because the reviewer denied approval.",
)
```

두 계층을 함께 사용하는 전체 예제는 [`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py)에서 확인할 수 있습니다.

## 자동 승인 결정 {#automatic-approval-decisions}

수동 `interruptions` 방식이 가장 일반적인 패턴이지만 유일한 방법은 아닙니다.

-   로컬 [`ShellTool`][agents.tool.ShellTool] 및 [`ApplyPatchTool`][agents.tool.ApplyPatchTool] 도구는 `on_approval` 기능을 사용해 코드에서 즉시 승인하거나 거부할 수 있습니다.
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] 기능은 `on_approval_request` 설정과 함께 `tool_config={"require_approval": "always"}` 기능을 사용해 같은 방식의 프로그래밍 결정을 내릴 수 있습니다.
-   일반 [`function_tool`][agents.tool.function_tool] 도구와 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 기능은 이 페이지에서 설명하는 수동 인터럽션 흐름을 사용합니다.

이러한 콜백이 결정을 반환하면 사람의 응답을 기다리기 위해 일시 중지하지 않고 실행이 계속됩니다. Realtime 및 음성 세션 API에 대해서는 [Realtime 가이드](realtime/guide.md)의 승인 흐름을 참조하세요.

## 스트리밍 및 세션 {#streaming-and-sessions}

동일한 인터럽션 흐름이 스트리밍 실행에서도 작동합니다. 스트리밍 실행이 일시 중지된 후 반복자가 종료될 때까지 [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] 항목을 계속 소비하고, [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] 항목을 검사해 해결한 다음, 재개된 출력도 계속 스트리밍하려면 [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] 기능으로 재개합니다. 이 패턴의 스트리밍 버전은 [스트리밍](streaming.md)을 참조하세요.

세션도 사용 중이라면 `RunState` 결과에서 재개할 때 동일한 세션 인스턴스를 계속 전달하거나, 동일한 세션 ID와 백업 저장소를 사용하도록 구성된 다른 세션 객체를 전달합니다. 그러면 재개된 턴이 저장된 동일한 대화 기록에 추가됩니다. 세션 수명 주기에 관한 자세한 내용은 [세션](sessions/index.md)을 참조하세요.

## 예제: 일시 중지, 승인 및 재개 {#example-pause-approve-resume}

아래 스니펫은 JavaScript HITL 가이드와 동일한 흐름을 보여 줍니다. 도구에 승인이 필요하면 일시 중지하고, 상태를 디스크에 저장한 후 다시 불러오며, 결정을 수집한 뒤 재개합니다.

```python
import asyncio
import json
from pathlib import Path

from agents import Agent, Runner, RunState
from agents.decorators import tool


async def needs_oakland_approval(_ctx, params, _call_id) -> bool:
    return "Oakland" in params.get("city", "")


@tool(needs_approval=needs_oakland_approval)
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

이 예제에서 `prompt_approval` 기능은 `input()` 기능을 사용하고 `run_in_executor(...)` 기능으로 실행되므로 동기식입니다. 승인 소스가 이미 비동기식이라면(예: HTTP 요청 또는 비동기 데이터베이스 쿼리) `async def` 함수를 사용하고 `await` 처리할 수 있습니다.

승인을 위해 일시 중지될 수 있는 실행에서 스트리밍을 사용하려면 `Runner.run_streamed` 기능을 호출하고, 완료될 때까지 `result.stream_events()` 항목을 소비한 다음, 위에 나온 것과 동일한 `result.to_state()` 처리 및 재개 단계를 따릅니다.

## 저장소 패턴 및 코드 예제 {#repository-patterns-and-examples}

- **스트리밍 승인**: `examples/agent_patterns/human_in_the_loop_stream.py` 예제는 `stream_events()` 항목을 모두 소비한 다음, `Runner.run_streamed(agent, state)` 기능으로 재개하기 전에 보류 중인 도구 호출을 승인하는 방법을 보여 줍니다.
- **사용자 지정 거부 텍스트**: `examples/agent_patterns/human_in_the_loop_custom_rejection.py` 예제는 승인이 거부될 때 실행 수준 `tool_error_formatter` 설정과 호출별 `rejection_message` 재정의를 결합하는 방법을 보여 줍니다.
- **도구로 사용하는 에이전트 승인**: `Agent.as_tool(..., needs_approval=...)` 예제는 위임된 에이전트 작업에 검토가 필요할 때 동일한 인터럽션 흐름을 적용합니다. 중첩된 인터럽션도 외부 실행에 노출되므로 중첩된 에이전트가 아니라 원래 최상위 에이전트를 재개합니다.
- **로컬 셸 및 apply_patch 도구**: `ShellTool` 및 `ApplyPatchTool` 도구도 `needs_approval` 기능을 지원합니다. 실행의 남은 기간 동안 해당 도구의 이후 호출에 사용할 결정을 캐시하려면 `state.approve(interruption, always_approve=True)` 또는 `state.reject(..., always_reject=True)` 값을 사용합니다. 콜백 내부에서 승인을 해결하려면 `on_approval` 값을 제공합니다. `examples/tools/shell.py` 예제는 기본적으로 operator에게 확인을 요청하는 대화형 콜백을 보여 줍니다. 모든 셸 호출을 거부하는 자동 정책을 적용하려면 `ShellTool` 객체에서 `needs_approval=True` 및 `on_approval=lambda _context, _item: {"approve": False, "reason": "Disabled by policy"}` 값을 설정합니다. 대신 애플리케이션에서 일시 중지된 실행을 검토하도록 하려면 인터럽션을 처리합니다(`examples/tools/shell_human_in_the_loop.py` 참조). 호스팅된 셸 환경은 `needs_approval` 또는 `on_approval` 기능을 지원하지 않습니다. [도구 가이드](tools.md)를 참조하세요.
- **로컬 MCP 서버**: MCP 도구 호출을 제한하려면 `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` 객체에서 `require_approval` 설정을 사용합니다(`examples/mcp/get_all_mcp_tools_example/main.py` 및 `examples/mcp/tool_filter_example/main.py` 참조).
- **호스티드 MCP 서버**: HITL을 강제하려면 `HostedMCPTool` 객체에서 `tool_config={"require_approval": "always"}` 값을 설정하고, 선택적으로 자동 승인 또는 거부를 위한 `on_approval_request` 값을 제공합니다(`examples/hosted_mcp/human_in_the_loop.py` 및 `examples/hosted_mcp/on_approval.py` 참조). 신뢰할 수 있는 서버에는 `"never"` 값을 사용합니다(`examples/hosted_mcp/simple.py` 참조).
- **세션 및 메모리**: 여러 턴에 걸쳐 승인 및 대화 기록을 유지하려면 `Runner.run` 기능에 세션을 전달합니다. SQLite 및 OpenAI Conversations 세션 변형은 `examples/memory/memory_session_hitl_example.py` 및 `examples/memory/openai_session_hitl_example.py` 예제에 있습니다.
- **실시간 에이전트**: 실시간 데모는 `RealtimeSession` 객체의 `approve_tool_call` / `reject_tool_call` 기능을 통해 도구 호출을 승인하거나 거부하는 WebSocket 메시지를 노출합니다. 서버 측 핸들러는 `examples/realtime/app/server.py` 예제를, API 인터페이스는 [Realtime 가이드](realtime/guide.md#tool-approvals)를 참조하세요.

## 장기 실행 승인 {#long-running-approvals}

`RunState` 기능은 지속성을 갖도록 설계되었습니다. 보류 중인 작업을 데이터베이스나 큐에 저장하려면 `state.to_json()` 또는 `state.to_string()` 기능을 사용하고, 나중에 다시 생성하려면 `RunState.from_json(...)` 또는 `RunState.from_string(...)` 기능을 사용합니다.

### 서버의 승인 상태 유지 {#keep-approval-state-on-the-server}

직렬화된 `RunState` 데이터에는 승인 결정, 보류 중인 도구 호출, 도구 인수를 포함한 실행 상태가 들어 있습니다. SDK는 이 상태를 복원합니다. `RunState.from_json()` 및 `RunState.from_string()` 기능은 스냅샷이나 이를 제출하는 사람을 인증하지 않습니다. 신뢰할 수 있는 저장소의 스냅샷이나 애플리케이션이 완전한 무결성과 소유권을 검증한 스냅샷만 역직렬화해야 합니다. 스키마 검사나 도구 호출 지문만으로는 스냅샷을 인증할 수 없습니다.

브라우저 또는 모바일 승인 인터페이스에서는 전체 스냅샷을 애플리케이션이 제어하는 서버 저장소에 보관합니다. 검토자에게는 검토 권한이 있는 도구 세부 정보와 보류 중인 결정에 대한 불투명 식별자만 전송합니다. 도구 이름과 인수는 신뢰할 수 없는 표시 콘텐츠로 취급하고 HTML을 렌더링할 때 이스케이프 처리합니다.

결정이 도착하면 서버에서 다음 작업을 수행해야 합니다.

1. 애플리케이션의 세션 또는 인증 미들웨어를 사용해 검토자를 인증합니다. 승인 요청 본문에서 검토자의 신원을 가져오지 마세요.
2. 해당 검토자가 저장된 실행 및 선택한 보류 중 호출에 조치할 수 있도록 권한을 부여합니다. 실행 ID나 결정 ID를 보유하고 있다는 사실만으로 권한이 부여되지는 않습니다.
3. 제출된 결정 식별자와 불리언 결정을 서버에 저장된 보류 중 요청과 대조해 검증합니다. 서버 소유 스냅샷을 불러오고 `state.get_interruptions()` 기능으로 보류 중인 항목을 가져옵니다. 클라이언트에서 대체 도구 호출, 인수, 승인 기록 또는 직렬화된 상태를 받지 마세요.
4. 해당 서버 소유 항목에 `state.approve(...)` 또는 `state.reject(...)` 기능을 적용한 다음 실행을 재개합니다. 동시에 제출되거나 재전송된 요청으로 동일한 스냅샷이 두 번 재개되지 않도록 각 보류 중 요청의 소비를 저장소와 조율합니다. 공유 저장소에서는 재개된 실행을 시작하기 전에 소유자 확인을 포함한 원자적 상태 전이를 사용합니다.

[서버 측 승인 예제](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/human_in_the_loop_server.py)는 CLI 클라이언트 시뮬레이션과 단일 프로세스의 단일 이벤트 루프로 제한된 저장소를 사용해 이 패턴을 보여 줍니다. 이 예제에서는 배치 내 보류 중인 모든 호출에 각각 하나의 결정이 필요합니다. 역직렬화 및 재개된 실행 전에 요청을 소비하므로 실패하거나 취소된 경우에도 요청이 소비됩니다. 프로덕션 애플리케이션에서는 인증, 요청 보호 조치, 저장소 보존, 재시도 전에 도구의 부작용을 조정하는 복구 기능을 제공해야 합니다. 이 예제는 배포 가능한 HTTP 서비스가 아닙니다.

`context` 값을 `context_override` 값으로 바꾸거나, `strict_context=True` 값을 설정하거나, 직렬화된 승인 기록만 제거하더라도 신뢰할 수 없는 스냅샷이 안전해지지는 않습니다. 다른 필드도 재개된 실행을 계속 제어합니다. 애플리케이션이 전체 스냅샷을 클라이언트를 통해 전송한다면 역직렬화하기 전에 무결성을 검증하고, 권한이 있는 사용자 및 실행과 연결하며, 재전송을 방지해야 합니다. 이러한 검증은 스냅샷을 암호화하거나 클라이언트에서 해당 내용을 숨기지 않습니다.

### 직렬화 옵션 {#serialization-options}

유용한 직렬화 옵션은 다음과 같습니다.

-   `context_serializer`: 매핑이 아닌 컨텍스트 객체의 직렬화 방식을 사용자 지정합니다.
-   `context_deserializer`: `RunState.from_json(...)` 또는 `RunState.from_string(...)` 기능으로 상태를 불러올 때 매핑이 아닌 컨텍스트 객체를 다시 구성합니다.
- `strict_context=True`: 컨텍스트가 이미 매핑이거나 `context_serializer` 값을 제공한 경우가 아니면 직렬화에 실패합니다. 컨텍스트가 이미 매핑이거나 `context_deserializer` 값을 제공한 경우가 아니면 역직렬화에 실패합니다.
- `context_override`: 상태를 불러올 때 직렬화된 컨텍스트를 교체합니다. 원래 컨텍스트 객체를 복원하지 않으려는 경우 유용하지만, 이미 직렬화된 페이로드에서 해당 컨텍스트를 제거하지는 않습니다.
- `include_tracing_api_key=True`: 재개된 작업이 동일한 자격 증명으로 트레이스를 계속 내보내야 하는 경우 직렬화된 트레이스 페이로드에 트레이싱 API 키를 포함합니다.

직렬화된 실행 상태에는 애플리케이션 컨텍스트뿐 아니라 승인, 사용량, 직렬화된 `tool_input`, 중첩된 도구형 에이전트 재개 정보, 트레이스 메타데이터, 서버 관리 대화 설정과 같은 SDK 관리 런타임 메타데이터가 포함됩니다. 직렬화된 상태를 저장하거나 전송할 계획이라면 `RunContextWrapper.context` 데이터를 영구 저장 데이터로 취급하고, 의도적으로 상태와 함께 전달하려는 경우가 아니라면 그 안에 비밀 정보를 넣지 마세요.

## 보류 중인 작업의 버전 관리 {#versioning-pending-tasks}

승인이 장시간 보류될 수 있다면 직렬화된 상태와 함께 에이전트 정의 또는 SDK의 버전 표식을 저장합니다. 그러면 모델, 프롬프트 또는 도구 정의가 변경될 때 발생할 수 있는 비호환성을 방지하도록 일치하는 코드 경로로 역직렬화를 라우팅할 수 있습니다.