---
search:
  exclude: true
---
# 릴리스 프로세스/변경 로그

이 프로젝트는 `0.Y.Z` 형식을 사용하는 시맨틱 버저닝의 약간 수정된 버전을 따릅니다. 앞의 `0`은 SDK가 여전히 빠르게 발전 중임을 나타냅니다. 각 구성 요소는 다음과 같이 증가합니다

## 마이너 (`Y`) 버전

베타로 표시되지 않은 공개 인터페이스에 **호환성이 깨지는 변경 사항**이 있을 때 마이너 버전 `Y`를 올립니다. 예를 들어 `0.0.x`에서 `0.1.x`로 이동할 때 호환성이 깨지는 변경이 포함될 수 있습니다.

호환성이 깨지는 변경을 원하지 않는다면, 프로젝트에서 `0.0.x` 버전에 고정(pin)하는 것을 권장합니다.

## 패치 (`Z`) 버전

호환성이 깨지지 않는 변경에는 `Z`를 올립니다

- 버그 수정
- 새 기능
- 비공개 인터페이스 변경
- 베타 기능 업데이트

## 호환성이 깨지는 변경 로그

### 0.13.0

이 마이너 릴리스는 **호환성이 깨지는 변경**을 도입하지는 않지만, 주목할 만한 Realtime 기본값 업데이트와 새로운 MCP 기능, 런타임 안정성 수정이 포함되어 있습니다.

주요 내용:

- 기본 websocket Realtime 모델이 이제 `gpt-realtime-1.5`이므로, 새 Realtime 에이전트 설정은 추가 구성 없이 더 새로운 모델을 사용합니다
- `MCPServer`는 이제 `list_resources()`, `list_resource_templates()`, `read_resource()`를 제공하고, `MCPServerStreamableHttp`는 이제 `session_id`를 제공하므로 streamable HTTP 세션을 재연결 또는 상태 비저장 워커 간에 재개할 수 있습니다
- Chat Completions 통합에서 이제 `should_replay_reasoning_content`를 통해 추론 콘텐츠 재생을 선택적으로 활성화할 수 있어, LiteLLM/DeepSeek 같은 어댑터의 제공자별 추론/도구 호출 연속성이 개선됩니다
- `SQLAlchemySession`의 동시 첫 쓰기, 추론 제거 후 고아 assistant 메시지 ID가 있는 compaction 요청, `remove_all_tools()`에서 MCP/추론 항목이 남는 문제, 함수 도구 배치 실행기의 경쟁 상태를 포함해 여러 런타임 및 세션 경계 사례를 수정했습니다

### 0.12.0

이 마이너 릴리스는 **호환성이 깨지는 변경**을 도입하지 않습니다. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)를 확인하세요.

### 0.11.0

이 마이너 릴리스는 **호환성이 깨지는 변경**을 도입하지 않습니다. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)를 확인하세요.

### 0.10.0

이 마이너 릴리스는 **호환성이 깨지는 변경**을 도입하지 않지만, OpenAI Responses 사용자에게 중요한 새 기능 영역인 Responses API용 websocket 전송 지원이 포함됩니다.

주요 내용:

- OpenAI Responses 모델에 대한 websocket 전송 지원 추가(옵트인, 기본 전송은 여전히 HTTP)
- 다중 턴 실행에서 websocket 지원 provider와 `RunConfig`를 재사용하기 위한 `responses_websocket_session()` 헬퍼 / `ResponsesWebSocketSession` 추가
- 스트리밍, tools, 승인, 후속 턴을 다루는 새 websocket 스트리밍 예제 추가(`examples/basic/stream_ws.py`)

### 0.9.0

이 버전에서는 Python 3.9를 더 이상 지원하지 않습니다. 이 메이저 버전은 3개월 전에 EOL에 도달했습니다. 더 최신 런타임 버전으로 업그레이드해 주세요.

또한 `Agent#as_tool()` 메서드에서 반환되는 값의 타입 힌트가 `Tool`에서 `FunctionTool`로 좁혀졌습니다. 이 변경은 일반적으로 호환성 문제를 일으키지 않지만, 코드가 더 넓은 유니온 타입에 의존한다면 일부 조정이 필요할 수 있습니다.

### 0.8.0

이 버전에서는 두 가지 런타임 동작 변경으로 인해 마이그레이션 작업이 필요할 수 있습니다:

- **동기식** Python callable을 래핑하는 함수 도구는 이제 이벤트 루프 스레드에서 실행되는 대신 `asyncio.to_thread(...)`를 통해 워커 스레드에서 실행됩니다. 도구 로직이 스레드 로컬 상태 또는 스레드 종속 리소스에 의존한다면, 비동기 도구 구현으로 마이그레이션하거나 도구 코드에서 스레드 종속성을 명시적으로 처리하세요
- 로컬 MCP 도구 실패 처리가 이제 구성 가능하며, 기본 동작은 전체 실행을 실패시키는 대신 모델에 보이는 오류 출력을 반환할 수 있습니다. fail-fast 의미론에 의존한다면 `mcp_config={"failure_error_function": None}`을 설정하세요. 서버 수준 `failure_error_function` 값은 에이전트 수준 설정을 재정의하므로, 명시적 핸들러가 있는 각 로컬 MCP 서버에도 `failure_error_function=None`을 설정하세요

### 0.7.0

이 버전에서는 기존 애플리케이션에 영향을 줄 수 있는 몇 가지 동작 변경이 있었습니다:

- 중첩 핸드오프 히스토리는 이제 **옵트인**입니다(기본 비활성화). v0.6.x의 기본 중첩 동작에 의존했다면 `RunConfig(nest_handoff_history=True)`를 명시적으로 설정하세요
- `gpt-5.1` / `gpt-5.2`의 기본 `reasoning.effort`가 `"none"`으로 변경되었습니다(이전 기본값은 SDK 기본값으로 설정된 `"low"`). 프롬프트 또는 품질/비용 프로필이 `"low"`에 의존했다면 `model_settings`에서 명시적으로 설정하세요

### 0.6.0

이 버전에서는 기본 핸드오프 히스토리가 원시 사용자/assistant 턴을 노출하는 대신 단일 assistant 메시지로 패키징되어, 다운스트림 에이전트에 간결하고 예측 가능한 요약을 제공합니다
- 기존 단일 메시지 핸드오프 전사본은 이제 기본적으로 `<CONVERSATION HISTORY>` 블록 앞에 "For context, here is the conversation so far between the user and the previous agent:"로 시작하므로, 다운스트림 에이전트가 명확히 라벨링된 요약을 받습니다

### 0.5.0

이 버전은 눈에 보이는 호환성 깨짐 변경을 도입하지 않지만, 새 기능과 내부의 몇 가지 중요한 업데이트를 포함합니다:

- `RealtimeRunner`가 [SIP 프로토콜 연결](https://platform.openai.com/docs/guides/realtime-sip)을 처리하도록 지원 추가
- Python 3.14 호환성을 위해 `Runner#run_sync`의 내부 로직을 대폭 수정

### 0.4.0

이 버전에서는 [openai](https://pypi.org/project/openai/) 패키지 v1.x를 더 이상 지원하지 않습니다. 이 SDK와 함께 openai v2.x를 사용해 주세요.

### 0.3.0

이 버전에서는 Realtime API 지원이 gpt-realtime 모델과 해당 API 인터페이스(GA 버전)로 마이그레이션됩니다.

### 0.2.0

이 버전에서는 이전에 인자로 `Agent`를 받던 일부 위치가 이제 대신 `AgentBase`를 인자로 받습니다. 예를 들어 MCP 서버의 `list_tools()` 호출이 그렇습니다. 이는 순수한 타이핑 변경이며, 여전히 `Agent` 객체를 받게 됩니다. 업데이트하려면 `Agent`를 `AgentBase`로 바꿔 타입 오류를 수정하면 됩니다.

### 0.1.0

이 버전에서는 [`MCPServer.list_tools()`][agents.mcp.server.MCPServer]에 `run_context`와 `agent`라는 두 개의 새 매개변수가 추가되었습니다. `MCPServer`를 서브클래싱하는 모든 클래스에 이 매개변수를 추가해야 합니다.