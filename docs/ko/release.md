---
search:
  exclude: true
---
# 릴리스 프로세스/변경 로그

이 프로젝트는 `0.Y.Z` 형식을 사용하는, semantic versioning의 약간 수정된 버전을 따릅니다. 앞의 `0`은 SDK가 여전히 빠르게 발전하고 있음을 나타냅니다. 각 구성 요소는 다음과 같이 증가합니다.

## 마이너(`Y`) 버전

베타로 표시되지 않은 공개 인터페이스에 대한 **호환되지 않는 변경 사항**이 있을 경우 마이너 버전 `Y`를 올립니다. 예를 들어 `0.0.x`에서 `0.1.x`로 변경될 때는 호환되지 않는 변경 사항이 포함될 수 있습니다.

호환되지 않는 변경 사항을 원하지 않는다면 프로젝트에서 `0.0.x` 버전에 고정하는 것을 권장합니다.

## 패치(`Z`) 버전

호환되지 않는 변경이 아닌 경우 `Z`를 증가시킵니다.

-   버그 수정
-   새 기능
-   비공개 인터페이스 변경
-   베타 기능 업데이트

## 호환되지 않는 변경 로그

### 0.14.0

이 마이너 릴리스는 **호환되지 않는 변경 사항**을 도입하지는 않지만, Sandbox Agents라는 주요한 새로운 베타 기능 영역과 함께 로컬, 컨테이너화된, 호스팅 환경 전반에서 이를 사용하는 데 필요한 런타임, 백엔드, 문서 지원을 추가합니다.

주요 내용:

-   `SandboxAgent`, `Manifest`, `SandboxRunConfig`를 중심으로 한 새로운 베타 샌드박스 런타임 표면을 추가하여, 에이전트가 파일, 디렉터리, Git 리포지토리, 마운트, 스냅샷, 재개 지원이 있는 영속적이고 격리된 작업공간 내에서 작업할 수 있도록 했습니다.
-   `UnixLocalSandboxClient`와 `DockerSandboxClient`를 통한 로컬 및 컨테이너화된 개발용 샌드박스 실행 백엔드를 추가했으며, 선택적 extras를 통해 Blaxel, Cloudflare, Daytona, E2B, Modal, Runloop, Vercel에 대한 호스팅 provider 통합도 추가했습니다.
-   향후 실행에서 이전 실행의 학습 내용을 재사용할 수 있도록 샌드박스 메모리 지원을 추가했으며, 점진적 공개, 멀티턴 그룹화, 구성 가능한 격리 경계, S3 기반 워크플로를 포함한 영속 메모리 예제를 제공합니다.
-   로컬 및 합성 작업공간 항목, S3/R2/GCS/Azure Blob Storage/S3 Files용 원격 스토리지 마운트, 이식 가능한 스냅샷, `RunState`, `SandboxSessionState`, 저장된 스냅샷을 통한 재개 흐름을 포함하는 더 넓은 작업공간 및 재개 모델을 추가했습니다.
-   `examples/sandbox/` 아래에 샌드박스 관련 예제와 튜토리얼을 대폭 추가했으며, skills를 활용한 코딩 작업, 핸드오프, 메모리, provider별 설정, 코드 리뷰, dataroom QA, 웹사이트 복제와 같은 엔드투엔드 워크플로를 다룹니다.
-   샌드박스를 인식하는 세션 준비, capability 바인딩, 상태 직렬화, 통합 트레이싱, prompt cache key 기본값, 더 안전한 민감한 MCP 출력 redaction을 포함하도록 핵심 런타임과 트레이싱 스택을 확장했습니다.

### 0.13.0

이 마이너 릴리스는 **호환되지 않는 변경 사항**을 도입하지는 않지만, 주목할 만한 Realtime 기본값 업데이트와 새로운 MCP 기능, 런타임 안정성 수정 사항을 포함합니다.

주요 내용:

-   기본 websocket Realtime 모델이 이제 `gpt-realtime-1.5`가 되어, 새로운 Realtime 에이전트 설정은 추가 구성 없이 더 새로운 모델을 사용합니다.
-   `MCPServer`가 이제 `list_resources()`, `list_resource_templates()`, `read_resource()`를 노출하며, `MCPServerStreamableHttp`도 이제 `session_id`를 노출하므로 streamable HTTP 세션을 재연결이나 stateless worker 간에 재개할 수 있습니다.
-   Chat Completions 통합은 이제 `should_replay_reasoning_content`를 통해 reasoning-content replay를 선택적으로 사용할 수 있어 LiteLLM/DeepSeek 같은 adapter에서 provider별 reasoning/tool-call 연속성이 향상됩니다.
-   `SQLAlchemySession`에서의 동시 첫 쓰기, reasoning 제거 후 assistant message ID가 고아 상태가 된 compaction 요청, `remove_all_tools()`가 MCP/reasoning 항목을 남기는 문제, 함수 도구 배치 실행기에서의 race를 포함한 여러 런타임 및 세션 경계 사례를 수정했습니다.

### 0.12.0

이 마이너 릴리스는 **호환되지 않는 변경 사항**을 도입하지 않습니다. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)를 확인하세요.

### 0.11.0

이 마이너 릴리스는 **호환되지 않는 변경 사항**을 도입하지 않습니다. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)를 확인하세요.

### 0.10.0

이 마이너 릴리스는 **호환되지 않는 변경 사항**을 도입하지는 않지만, OpenAI Responses 사용자를 위한 중요한 새 기능 영역인 Responses API의 websocket 전송 지원을 포함합니다.

주요 내용:

-   OpenAI Responses 모델에 대한 websocket 전송 지원을 추가했습니다(옵트인 방식이며 HTTP는 여전히 기본 전송 방식입니다)
-   멀티턴 실행 전반에서 공유 websocket 지원 provider와 `RunConfig`를 재사용하기 위한 `responses_websocket_session()` 헬퍼 / `ResponsesWebSocketSession`를 추가했습니다
-   스트리밍, 도구, 승인, 후속 턴을 다루는 새로운 websocket 스트리밍 예제(`examples/basic/stream_ws.py`)를 추가했습니다

### 0.9.0

이 버전에서는 Python 3.9가 더 이상 지원되지 않습니다. 이 주요 버전은 3개월 전에 EOL에 도달했기 때문입니다. 더 새로운 런타임 버전으로 업그레이드해 주세요.

또한 `Agent#as_tool()` 메서드에서 반환되는 값의 타입 힌트가 `Tool`에서 `FunctionTool`로 더 좁혀졌습니다. 이 변경은 일반적으로 문제를 일으키지는 않지만, 코드가 더 넓은 union 타입에 의존한다면 일부 조정이 필요할 수 있습니다.

### 0.8.0

이 버전에서는 두 가지 런타임 동작 변경으로 인해 마이그레이션 작업이 필요할 수 있습니다.

- Function tools로 감싼 **동기식** Python callable은 이제 이벤트 루프 스레드에서 실행되는 대신 `asyncio.to_thread(...)`를 통해 worker thread에서 실행됩니다. 도구 로직이 thread-local 상태나 thread-affine 리소스에 의존한다면 async 도구 구현으로 마이그레이션하거나 도구 코드에서 스레드 선호성을 명시적으로 처리하세요.
- 로컬 MCP 도구 실패 처리 방식이 이제 구성 가능하며, 기본 동작은 전체 실행을 실패시키는 대신 모델이 볼 수 있는 오류 출력을 반환할 수 있습니다. fail-fast 의미론에 의존한다면 `mcp_config={"failure_error_function": None}`를 설정하세요. 서버 수준의 `failure_error_function` 값은 에이전트 수준 설정을 재정의하므로, 명시적 핸들러가 있는 각 로컬 MCP 서버에도 `failure_error_function=None`을 설정하세요.

### 0.7.0

이 버전에는 기존 애플리케이션에 영향을 줄 수 있는 몇 가지 동작 변경이 있습니다.

- 중첩 핸드오프 기록은 이제 **옵트인**입니다(기본적으로 비활성화). v0.6.x의 기본 중첩 동작에 의존했다면 `RunConfig(nest_handoff_history=True)`를 명시적으로 설정하세요.
- `gpt-5.1` / `gpt-5.2`의 기본 `reasoning.effort`가 이제 `"none"`으로 변경되었습니다(이전에는 SDK 기본값으로 구성된 `"low"`였습니다). 프롬프트나 품질/비용 프로필이 `"low"`에 의존했다면 `model_settings`에 명시적으로 설정하세요.

### 0.6.0

이 버전에서는 이제 기본 핸드오프 기록이 원문의 사용자/assistant 턴을 노출하는 대신 단일 assistant 메시지로 패키징되어, 다운스트림 에이전트에 간결하고 예측 가능한 요약을 제공합니다
- 기존 단일 메시지 핸드오프 transcript는 이제 기본적으로 `<CONVERSATION HISTORY>` 블록 앞에 "For context, here is the conversation so far between the user and the previous agent:"로 시작하므로, 다운스트림 에이전트가 명확하게 표시된 요약을 받을 수 있습니다

### 0.5.0

이 버전은 눈에 띄는 호환되지 않는 변경 사항은 도입하지 않지만, 새로운 기능과 내부적으로 몇 가지 중요한 업데이트를 포함합니다.

- `RealtimeRunner`가 [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip)를 처리하도록 지원을 추가했습니다
- Python 3.14 호환성을 위해 `Runner#run_sync`의 내부 로직을 크게 개정했습니다

### 0.4.0

이 버전에서는 [openai](https://pypi.org/project/openai/) 패키지의 v1.x 버전이 더 이상 지원되지 않습니다. 이 SDK와 함께 openai v2.x를 사용하세요.

### 0.3.0

이 버전에서는 Realtime API 지원이 gpt-realtime 모델과 해당 API 인터페이스(GA 버전)로 마이그레이션됩니다.

### 0.2.0

이 버전에서는 이전에 인수로 `Agent`를 받던 몇몇 위치가 이제 대신 `AgentBase`를 인수로 받습니다. 예를 들어 MCP 서버의 `list_tools()` 호출이 그렇습니다. 이는 순수하게 타이핑 변경일 뿐이며, 여전히 `Agent` 객체를 받게 됩니다. 업데이트하려면 `Agent`를 `AgentBase`로 바꿔 타입 오류만 수정하면 됩니다.

### 0.1.0

이 버전에서는 [`MCPServer.list_tools()`][agents.mcp.server.MCPServer]에 `run_context`와 `agent`라는 두 개의 새로운 매개변수가 추가되었습니다. `MCPServer`를 서브클래싱하는 모든 클래스에 이 매개변수들을 추가해야 합니다.