---
search:
  exclude: true
---
# 가이드

이 가이드는 OpenAI Agents SDK 의 실시간 기능을 사용해 음성 지원 AI 에이전트를 구축하는 방법을 심층적으로 다룹니다

!!! warning "Beta feature"
실시간 에이전트는 베타입니다. 구현을 개선하는 과정에서 일부 호환성이 깨지는 변경이 있을 수 있습니다.

## 개요

실시간 에이전트는 오디오와 텍스트 입력을 실시간으로 처리하고 실시간 오디오로 응답하는 대화 흐름을 지원합니다. OpenAI 의 Realtime API 와 영구 연결을 유지하여 낮은 지연으로 자연스러운 음성 대화를 가능하게 하며, 인터럽션(중단 처리)도 우아하게 처리할 수 있습니다.

## 아키텍처

### 핵심 구성 요소

실시간 시스템은 다음과 같은 주요 구성 요소로 이루어져 있습니다:

-   **RealtimeAgent**: instructions, tools, handoffs 로 구성된 에이전트
-   **RealtimeRunner**: 구성을 관리합니다. `runner.run()` 을 호출해 세션을 얻을 수 있습니다.
-   **RealtimeSession**: 단일 상호작용 세션입니다. 일반적으로 사용자가 대화를 시작할 때마다 하나를 만들고, 대화가 끝날 때까지 유지합니다.
-   **RealtimeModel**: 기반 모델 인터페이스(일반적으로 OpenAI 의 WebSocket 구현)

### 세션 흐름

일반적인 실시간 세션은 다음 흐름을 따릅니다:

1. instructions, tools, handoffs 로 **RealtimeAgent(들)을 생성**합니다.
2. 에이전트와 구성 옵션으로 **RealtimeRunner 를 설정**합니다.
3. `await runner.run()` 으로 **세션을 시작**하면 RealtimeSession 이 반환됩니다.
4. `send_audio()` 또는 `send_message()` 를 사용해 세션에 **오디오 또는 텍스트 메시지를 전송**합니다.
5. 세션을 순회하며 **이벤트를 수신**합니다. 이벤트에는 오디오 출력, 전사(transcript), 도구 호출, 핸드오프, 오류가 포함됩니다.
6. 사용자가 에이전트 발화 중에 말하는 **인터럽션(중단 처리)을 처리**합니다. 이 경우 현재 오디오 생성이 자동으로 중지됩니다.

세션은 대화 기록을 유지하고 실시간 모델과의 영구 연결을 관리합니다.

## 에이전트 구성

RealtimeAgent 는 일반 Agent 클래스와 유사하게 동작하지만 몇 가지 중요한 차이가 있습니다. 전체 API 세부 사항은 [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API 레퍼런스를 참고하세요.

일반 에이전트와의 주요 차이점:

-   모델 선택은 에이전트 수준이 아니라 세션 수준에서 구성됩니다.
-   structured output 지원이 없습니다(`outputType` 을 지원하지 않음).
-   음성은 에이전트별로 구성할 수 있지만, 첫 번째 에이전트가 말한 이후에는 변경할 수 없습니다.
-   tools, handoffs, instructions 같은 다른 기능은 동일하게 동작합니다.

## 세션 구성

### 모델 설정

세션 구성은 기반 실시간 모델의 동작을 제어할 수 있게 합니다. 모델 이름(예: `gpt-realtime`), 음성 선택(alloy, echo, fable, onyx, nova, shimmer), 지원 모달리티(text 및/또는 audio)를 설정할 수 있습니다. 오디오 형식은 입력과 출력 모두에 대해 설정할 수 있으며, 기본값은 PCM16 입니다.

### 오디오 구성

오디오 설정은 세션이 음성 입력과 출력을 처리하는 방식을 제어합니다. Whisper 같은 모델을 사용한 입력 오디오 전사를 구성하고, 언어 선호를 설정하며, 도메인 특화 용어의 정확도를 높이기 위해 전사 프롬프트를 제공할 수 있습니다. 턴 감지 설정은 에이전트가 언제 응답을 시작하고 멈출지를 제어하며, 음성 활동 감지 임계값, 무음 지속 시간, 감지된 음성 전후 패딩 옵션을 포함합니다.

## 도구 및 함수

### 도구 추가

일반 에이전트와 마찬가지로, 실시간 에이전트는 대화 중 실행되는 함수 도구를 지원합니다:

```python
from agents import function_tool

@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    # Your weather API logic here
    return f"The weather in {city} is sunny, 72°F"

@function_tool
def book_appointment(date: str, time: str, service: str) -> str:
    """Book an appointment."""
    # Your booking logic here
    return f"Appointment booked for {service} on {date} at {time}"

agent = RealtimeAgent(
    name="Assistant",
    instructions="You can help with weather and appointments.",
    tools=[get_weather, book_appointment],
)
```

## 핸드오프

### 핸드오프 생성

핸드오프는 대화를 전문 에이전트 간에 전환할 수 있게 합니다.

```python
from agents.realtime import realtime_handoff

# Specialized agents
billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing and payment issues.",
)

technical_agent = RealtimeAgent(
    name="Technical Support",
    instructions="You handle technical troubleshooting.",
)

# Main agent with handoffs
main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="You are the main customer service agent. Hand off to specialists when needed.",
    handoffs=[
        realtime_handoff(billing_agent, tool_description="Transfer to billing support"),
        realtime_handoff(technical_agent, tool_description="Transfer to technical support"),
    ]
)
```

## 이벤트 처리

세션은 session 객체를 순회하여 수신할 수 있는 이벤트를 스트리밍합니다. 이벤트에는 오디오 출력 청크, 전사 결과, 도구 실행 시작과 종료, 에이전트 핸드오프, 오류가 포함됩니다. 처리해야 할 주요 이벤트는 다음과 같습니다:

-   **audio**: 에이전트 응답의 원문 오디오 데이터
-   **audio_end**: 에이전트 발화 종료
-   **audio_interrupted**: 사용자가 에이전트를 중단함
-   **tool_start/tool_end**: 도구 실행 라이프사이클
-   **handoff**: 에이전트 핸드오프 발생
-   **error**: 처리 중 오류 발생

전체 이벤트 세부 사항은 [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] 를 참고하세요.

## 가드레일

실시간 에이전트는 출력 가드레일만 지원합니다. 이 가드레일은 실시간 생성 중 성능 문제를 피하기 위해 디바운스 처리되며, 매 단어마다가 아니라 주기적으로 실행됩니다. 기본 디바운스 길이는 100 자이며, 구성으로 변경할 수 있습니다.

가드레일은 `RealtimeAgent` 에 직접 연결하거나 세션의 `run_config` 를 통해 제공할 수 있습니다. 두 소스의 가드레일은 함께 실행됩니다.

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

가드레일이 트리거되면 `guardrail_tripped` 이벤트를 생성하며, 에이전트의 현재 응답을 인터럽트할 수 있습니다. 디바운스 동작은 안전성과 실시간 성능 요구 사항 사이의 균형을 맞추는 데 도움이 됩니다. 텍스트 에이전트와 달리, 실시간 에이전트는 가드레일이 트리거되어도 Exception 을 발생시키지 **않습니다**.

## 오디오 처리

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] 를 사용해 세션에 오디오를 보내거나, [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] 로 텍스트를 보낼 수 있습니다.

오디오 출력의 경우 `audio` 이벤트를 수신하고, 선호하는 오디오 라이브러리로 오디오 데이터를 재생하세요. 사용자가 에이전트를 중단할 때 즉시 재생을 멈추고 대기 중인 오디오를 비우기 위해 `audio_interrupted` 이벤트도 반드시 수신해야 합니다.

## SIP 통합

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 를 통해 들어오는 전화 통화에 실시간 에이전트를 연결할 수 있습니다. SDK 는 동일한 에이전트 흐름을 재사용하면서 SIP 로 미디어를 협상하는 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] 을 제공합니다.

사용하려면 runner 에 모델 인스턴스를 전달하고, 세션을 시작할 때 SIP `call_id` 를 제공하세요. call ID 는 수신 통화를 알리는 웹훅이 전달합니다.

```python
from agents.realtime import RealtimeAgent, RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(
    starting_agent=agent,
    model=OpenAIRealtimeSIPModel(),
)

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
        "initial_model_settings": {
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        },
    },
) as session:
    async for event in session:
        ...
```

발신자가 전화를 끊으면 SIP 세션이 종료되고 실시간 연결이 자동으로 닫힙니다. 전체 전화 예제는 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) 를 참고하세요.

## 모델 직접 접근

기본 모델에 접근하여 커스텀 리스너를 추가하거나 고급 작업을 수행할 수 있습니다:

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

이를 통해 연결을 더 낮은 수준에서 제어해야 하는 고급 사용 사례를 위해 [`RealtimeModel`][agents.realtime.model.RealtimeModel] 인터페이스에 직접 접근할 수 있습니다.

## 예제

완전한 동작 예제는 UI 구성 요소가 있는 데모와 없는 데모를 포함한 [examples/realtime directory](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) 를 확인하세요.

## Azure OpenAI 엔드포인트 형식

Azure OpenAI 에 연결할 때는 GA Realtime 엔드포인트 형식을 사용하고 `model_config` 의 headers 를 통해 자격 증명을 전달하세요:

```python
model_config = {
    "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
    "headers": {"api-key": "<your-azure-api-key>"},
}
```

토큰 기반 인증의 경우 `headers` 에 `{"authorization": f"Bearer {token}"}` 를 사용하세요.