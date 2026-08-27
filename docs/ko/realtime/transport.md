---
search:
  exclude: true
---
# 실시간 트랜스포트

이 페이지를 사용하여 실시간 에이전트를 Python 애플리케이션에 통합하는 방법을 결정할 수 있습니다.

!!! note "Python SDK 경계"

    Python SDK에는 브라우저 WebRTC 트랜스포트가 포함되어 있지 **않습니다**. 이 페이지에서는 Python SDK의 트랜스포트 선택지인 서버 측 WebSocket과 SIP 연결 흐름만 다룹니다. 브라우저 WebRTC는 별도의 플랫폼 주제이며, 공식 [WebRTC를 사용하는 Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/) 가이드에 문서화되어 있습니다.

## 선택 가이드 {#decision-guide}

| 목표 | 시작 지점 | 이유 |
| --- | --- | --- |
| 서버에서 관리하는 실시간 앱 구축 | [빠른 시작](quickstart.md) | 기본 Python 경로는 `RealtimeRunner`에서 관리하는 서버 측 WebSocket 세션입니다. |
| 선택할 트랜스포트와 배포 구조 파악 | 이 페이지 | 트랜스포트나 배포 구조를 확정하기 전에 이 페이지를 참조합니다. |
| 에이전트를 전화 또는 SIP 통화에 연결 | [실시간 가이드](guide.md) 및 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | 저장소는 `call_id`에서 구동하는 SIP 연결 흐름을 제공합니다. |

## 서버 측 WebSocket 기반의 기본 Python 경로 {#server-side-websocket-is-the-default-python-path}

사용자 지정 `RealtimeModel`를 전달하지 않으면 `RealtimeRunner`은 `OpenAIRealtimeWebSocketModel`를 사용합니다.

따라서 표준 Python 토폴로지는 다음과 같습니다.

1. Python 서비스에서 `RealtimeRunner`을 생성합니다.
2. `await runner.run()`은 `RealtimeSession`을 반환합니다.
3. `RealtimeSession`을 비동기 컨텍스트 관리자로 진입한 다음 텍스트, 구조화된 메시지 또는 오디오를 전송합니다.
4. `RealtimeSessionEvent` 항목을 소비하고 오디오 또는 트랜스크립트를 애플리케이션에 전달합니다.

핵심 데모 앱, CLI 예제 및 Twilio Media Streams 예제에서 이 토폴로지를 사용합니다.

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

서버에서 오디오 파이프라인, 도구 실행, 승인 흐름 및 기록 처리를 담당하는 경우 이 경로를 사용합니다.

### 저수준 WebSocket 조정 {#low-level-websocket-tuning}

기반 서버 측 WebSocket 연결을 조정해야 할 때 `transport_config`를 `OpenAIRealtimeWebSocketModel`에 전달합니다.

```python
from agents.realtime import (
    OpenAIRealtimeWebSocketModel,
    RealtimeAgent,
    RealtimeRunner,
)

agent = RealtimeAgent(name="Assistant")
model = OpenAIRealtimeWebSocketModel(
    transport_config={
        "ping_interval": 20.0,
        "ping_timeout": 60.0,
        "handshake_timeout": 30.0,
        "max_size": 8 * 1024 * 1024,
    }
)
runner = RealtimeRunner(starting_agent=agent, model=model)
```

지원되는 옵션은 다음과 같습니다.

-   `ping_interval`: 클라이언트의 연결 유지 핑 간격(초)입니다. 핑을 비활성화하려면 `None`로 설정합니다.
-   `ping_timeout`: 연결을 끊기 전에 pong을 기다리는 시간(초)입니다. 하트비트 시간 초과 없이 지연된 pong을 허용하려면 `None`로 설정합니다.
-   `handshake_timeout`: 초기 연결 핸드셰이크를 기다리는 시간(초)입니다.
-   `max_size`: 수신 WebSocket 메시지의 최대 크기(바이트)입니다. SDK 기본값은 `None`이며 수신 메시지 크기를 제한하지 않습니다. 메시지별 메모리 사용량을 제한해야 할 때는 명시적인 제한을 설정합니다.

이 설정은 Realtime API 세션이 아닌 클라이언트 연결을 구성합니다. 엔드포인트, 인증, 통화 연결 및 재생 설정에는 계속해서 `RealtimeModelConfig`을 사용합니다.

## 텔레포니 경로인 SIP 연결 {#sip-attach-is-the-telephony-path}

이 저장소에 문서화된 텔레포니 흐름에서 Python SDK는 `call_id`를 통해 기존 실시간 통화에 연결합니다.

이 토폴로지는 다음과 같습니다.

1. OpenAI가 `realtime.call.incoming`와 같은 웹훅을 서비스로 전송합니다.
2. 서비스가 Realtime Calls API를 통해 통화를 수락합니다.
3. Python 서비스가 `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())`을 시작합니다.
4. 세션이 `model_config={"call_id": ...}`을 사용하여 연결된 다음 다른 실시간 세션과 마찬가지로 이벤트를 처리합니다.

이 토폴로지는 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip)에 나와 있습니다.

더 광범위한 Realtime API에서는 일부 서버 측 제어 패턴에 `call_id`도 사용하지만, 이 저장소에서 제공하는 연결 예제는 SIP입니다.

## SDK 범위 밖의 브라우저 WebRTC {#browser-webrtc-is-outside-this-sdk}

앱의 기본 클라이언트가 Realtime WebRTC를 사용하는 브라우저인 경우 다음 사항에 유의합니다.

-   이 저장소의 Python SDK 문서 범위 밖으로 간주합니다.
-   클라이언트 측 흐름과 이벤트 모델은 공식 [WebRTC를 사용하는 Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/) 및 [실시간 대화](https://developers.openai.com/api/docs/guides/realtime-conversations/) 문서를 참조합니다.
-   브라우저 WebRTC 클라이언트 외에 사이드밴드 서버 연결이 필요하다면 공식 [실시간 서버 측 제어](https://developers.openai.com/api/docs/guides/realtime-server-controls/) 가이드를 참조합니다.
-   이 저장소에서 브라우저 측 `RTCPeerConnection` 추상화 또는 즉시 사용할 수 있는 브라우저 WebRTC 샘플을 제공한다고 기대해서는 안 됩니다.

현재 이 저장소는 브라우저 WebRTC와 Python 사이드밴드를 함께 사용하는 예제도 제공하지 않습니다.

## 사용자 지정 엔드포인트 및 연결 지점 {#custom-endpoints-and-attach-points}

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig]의 트랜스포트 구성 인터페이스를 사용하면 기본 트랜스포트 동작을 사용자 지정할 수 있습니다.

-   `url`: WebSocket 엔드포인트 재정의
-   `headers`: Azure 인증 헤더와 같은 명시적 헤더 제공
-   `api_key`: API 키를 직접 또는 콜백을 통해 전달
-   `call_id`: 기존 실시간 통화에 연결. 이 저장소에 문서화된 예제는 SIP입니다.
-   `playback_tracker`: 인터럽션(중단 처리)을 위해 실제 재생 진행 상황 보고

토폴로지를 선택한 후 자세한 수명 주기와 기능 범위는 [실시간 에이전트 가이드](guide.md)를 참조합니다.