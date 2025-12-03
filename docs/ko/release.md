---
search:
  exclude: true
---
# 릴리스 프로세스/변경 로그

이 프로젝트는 `0.Y.Z` 형식의 시맨틱 버저닝을 약간 수정해 따릅니다. 선행 `0`은 SDK 가 아직 빠르게 발전 중임을 나타냅니다. 각 구성 요소는 다음과 같이 증가합니다:

## 부 버전(`Y`)

베타로 표시되지 않은 모든 공개 인터페이스에 **호환성 파괴 변경**이 있을 때 부 버전 `Y`를 올립니다. 예를 들어, `0.0.x`에서 `0.1.x`로 올라갈 때 브레이킹 체인지가 포함될 수 있습니다.

브레이킹 체인지를 원하지 않는 경우, 프로젝트에서 `0.0.x` 버전에 고정하는 것을 권장합니다.

## 패치(`Z`) 버전

호환성에 영향을 주지 않는 변경에 대해 `Z`를 올립니다:

- 버그 수정
- 신규 기능
- 비공개 인터페이스 변경
- 베타 기능 업데이트

## 호환성 파괴 변경 로그

### 0.6.0

이 버전에서는 기본 핸드오프 히스토리가 원문 사용자/assistant 턴을 노출하는 대신 단일 assistant 메시지에 패키징되어, 다운스트림 에이전트에 간결하고 예측 가능한 요약을 제공합니다
- 기존의 단일 메시지 핸드오프 기록은 이제 기본적으로 `<CONVERSATION HISTORY>` 블록 앞에 "For context, here is the conversation so far between the user and the previous agent:"로 시작하여, 다운스트림 에이전트가 명확하게 라벨링된 요약을 받도록 합니다

### 0.5.0

이 버전은 눈에 띄는 브레이킹 체인지는 도입하지 않지만, 신규 기능과 내부의 몇 가지 중요한 업데이트를 포함합니다:

- `RealtimeRunner`가 [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip)을 처리하도록 지원 추가
- Python 3.14 호환성을 위해 `Runner#run_sync`의 내부 로직을 대폭 수정

### 0.4.0

이 버전부터 [openai](https://pypi.org/project/openai/) 패키지 v1.x 버전은 더 이상 지원되지 않습니다. 이 SDK 와 함께 openai v2.x 를 사용하세요.

### 0.3.0

이 버전에서는 Realtime API 지원이 gpt-realtime 모델과 해당 API 인터페이스(GA 버전)로 마이그레이션됩니다.

### 0.2.0

이 버전에서는 일부에서 `Agent` 를 인자로 받던 것을 이제 `AgentBase` 를 인자로 받도록 변경했습니다. 예: MCP 서버의 `list_tools()` 호출. 이는 전적으로 타이핑 관련 변경이며, 여전히 `Agent` 객체를 받게 됩니다. 업데이트하려면 `Agent` 를 `AgentBase` 로 바꿔 타입 오류를 해결하세요.

### 0.1.0

이 버전에서 [`MCPServer.list_tools()`][agents.mcp.server.MCPServer]에 `run_context` 와 `agent` 두 매개변수가 추가되었습니다. `MCPServer` 를 상속하는 모든 클래스에 이 매개변수를 추가해야 합니다.