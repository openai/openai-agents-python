---
search:
  exclude: true
---
# 실시간 에이전트 가이드

이 가이드에서는 OpenAI Agents SDK의 실시간 계층이 OpenAI Realtime API에 어떻게 매핑되는지와 Python SDK가 여기에 어떤 추가 동작을 제공하는지 설명합니다.

!!! note "여기서 시작"

    기본 Python 경로를 사용하려면 먼저 [빠른 시작](quickstart.md)을 읽어 보세요. 애플리케이션에서 서버 측 WebSocket과 SIP 중 무엇을 사용할지 결정하려면 [실시간 전송](transport.md)을 읽어 보세요. 브라우저 WebRTC 전송은 Python SDK에 포함되지 않습니다.

## 개요 {#overview}

실시간 에이전트는 Realtime API에 장기 연결을 유지하므로 모델이 텍스트와 오디오를 점진적으로 처리하고, 오디오 출력을 스트리밍하고, 도구를 호출하며, 매 턴마다 새로운 요청을 다시 시작하지 않고도 인터럽션(중단 처리)을 처리할 수 있습니다.

주요 SDK 구성 요소는 다음과 같습니다.

-   **RealtimeAgent**: 하나의 실시간 전문가를 위한 instructions, 도구, 출력 가드레일, 핸드오프
-   **RealtimeRunner**: 시작 에이전트를 실시간 전송에 연결하는 세션 팩토리
-   **RealtimeSession**: 입력을 보내고, 이벤트를 수신하고, 기록을 추적하고, 도구를 실행하는 라이브 세션
-   **RealtimeModel**: 전송 추상화입니다. 기본값은 OpenAI의 서버 측 WebSocket 구현입니다.

## 세션 수명 주기 {#session-lifecycle}

일반적인 실시간 세션은 다음과 같습니다.

1. 하나 이상의 `RealtimeAgent`을 생성합니다.
2. 시작 에이전트로 `RealtimeRunner`을 생성합니다.
3. `RealtimeSession`을 가져오려면 `await runner.run()`를 호출합니다.
4. `async with session:` 또는 `await session.enter()`를 사용해 세션에 진입합니다.
5. `send_message()` 또는 `send_audio()`을 사용해 사용자 입력을 보냅니다.
6. 대화가 끝날 때까지 세션 이벤트를 순회합니다.

텍스트 전용 실행과 달리 `runner.run()`은 최종 결과를 즉시 생성하지 않습니다. 대신 로컬 기록, 백그라운드 도구 실행, 가드레일 상태, 활성 에이전트 구성을 전송 계층과 동기화하는 라이브 세션 객체를 반환합니다.

기본적으로 `RealtimeRunner`은 `OpenAIRealtimeWebSocketModel`을 사용하므로 기본 Python 경로는 Realtime API에 대한 서버 측 WebSocket 연결입니다. 다른 `RealtimeModel`을 전달해도 동일한 세션 수명 주기와 에이전트 기능이 적용되며, 연결 메커니즘만 달라질 수 있습니다.

Realtime API 서버가 기본 WebSocket 연결을 정상적으로 종료하면 모델 전송은 `disconnected` [`RealtimeModelConnectionStatusEvent`][agents.realtime.model_events.RealtimeModelConnectionStatusEvent]를 내보낸 다음 [`RealtimeModelEndOfStreamEvent`][agents.realtime.model_events.RealtimeModelEndOfStreamEvent]를 내보냅니다. `RealtimeSession`는 두 이벤트를 모두 `raw_model_event` 내부로 전달하고, 이미 대기열에 있는 이벤트를 모두 처리한 다음 예외를 발생시키지 않고 비동기 순회를 종료합니다. 호출자가 시작한 `session.close()`은 이러한 서버 연결 해제 이벤트를 합성하지 않습니다. 예기치 않은 WebSocket 오류는 정상적인 서버 종료처럼 순회를 끝내는 대신 세션의 예외 경로를 통해 계속 처리됩니다.

## 에이전트 및 세션 구성 {#agent-and-session-configuration}

`RealtimeAgent`은 의도적으로 일반 `Agent` 유형보다 범위가 좁습니다.

-   모델 선택은 에이전트별이 아니라 세션 수준에서 구성합니다.
-   structured outputs은 지원되지 않습니다.
-   음성을 구성할 수 있지만, 세션에서 음성 오디오를 이미 생성한 후에는 변경할 수 없습니다.
-   instructions, 함수 도구, 핸드오프, 훅, 출력 가드레일은 모두 계속 작동합니다.

`RealtimeSessionModelSettings`은 새로운 중첩 `audio` 구성과 이전의 플랫 별칭을 모두 지원합니다. 새 코드에는 중첩 구조를 사용하는 것이 좋으며, 새로운 실시간 에이전트에는 `gpt-realtime-2.1`로 시작하세요.

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

`RealtimeRunner(config=...)`의 유용한 실행 수준 설정은 다음과 같습니다.

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

전체 유형화 인터페이스는 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig]과 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]을 참조하세요.

### 입력 트랜스크립션 설정 {#input-transcription-settings}

입력 트랜스크립션은 `audio.input.transcription`에서 구성합니다. 지연 시간이 짧은 증분 트랜스크립트에는 `gpt-live-transcribe`을 사용하고, 오디오 턴이 커밋된 후 트랜스크립션을 시작해야 하거나 애플리케이션에 감지된 언어 출력이 필요한 경우 WebSocket에서 `gpt-transcribe`를 사용합니다. Agents SDK는 모델별 GA 트랜스크립션 설정을 중첩된 세션 구성으로 전달합니다.

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "audio": {
                "input": {
                    "transcription": {
                        "model": "gpt-live-transcribe",
                        "prompt": "A support call about the OpenAI Agents SDK.",
                        "keywords": ["RunState", "MCPServerManager"],
                        "languages": ["en", "ja"],
                    },
                    "turn_detection": None,
                }
            }
        }
    },
)
```

`gpt-live-transcribe`의 경우 `prompt`은 자유 형식의 녹음 컨텍스트를 제공하고, `keywords`은 오디오에 포함될 수 있는 리터럴 용어를 나열하며, `languages`은 예상 입력 언어를 나열합니다. 이 모델은 단수형 `language` 대신 복수형 `languages`를 사용합니다. 두 필드를 모두 보내지 마세요.

이 SDK에 고정된 OpenAI 클라이언트 버전은 `delay`을 `gpt-realtime-whisper`에서만 지원합니다. 해당 모델의 지연 시간과 정확도 간 절충은 다음과 같이 구성합니다.

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "audio": {
                "input": {
                    "transcription": {
                        "model": "gpt-realtime-whisper",
                        "delay": "low",
                    },
                    "turn_detection": None,
                }
            }
        }
    },
)
```

`delay` 설정에는 `minimal`, `low`, `medium`, `high` 또는 `xhigh`을 사용할 수 있습니다. 값이 낮으면 부분 텍스트가 더 일찍 생성될 수 있고, 값이 높으면 트랜스크립션 모델에 더 많은 오디오 컨텍스트를 제공하여 인식 정확도를 높일 수 있습니다. 각 수준의 타이밍이 고정되어 있다고 가정하지 말고 대표적인 오디오로 벤치마크하세요.

WebSocket 기반 Realtime 세션에서 `gpt-transcribe`은 커밋된 오디오 턴 이후에 트랜스크립션을 시작해야 하거나 애플리케이션에 감지된 언어 출력이 필요한 경우에만 사용합니다. 모델은 이전에 트랜스크립션된 턴을 컨텍스트로 자동 사용합니다. `gpt-transcribe` 완료 이벤트는 감지된 언어를 `languages` 출력 필드에 보고합니다. 이 출력 필드는 위에 표시된 예상 언어 입력 `gpt-live-transcribe`와 다릅니다.

`audio.input.turn_detection`을 `None`로 설정하면 자동 턴 감지가 비활성화됩니다. 그러면 애플리케이션이 [수동 응답 제어](#manual-response-control)에 설명된 대로 오디오 턴을 커밋하고 응답 생성을 제어해야 합니다. 모델 동작, 유효성 검사 규칙, 지연 시간 지침은 OpenAI API의 [Realtime 트랜스크립션 가이드](https://developers.openai.com/api/docs/guides/realtime-transcription)를 참조하세요.

## 입력 및 출력 {#inputs-and-outputs}

### 텍스트 및 구조화된 사용자 메시지 {#text-and-structured-user-messages}

일반 텍스트 또는 구조화된 Realtime 메시지에는 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message]를 사용합니다.

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

구조화된 메시지는 Realtime 대화에 이미지 입력을 포함하는 주요 방법입니다. [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)의 웹 데모 예제는 이 방식으로 `input_image` 메시지를 전달합니다.

### 오디오 입력 {#audio-input}

가공되지 않은 오디오 바이트를 스트리밍하려면 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio]을 사용합니다.

```python
await session.send_audio(audio_bytes)
```

서버 측 턴 감지가 비활성화된 경우 턴 경계를 직접 표시해야 합니다. 상위 수준의 편의 기능은 다음과 같습니다.

```python
await session.send_audio(audio_bytes, commit=True)
```

더 낮은 수준의 제어가 필요한 경우 기본 모델 전송을 통해 `input_audio_buffer.commit`과 같은 Realtime API 클라이언트 이벤트를 직접 보낼 수도 있습니다.

### 수동 응답 제어 {#manual-response-control}

`session.send_message()`은 상위 수준 경로를 사용해 사용자 입력을 보내고 응답을 시작합니다. 일부 구성에서는 가공되지 않은 오디오 버퍼링이 동일한 동작을 자동으로 수행하지 **않습니다**.

Realtime API 수준에서 수동 턴 제어는 `turn_detection`를 `null`으로 설정하는 `session.update` 이벤트를 보낸 다음, `input_audio_buffer.commit`와 `response.create`를 직접 보내는 것을 의미합니다.

턴을 수동으로 관리하는 경우 모델 전송을 통해 가공되지 않은 클라이언트 이벤트를 보낼 수 있습니다.

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
-   응답을 트리거하기 전에 사용자 입력을 검사하거나 통제하려는 경우
-   대역 외 응답에 사용자 지정 프롬프트가 필요한 경우

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)의 SIP 예제는 시작 인사말을 강제로 생성하기 위해 가공되지 않은 `response.create`을 사용합니다.

## 이벤트, 기록 및 인터럽션(중단 처리) {#events-history-and-interruptions}

`RealtimeSession`은 상위 수준 SDK 이벤트를 내보내는 동시에 필요할 때 가공되지 않은 모델 이벤트도 계속 전달합니다.

중요한 세션 이벤트는 다음과 같습니다.

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

UI 상태에 가장 유용한 이벤트는 일반적으로 `history_added`와 `history_updated`입니다. 이러한 이벤트는 사용자 메시지, 어시스턴트 메시지, 도구 호출을 포함한 세션의 로컬 기록을 `RealtimeItem` 객체로 제공합니다.

### 사용량 집계 {#usage-accounting}

완료된 모델 응답에 사용량이 포함된 경우 SDK의 OpenAI `RealtimeModel` 전송은 `raw_model_event` 내부에서 [`RealtimeModelUsageEvent`][agents.realtime.model_events.RealtimeModelUsageEvent]를 내보냅니다. `usage` 필드에는 해당 응답의 토큰 수가 포함되며, `input_tokens_details`와 `output_tokens_details`은 선택적 모달리티별 내역을 제공합니다.

또한 세션은 각 응답의 사용량을 공유 [`RunContextWrapper.usage`][agents.run_context.RunContextWrapper.usage]에 추가합니다. 라이브 세션의 누적 사용량을 확인하려면 `agent_end`과 같은 후속 상위 수준 이벤트의 `event.info.context.usage`에서 읽습니다.

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

사용량은 모델 제공자가 완료된 응답에 사용량을 포함한 경우에만 보고됩니다. 누적 값은 해당 `RealtimeSession`이 수신한 응답에 적용되며, 여러 세션에 걸친 합계가 아닙니다.

### 인터럽션(중단 처리) 및 재생 추적 {#interruptions-and-playback-tracking}

사용자가 어시스턴트를 중단하면 세션은 `audio_interrupted`을 내보내고, 서버 측 대화가 사용자가 실제로 들은 내용과 일치하도록 기록을 업데이트합니다.

지연 시간이 짧은 로컬 재생에서는 기본 재생 추적기로 충분한 경우가 많습니다. 원격 또는 지연 재생 시나리오, 특히 전화 통신에서는 생성된 오디오를 모두 이미 들었다고 가정하지 않고 실제 재생 위치에서 중단된 응답을 잘라내도록 [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker]를 사용합니다.

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py)의 Twilio 예제에서 이 패턴을 확인할 수 있습니다.

## 도구, 승인, 핸드오프 및 가드레일 {#tools-approvals-handoffs-and-guardrails}

### 함수 도구 {#function-tools}

실시간 에이전트는 라이브 대화 중 함수 도구를 지원합니다.

```python
from agents.decorators import tool


@tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"The weather in {city} is sunny, 72F."


agent = RealtimeAgent(
    name="Assistant",
    instructions="You can answer weather questions.",
    tools=[get_weather],
)
```

### 도구 승인 {#tool-approvals}

함수 도구를 실행하기 전에 사람의 승인을 요구하도록 설정할 수 있습니다. 이 경우 세션은 `tool_approval_required`을 내보내고 `approve_tool_call()` 또는 `reject_tool_call()`을 호출할 때까지 도구 실행을 일시 중지합니다.

도구에 입력 가드레일도 있는 경우, 승인 후 실행 직전에 해당 가드레일이 실행됩니다. 승인 이벤트가 발생하기 전에 가드레일을 실행하려면 `RealtimeRunner(..., config={"tool_execution": {"pre_approval_tool_input_guardrails": True}})`로 러너를 생성합니다. 이 사전 승인 검사를 통과한 호출도 승인 후 실행 전에 다시 검사됩니다.

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

구체적인 서버 측 승인 루프는 [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)를 참조하세요. 휴먼인더루프 문서의 [휴먼인더루프 (HITL)](../human_in_the_loop.md)에서도 이 흐름을 안내합니다.

### 핸드오프 {#handoffs}

Realtime 핸드오프를 사용하면 한 에이전트가 라이브 대화를 다른 전문가에게 전달할 수 있습니다.

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

핸드오프로 직접 사용되는 `RealtimeAgent` 객체는 자동으로 래핑되며, `realtime_handoff(...)`을 사용하면 이름, 설명, 유효성 검사, 콜백, 가용성을 사용자 지정할 수 있습니다. Realtime 핸드오프는 일반 핸드오프의 `input_filter`을 지원하지 **않습니다**.

### 가드레일 {#guardrails}

실시간 에이전트는 에이전트 응답에 대한 출력 가드레일과 함수 도구 호출에 대한 입력 가드레일을 지원합니다. 출력 가드레일 검사는 디바운스됩니다. 각 검사는 모든 부분 델타마다 실행되는 대신 누적된 출력 텍스트 및 오디오 트랜스크립트 델타를 대상으로 실행되며, 예외를 발생시키는 대신 `guardrail_tripped`를 내보냅니다.

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

실시간 출력 가드레일이 오디오 트랜스크립트에서 트리거되면 세션은 활성 응답을 중단하고 `response.cancel`을 강제 적용하며 `guardrail_tripped`을 내보낸 다음, 트리거된 가드레일의 이름을 포함한 후속 사용자 메시지를 보내 모델이 대체 응답을 생성하도록 합니다. 트립와이어가 작동할 때 일부 오디오가 이미 버퍼링되어 있을 수 있으므로 오디오 플레이어는 계속 `audio_interrupted`를 수신하고 로컬 재생을 즉시 중지해야 합니다. 기본 제공 OpenAI Realtime 전송을 사용할 때 가드레일 검사가 검사 대상 응답이 끝난 후 완료되면, 세션은 해당 응답의 버퍼링된 재생만 중단하고 이후에 시작된 응답은 취소하지 않습니다. 텍스트 전용 출력의 경우 세션은 대신 응답 범위가 지정된 `response.cancel`을 보냅니다. 중지할 오디오 재생이 없으므로 `audio_interrupted`는 내보내지 않습니다. 기본 제공 OpenAI Realtime 모델을 사용할 때 텍스트 전용 경로에서도 동일한 `guardrail_tripped` 이벤트와 후속 사용자 메시지가 내보내집니다.

사용자 지정 `RealtimeModel` 전송은 동일한 소스 범위 오디오 중단 동작을 제공하기 위해 `RealtimeModelSendInterrupt.response_id`과 `playback_only`을 준수해야 합니다. 또한 텍스트 전용 출력 경로의 복구 메시지를 지원하려면 `RealtimeModel.send_event_if()`를 재정의해야 합니다. 구현은 전송에서 실제로 이벤트를 커밋하는 경계에서 제공된 조건을 다시 검사하거나, 조건 검사와 이벤트 커밋을 함께 직렬화해야 합니다. 기본 구현은 복구 메시지를 안전하게 건너뜁니다. 조건을 한 번 검사한 뒤 이벤트를 별도로 보내면 해당 검사와 이벤트 커밋 사이에 다른 응답이 시작될 수 있기 때문입니다. 응답 취소와 `guardrail_tripped` 이벤트는 계속 발생합니다.

## SIP 및 전화 통신 {#sip-and-telephony}

Python SDK는 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel]을 통해 일급 SIP 연결 흐름을 제공합니다.

Realtime Calls API를 통해 전화가 수신되고, 그 결과 생성된 `call_id`에 에이전트 세션을 연결하려는 경우 사용합니다.

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

먼저 전화를 수락해야 하고 수락 페이로드를 에이전트에서 파생된 세션 구성과 일치시키려면 `OpenAIRealtimeSIPModel.build_initial_session_payload(...)`을 사용합니다. 전체 흐름은 [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)에 나와 있습니다.

## 저수준 접근 및 사용자 지정 엔드포인트 {#low-level-access-and-custom-endpoints}

`session.model`를 통해 기본 전송 객체에 접근할 수 있습니다.

다음과 같은 경우에 사용합니다.

-   `session.model.add_listener(...)`을 통한 사용자 지정 리스너
-   `response.create` 또는 `session.update`과 같은 가공되지 않은 클라이언트 이벤트
-   `model_config`를 통한 사용자 지정 `url`, `headers` 또는 `api_key` 처리
-   기존 Realtime 통화에 대한 `call_id` 연결

`RealtimeModelConfig`는 다음을 지원합니다.

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

이 저장소에 포함된 `call_id` 예제는 SIP입니다. 더 광범위한 Realtime API도 일부 서버 측 제어 흐름에 `call_id`를 사용하지만, 여기에서는 Python 예제로 패키징되어 있지 않습니다.

Azure OpenAI에 연결할 때는 GA Realtime 엔드포인트 URL과 명시적인 헤더를 전달합니다. 예시는 다음과 같습니다.

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

토큰 기반 인증에는 `headers`에 전달자 토큰을 사용합니다.

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers`를 전달하면 SDK가 `Authorization`를 자동으로 추가하지 않습니다. 실시간 에이전트에서 기존 베타 경로(`/openai/realtime?api-version=...`)를 사용하지 마세요.

## 추가 자료 {#further-reading}

-   [실시간 전송](transport.md)
-   [빠른 시작](quickstart.md)
-   [OpenAI Realtime 대화](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime 서버 측 제어](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)