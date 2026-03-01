---
search:
  exclude: true
---
# 릴리스 프로세스/변경 로그

이 프로젝트는 `0.Y.Z` 형식을 사용하는, 약간 수정된 시맨틱 버저닝을 따릅니다. 앞의 `0`은 SDK가 아직 빠르게 발전 중임을 의미합니다. 구성 요소 증가는 다음과 같습니다.

## 마이너(`Y`) 버전

베타로 표시되지 않은 모든 공개 인터페이스에 대한 **호환성이 깨지는 변경(breaking change)** 이 있을 때 마이너 버전 `Y`를 올립니다. 예를 들어 `0.0.x`에서 `0.1.x`로 가는 경우에는 호환성이 깨지는 변경이 포함될 수 있습니다.

호환성이 깨지는 변경을 원하지 않는다면, 프로젝트에서 `0.0.x` 버전에 고정(pin)하는 것을 권장합니다.

## 패치(`Z`) 버전

호환성이 깨지지 않는 변경에 대해 `Z`를 올립니다.

-   버그 수정
-   새 기능
-   비공개 인터페이스 변경
-   베타 기능 업데이트

## 호환성이 깨지는 변경 변경 로그

### 0.10.0

이 마이너 릴리스는 호환성이 깨지는 변경을 **도입하지 않지만**, OpenAI Responses 사용자를 위한 중요한 새 기능 영역을 포함합니다. 즉, Responses API에 대한 websocket 전송 지원입니다.

주요 내용:

-   OpenAI Responses 모델에 대한 websocket 전송 지원을 추가했습니다(선택 사항; HTTP는 기본 전송 방식으로 유지)
-   멀티 턴 실행 전반에서 websocket 사용 가능한 provider와 `RunConfig`를 공유해 재사용할 수 있도록 `responses_websocket_session()` 헬퍼 / `ResponsesWebSocketSession`을 추가했습니다
-   스트리밍, 도구, 승인, 후속 턴을 다루는 새로운 websocket 스트리밍 예제(`examples/basic/stream_ws.py`)를 추가했습니다

### 0.9.0

이 버전부터 Python 3.9는 더 이상 지원되지 않습니다. 이 메이저 버전은 3개월 전에 EOL에 도달했습니다. 더 최신 런타임 버전으로 업그레이드해 주세요.

또한 `Agent#as_tool()` 메서드가 반환하는 값의 타입 힌트가 `Tool`에서 `FunctionTool`로 좁혀졌습니다. 이 변경은 일반적으로 호환성 문제를 일으키지 않지만, 코드가 더 넓은 유니온 타입에 의존하고 있다면 일부 조정이 필요할 수 있습니다.

### 0.8.0

이 버전에서는 두 가지 런타임 동작 변경으로 인해 마이그레이션 작업이 필요할 수 있습니다.

- **동기식(synchronous)** Python 호출 가능 객체(callable)를 래핑하는 함수 도구는 이제 이벤트 루프 스레드에서 실행하는 대신 `asyncio.to_thread(...)`를 통해 워커 스레드에서 실행됩니다. 도구 로직이 스레드 로컬 상태나 스레드에 종속적인 리소스에 의존한다면, async 도구 구현으로 마이그레이션하거나 도구 코드에서 스레드 종속성을 명시적으로 처리하세요
- 로컬 MCP 도구 실패 처리 방식이 이제 구성 가능해졌으며, 기본 동작은 전체 실행을 실패시키는 대신 모델에 보이는 오류 출력(model-visible error output)을 반환할 수 있습니다. fail-fast 의미론에 의존한다면 `mcp_config={"failure_error_function": None}`을 설정하세요. 서버 레벨의 `failure_error_function` 값은 에이전트 레벨 설정을 덮어쓰므로, 명시적 핸들러가 있는 각 로컬 MCP 서버마다 `failure_error_function=None`을 설정하세요

### 0.7.0

이 버전에서는 기존 애플리케이션에 영향을 줄 수 있는 몇 가지 동작 변경이 있었습니다.

- 중첩된 핸드오프 기록은 이제 **opt-in**입니다(기본적으로 비활성화). v0.6.x의 기본 중첩 동작에 의존했다면 `RunConfig(nest_handoff_history=True)`를 명시적으로 설정하세요
- `gpt-5.1` / `gpt-5.2`의 기본 `reasoning.effort`가 SDK 기본값으로 설정되던 이전 기본값 `"low"`에서 `"none"`으로 변경되었습니다. 프롬프트나 품질/비용 프로파일이 `"low"`에 의존했다면 `model_settings`에서 명시적으로 설정하세요

### 0.6.0

이 버전부터 기본 핸드오프 기록은 원문 사용자/어시스턴트 턴을 노출하는 대신 하나의 assistant 메시지로 패키징되어, 다운스트림 에이전트가 간결하고 예측 가능한 요약을 받습니다
- 기존의 단일 메시지 핸드오프 대화 기록은 이제 기본적으로 `<CONVERSATION HISTORY>` 블록 앞에 "For context, here is the conversation so far between the user and the previous agent:"로 시작하므로, 다운스트림 에이전트가 명확하게 라벨된 요약을 받습니다

### 0.5.0

이 버전은 눈에 띄는 호환성이 깨지는 변경을 도입하지 않지만, 새 기능과 내부적으로 몇 가지 중요한 업데이트를 포함합니다.

-   `RealtimeRunner`가 [SIP 프로토콜 연결](https://platform.openai.com/docs/guides/realtime-sip)을 처리할 수 있도록 지원을 추가했습니다
-   Python 3.14 호환성을 위해 `Runner#run_sync`의 내부 로직을 크게 개정했습니다

### 0.4.0

이 버전부터 [openai](https://pypi.org/project/openai/) 패키지 v1.x 버전은 더 이상 지원되지 않습니다. 이 SDK와 함께 openai v2.x를 사용해 주세요.

### 0.3.0

이 버전에서 Realtime API 지원은 gpt-realtime 모델과 해당 API 인터페이스(GA 버전)로 마이그레이션됩니다.

### 0.2.0

이 버전에서는 이전에 인자(arg)로 `Agent`를 받던 몇몇 위치가 이제 대신 `AgentBase`를 받습니다. 예를 들어 MCP 서버의 `list_tools()` 호출이 그렇습니다. 이는 순수하게 타이핑 변경이며, 여전히 `Agent` 객체를 받게 됩니다. 업데이트하려면 `Agent`를 `AgentBase`로 바꿔 타입 오류만 수정하면 됩니다.

### 0.1.0

이 버전에서 [`MCPServer.list_tools()`][agents.mcp.server.MCPServer]에 `run_context`와 `agent`라는 두 개의 새 매개변수가 추가되었습니다. `MCPServer`를 서브클래싱하는 모든 클래스에 이 매개변수들을 추가해야 합니다.