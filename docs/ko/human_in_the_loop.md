---
search:
  exclude: true
---
# 휴먼인더루프 (HITL)

휴먼인더루프 (HITL) 흐름을 사용하면 사람이 민감한 도구 호출을 승인하거나 거부할 때까지 에이전트 실행을 일시 중지할 수 있습니다. 도구는 승인이 필요한 시점을 선언하고, 실행 결과는 대기 중인 승인을 인터럽션(중단 처리)으로 노출하며, `RunState`를 사용하면 일시 중지된 실행을 직렬화하고 결정이 내려진 후 재개할 수 있습니다.

이 승인 인터페이스는 현재 최상위 에이전트에 국한되지 않고 실행 전체에 적용됩니다. 도구가 현재 에이전트에 속하는 경우, 핸드오프를 통해 도달한 에이전트에 속하는 경우, 중첩된 [`Agent.as_tool()`][agents.agent.Agent.as_tool] 실행에 속하는 경우 모두 같은 패턴이 적용됩니다. 중첩된 `Agent.as_tool()`의 경우에도 인터럽션은 외부 실행에 노출되므로, 외부 `RunState`에서 승인하거나 거부한 후 원래의 최상위 실행을 재개합니다.

`Agent.as_tool()`를 사용하면 두 계층에서 승인이 발생할 수 있습니다. 에이전트 도구 자체가 `Agent.as_tool(..., needs_approval=...)`를 통해 승인을 요구할 수 있고, 중첩 실행이 시작된 후 중첩된 에이전트 내부의 도구가 자체 승인을 요청할 수도 있습니다. 두 경우 모두 동일한 외부 실행의 인터럽션(중단 처리) 흐름을 통해 처리됩니다.

이 페이지에서는 `interruptions`를 통한 수동 승인 흐름을 중점적으로 설명합니다. 애플리케이션이 코드에서 결정을 내릴 수 있다면 일부 도구 유형은 프로그래밍 방식의 승인 콜백도 지원하므로 실행을 일시 중지하지 않고 계속할 수 있습니다.

## 승인이 필요한 도구 표시 {#marking-tools-that-need-approval}

항상 승인을 요구하려면 `needs_approval`을 `True`로 설정하고, 호출별로 결정하려면 비동기 함수를 제공합니다. 이 호출 가능 객체는 실행 컨텍스트, 파싱된 도구 매개변수, 도구 호출 ID를 받습니다.

SDK가 인수를 안전하게 검사할 수 없는 경우 호출 가능 승인 규칙은 기본적으로 승인을 요구합니다. 인수가 잘못된 JSON이거나, 유효한 JSON이지만 객체가 아니거나(예: `null` 또는 목록), `NaN`, `Infinity`, `-Infinity` 같은 비표준 상수를 포함하면 호출 가능 객체는 호출되지 않으며 해당 호출에는 수동 승인이 필요합니다. 이 동작은 Runner와 Realtime 도구 호출에서 동일합니다.

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

`needs_approval`은 [`function_tool`][agents.tool.function_tool], [`Agent.as_tool`][agents.agent.Agent.as_tool], [`ShellTool`][agents.tool.ShellTool], [`ApplyPatchTool`][agents.tool.ApplyPatchTool]에서 사용할 수 있습니다. 로컬 MCP 서버도 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio], [`MCPServerSse`][agents.mcp.server.MCPServerSse], [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]의 `require_approval`을 통해 승인을 지원합니다. 호스티드 MCP 서버는 [`HostedMCPTool`][agents.tool.HostedMCPTool]에서 `tool_config={"require_approval": "always"}` 및 선택적인 `on_approval_request` 콜백을 통해 승인을 지원합니다. 셸 및 apply_patch 도구에서는 인터럽션(중단 처리)을 노출하지 않고 자동으로 승인하거나 거부하려는 경우 `on_approval` 콜백을 사용할 수 있습니다.

## 승인 흐름의 작동 방식 {#how-the-approval-flow-works}

1. 모델이 도구 호출을 생성하면 Runner가 해당 승인 규칙(`needs_approval`, `require_approval` 또는 이에 해당하는 호스티드 MCP 규칙)을 평가합니다.
2. 해당 도구 호출에 관한 승인 결정이 이미 [`RunContextWrapper`][agents.run_context.RunContextWrapper]에 저장되어 있으면 Runner는 확인을 요청하지 않고 진행합니다. 호출별 승인은 특정 호출 ID에만 적용됩니다. 실행의 나머지 기간에 동일한 도구 ID를 사용하는 향후 호출에도 같은 결정을 유지하려면 `always_approve=True` 또는 `always_reject=True`을 전달합니다.
3. 승인 규칙상 승인이 필요하지만 해당 도구 호출에 관한 결정이 저장되어 있지 않으면 실행이 일시 중지되고, `RunResult.interruptions`(또는 `RunResultStreaming.interruptions`)에 `agent.name`, `tool_name`, `arguments` 등의 세부 정보가 포함된 [`ToolApprovalItem`][agents.items.ToolApprovalItem] 항목이 들어갑니다. 여기에는 핸드오프 후 또는 중첩된 `Agent.as_tool()` 실행 내부에서 발생한 승인도 포함됩니다.
4. `result.to_state()`를 사용해 결과를 `RunState`로 변환하고 `state.approve(...)` 또는 `state.reject(...)`을 호출한 다음, `Runner.run(agent, state)` 또는 `Runner.run_streamed(agent, state)`으로 재개합니다. 여기서 `agent`는 해당 실행의 원래 최상위 에이전트입니다.
5. 재개된 실행은 중단된 지점부터 계속되며, 새로운 승인이 필요하면 이 흐름에 다시 진입합니다.

`always_approve=True` 또는 `always_reject=True`으로 생성한 지속 결정은 실행 상태에 저장되므로, 나중에 동일한 일시 중지 실행을 재개할 때 `state.to_string()` / `RunState.from_string(...)` 및 `state.to_json()` / `RunState.from_json(...)`을 거쳐도 유지됩니다.

[`HostedMCPTool`][agents.tool.HostedMCPTool]에서 발생한 승인 요청의 경우 Agents SDK는 `server_label`와 도구 이름의 조합으로 지속 도구 결정을 식별합니다. 한 호스티드 MCP 서버의 `lookup_account`에 대한 항상 승인 결정은 다른 서버에서 이름이 같은 도구를 승인하지 않습니다. Agents SDK는 호스티드 MCP 승인 요청에 비어 있지 않은 두 ID 필드가 모두 포함된 경우에만 항상 승인 또는 항상 거부 결정을 유지합니다.

대기 중인 모든 승인을 한 번에 처리할 필요는 없습니다. `interruptions`에는 일반 함수 도구, 호스티드 MCP 승인, 중첩된 `Agent.as_tool()` 승인이 함께 포함될 수 있습니다. 일부 항목만 승인하거나 거부한 후 다시 실행하면 처리된 호출은 계속 진행되고, 미처리된 호출은 `interruptions`에 남아 실행을 다시 일시 중지합니다.

## 사용자 지정 거부 메시지 {#custom-rejection-messages}

기본적으로 거부된 도구 호출은 SDK의 표준 거부 텍스트를 실행에 반환합니다. 이 메시지는 두 계층에서 사용자 지정할 수 있습니다.

-   실행 전체의 대체 메시지: [`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]을 설정하여 실행 전체에서 승인 거부 시 모델에 표시되는 기본 메시지를 제어합니다.
-   호출별 재정의: 특정 거부 도구 호출 하나에 다른 메시지를 표시하려면 `state.reject(...)`에 `rejection_message=...`을 전달합니다.

둘 다 제공하면 호출별 `rejection_message`이 실행 전체 포매터보다 우선합니다.

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

두 계층을 함께 사용하는 전체 예제는 [`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py)을 참조하세요.

## 자동 승인 결정 {#automatic-approval-decisions}

수동 `interruptions`이 가장 일반적인 패턴이지만 유일한 방식은 아닙니다.

-   로컬 [`ShellTool`][agents.tool.ShellTool] 및 [`ApplyPatchTool`][agents.tool.ApplyPatchTool]은 `on_approval`를 사용하여 코드에서 즉시 승인하거나 거부할 수 있습니다.
-   [`HostedMCPTool`][agents.tool.HostedMCPTool]은 `tool_config={"require_approval": "always"}`과 `on_approval_request`을 함께 사용하여 같은 방식의 프로그래밍 방식 결정을 내릴 수 있습니다.
-   일반 [`function_tool`][agents.tool.function_tool] 도구와 [`Agent.as_tool()`][agents.agent.Agent.as_tool]은 이 페이지의 수동 인터럽션(중단 처리) 흐름을 사용합니다.

이러한 콜백이 결정을 반환하면 사람의 응답을 기다리기 위해 일시 중지하지 않고 실행이 계속됩니다. Realtime 및 음성 세션 API는 [Realtime 가이드](realtime/guide.md)의 승인 흐름을 참조하세요.

## 스트리밍 및 세션 {#streaming-and-sessions}

동일한 인터럽션(중단 처리) 흐름이 스트리밍 실행에서도 작동합니다. 스트리밍된 실행이 일시 중지된 후 반복자가 끝날 때까지 [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events]을 계속 소비하고, [`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions]을 검사하여 처리한 다음, 재개된 출력에서도 스트리밍을 유지하려면 [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed]으로 재개합니다. 이 패턴의 스트리밍 버전은 [스트리밍](streaming.md)을 참조하세요.

세션도 사용 중이라면 `RunState`에서 재개할 때 동일한 세션 인스턴스를 계속 전달하거나, 동일한 세션 ID 및 백업 스토어를 사용하도록 구성된 다른 세션 객체를 전달합니다. 그러면 재개된 턴이 동일하게 저장된 대화 기록에 추가됩니다. 세션 수명 주기에 관한 자세한 내용은 [세션](sessions/index.md)을 참조하세요.

## 예제: 일시 중지, 승인, 재개 {#example-pause-approve-resume}

아래 코드 조각은 JavaScript HITL 가이드와 동일한 흐름을 보여 줍니다. 도구에 승인이 필요하면 일시 중지하고, 상태를 디스크에 저장하고, 다시 로드한 후 결정을 수집하여 재개합니다.

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

이 예제에서 `prompt_approval`는 `input()`을 사용하고 `run_in_executor(...)`으로 실행되므로 동기식입니다. 승인 소스가 이미 비동기식이라면(예: HTTP 요청 또는 비동기 데이터베이스 쿼리) `async def` 함수를 사용하고 직접 `await`할 수 있습니다.

승인을 위해 일시 중지될 수 있는 실행에서 스트리밍을 사용하려면 `Runner.run_streamed`을 호출하고 완료될 때까지 `result.stream_events()`을 소비한 다음, 위에 나온 것과 동일한 `result.to_state()` 및 재개 단계를 따릅니다.

## 저장소 패턴 및 코드 예제 {#repository-patterns-and-examples}

- **스트리밍 승인**: `examples/agent_patterns/human_in_the_loop_stream.py`은 `stream_events()`을 모두 소비한 다음, `Runner.run_streamed(agent, state)`으로 재개하기 전에 대기 중인 도구 호출을 승인하는 방법을 보여 줍니다.
- **사용자 지정 거부 텍스트**: `examples/agent_patterns/human_in_the_loop_custom_rejection.py`은 승인이 거부될 때 실행 수준 `tool_error_formatter`과 호출별 `rejection_message` 재정의를 결합하는 방법을 보여 줍니다.
- **도구로 사용하는 에이전트 승인**: `Agent.as_tool(..., needs_approval=...)`은 위임된 에이전트 작업을 검토해야 할 때 동일한 인터럽션(중단 처리) 흐름을 적용합니다. 중첩된 인터럽션(중단 처리)도 외부 실행에 노출되므로 중첩된 에이전트가 아니라 원래의 최상위 에이전트를 재개합니다.
- **로컬 셸 및 apply_patch 도구**: `ShellTool` 및 `ApplyPatchTool`도 `needs_approval`을 지원합니다. 실행의 나머지 기간에 해당 도구를 향후 호출할 때 사용할 결정을 캐시하려면 `state.approve(interruption, always_approve=True)` 또는 `state.reject(..., always_reject=True)`을 사용합니다. 자동 결정에는 `on_approval`을 제공합니다(`examples/tools/shell.py` 참조). 수동 결정에는 인터럽션(중단 처리)을 처리합니다(`examples/tools/shell_human_in_the_loop.py` 참조). 호스티드 셸 환경은 `needs_approval` 또는 `on_approval`을 지원하지 않습니다. [도구 가이드](tools.md)를 참조하세요.
- **로컬 MCP 서버**: MCP 도구 호출을 제한하려면 `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp`에서 `require_approval`을 사용합니다(`examples/mcp/get_all_mcp_tools_example/main.py` 및 `examples/mcp/tool_filter_example/main.py` 참조).
- **호스티드 MCP 서버**: HITL을 강제하려면 `HostedMCPTool`에서 `tool_config={"require_approval": "always"}`을 설정하고, 선택적으로 자동 승인 또는 거부를 위한 `on_approval_request`을 제공합니다(`examples/hosted_mcp/human_in_the_loop.py` 및 `examples/hosted_mcp/on_approval.py` 참조). 신뢰할 수 있는 서버에는 `"never"`을 사용합니다(`examples/hosted_mcp/simple.py` 참조).
- **세션 및 메모리**: 승인과 대화 기록이 여러 턴에 걸쳐 유지되도록 `Runner.run`에 세션을 전달합니다. SQLite 및 OpenAI Conversations 세션 변형은 `examples/memory/memory_session_hitl_example.py` 및 `examples/memory/openai_session_hitl_example.py`에 있습니다.
- **실시간 에이전트**: 실시간 데모는 `RealtimeSession`에서 `approve_tool_call` / `reject_tool_call`을 통해 도구 호출을 승인하거나 거부하는 WebSocket 메시지를 제공합니다. 서버 측 핸들러는 `examples/realtime/app/server.py`을, API 인터페이스는 [Realtime 가이드](realtime/guide.md#tool-approvals)를 참조하세요.

## 장기 실행 승인 {#long-running-approvals}

`RunState`은 지속 가능하도록 설계되었습니다. `state.to_json()` 또는 `state.to_string()`을 사용하여 대기 중인 작업을 데이터베이스나 큐에 저장하고, 나중에 `RunState.from_json(...)` 또는 `RunState.from_string(...)`으로 다시 생성합니다.

유용한 직렬화 옵션은 다음과 같습니다.

-   `context_serializer`: 매핑이 아닌 컨텍스트 객체의 직렬화 방식을 사용자 지정합니다.
-   `context_deserializer`: `RunState.from_json(...)` 또는 `RunState.from_string(...)`을 사용하여 상태를 로드할 때 매핑이 아닌 컨텍스트 객체를 다시 구성합니다.
- `strict_context=True`: 컨텍스트가 이미 매핑이거나 `context_serializer`을 제공한 경우가 아니면 직렬화에 실패하고, 컨텍스트가 이미 매핑이거나 `context_deserializer`을 제공한 경우가 아니면 역직렬화에 실패합니다.
- `context_override`: 상태를 로드할 때 직렬화된 컨텍스트를 대체합니다. 원래 컨텍스트 객체를 복원하지 않으려는 경우 유용하지만, 이미 직렬화된 페이로드에서 해당 컨텍스트를 제거하지는 않습니다.
- `include_tracing_api_key=True`: 재개된 작업이 동일한 자격 증명으로 트레이스를 계속 내보내야 하는 경우 직렬화된 트레이스 페이로드에 트레이싱 API 키를 포함합니다.

직렬화된 실행 상태에는 애플리케이션 컨텍스트와 함께 승인, 사용량, 직렬화된 `tool_input`, 중첩된 도구로서의 에이전트 실행 재개, 트레이스 메타데이터, 서버 관리형 대화 설정 등 SDK가 관리하는 런타임 메타데이터가 포함됩니다. 직렬화된 상태를 저장하거나 전송하려는 경우 `RunContextWrapper.context`를 영구 데이터로 취급하고, 상태와 함께 이동하도록 의도한 경우가 아니라면 여기에 비밀 정보를 넣지 마세요.

## 대기 중인 작업의 버전 관리 {#versioning-pending-tasks}

승인이 장시간 대기할 수 있다면 직렬화된 상태와 함께 에이전트 정의 또는 SDK의 버전 표시를 저장합니다. 그러면 역직렬화 시 일치하는 코드 경로로 라우팅하여 모델, 프롬프트 또는 도구 정의가 변경될 때 발생하는 비호환성을 방지할 수 있습니다.