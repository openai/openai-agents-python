---
search:
  exclude: true
---
# 실시간 에이전트 가이드

이 가이드는 OpenAI Agents SDK의 실시간 레이어가 OpenAI Realtime API에 어떻게 대응되는지, 그리고 Python SDK가 그 위에 어떤 추가 동작을 더하는지 설명합니다.

!!! warning "베타 기능"

    실시간 에이전트는 베타입니다. 구현을 개선하는 과정에서 일부 호환성이 깨지는 변경이 있을 수 있습니다.

!!! note "시작점"

    기본 Python 경로를 원한다면 먼저 [빠른 시작](quickstart.md)을 읽어보세요. 앱에서 서버 측 WebSocket 또는 SIP를 사용해야 할지 결정하는 중이라면 [실시간 전송](transport.md)을 읽어보세요. 브라우저 WebRTC 전송은 Python SDK의 일부가 아닙니다.

## 개요

실시간 에이전트는 Realtime API에 오래 유지되는 연결을 열어 두어, 모델이 텍스트와 오디오를 점진적으로 처리하고, 오디오 출력을 스트리밍하고, 도구를 호출하고, 매 턴마다 새 요청을 다시 시작하지 않고도 인터럽션(중단 처리)을 처리할 수 있게 합니다.

주요 SDK 구성 요소는 다음과 같습니다.

-   **RealtimeAgent**: 한 명의 실시간 전문가를 위한 instructions, tools, 출력 가드레일 및 핸드오프
-   **RealtimeRunner**: 시작 에이전트를 실시간 전송에 연결하는 세션 팩토리
-   **RealtimeSession**: 입력을 보내고, 이벤트를 수신하고, 기록을 추적하고, 도구를 실행하는 라이브 세션
-   **RealtimeModel**: 전송 추상화입니다. 기본값은 OpenAI의 서버 측 WebSocket 구현입니다.

## 세션 수명 주기

일반적인 실시간 세션은 다음과 같습니다.

1. 하나 이상의 `RealtimeAgent`를 만듭니다.
2. 시작 에이전트로 `RealtimeRunner`를 만듭니다.
3. `await runner.run()`을 호출해 `RealtimeSession`을 가져옵니다.
4. `async with session:` 또는 `await session.enter()`로 세션에 진입합니다.
5. `send_message()` 또는 `send_audio()`로 사용자 입력을 보냅니다.
6. 대화가 끝날 때까지 세션 이벤트를 순회합니다.

텍스트 전용 실행과 달리, `runner.run()`은 최종 결과를 즉시 생성하지 않습니다. 대신 로컬 기록, 백그라운드 도구 실행, 가드레일 상태, 활성 에이전트 구성을 전송 레이어와 동기화해 유지하는 라이브 세션 객체를 반환합니다.

기본적으로 `RealtimeRunner`는 `OpenAIRealtimeWebSocketModel`을 사용하므로 기본 Python 경로는 Realtime API에 대한 서버 측 WebSocket 연결입니다. 다른 `RealtimeModel`을 전달하더라도 동일한 세션 수명 주기와 에이전트 기능은 그대로 적용되며, 연결 방식만 달라질 수 있습니다.

## 에이전트 및 세션 구성

`RealtimeAgent`는 일반 `Agent` 타입보다 의도적으로 범위가 더 좁습니다.

-   모델 선택은 에이전트별이 아니라 세션 수준에서 구성됩니다.
-   Structured outputs는 지원되지 않습니다.
-   음성은 구성할 수 있지만, 세션이 이미 음성 오디오를 생성한 후에는 변경할 수 없습니다.
-   instructions, 함수 도구, 핸드오프, 훅, 출력 가드레일은 모두 계속 작동합니다.

`RealtimeSessionModelSettings`는 최신 중첩 `audio` 구성과 기존 플랫 별칭을 모두 지원합니다. 새 코드에는 중첩 형태를 권장하며, 새 실시간 에이전트에는 `gpt-realtime-2`로 시작하세요.

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime-2",
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

`RealtimeRunner(config=...)`에서 사용할 수 있는 유용한 실행 수준 설정은 다음과 같습니다.

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

전체 타입 지정 인터페이스는 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 및 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]를 참고하세요.

## 입력 및 출력

### 텍스트 및 구조화된 사용자 메시지

일반 텍스트 또는 구조화된 실시간 메시지를 보내려면 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message]를 사용하세요.

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

구조화된 메시지는 실시간 대화에 이미지 입력을 포함하는 주된 방법입니다. [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)의 예제 웹 데모는 이러한 방식으로 `input_image` 메시지를 전달합니다.

### 오디오 입력

원문 오디오 바이트를 스트리밍하려면 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio]를 사용하세요.

```python
await session.send_audio(audio_bytes)
```

서버 측 턴 감지가 비활성화되어 있으면 턴 경계를 표시할 책임은 사용자에게 있습니다. 고수준 편의 메서드는 다음과 같습니다.

```python
await session.send_audio(audio_bytes, commit=True)
```

더 낮은 수준의 제어가 필요하다면 기본 모델 전송을 통해 `input_audio_buffer.commit` 같은 원문 클라이언트 이벤트도 보낼 수 있습니다.

### 수동 응답 제어

`session.send_message()`는 고수준 경로를 사용해 사용자 입력을 보내고 응답을 시작합니다. 원문 오디오 버퍼링은 모든 구성에서 이와 동일한 작업을 자동으로 수행하지는 **않습니다**.

Realtime API 수준에서 수동 턴 제어란 원문 `session.update`로 `turn_detection`을 지운 다음, `input_audio_buffer.commit` 및 `response.create`를 직접 보내는 것을 의미합니다.

턴을 수동으로 관리하고 있다면 모델 전송을 통해 원문 클라이언트 이벤트를 보낼 수 있습니다.

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

-   `turn_detection`이 비활성화되어 있고 모델이 언제 응답해야 할지 직접 결정하려는 경우
-   응답을 트리거하기 전에 사용자 입력을 검사하거나 게이트하려는 경우
-   대역 외 응답을 위한 사용자 지정 프롬프트가 필요한 경우

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)의 SIP 예제는 원문 `response.create`를 사용해 시작 인사를 강제로 생성합니다.

## 이벤트, 기록 및 인터럽션(중단 처리)

`RealtimeSession`은 필요할 때 원문 모델 이벤트를 계속 전달하면서도 더 높은 수준의 SDK 이벤트를 내보냅니다.

유용한 세션 이벤트는 다음과 같습니다.

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

UI 상태에 가장 유용한 이벤트는 일반적으로 `history_added`와 `history_updated`입니다. 이 이벤트들은 사용자 메시지, 어시스턴트 메시지, 도구 호출을 포함해 세션의 로컬 기록을 `RealtimeItem` 객체로 노출합니다.

### 인터럽션(중단 처리) 및 재생 추적

사용자가 어시스턴트를 중단하면 세션은 `audio_interrupted`를 내보내고, 사용자가 실제로 들은 내용과 서버 측 대화가 일치하도록 기록을 업데이트합니다.

지연 시간이 낮은 로컬 재생에서는 기본 재생 추적기로 충분한 경우가 많습니다. 원격 또는 지연된 재생 시나리오, 특히 전화 통신에서는 생성된 모든 오디오를 이미 들었다고 가정하는 대신 실제 재생 진행률을 기준으로 인터럽션(중단 처리) 잘라내기가 수행되도록 [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker]를 사용하세요.

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py)의 Twilio 예제는 이 패턴을 보여줍니다.

## 도구, 승인, 핸드오프 및 가드레일

### 함수 도구

실시간 에이전트는 라이브 대화 중 함수 도구를 지원합니다.

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

함수 도구는 실행 전에 사람의 승인을 요구할 수 있습니다. 이 경우 세션은 `tool_approval_required`를 내보내고, `approve_tool_call()` 또는 `reject_tool_call()`을 호출할 때까지 도구 실행을 일시 중지합니다.

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

구체적인 서버 측 승인 루프는 [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)를 참고하세요. 휴먼인더루프 (HITL) 문서의 [휴먼인더루프 (HITL)](../human_in_the_loop.md) 섹션에서도 이 흐름을 참조합니다.

### 핸드오프

실시간 핸드오프를 사용하면 한 에이전트가 라이브 대화를 다른 전문가에게 넘길 수 있습니다.

```python
from agents.realtime import RealtimeAgent, realtime_handoff

billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing issues.",
)

main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="Triage the request and hand off when needed.",
    handoffs=[realtime_handoff(billing_agent, tool_description="Transfer to billing support")],
)
```

단독 `RealtimeAgent` 핸드오프는 자동으로 래핑되며, `realtime_handoff(...)`를 사용하면 이름, 설명, 검증, 콜백, 사용 가능 여부를 사용자 지정할 수 있습니다. 실시간 핸드오프는 일반 핸드오프의 `input_filter`를 지원하지 **않습니다**.

### 가드레일

실시간 에이전트는 출력 가드레일만 지원합니다. 출력 가드레일은 모든 부분 토큰마다가 아니라 디바운스된 트랜스크립트 누적에 대해 실행되며, 예외를 발생시키는 대신 `guardrail_tripped`를 내보냅니다.

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

실시간 출력 가드레일이 발동하면 세션은 활성 응답을 중단하고,
`response.cancel`을 강제하며, `guardrail_tripped`를 내보내고, 트리거된 가드레일의 이름을 포함한
후속 사용자 메시지를 보내 모델이 대체 응답을 생성할 수 있게 합니다. 가드레일은
디바운스된 트랜스크립트 텍스트에서 실행되고 트립와이어가 작동할 때 일부 오디오가 이미 버퍼링되어 있을 수 있으므로, 오디오 플레이어는 여전히
`audio_interrupted`를 수신하고 로컬 재생을 즉시 중지해야 합니다.

## SIP 및 전화 통신

Python SDK에는 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel]를 통한 일급 SIP 연결(attach) 흐름이 포함되어 있습니다.

Realtime Calls API를 통해 전화가 들어오고 그 결과 `call_id`에 에이전트 세션을 연결하려는 경우 사용하세요.

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

먼저 전화를 수락해야 하고 accept 페이로드가 에이전트에서 파생된 세션 구성과 일치하길 원한다면 `OpenAIRealtimeSIPModel.build_initial_session_payload(...)`를 사용하세요. 전체 흐름은 [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)에 나와 있습니다.

## 저수준 접근 및 사용자 지정 엔드포인트

`session.model`을 통해 기본 전송 객체에 접근할 수 있습니다.

다음이 필요할 때 사용하세요.

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

이 리포지토리에 포함되어 제공되는 `call_id` 예제는 SIP입니다. 더 넓은 Realtime API에서도 일부 서버 측 제어 흐름에 `call_id`를 사용하지만, 여기에는 Python 예제로 패키징되어 있지 않습니다.

Azure OpenAI에 연결할 때는 GA Realtime 엔드포인트 URL과 명시적인 헤더를 전달하세요. 예를 들면 다음과 같습니다.

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

토큰 기반 인증에는 `headers`에 베어러 토큰을 사용하세요.

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers`를 전달하면 SDK가 `Authorization`을 자동으로 추가하지 않습니다. 실시간 에이전트에서는 레거시 베타 경로(`/openai/realtime?api-version=...`)를 피하세요.

## 추가 자료

-   [실시간 전송](transport.md)
-   [빠른 시작](quickstart.md)
-   [OpenAI Realtime 대화](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime 서버 측 제어](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)