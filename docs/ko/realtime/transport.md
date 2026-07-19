---
search:
  exclude: true
---
# 실시간 전송 방식

이 페이지에서는 실시간 에이전트를 Python 애플리케이션에 통합하는 방법을 결정할 수 있습니다.

!!! note "Python SDK 범위"

    Python SDK에는 브라우저 WebRTC 전송 기능이 **포함되어 있지 않습니다**. 이 페이지에서는 Python SDK의 전송 방식인 서버 측 WebSocket과 SIP 연결 흐름만 다룹니다. 브라우저 WebRTC는 별도의 플랫폼 주제이며, 공식 [WebRTC를 사용하는 Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/) 가이드에 설명되어 있습니다.

## 선택 가이드

| 목표 | 시작점 | 이유 |
| --- | --- | --- |
| 서버에서 관리하는 실시간 앱 구축 | [빠른 시작](quickstart.md) | 기본 Python 경로는 `RealtimeRunner`가 관리하는 서버 측 WebSocket 세션입니다. |
| 선택할 전송 방식과 배포 구조 파악 | 이 페이지 | 전송 방식이나 배포 구조를 확정하기 전에 이 페이지를 참조하세요. |
| 에이전트를 전화 또는 SIP 통화에 연결 | [실시간 가이드](guide.md) 및 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | 저장소에는 `call_id`로 구동되는 SIP 연결 흐름이 포함되어 있습니다. |

## 기본 Python 경로인 서버 측 WebSocket

사용자 지정 `RealtimeModel`을 전달하지 않으면 `RealtimeRunner`는 `OpenAIRealtimeWebSocketModel`을 사용합니다.

즉, 표준 Python 토폴로지는 다음과 같습니다.

1. Python 서비스가 `RealtimeRunner`를 생성합니다.
2. `await runner.run()`이 `RealtimeSession`을 반환합니다.
3. 세션에 진입하여 텍스트, 구조화된 메시지 또는 오디오를 전송합니다.
4. `RealtimeSessionEvent` 항목을 처리하고 오디오 또는 트랜스크립트를 애플리케이션에 전달합니다.

핵심 데모 앱, CLI 예제 및 Twilio Media Streams 예제에서 사용하는 토폴로지는 다음과 같습니다.

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

서버에서 오디오 파이프라인, 도구 실행, 승인 흐름 및 기록 처리를 담당하는 경우 이 경로를 사용하세요.

### 저수준 WebSocket 조정

기반이 되는 서버 측 WebSocket 연결을 조정해야 할 때는 `OpenAIRealtimeWebSocketModel`에 `transport_config`를 전달합니다.

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

-   `ping_interval`: 클라이언트 연결 유지 ping 사이의 시간(초)입니다. ping을 비활성화하려면 `None`으로 설정합니다.
-   `ping_timeout`: 연결을 끊기 전에 pong을 기다리는 시간(초)입니다. 하트비트 시간 초과 없이 지연된 pong을 허용하려면 `None`으로 설정합니다.
-   `handshake_timeout`: 초기 연결 핸드셰이크를 기다리는 시간(초)입니다.
-   `max_size`: 수신 WebSocket 메시지의 최대 크기(바이트)입니다. SDK 기본값은 `None`이며 수신 메시지 크기를 제한하지 않습니다. 메시지별 메모리 사용량을 제한해야 하는 경우 명시적으로 한도를 설정하세요.

이 설정은 Realtime API 세션이 아니라 클라이언트 연결을 구성합니다. 엔드포인트, 인증, 통화 연결 및 재생 설정에는 계속 `RealtimeModelConfig`를 사용하세요.

## 전화 통신을 위한 SIP 연결

이 저장소에 문서화된 전화 통신 흐름에서 Python SDK는 `call_id`를 통해 기존 실시간 통화에 연결됩니다.

이 토폴로지는 다음과 같습니다.

1. OpenAI가 `realtime.call.incoming`과 같은 웹훅을 서비스에 전송합니다.
2. 서비스가 Realtime Calls API를 통해 통화를 수락합니다.
3. Python 서비스가 `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())`를 시작합니다.
4. 세션이 `model_config={"call_id": ...}`로 연결된 후 다른 실시간 세션과 동일하게 이벤트를 처리합니다.

이 토폴로지는 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip)에 나와 있습니다.

더 광범위한 Realtime API에서도 일부 서버 측 제어 패턴에 `call_id`를 사용하지만, 이 저장소에 포함된 연결 예제는 SIP를 사용합니다.

## SDK 범위 밖의 브라우저 WebRTC

앱의 기본 클라이언트가 Realtime WebRTC를 사용하는 브라우저인 경우 다음 사항을 따르세요.

-   이 저장소의 Python SDK 문서 범위 밖으로 간주하세요.
-   클라이언트 측 흐름과 이벤트 모델은 공식 [WebRTC를 사용하는 Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/) 및 [실시간 대화](https://developers.openai.com/api/docs/guides/realtime-conversations/) 문서를 참조하세요.
-   브라우저 WebRTC 클라이언트에 사이드밴드 서버 연결을 추가해야 하는 경우 공식 [실시간 서버 측 제어](https://developers.openai.com/api/docs/guides/realtime-server-controls/) 가이드를 참조하세요.
-   이 저장소에서 브라우저 측 `RTCPeerConnection` 추상화 또는 즉시 사용할 수 있는 브라우저 WebRTC 샘플을 제공할 것으로 기대해서는 안 됩니다.

또한 이 저장소는 현재 브라우저 WebRTC와 Python 사이드밴드를 함께 사용하는 예제를 제공하지 않습니다.

## 사용자 지정 엔드포인트 및 연결 지점

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig]에서 제공하는 전송 설정 인터페이스를 사용하면 기본 경로를 조정할 수 있습니다.

-   `url`: WebSocket 엔드포인트 재정의
-   `headers`: Azure 인증 헤더와 같은 명시적 헤더 제공
-   `api_key`: API 키를 직접 또는 콜백을 통해 전달
-   `call_id`: 기존 실시간 통화에 연결. 이 저장소에 문서화된 예제는 SIP를 사용합니다.
-   `playback_tracker`: 인터럽션(중단 처리)을 위해 실제 재생 진행 상황 보고

토폴로지를 선택한 후의 상세한 수명 주기와 기능 범위는 [실시간 에이전트 가이드](guide.md)를 참조하세요.