---
search:
  exclude: true
---
# 릴리스 프로세스/변경 로그

이 프로젝트는 `0.Y.Z` 형식의 의미 버전(semantic versioning)을 약간 수정해 따릅니다. 앞의 `0`은 SDK가 아직 빠르게 발전 중임을 나타냅니다. 각 요소는 다음과 같이 올립니다:

## 마이너 (`Y`) 버전

베타로 표시되지 않은 모든 공개 인터페이스에 대한 **호환성 깨뜨리는 변경 사항**에 대해 마이너 버전 `Y`를 올립니다. 예를 들어, `0.0.x`에서 `0.1.x`로 올라갈 때 브레이킹 변경이 포함될 수 있습니다.

브레이킹 변경을 원하지 않으면, 프로젝트에서 `0.0.x` 버전으로 고정할 것을 권장합니다.

## 패치 (`Z`) 버전

호환성 깨뜨리지 않는 변경에 대해서는 `Z`를 올립니다:

- 버그 수정
- 새 기능
- 비공개 인터페이스 변경
- 베타 기능 업데이트

## 브레이킹 변경 내역

### 0.6.0

이 버전에서는 기본 핸드오프 히스토리가 원문 사용자/assistant 턴을 노출하는 대신 단일 assistant 메시지로 묶여, 다운스트림 에이전트가 간결하고 예측 가능한 요약을 받도록 합니다
- 기존 단일 메시지 핸드오프 대화록은 이제 기본적으로 `<CONVERSATION HISTORY>` 블록 앞에 "For context, here is the conversation so far between the user and the previous agent:"로 시작하여, 다운스트림 에이전트가 명확하게 라벨링된 요약을 받도록 합니다

### 0.5.0

이 버전은 가시적인 브레이킹 변경을 도입하지 않지만, 새로운 기능과 내부적으로 의미 있는 업데이트가 포함됩니다:

- `RealtimeRunner`가 [SIP 프로토콜 연결](https://platform.openai.com/docs/guides/realtime-sip)을 처리하도록 지원 추가
- Python 3.14 호환성을 위해 `Runner#run_sync`의 내부 로직을 대폭 개정

### 0.4.0

이 버전에서는 [openai](https://pypi.org/project/openai/) 패키지 v1.x 버전을 더 이상 지원하지 않습니다. 이 SDK와 함께 openai v2.x를 사용하세요.

### 0.3.0

이 버전에서는 Realtime API 지원이 gpt-realtime 모델과 해당 API 인터페이스(GA 버전)로 이전됩니다.

### 0.2.0

이 버전에서는 과거에 `Agent`를 인자로 받던 일부 위치가 이제 `AgentBase`를 인자로 받도록 변경되었습니다. 예: MCP 서버의 `list_tools()` 호출. 이는 순수한 타입 변경이며, 여전히 `Agent` 객체를 받게 됩니다. 업데이트하려면 `Agent`를 `AgentBase`로 바꿔 타입 오류만 수정하면 됩니다.

### 0.1.0

이 버전에서는 [`MCPServer.list_tools()`][agents.mcp.server.MCPServer]에 `run_context`와 `agent`라는 두 개의 새 매개변수가 추가되었습니다. `MCPServer`를 상속하는 모든 클래스에 이 매개변수를 추가해야 합니다.