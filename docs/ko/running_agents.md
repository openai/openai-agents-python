---
search:
  exclude: true
---
# 에이전트 실행

[`Runner`][agents.run.Runner] 클래스를 통해 에이전트를 실행할 수 있습니다. 3가지 옵션이 있습니다.

1. [`Runner.run()`][agents.run.Runner.run]: 비동기로 실행되며 [`RunResult`][agents.result.RunResult]를 반환합니다.
2. [`Runner.run_sync()`][agents.run.Runner.run_sync]: 동기 메서드이며 내부적으로 `.run()`을 실행합니다.
3. [`Runner.run_streamed()`][agents.run.Runner.run_streamed]: 비동기로 실행되며 [`RunResultStreaming`][agents.result.RunResultStreaming]을 반환합니다. 스트리밍 모드로 LLM을 호출하고, 이벤트가 수신되는 대로 스트리밍합니다.

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="You are a helpful assistant")

    result = await Runner.run(agent, "Write a haiku about recursion in programming.")
    print(result.final_output)
    # Code within the code,
    # Functions calling themselves,
    # Infinite loop's dance
```

자세한 내용은 [결과 가이드](results.md)를 읽어보세요.

## Runner 수명 주기 및 구성

### 에이전트 루프

`Runner`의 run 메서드를 사용할 때 시작 에이전트와 입력을 전달합니다. 입력은 다음 중 하나일 수 있습니다.

-   문자열(사용자 메시지로 처리됨)
-   OpenAI Responses API 형식의 입력 항목 목록
-   중단된 실행을 재개할 때의 [`RunState`][agents.run_state.RunState]

그러면 runner가 루프를 실행합니다.

1. 현재 입력으로 현재 에이전트에 대해 LLM을 호출합니다.
2. LLM이 출력을 생성합니다.
    1. LLM이 `final_output`을 반환하면 루프가 종료되고 결과를 반환합니다.
    2. LLM이 핸드오프를 수행하면 현재 에이전트와 입력을 업데이트하고 루프를 다시 실행합니다.
    3. LLM이 도구 호출을 생성하면 해당 도구 호출을 실행하고 결과를 추가한 뒤 루프를 다시 실행합니다.
3. 전달된 `max_turns`를 초과하면 [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded] 예외를 발생시킵니다. 이 턴 제한을 비활성화하려면 `max_turns=None`을 전달하세요.

!!! note

    LLM 출력이 "최종 출력"으로 간주되는 규칙은, 원하는 타입의 텍스트 출력을 생성하고 도구 호출이 없는 경우입니다.

### 스트리밍

스트리밍을 사용하면 LLM이 실행되는 동안 스트리밍 이벤트를 추가로 받을 수 있습니다. 스트림이 완료되면 [`RunResultStreaming`][agents.result.RunResultStreaming]에는 생성된 모든 새 출력을 포함해 실행에 대한 전체 정보가 포함됩니다. 스트리밍 이벤트는 `.stream_events()`를 호출해 받을 수 있습니다. 자세한 내용은 [스트리밍 가이드](streaming.md)를 읽어보세요.

#### Responses WebSocket 전송(선택적 헬퍼)

OpenAI Responses websocket 전송을 활성화하면 일반 `Runner` API를 계속 사용할 수 있습니다. 연결 재사용을 위해 websocket 세션 헬퍼가 권장되지만 필수는 아닙니다.

이는 websocket 전송을 통한 Responses API이며, [Realtime API](realtime/guide.md)가 아닙니다.

전송 선택 규칙과 구체적인 모델 객체 또는 사용자 지정 제공자 관련 주의 사항은 [모델](models/index.md#responses-websocket-transport)을 참조하세요.

##### 패턴 1: 세션 헬퍼 없음(동작함)

websocket 전송만 원하고 SDK가 공유 제공자/세션을 관리할 필요가 없을 때 사용하세요.

```python
import asyncio

from agents import Agent, Runner, set_default_openai_responses_transport


async def main():
    set_default_openai_responses_transport("websocket")

    agent = Agent(name="Assistant", instructions="Be concise.")
    result = Runner.run_streamed(agent, "Summarize recursion in one sentence.")

    async for event in result.stream_events():
        if event.type == "raw_response_event":
            continue
        print(event.type)


asyncio.run(main())
```

이 패턴은 단일 실행에는 적합합니다. `Runner.run()` / `Runner.run_streamed()`를 반복해서 호출하면 동일한 `RunConfig` / 제공자 인스턴스를 수동으로 재사용하지 않는 한 각 실행이 다시 연결될 수 있습니다.

##### 패턴 2: `responses_websocket_session()` 사용(다중 턴 재사용에 권장)

여러 실행에서(동일한 `run_config`를 상속하는 중첩 agent-as-tool 호출 포함) 공유 websocket 지원 제공자와 `RunConfig`를 사용하려면 [`responses_websocket_session()`][agents.responses_websocket_session]을 사용하세요.

```python
import asyncio

from agents import Agent, responses_websocket_session


async def main():
    agent = Agent(name="Assistant", instructions="Be concise.")

    async with responses_websocket_session(
        responses_websocket_options={"ping_interval": 20.0, "ping_timeout": 60.0},
    ) as ws:
        first = ws.run_streamed(agent, "Say hello in one short sentence.")
        async for _event in first.stream_events():
            pass

        second = ws.run_streamed(
            agent,
            "Now say goodbye.",
            previous_response_id=first.last_response_id,
        )
        async for _event in second.stream_events():
            pass


asyncio.run(main())
```

컨텍스트가 종료되기 전에 스트리밍 결과 소비를 마치세요. websocket 요청이 아직 진행 중일 때 컨텍스트를 종료하면 공유 연결이 강제로 닫힐 수 있습니다.

긴 추론 턴에서 websocket keepalive 타임아웃이 발생하면 `ping_timeout`을 늘리거나 `ping_timeout=None`으로 설정해 heartbeat 타임아웃을 비활성화하세요. 신뢰성이 websocket 지연 시간보다 중요한 실행에는 HTTP/SSE 전송을 사용하세요.

### Run config

`run_config` 매개변수를 사용하면 에이전트 실행에 대한 일부 전역 설정을 구성할 수 있습니다.

#### 일반적인 run config 카테고리

각 에이전트 정의를 변경하지 않고 단일 실행의 동작을 재정의하려면 `RunConfig`를 사용하세요.

##### 모델, 제공자 및 세션 기본값

-   [`model`][agents.run.RunConfig.model]: 각 Agent가 가진 `model`과 관계없이 사용할 전역 LLM 모델을 설정할 수 있습니다.
-   [`model_provider`][agents.run.RunConfig.model_provider]: 모델 이름 조회에 사용할 모델 제공자이며 기본값은 OpenAI입니다.
-   [`model_settings`][agents.run.RunConfig.model_settings]: 에이전트별 설정을 재정의합니다. 예를 들어 전역 `temperature` 또는 `top_p`를 설정할 수 있습니다.
-   [`session_settings`][agents.run.RunConfig.session_settings]: 실행 중 기록을 가져올 때 세션 수준 기본값(예: `SessionSettings(limit=...)`)을 재정의합니다.
-   [`session_input_callback`][agents.run.RunConfig.session_input_callback]: Sessions를 사용할 때 각 턴 전에 새 사용자 입력이 세션 기록과 병합되는 방식을 사용자 지정합니다. 콜백은 동기 또는 비동기일 수 있습니다.

##### 가드레일, 핸드오프 및 모델 입력 형성

-   [`input_guardrails`][agents.run.RunConfig.input_guardrails], [`output_guardrails`][agents.run.RunConfig.output_guardrails]: 모든 실행에 포함할 입력 또는 출력 가드레일 목록입니다.
-   [`handoff_input_filter`][agents.run.RunConfig.handoff_input_filter]: 핸드오프에 이미 필터가 없는 경우 모든 핸드오프에 적용할 전역 입력 필터입니다. 입력 필터를 사용하면 새 에이전트로 전송되는 입력을 편집할 수 있습니다. 자세한 내용은 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 문서를 참조하세요.
-   [`nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]: 다음 에이전트를 호출하기 전에 이전 대화 기록을 단일 assistant 메시지로 축약하는 옵트인 베타입니다. 중첩 핸드오프를 안정화하는 동안 기본적으로 비활성화되어 있습니다. 활성화하려면 `True`로 설정하고, 원문 대화 기록을 그대로 전달하려면 `False`로 둡니다. 모든 [Runner 메서드][agents.run.Runner]는 전달된 `RunConfig`가 없을 때 자동으로 `RunConfig`를 생성하므로, 빠른 시작과 예제는 기본적으로 꺼진 상태를 유지하며, 명시적인 [`Handoff.input_filter`][agents.handoffs.Handoff.input_filter] 콜백은 계속 이를 재정의합니다. 개별 핸드오프는 [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history]를 통해 이 설정을 재정의할 수 있습니다.
-   [`handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper]: `nest_handoff_history`에 옵트인할 때마다 정규화된 대화 기록(기록 + 핸드오프 항목)을 받는 선택적 callable입니다. 다음 에이전트로 전달할 정확한 입력 항목 목록을 반환해야 하며, 전체 핸드오프 필터를 작성하지 않고도 내장 요약을 대체할 수 있습니다.
-   [`call_model_input_filter`][agents.run.RunConfig.call_model_input_filter]: 모델 호출 직전에 완전히 준비된 모델 입력(instructions 및 입력 항목)을 편집하는 훅입니다. 예를 들어 기록을 줄이거나 시스템 프롬프트를 삽입할 수 있습니다.
-   [`reasoning_item_id_policy`][agents.run.RunConfig.reasoning_item_id_policy]: runner가 이전 출력을 다음 턴 모델 입력으로 변환할 때 reasoning 항목 ID를 보존할지 생략할지 제어합니다.

##### 트레이싱 및 관측 가능성

-   [`tracing_disabled`][agents.run.RunConfig.tracing_disabled]: 전체 실행에 대해 [트레이싱](tracing.md)을 비활성화할 수 있습니다.
-   [`tracing`][agents.run.RunConfig.tracing]: 실행별 트레이싱 API 키와 같은 trace 내보내기 설정을 재정의하려면 [`TracingConfig`][agents.tracing.TracingConfig]를 전달합니다.
-   [`trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]: trace에 LLM 및 도구 호출 입력/출력과 같이 잠재적으로 민감한 데이터를 포함할지 여부를 구성합니다.
-   [`workflow_name`][agents.run.RunConfig.workflow_name], [`trace_id`][agents.run.RunConfig.trace_id], [`group_id`][agents.run.RunConfig.group_id]: 실행의 트레이싱 워크플로 이름, trace ID 및 trace 그룹 ID를 설정합니다. 최소한 `workflow_name`은 설정하는 것을 권장합니다. 그룹 ID는 여러 실행 간 trace를 연결할 수 있는 선택적 필드입니다.
-   [`trace_metadata`][agents.run.RunConfig.trace_metadata]: 모든 trace에 포함할 메타데이터입니다.

##### 도구 실행, 승인 및 도구 오류 동작

-   [`tool_execution`][agents.run.RunConfig.tool_execution]: 한 번에 실행되는 함수 도구 수 제한과 같이 로컬 도구 호출에 대한 SDK 측 실행 동작을 구성합니다.
-   [`tool_error_formatter`][agents.run.RunConfig.tool_error_formatter]: 승인 흐름 중 도구 호출이 거부될 때 모델에 표시되는 메시지를 사용자 지정합니다.

중첩 핸드오프는 옵트인 베타로 제공됩니다. 축약된 대화 기록 동작을 활성화하려면 `RunConfig(nest_handoff_history=True)`를 전달하거나 특정 핸드오프에 대해 `handoff(..., nest_handoff_history=True)`를 설정하세요. 원문 대화 기록을 유지하려는 경우(기본값), 플래그를 설정하지 않거나 필요한 방식 그대로 대화를 전달하는 `handoff_input_filter`(또는 `handoff_history_mapper`)를 제공하세요. 사용자 지정 mapper를 작성하지 않고 생성된 요약에 사용되는 래퍼 텍스트를 변경하려면 [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]를 호출하세요(기본값으로 복원하려면 [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]).

#### Run config 세부 정보

##### `tool_execution`

실행에 대해 SDK가 로컬 함수 도구 동시성을 제한하도록 하려면 `tool_execution`을 사용하세요.

```python
from agents import Agent, RunConfig, Runner, ToolExecutionConfig

agent = Agent(name="Assistant", tools=[...])

result = await Runner.run(
    agent,
    "Run the required tool calls.",
    run_config=RunConfig(
        tool_execution=ToolExecutionConfig(max_function_tool_concurrency=2),
    ),
)
```

`max_function_tool_concurrency=None`은 기본 동작을 유지합니다. 모델이 한 턴에 여러 함수 도구 호출을 내보내면 SDK는 내보낸 모든 로컬 함수 도구 호출을 시작합니다. 동시에 실행되는 로컬 함수 도구 수를 제한하려면 정수 값을 설정하세요.

이는 제공자 측 [`ModelSettings.parallel_tool_calls`][agents.model_settings.ModelSettings.parallel_tool_calls]와 별개입니다. `parallel_tool_calls`는 모델이 단일 응답에서 여러 도구 호출을 내보낼 수 있는지 여부를 제어합니다. `tool_execution.max_function_tool_concurrency`는 모델이 도구 호출을 내보낸 후 SDK가 로컬 함수 도구 호출을 실행하는 방식을 제어합니다.

##### `tool_error_formatter`

승인 흐름에서 도구 호출이 거부될 때 모델에 반환되는 메시지를 사용자 지정하려면 `tool_error_formatter`를 사용하세요.

formatter는 다음을 포함하는 [`ToolErrorFormatterArgs`][agents.run_config.ToolErrorFormatterArgs]를 받습니다.

-   `kind`: 오류 카테고리입니다. 현재 이는 `"approval_rejected"`입니다.
-   `tool_type`: 도구 런타임입니다(`"function"`, `"computer"`, `"shell"`, `"apply_patch"` 또는 `"custom"`).
-   `tool_name`: 도구 이름입니다.
-   `call_id`: 도구 호출 ID입니다.
-   `default_message`: SDK의 기본 모델 표시 메시지입니다.
-   `run_context`: 활성 실행 컨텍스트 래퍼입니다.

메시지를 대체하려면 문자열을 반환하고, SDK 기본값을 사용하려면 `None`을 반환하세요.

```python
from agents import Agent, RunConfig, Runner, ToolErrorFormatterArgs


def format_rejection(args: ToolErrorFormatterArgs[None]) -> str | None:
    if args.kind == "approval_rejected":
        return (
            f"Tool call '{args.tool_name}' was rejected by a human reviewer. "
            "Ask for confirmation or propose a safer alternative."
        )
    return None


agent = Agent(name="Assistant")
result = Runner.run_sync(
    agent,
    "Please delete the production database.",
    run_config=RunConfig(tool_error_formatter=format_rejection),
)
```

##### `reasoning_item_id_policy`

`reasoning_item_id_policy`는 runner가 기록을 다음으로 전달할 때(예: `RunResult.to_input_list()` 또는 세션 기반 실행 사용 시) reasoning 항목이 다음 턴 모델 입력으로 변환되는 방식을 제어합니다.

-   `None` 또는 `"preserve"`(기본값): reasoning 항목 ID를 유지합니다.
-   `"omit"`: 생성된 다음 턴 입력에서 reasoning 항목 ID를 제거합니다.

`"omit"`는 주로 reasoning 항목이 `id`와 함께 전송되었지만 필수 후속 항목이 없는 경우 발생하는 Responses API 400 오류 범주에 대한 옵트인 완화책으로 사용하세요(예: `Item 'rs_...' of type 'reasoning' was provided without its required following item.`).

이는 SDK가 이전 출력에서 후속 입력을 구성하는 다중 턴 에이전트 실행에서 발생할 수 있습니다(세션 지속성, 서버 관리 대화 델타, 스트리밍/비스트리밍 후속 턴, 재개 경로 포함). 이때 reasoning 항목 ID는 보존되지만 제공자는 해당 ID가 대응되는 후속 항목과 계속 짝을 이루도록 요구합니다.

`reasoning_item_id_policy="omit"`를 설정하면 reasoning 내용은 유지하되 reasoning 항목 `id`를 제거하여, SDK가 생성한 후속 입력에서 해당 API 불변 조건이 트리거되는 것을 방지합니다.

범위 참고 사항:

-   이는 SDK가 후속 입력을 빌드할 때 생성/전달하는 reasoning 항목만 변경합니다.
-   사용자가 제공한 초기 입력 항목은 다시 작성하지 않습니다.
-   `call_model_input_filter`는 이 정책이 적용된 후에도 의도적으로 reasoning ID를 다시 도입할 수 있습니다.

## 상태 및 대화 관리

### 메모리 전략 선택

다음 턴으로 상태를 전달하는 일반적인 방법은 네 가지입니다.

| 전략 | 상태가 있는 위치 | 적합한 용도 | 다음 턴에 전달하는 내용 |
| --- | --- | --- | --- |
| `result.to_input_list()` | 앱 메모리 | 작은 채팅 루프, 완전한 수동 제어, 모든 제공자 | `result.to_input_list()`의 목록과 다음 사용자 메시지 |
| `session` | 사용자의 스토리지와 SDK | 지속형 채팅 상태, 재개 가능한 실행, 사용자 지정 저장소 | 동일한 `session` 인스턴스 또는 같은 저장소를 가리키는 다른 인스턴스 |
| `conversation_id` | OpenAI Conversations API | 작업자나 서비스 간 공유하려는 명명된 서버 측 대화 | 동일한 `conversation_id`와 새 사용자 턴만 |
| `previous_response_id` | OpenAI Responses API | 대화 리소스를 만들지 않는 경량 서버 관리형 이어가기 | `result.last_response_id`와 새 사용자 턴만 |

`result.to_input_list()`와 `session`은 클라이언트 관리형입니다. `conversation_id`와 `previous_response_id`는 OpenAI 관리형이며 OpenAI Responses API를 사용할 때만 적용됩니다. 대부분의 애플리케이션에서는 대화당 하나의 지속성 전략을 선택하세요. 두 계층을 의도적으로 조정하지 않는 한 클라이언트 관리 기록과 OpenAI 관리 상태를 혼합하면 컨텍스트가 중복될 수 있습니다.

!!! note

    세션 지속성은 동일한 실행에서 서버 관리 대화 설정
    (`conversation_id`, `previous_response_id` 또는 `auto_previous_response_id`)과 함께 사용할 수 없습니다.
    호출마다 하나의 접근 방식을 선택하세요.

### 대화/채팅 스레드

run 메서드 중 하나를 호출하면 하나 이상의 에이전트가 실행될 수 있고(따라서 하나 이상의 LLM 호출이 발생할 수 있음), 이는 채팅 대화에서 하나의 논리적 턴을 나타냅니다. 예를 들면 다음과 같습니다.

1. 사용자 턴: 사용자가 텍스트 입력
2. Runner 실행: 첫 번째 에이전트가 LLM을 호출하고 도구를 실행하며 두 번째 에이전트로 핸드오프하고, 두 번째 에이전트가 더 많은 도구를 실행한 뒤 출력을 생성합니다.

에이전트 실행이 끝나면 사용자에게 무엇을 보여줄지 선택할 수 있습니다. 예를 들어 에이전트가 생성한 모든 새 항목을 사용자에게 보여줄 수도 있고, 최종 출력만 보여줄 수도 있습니다. 어느 쪽이든 사용자가 후속 질문을 할 수 있으며, 이 경우 run 메서드를 다시 호출할 수 있습니다.

#### 수동 대화 관리

다음 턴의 입력을 얻기 위해 [`RunResultBase.to_input_list()`][agents.result.RunResultBase.to_input_list] 메서드를 사용하여 대화 기록을 수동으로 관리할 수 있습니다.

```python
async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    thread_id = "thread_123"  # Example thread ID
    with trace(workflow_name="Conversation", group_id=thread_id):
        # First turn
        result = await Runner.run(agent, "What city is the Golden Gate Bridge in?")
        print(result.final_output)
        # San Francisco

        # Second turn
        new_input = result.to_input_list() + [{"role": "user", "content": "What state is it in?"}]
        result = await Runner.run(agent, new_input)
        print(result.final_output)
        # California
```

#### 세션을 사용한 자동 대화 관리

더 간단한 접근 방식으로, `.to_input_list()`를 수동으로 호출하지 않고도 대화 기록을 자동으로 처리하기 위해 [Sessions](sessions/index.md)를 사용할 수 있습니다.

```python
from agents import Agent, Runner, SQLiteSession

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create session instance
    session = SQLiteSession("conversation_123")

    thread_id = "thread_123"  # Example thread ID
    with trace(workflow_name="Conversation", group_id=thread_id):
        # First turn
        result = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
        print(result.final_output)
        # San Francisco

        # Second turn - agent automatically remembers previous context
        result = await Runner.run(agent, "What state is it in?", session=session)
        print(result.final_output)
        # California
```

Sessions는 자동으로 다음을 수행합니다.

-   각 실행 전에 대화 기록을 가져옵니다.
-   각 실행 후 새 메시지를 저장합니다.
-   서로 다른 세션 ID에 대해 별도의 대화를 유지합니다.

자세한 내용은 [Sessions 문서](sessions/index.md)를 참조하세요.


#### 서버 관리 대화

`to_input_list()` 또는 `Sessions`로 로컬에서 처리하는 대신, OpenAI 대화 상태 기능이 서버 측에서 대화 상태를 관리하도록 할 수도 있습니다. 이를 통해 과거 메시지를 모두 수동으로 다시 보내지 않고도 대화 기록을 보존할 수 있습니다. 아래 서버 관리 접근 방식 중 어느 것이든 각 요청에는 새 턴의 입력만 전달하고 저장된 ID를 재사용하세요. 자세한 내용은 [OpenAI Conversation 상태 가이드](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)를 참조하세요.

OpenAI는 턴 간 상태를 추적하는 두 가지 방법을 제공합니다.

##### 1. `conversation_id` 사용

먼저 OpenAI Conversations API를 사용해 대화를 생성한 다음 이후 모든 호출에서 해당 ID를 재사용합니다.

```python
from agents import Agent, Runner
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    # Create a server-managed conversation
    conversation = await client.conversations.create()
    conv_id = conversation.id

    while True:
        user_input = input("You: ")
        result = await Runner.run(agent, user_input, conversation_id=conv_id)
        print(f"Assistant: {result.final_output}")
```

##### 2. `previous_response_id` 사용

또 다른 옵션은 **응답 체이닝**으로, 각 턴이 이전 턴의 응답 ID에 명시적으로 연결됩니다.

```python
from agents import Agent, Runner

async def main():
    agent = Agent(name="Assistant", instructions="Reply very concisely.")

    previous_response_id = None

    while True:
        user_input = input("You: ")

        # Setting auto_previous_response_id=True enables response chaining automatically
        # for the first turn, even when there's no actual previous response ID yet.
        result = await Runner.run(
            agent,
            user_input,
            previous_response_id=previous_response_id,
            auto_previous_response_id=True,
        )
        previous_response_id = result.last_response_id
        print(f"Assistant: {result.final_output}")
```

실행이 승인을 위해 일시 중지되고 [`RunState`][agents.run_state.RunState]에서 재개하는 경우,
SDK는 저장된 `conversation_id` / `previous_response_id` / `auto_previous_response_id`
설정을 유지하므로 재개된 턴은 동일한 서버 관리 대화에서 계속됩니다.

`conversation_id`와 `previous_response_id`는 상호 배타적입니다. 여러 시스템 간 공유할 수 있는 명명된 대화 리소스를 원하면 `conversation_id`를 사용하세요. 한 턴에서 다음 턴으로 이어지는 가장 가벼운 Responses API 이어가기 기본 구성 요소를 원하면 `previous_response_id`를 사용하세요.

!!! note

    SDK는 `conversation_locked` 오류를 backoff로 자동 재시도합니다. 서버 관리
    대화 실행에서는 재시도 전에 내부 대화 추적기 입력을 되감아
    동일하게 준비된 항목을 깔끔하게 다시 전송할 수 있도록 합니다.

    로컬 세션 기반 실행(`conversation_id`,
    `previous_response_id` 또는 `auto_previous_response_id`와 함께 사용할 수 없음)에서도 SDK는 재시도 후 중복 기록 항목을 줄이기 위해 최근 지속된 입력 항목을 최선의 노력으로
    롤백합니다.

    이 호환성 재시도는 `ModelSettings.retry`를 구성하지 않아도 발생합니다. 모델 요청에 대한
    더 폭넓은 옵트인 재시도 동작은 [Runner 관리 재시도](models/index.md#runner-managed-retries)를 참조하세요.

## 훅 및 사용자 지정

### 모델 입력 필터 호출

모델 호출 직전에 모델 입력을 편집하려면 `call_model_input_filter`를 사용하세요. 이 훅은 현재 에이전트, 컨텍스트, 결합된 입력 항목(있는 경우 세션 기록 포함)을 받고 새로운 `ModelInputData`를 반환합니다.

반환 값은 [`ModelInputData`][agents.run.ModelInputData] 객체여야 합니다. 해당 `input` 필드는 필수이며 입력 항목 목록이어야 합니다. 다른 형태를 반환하면 `UserError`가 발생합니다.

```python
from agents import Agent, Runner, RunConfig
from agents.run import CallModelData, ModelInputData

def drop_old_messages(data: CallModelData[None]) -> ModelInputData:
    # Keep only the last 5 items and preserve existing instructions.
    trimmed = data.model_data.input[-5:]
    return ModelInputData(input=trimmed, instructions=data.model_data.instructions)

agent = Agent(name="Assistant", instructions="Answer concisely.")
result = Runner.run_sync(
    agent,
    "Explain quines",
    run_config=RunConfig(call_model_input_filter=drop_old_messages),
)
```

runner는 준비된 입력 목록의 복사본을 훅에 전달하므로 호출자의 원본 목록을 제자리에서 변경하지 않고도 이를 줄이거나, 교체하거나, 재정렬할 수 있습니다.

세션을 사용하는 경우 `call_model_input_filter`는 세션 기록이 이미 로드되어 현재 턴과 병합된 후에 실행됩니다. 해당 이전 병합 단계 자체를 사용자 지정하려면 [`session_input_callback`][agents.run.RunConfig.session_input_callback]을 사용하세요.

`conversation_id`, `previous_response_id` 또는 `auto_previous_response_id`와 함께 OpenAI 서버 관리 대화 상태를 사용하는 경우, 훅은 다음 Responses API 호출을 위해 준비된 페이로드에서 실행됩니다. 해당 페이로드는 이전 기록 전체를 재생한 것이 아니라 이미 새 턴 델타만 나타낼 수 있습니다. 반환하는 항목만 해당 서버 관리 이어가기에서 전송된 것으로 표시됩니다.

민감한 데이터를 수정하거나, 긴 기록을 줄이거나, 추가 시스템 지침을 삽입하려면 실행별로 `run_config`를 통해 훅을 설정하세요.

## 오류 및 복구

### 오류 핸들러

모든 `Runner` 진입점은 오류 종류를 키로 하는 dict인 `error_handlers`를 받습니다. 지원되는 키는 `"max_turns"`와 `"model_refusal"`입니다. `MaxTurnsExceeded` 또는 `ModelRefusalError`를 발생시키는 대신 제어된 최종 출력을 반환하려는 경우 사용하세요.

```python
from agents import (
    Agent,
    RunErrorHandlerInput,
    RunErrorHandlerResult,
    Runner,
)

agent = Agent(name="Assistant", instructions="Be concise.")


def on_max_turns(_data: RunErrorHandlerInput[None]) -> RunErrorHandlerResult:
    return RunErrorHandlerResult(
        final_output="I couldn't finish within the turn limit. Please narrow the request.",
        include_in_history=False,
    )


result = Runner.run_sync(
    agent,
    "Analyze this long transcript",
    max_turns=3,
    error_handlers={"max_turns": on_max_turns},
)
print(result.final_output)
```

대체 출력이 대화 기록에 추가되지 않도록 하려면 `include_in_history=False`를 설정하세요.

모델 거부가 `ModelRefusalError`로 실행을 종료하는 대신 애플리케이션별 대체 출력을 생성해야 하는 경우 `"model_refusal"`을 사용하세요.

```python
from pydantic import BaseModel

from agents import Agent, ModelRefusalError, RunErrorHandlerInput, Runner


class Recipe(BaseModel):
    ingredients: list[str]
    refusal_reason: str | None = None


def on_model_refusal(data: RunErrorHandlerInput[None]) -> Recipe:
    assert isinstance(data.error, ModelRefusalError)
    return Recipe(ingredients=[], refusal_reason=data.error.refusal)


agent = Agent(
    name="Recipe assistant",
    instructions="Return a structured recipe.",
    output_type=Recipe,
)

result = Runner.run_sync(
    agent,
    "Make me something unsafe.",
    error_handlers={"model_refusal": on_model_refusal},
)
print(result.final_output)
```

## 내구성 있는 실행 통합 및 휴먼인더루프

도구 승인 일시 중지/재개 패턴은 전용 [휴먼인더루프 가이드](human_in_the_loop.md)부터 시작하세요.
아래 통합은 실행이 긴 대기, 재시도 또는 프로세스 재시작에 걸칠 수 있는 내구성 있는 오케스트레이션을 위한 것입니다.

### Dapr

Agents SDK [Dapr](https://dapr.io) Diagrid 통합을 사용하면 휴먼인더루프 지원으로 장애에서 자동 복구되는 내구성 있고 장기 실행되는 에이전트를 실행할 수 있습니다. Dapr은 벤더 중립적인 [CNCF](https://cncf.io) 워크플로 오케스트레이터입니다. Dapr 및 OpenAI 에이전트를 [여기](https://docs.diagrid.io/getting-started/quickstarts/ai-agents/?agentframework=openai)에서 시작하세요.

### Temporal

Agents SDK [Temporal](https://temporal.io/) 통합을 사용하면 휴먼인더루프 작업을 포함해 내구성 있고 장기 실행되는 워크플로를 실행할 수 있습니다. Temporal과 Agents SDK가 함께 작동하여 장기 실행 작업을 완료하는 데모를 [이 동영상](https://www.youtube.com/watch?v=fFBZqzT4DD8)에서 보고, [문서는 여기에서 확인하세요](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents). 

### Restate

Agents SDK [Restate](https://restate.dev/) 통합을 사용하면 사람 승인, 핸드오프, 세션 관리를 포함한 경량의 내구성 있는 에이전트를 사용할 수 있습니다. 이 통합은 Restate의 단일 바이너리 런타임을 종속성으로 필요로 하며, 에이전트를 프로세스/컨테이너 또는 서버리스 함수로 실행하는 것을 지원합니다.
자세한 내용은 [개요](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk)를 읽거나 [문서](https://docs.restate.dev/ai)를 확인하세요.

### DBOS

Agents SDK [DBOS](https://dbos.dev/) 통합을 사용하면 장애 및 재시작 전반에서 진행 상황을 보존하는 신뢰할 수 있는 에이전트를 실행할 수 있습니다. 장기 실행 에이전트, 휴먼인더루프 워크플로, 핸드오프를 지원합니다. 동기 및 비동기 메서드를 모두 지원합니다. 이 통합에는 SQLite 또는 Postgres 데이터베이스만 필요합니다. 자세한 내용은 통합 [repo](https://github.com/dbos-inc/dbos-openai-agents)와 [문서](https://docs.dbos.dev/integrations/openai-agents)를 확인하세요.

## 예외

SDK는 특정 경우 예외를 발생시킵니다. 전체 목록은 [`agents.exceptions`][]에 있습니다. 개요는 다음과 같습니다.

-   [`AgentsException`][agents.exceptions.AgentsException]: SDK 내에서 발생하는 모든 예외의 기본 클래스입니다. 다른 모든 특정 예외가 파생되는 일반 타입 역할을 합니다.
-   [`MaxTurnsExceeded`][agents.exceptions.MaxTurnsExceeded]: 이 예외는 에이전트 실행이 `Runner.run`, `Runner.run_sync` 또는 `Runner.run_streamed` 메서드에 전달된 `max_turns` 제한을 초과할 때 발생합니다. 에이전트가 지정된 상호작용 턴 수 내에 작업을 완료하지 못했음을 나타냅니다. 제한을 비활성화하려면 `max_turns=None`을 설정하세요.
-   [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError]: 이 예외는 기본 모델(LLM)이 예기치 않거나 잘못된 출력을 생성할 때 발생합니다. 여기에는 다음이 포함될 수 있습니다.
    -   잘못된 JSON: 모델이 도구 호출 또는 직접 출력에서 잘못된 JSON 구조를 제공하는 경우, 특히 특정 `output_type`이 정의된 경우
    -   예기치 않은 도구 관련 실패: 모델이 예상된 방식으로 도구를 사용하지 못하는 경우
-   [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]: 이 예외는 함수 도구 호출이 구성된 타임아웃을 초과하고 도구가 `timeout_behavior="raise_exception"`을 사용할 때 발생합니다.
-   [`UserError`][agents.exceptions.UserError]: 이 예외는 (SDK를 사용해 코드를 작성하는) 사용자가 SDK 사용 중 오류를 만들 때 발생합니다. 일반적으로 잘못된 코드 구현, 유효하지 않은 구성 또는 SDK API의 오용으로 인해 발생합니다.
-   [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered], [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]: 이 예외는 각각 입력 가드레일 또는 출력 가드레일의 조건이 충족될 때 발생합니다. 입력 가드레일은 처리 전에 들어오는 메시지를 확인하고, 출력 가드레일은 전달 전에 에이전트의 최종 응답을 확인합니다.