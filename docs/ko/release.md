---
search:
  exclude: true
---
# 릴리스 프로세스/변경 로그

이 프로젝트는 `0.Y.Z` 형식의 의미적 버전 관리 방식을 약간 변형하여 따릅니다. 앞의 `0`은 SDK 가 아직 빠르게 발전 중임을 나타냅니다. 각 구성 요소는 다음과 같이 증가됩니다:

## 마이너(`Y`) 버전

베타로 표시되지 않은 공개 인터페이스에 **호환성 파괴 변경**이 있을 때 마이너 버전 `Y` 를 증가시킵니다. 예를 들어 `0.0.x` 에서 `0.1.x` 로 올라갈 때는 호환성 파괴 변경이 포함될 수 있습니다.

호환성 파괴 변경을 원하지 않으시면, 프로젝트에서 `0.0.x` 버전에 고정하는 것을 권장합니다.

## 패치(`Z`) 버전

다음과 같은 비호환성 파괴 변경 사항이 있을 때 `Z` 를 증가시킵니다:

- 버그 수정
- 새로운 기능
- 비공개 인터페이스 변경
- 베타 기능 업데이트

## 호환성 파괴 변경 로그

### 0.6.0

이 버전에서는 기본 핸드오프 내역이 이제 원문 user/assistant 턴을 노출하는 대신 단일 assistant 메시지로 패키징되어, 하위 에이전트가 간결하고 예측 가능한 요약을 받도록 합니다
- 기존의 단일 메시지 핸드오프 대화록은 이제 기본적으로 `<CONVERSATION HISTORY>` 블록 앞에 "맥락을 위해, 지금까지 사용자와 이전 에이전트 간의 대화는 다음과 같습니다:"(원문: "For context, here is the conversation so far between the user and the previous agent:") 로 시작하므로, 하위 에이전트가 명확하게 라벨링된 요약을 받습니다

### 0.5.0

이 버전은 눈에 보이는 호환성 파괴 변경을 도입하지 않지만, 새로운 기능과 내부적으로 몇 가지 중요한 업데이트가 포함되어 있습니다:

- `RealtimeRunner` 가 [SIP 프로토콜 연결](https://platform.openai.com/docs/guides/realtime-sip) 을 처리하도록 지원 추가
- Python 3.14 호환성을 위해 `Runner#run_sync` 의 내부 로직을 대폭 수정

### 0.4.0

이 버전부터는 [openai](https://pypi.org/project/openai/) 패키지 v1.x 버전을 더 이상 지원하지 않습니다. 이 SDK 와 함께 openai v2.x 를 사용해 주세요.

### 0.3.0

이 버전에서는 Realtime API 지원이 gpt-realtime 모델과 해당 API 인터페이스(GA 버전)로 마이그레이션됩니다.

### 0.2.0

이 버전에서는 기존에 `Agent` 를 인자로 받던 몇몇 위치가 이제 `AgentBase` 를 인자로 받도록 변경되었습니다. 예를 들어 MCP 서버의 `list_tools()` 호출 등입니다. 이는 순수하게 타입 관련 변경이며, 여전히 `Agent` 객체를 받게 됩니다. 업데이트하려면 `Agent` 를 `AgentBase` 로 바꾸어 타입 오류만 수정하시면 됩니다.

### 0.1.0

이 버전에서 [`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 에 `run_context` 와 `agent` 두 가지 새로운 매개변수가 추가되었습니다. `MCPServer` 를 상속하는 모든 클래스에 이 매개변수를 추가해야 합니다.