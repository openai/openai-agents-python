---
search:
  exclude: true
---
# Realtime agents 가이드

이 가이드에서는 OpenAI Agents SDK의 실시간 계층이 OpenAI Realtime API에 어떻게 매핑되는지와 Python SDK가 그 위에 추가하는 동작을 설명합니다.

!!! note "시작하기"

    기본 Python 경로를 사용하려면 먼저 [빠른 시작](quickstart.md)을 읽어보세요. 애플리케이션에서 서버 측 WebSocket과 SIP 중 무엇을 사용할지 결정하려면 [실시간 전송](transport.md)을 읽어보세요. 브라우저 WebRTC 전송은 Python SDK에 포함되지 않습니다.

## 개요

Realtime agents는 Realtime API와의 장기 연결을 유지하므로 모델이 텍스트와 오디오를 점진적으로 처리하고, 오디오 출력을 스트리밍하고, 도구를 호출하고, 매 턴마다 새로운 요청을 다시 시작하지 않고도 인터럽션(중단 처리)을 처리할 수 있습니다.

주요 SDK 구성 요소는 다음과 같습니다.

-   **RealtimeAgent**: 한 실시간 전문 에이전트를 위한 지침, 도구, 출력 가드레일 및 핸드오프
-   **RealtimeRunner**: 시작 에이전트를 실시간 전송에 연결하는 세션 팩토리
-   **RealtimeSession**: 입력을 전송하고, 이벤트를 수신하고, 기록을 추적하고, 도구를 실행하는 라이브 세션
-   **RealtimeModel**: 전송 추상화입니다. 기본값은 OpenAI의 서버 측 WebSocket 구현입니다.

## 세션 수명 주기

일반적인 실시간 세션은 다음과 같이 진행됩니다.

1. 하나 이상의 `RealtimeAgent`를 생성합니다.
2. 시작 에이전트로 `RealtimeRunner`를 생성합니다.
3. `await runner.run()`을 호출하여 `RealtimeSession`을 가져옵니다.
4. `async with session:` 또는 `await session.enter()`을 사용하여 세션에 진입합니다.
5. `send_message()` 또는 `send_audio()`를 사용하여 사용자 입력을 전송합니다.
6. 대화가 종료될 때까지 세션 이벤트를 순회합니다.

텍스트 전용 실행과 달리 `runner.run()`은 최종 결과를 즉시 생성하지 않습니다. 대신 로컬 기록, 백그라운드 도구 실행, 가드레일 상태 및 활성 에이전트 구성을 전송 계층과 동기화된 상태로 유지하는 라이브 세션 객체를 반환합니다.

기본적으로 `RealtimeRunner`는 `OpenAIRealtimeWebSocketModel`을 사용하므로 기본 Python 경로는 Realtime API에 대한 서버 측 WebSocket 연결입니다. 다른 `RealtimeModel`을 전달하더라도 동일한 세션 수명 주기와 에이전트 기능이 적용되며, 연결 방식만 달라질 수 있습니다.

## 에이전트 및 세션 구성

`RealtimeAgent`는 의도적으로 일반 `Agent` 유형보다 범위가 제한되어 있습니다.

-   모델 선택은 에이전트별이 아니라 세션 수준에서 구성합니다.
-   structured outputs은 지원되지 않습니다.
-   음성은 구성할 수 있지만 세션에서 음성 오디오를 이미 생성한 후에는 변경할 수 없습니다.
-   지침, 함수 도구, 핸드오프, 훅 및 출력 가드레일은 모두 계속 작동합니다.

`RealtimeSessionModelSettings`는 새로운 중첩 `audio` 구성과 이전의 평면 별칭을 모두 지원합니다. 새 코드에서는 중첩 구조를 사용하는 것이 좋으며, 새로운 Realtime agents에는 `gpt-realtime-2.1`부터 사용하세요.

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime-2.1",
            "audio": {
                "input": {
                    "format": "pcm16",
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
                },
                "output": {"format": "pcm16", "voice": "ash"},
            },
            "tool_choice": "auto",
        }
    },
)
```

유용한 세션 수준 설정은 다음과 같습니다.

-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`, `audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

`RealtimeRunner(config=...)`에서 유용한 실행 수준 설정은 다음과 같습니다.

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

전체 타입 지정 인터페이스는 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 및 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]를 참조하세요.

## 입력 및 출력

### 텍스트 및 구조화된 사용자 메시지

일반 텍스트 또는 구조화된 실시간 메시지에는 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message]를 사용하세요.

```python
from agents.realtime import RealtimeUserInputMessage

await session.send_message("Summarize what we discussed so far.")

message: RealtimeUserInputMessage = {
    "type": "message",
    "role": "user",
    "content": [
        {"type": "input_text", "text": "Describe this image."},
        {"type": "input_image", "image_url": image_data_url, "detail": "high"},
    ],
}
await session.send_message(message)
```

구조화된 메시지는 실시간 대화에 이미지 입력을 포함하는 주요 방법입니다. [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)의 웹 데모 코드 예제에서는 이 방식으로 `input_image` 메시지를 전달합니다.

### 오디오 입력

원문 오디오 바이트를 스트리밍하려면 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio]를 사용하세요.

```python
await session.send_audio(audio_bytes)
```

서버 측 턴 감지가 비활성화된 경우 턴 경계를 직접 표시해야 합니다. 다음과 같은 상위 수준 편의 기능을 사용할 수 있습니다.

```python
await session.send_audio(audio_bytes, commit=True)
```

더 낮은 수준의 제어가 필요한 경우 기본 모델 전송을 통해 `input_audio_buffer.commit`과 같은 원문 클라이언트 이벤트를 전송할 수도 있습니다.

### 수동 응답 제어

`session.send_message()`는 상위 수준 경로를 사용하여 사용자 입력을 전송하고 응답을 자동으로 시작합니다. 원문 오디오 버퍼링은 모든 구성에서 동일한 작업을 **자동으로 수행하지는 않습니다**.

Realtime API 수준에서 수동 턴 제어를 사용하려면 원문 `session.update`로 `turn_detection`을 지운 다음, `input_audio_buffer.commit`과 `response.create`를 직접 전송해야 합니다.

턴을 수동으로 관리하는 경우 모델 전송을 통해 원문 클라이언트 이벤트를 전송할 수 있습니다.

```python
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

await session.model.send_event(
    RealtimeModelSendRawMessage(
        message={
            "type": "response.create",
        }
    )
)
```

이 패턴은 다음과 같은 경우에 유용합니다.

-   `turn_detection`이 비활성화되어 있고 모델이 응답할 시점을 직접 결정하려는 경우
-   응답을 트리거하기 전에 사용자 입력을 검사하거나 제어하려는 경우
-   대역 외 응답에 사용자 지정 프롬프트가 필요한 경우

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)의 SIP 코드 예제에서는 원문 `response.create`를 사용하여 첫 인사말을 강제로 생성합니다.

## 이벤트, 기록 및 인터럽션(중단 처리)

`RealtimeSession`은 상위 수준 SDK 이벤트를 내보내면서, 필요할 때 사용할 수 있도록 원문 모델 이벤트도 계속 전달합니다.

주요 세션 이벤트는 다음과 같습니다.

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

UI 상태에 가장 유용한 이벤트는 일반적으로 `history_added`와 `history_updated`입니다. 이러한 이벤트는 사용자 메시지, 어시스턴트 메시지 및 도구 호출을 포함한 세션의 로컬 기록을 `RealtimeItem` 객체로 제공합니다.

### 사용량 집계

완료된 모델 응답에 사용량이 포함된 경우 OpenAI 실시간 모델은 `raw_model_event` 내에서 [`RealtimeModelUsageEvent`][agents.realtime.model_events.RealtimeModelUsageEvent]를 내보냅니다. `usage` 필드에는 해당 응답의 토큰 수가 포함되며, `input_tokens_details`와 `output_tokens_details`는 선택적인 모달리티별 내역을 제공합니다.

세션은 각 응답의 사용량도 공유 [`RunContextWrapper.usage`][agents.run_context.RunContextWrapper.usage]에 추가합니다. 라이브 세션의 누적 사용량을 확인하려면 이후의 `agent_end` 같은 상위 수준 이벤트에서 `event.info.context.usage`를 읽으세요.

```python
from agents.realtime import RealtimeModelUsageEvent

async for event in session:
    if event.type == "raw_model_event" and isinstance(
        event.data, RealtimeModelUsageEvent
    ):
        response_usage = event.data.usage
        print("Response tokens:", response_usage.total_tokens)
        print("Input modalities:", event.data.input_tokens_details)
        print("Output modalities:", event.data.output_tokens_details)
    elif event.type == "agent_end":
        session_usage = event.info.context.usage
        print("Session tokens:", session_usage.total_tokens)
```

사용량은 모델 제공자가 완료된 응답에 사용량을 포함한 경우에만 보고됩니다. 누적 값에는 해당 `RealtimeSession`이 수신한 응답이 포함되며, 세션 간 합계가 아닙니다.

### 인터럽션(중단 처리) 및 재생 추적

사용자가 어시스턴트의 응답을 중단하면 세션은 `audio_interrupted`를 내보내고 서버 측 대화가 사용자가 실제로 들은 내용과 일치하도록 기록을 업데이트합니다.

지연 시간이 짧은 로컬 재생에서는 기본 재생 추적기로 충분한 경우가 많습니다. 원격 또는 지연 재생 시나리오, 특히 전화 통신에서는 생성된 모든 오디오가 이미 재생되었다고 가정하는 대신 실제 재생 진행률을 기준으로 인터럽션(중단 처리) 시점의 잘라내기를 수행하도록 [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker]를 사용하세요.

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py)의 Twilio 코드 예제에서 이 패턴을 확인할 수 있습니다.

## 도구, 승인, 핸드오프 및 가드레일

### 함수 도구

Realtime agents는 라이브 대화 중 함수 도구를 지원합니다.

```python
from agents import function_tool


@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"The weather in {city} is sunny, 72F."


agent = RealtimeAgent(
    name="Assistant",
    instructions="You can answer weather questions.",
    tools=[get_weather],
)
```

### 도구 승인

함수 도구는 실행 전에 사람의 승인을 요구할 수 있습니다. 이 경우 세션은 `tool_approval_required`를 내보내고 `approve_tool_call()` 또는 `reject_tool_call()`을 호출할 때까지 도구 실행을 일시 중지합니다.

도구에 입력 가드레일도 있는 경우 해당 가드레일은 승인 후 실행 직전에 작동합니다. 승인 이벤트가 발생하기 전에 가드레일을 실행하려면 `RealtimeRunner(..., config={"tool_execution": {"pre_approval_tool_input_guardrails": True}})`로 러너를 생성하세요. 이 사전 승인 검사를 통과한 호출도 승인 후 실행 전에 다시 검사됩니다.

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

구체적인 서버 측 승인 루프는 [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)를 참조하세요. 휴먼인더루프 (HITL) 문서의 [휴먼인더루프 (HITL)](../human_in_the_loop.md)에서도 이 흐름을 안내합니다.

### 핸드오프

실시간 핸드오프를 사용하면 한 에이전트가 라이브 대화를 다른 전문 에이전트에게 전달할 수 있습니다.

```python
from agents.realtime import RealtimeAgent, realtime_handoff

billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing issues.",
)

main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="Triage the request and hand off when needed.",
    handoffs=[
        realtime_handoff(
            billing_agent,
            tool_description_override="Transfer to billing support",
        )
    ],
)
```

별도 설정이 없는 `RealtimeAgent` 핸드오프는 자동으로 래핑되며, `realtime_handoff(...)`를 사용하면 이름, 설명, 유효성 검사, 콜백 및 가용성을 사용자 지정할 수 있습니다. 실시간 핸드오프는 일반 핸드오프의 `input_filter`를 지원하지 **않습니다**.

### 가드레일

Realtime agents는 에이전트 응답에 대한 출력 가드레일과 함수 도구 호출에 대한 입력 가드레일을 지원합니다. 출력 가드레일은 모든 부분 토큰이 아니라 디바운스된 트랜스크립트 누적 내용에 대해 실행되며, 예외를 발생시키는 대신 `guardrail_tripped`를 내보냅니다.

```python
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail


def sensitive_data_check(context, agent, output):
    return GuardrailFunctionOutput(
        tripwire_triggered="password" in output,
        output_info=None,
    )


agent = RealtimeAgent(
    name="Assistant",
    instructions="...",
    output_guardrails=[OutputGuardrail(guardrail_function=sensitive_data_check)],
)
```

실시간 출력 가드레일이 작동하면 세션은 활성 응답을 중단하고, `response.cancel`을 강제로 실행하고, `guardrail_tripped`를 내보내며, 모델이 대체 응답을 생성할 수 있도록 작동한 가드레일의 이름이 포함된 후속 사용자 메시지를 전송합니다. 가드레일은 디바운스된 트랜스크립트 텍스트에 대해 실행되고 트립와이어가 작동할 때 일부 오디오가 이미 버퍼링되어 있을 수 있으므로, 오디오 플레이어는 계속 `audio_interrupted`를 수신하고 로컬 재생을 즉시 중지해야 합니다.

## SIP 및 전화 통신

Python SDK는 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel]을 통한 일급 SIP 연결 흐름을 제공합니다.

Realtime Calls API를 통해 전화가 수신되고 생성된 `call_id`에 에이전트 세션을 연결하려는 경우 사용하세요.

```python
from agents.realtime import RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(starting_agent=agent, model=OpenAIRealtimeSIPModel())

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
    }
) as session:
    async for event in session:
        ...
```

먼저 전화를 수락해야 하며 수락 페이로드가 에이전트에서 파생된 세션 구성과 일치하도록 하려면 `OpenAIRealtimeSIPModel.build_initial_session_payload(...)`를 사용하세요. 전체 흐름은 [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)에 나와 있습니다.

## 저수준 접근 및 사용자 지정 엔드포인트

`session.model`을 통해 기본 전송 객체에 접근할 수 있습니다.

다음과 같은 경우에 사용하세요.

-   `session.model.add_listener(...)`를 통한 사용자 지정 리스너
-   `response.create` 또는 `session.update` 같은 원문 클라이언트 이벤트
-   `model_config`를 통한 사용자 지정 `url`, `headers` 또는 `api_key` 처리
-   기존 실시간 호출에 대한 `call_id` 연결

`RealtimeModelConfig`는 다음을 지원합니다.

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

이 저장소에서 제공하는 `call_id` 코드 예제는 SIP를 사용합니다. 더 광범위한 Realtime API에서도 일부 서버 측 제어 흐름에 `call_id`를 사용하지만, 여기서는 해당 흐름을 Python 코드 예제로 제공하지 않습니다.

Azure OpenAI에 연결할 때는 GA Realtime 엔드포인트 URL과 명시적인 헤더를 전달하세요. 예를 들면 다음과 같습니다.

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

토큰 기반 인증에는 `headers`에서 전달자 토큰을 사용하세요.

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers`를 전달하면 SDK가 `Authorization`을 자동으로 추가하지 않습니다. Realtime agents에서 레거시 베타 경로(`/openai/realtime?api-version=...`)를 사용하지 마세요.

## 추가 자료

-   [실시간 전송](transport.md)
-   [빠른 시작](quickstart.md)
-   [OpenAI Realtime 대화](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime 서버 측 제어](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)